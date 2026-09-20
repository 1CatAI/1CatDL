"""Disposable-wallet acceptance for the prepaid-code flow; never real credit."""
import http.cookiejar
import json
import tempfile
import unittest
import urllib.error
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core import Core
from recharge_codes import RedemptionRateLimited


class CodeCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Core(Path(self.tmp.name))
        self.core.ensure_admin('operator','local-test-password')
        for user in ('alice','bob'):
            self.core.register(user,'local-test-password')
            self.core.recharge('operator',user,2000,idempotency='setup-credit:'+user)

    def tearDown(self): self.tmp.cleanup()

    def issue(self, **changes):
        args = dict(admin='operator',cents=10000,count=1,kind='cash',bound_owner=None,note='verified-test-receipt',idempotency='test-batch-1')
        args.update(changes)
        return self.core.issue_recharge_codes(**args)

    def test_issue_does_not_credit_and_never_persists_plaintext(self):
        result = self.issue(count=3)
        codes = [r['code'] for r in result['codes']]
        self.assertEqual(len(set(codes)),3)
        for code in codes: self.assertRegex(code,r'^1CAT-(?:[A-F0-9]{4}-){7}[A-F0-9]{4}$')
        self.assertEqual(self.core.profile('alice')['balanceCents'],2000)
        with self.core._connection() as db: dump = '\n'.join(db.iterdump())
        for code in codes:
            self.assertNotIn(code,dump); self.assertNotIn(code.replace('-',''),dump)
        listed = self.core.list_recharge_codes('operator')
        self.assertEqual(listed['summary']['availableCents'],30000)
        self.assertNotIn('code_hash',json.dumps(listed))
        self.assertNotIn('code',listed['codes'][0])

    def test_issue_retry_returns_metadata_only_and_changed_intent_rejected(self):
        result = self.issue()
        retry = self.issue()
        self.assertTrue(retry['replayed'])
        self.assertEqual(result['batchId'],retry['batchId'])
        self.assertNotIn('code',retry['codes'][0])
        with self.assertRaises(ValueError): self.issue(cents=101)
        self.assertEqual(self.core.list_recharge_codes('operator')['summary']['total'],1)

    def test_once_only_same_owner_retry_and_ledger_source(self):
        result = self.issue()
        code = result['codes'][0]['code']
        first = self.core.redeem_recharge_code('alice', '  '+code.lower()+'\n')
        self.assertEqual(first['balanceCents'],12000)
        self.assertFalse(first['alreadyRedeemed'])
        self.assertTrue(self.core.redeem_recharge_code('alice',code.replace('-',''))['alreadyRedeemed'])
        with self.assertRaises(ValueError): self.core.redeem_recharge_code('bob',code)
        rows = [r for r in self.core.ledger('alice') if r['reason'].startswith('redeem_cash:')]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['cents'],10000)
        self.assertEqual(self.core.list_recharge_codes('operator')['summary']['redeemedCashCents'],10000)

    def test_bound_customer_gift_and_missing_customer(self):
        row = self.issue(kind='gift',note='',bound_owner='alice')['codes'][0]
        with self.assertRaises(ValueError): self.core.redeem_recharge_code('bob',row['code'])
        result = self.core.redeem_recharge_code('alice',row['code'])
        self.assertEqual(result['kind'],'gift')
        self.assertTrue(self.core.ledger('alice')[0]['reason'].startswith('redeem_gift:'))
        with self.assertRaises(ValueError): self.issue(idempotency='missing-user',bound_owner='no-such-user')
        with self.assertRaises(ValueError): self.issue(idempotency='admin-binding',bound_owner='operator')

    def test_permissions_and_input_limits(self):
        for method in (lambda: self.issue(admin='alice'), lambda: self.core.list_recharge_codes('alice'), lambda: self.core.revoke_recharge_codes('alice',code_id=1), lambda: self.core.redeem_recharge_code('operator','invalid')):
            with self.assertRaises(PermissionError): method()
        for kwargs in ({'cents':True},{'cents':0},{'cents':1000001},{'cents':1.5},{'count':0},{'count':101},{'count':True},{'count':11,'cents':1000000},{'kind':'unknown'},{'note':''},{'note':'x'*121},{'idempotency':None},{'bound_owner':True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.issue(**kwargs)
        self.assertEqual(self.core.list_recharge_codes('operator')['summary']['total'],0)

    def test_batch_and_single_revocation_preserve_redeemed_credit(self):
        result = self.issue(count=3)
        rows = result['codes']
        self.core.redeem_recharge_code('alice',rows[0]['code'])
        self.assertEqual(self.core.revoke_recharge_codes('operator',code_id=rows[0]['id'])['revoked'],0)
        self.assertEqual(self.core.revoke_recharge_codes('operator',code_id=rows[1]['id'])['revoked'],1)
        self.assertEqual(self.core.revoke_recharge_codes('operator',batch_id=result['batchId'])['revoked'],1)
        self.assertEqual(self.core.revoke_recharge_codes('operator',batch_id=result['batchId'])['revoked'],0)
        with self.assertRaises(ValueError): self.core.redeem_recharge_code('alice',rows[2]['code'])
        self.assertEqual(self.core.profile('alice')['balanceCents'],12000)

    def test_concurrent_two_customers_single_credit(self):
        code = self.issue()['codes'][0]['code']
        def redeem(user):
            try: return self.core.redeem_recharge_code(user,code)
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(redeem,['alice','bob']*8))
        self.assertEqual(sum(r is not None and not r['alreadyRedeemed'] for r in results),1)
        self.assertEqual(sum(self.core.profile(n)['balanceCents'] for n in ('alice','bob')),14000)

    def test_concurrent_generation_retry_has_one_plaintext_response(self):
        with ThreadPoolExecutor(max_workers=4) as pool: results = list(pool.map(lambda _: self.issue(count=4),range(4)))
        self.assertEqual(sum(not r['replayed'] for r in results),1)
        self.assertEqual(self.core.list_recharge_codes('operator')['summary']['total'],4)

    def test_atomic_failure_rolls_back_code_balance_and_ledger(self):
        row = self.issue()['codes'][0]
        with patch.object(self.core,'_audit',side_effect=RuntimeError('injected-failure')):
            with self.assertRaises(RuntimeError): self.core.redeem_recharge_code('alice',row['code'])
        self.assertEqual(self.core.profile('alice')['balanceCents'],2000)
        self.assertEqual(self.core.list_recharge_codes('operator')['codes'][0]['status'],'available')
        self.assertEqual(len(self.core.ledger('alice')),1)
        self.core.redeem_recharge_code('alice',row['code'])

    def test_invalid_attempts_persist_across_restart_then_expire(self):
        code = self.issue()['codes'][0]['code']
        for _ in range(10):
            with self.assertRaises(ValueError): self.core.redeem_recharge_code('alice','bad')
        restarted = Core(Path(self.tmp.name))
        with self.assertRaises(RedemptionRateLimited): restarted.redeem_recharge_code('alice',code)
        with restarted._transaction() as db:
            db.execute('UPDATE recharge_attempts SET window_at=? WHERE owner=?',((datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat(),'alice'))
        self.assertEqual(restarted.redeem_recharge_code('alice',code)['balanceCents'],12000)

    def test_pagination_filter_audit_and_reinitialization(self):
        result = self.issue(count=60,cents=100)
        first = self.core.list_recharge_codes('operator')
        second = self.core.list_recharge_codes('operator',before=first['nextCursor'])
        self.assertEqual(len(first['codes']),50); self.assertEqual(len(second['codes']),10)
        self.assertFalse(set(r['id'] for r in first['codes']) & set(r['id'] for r in second['codes']))
        self.core.revoke_recharge_codes('operator',code_id=result['codes'][0]['id'])
        self.assertEqual(len(self.core.list_recharge_codes('operator',status='revoked')['codes']),1)
        self.assertEqual(len(self.core.list_recharge_codes('operator',query='no-match')['codes']),0)
        Core(Path(self.tmp.name))
        self.assertEqual(self.core.list_recharge_codes('operator')['summary']['total'],60)
        self.assertEqual(self.core.audit('operator')[0]['event'],'recharge_codes_revoked')


class CodeHttpTests(unittest.TestCase):
    def setUp(self):
        from test_server_http import server
        self.server = server
        self.tmp = tempfile.TemporaryDirectory()
        self.service = server.Service(start_background=False)
        self.service.core = Core(Path(self.tmp.name))
        self.service.core.ensure_admin('operator','local-test-password')
        self.service.core.register('alice','local-test-password')
        self.service.core.recharge('operator','alice',2000,idempotency='setup-credit:alice')
        server.SERVICE = self.service
        self.httpd = server.ReusableServer(('127.0.0.1',0),server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,daemon=True); self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'
        self.admin = self.client('operator'); self.customer = self.client('alice')

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.service.close(); self.tmp.cleanup()

    def client(self, name):
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request(client,'/api/auth/login',{'name':name,'password':'local-test-password'})
        return client

    def request(self, client, path, payload=None, headers=None):
        req = urllib.request.Request(self.base+path,data=json.dumps(payload).encode() if payload is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
        try:
            with client.open(req,timeout=3) as response: return response.status,json.loads(response.read()),response.headers
        except urllib.error.HTTPError as error: return error.code,json.loads(error.read()),error.headers

    def test_http_generation_redeem_privacy_and_revoke(self):
        payload = {'cents':5000,'count':2,'kind':'cash','note':'http-test','boundOwner':'alice'}
        status,issued,headers = self.request(self.admin,'/api/admin/recharge-codes',payload,{'X-Idempotency-Key':'http-issue-1'})
        self.assertEqual(status,201); self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(self.request(self.customer,'/api/admin/recharge-codes')[0],401)
        self.assertEqual(self.request(self.customer,'/api/admin/recharge-codes',payload,{'X-Idempotency-Key':'http-forbidden'})[0],401)
        row = issued['codes'][0]
        status,response,_ = self.request(self.customer,'/api/rental/redeem-code',{'code':row['code']})
        self.assertEqual(status,200); self.assertEqual(response['result']['balanceCents'],7000)
        self.assertTrue(self.request(self.customer,'/api/rental/redeem-code',{'code':row['code']})[1]['result']['alreadyRedeemed'])
        self.assertNotIn(row['code'],json.dumps(self.request(self.admin,'/api/admin/recharge-codes')[1]))
        self.assertEqual(self.request(self.admin,'/api/admin/recharge-code-batches/'+issued['batchId']+'/revoke',{})[1]['revoked'],1)
        self.assertEqual(self.request(self.customer,'/api/rental/redeem-code',{'code':issued['codes'][1]['code']})[0],409)

    def test_http_auth_origin_limits_and_bad_filters(self):
        bare = urllib.request.build_opener()
        self.assertEqual(self.request(bare,'/api/rental/redeem-code',{'code':'bad'})[0],401)
        self.assertEqual(self.request(self.customer,'/api/rental/redeem-code',{'code':'bad'},{'Origin':'https://evil.example'})[0],401)
        self.assertEqual(self.request(self.admin,'/api/admin/recharge-codes?before=invalid')[0],400)
        for _ in range(10): self.assertEqual(self.request(self.customer,'/api/rental/redeem-code',{'code':'bad'})[0],409)
        self.assertEqual(self.request(self.customer,'/api/rental/redeem-code',{'code':'bad'})[0],429)


if __name__ == '__main__': unittest.main()
