"""Recoverable customer deletion: no real wallets, disks or customer accounts."""
import http.cookiejar
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from core import Core

PASSWORD = 'local-only-account-test-password'


class AccountDeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='account-deletion-')
        self.core = Core(self.tmp.name)
        self.core.ensure_admin('operator', PASSWORD)
        self.core.register('alice', PASSWORD)
        self.core.recharge('operator', 'alice', 2000, idempotency='initial-credit')

    def tearDown(self):
        self.tmp.cleanup()

    def delete(self):
        return self.core.delete_customer('operator', 'alice', 'alice')

    def test_delete_revokes_sessions_keeps_balance_ledger_and_reserved_name(self):
        tokens = [self.core.login('alice', PASSWORD) for _ in range(2)]
        ledger = self.core.ledger('alice')
        result = self.delete()
        self.assertEqual(result['balanceCents'], 2000)
        self.assertTrue(result['deletedAt'])
        self.assertEqual(self.core.customers('operator'), [])
        deleted = self.core.customers('operator', 'deleted')
        self.assertEqual([(r['name'], r['deletedBy'], r['instanceCount']) for r in deleted], [('alice', 'operator', 0)])
        self.assertEqual(self.core.ledger('alice'), ledger)
        self.assertNotIn('password_hash', json.dumps(deleted))
        for token in tokens:
            with self.assertRaises(PermissionError):
                self.core.authenticate(token)
        with self.assertRaises(PermissionError):
            self.core.login('alice', PASSWORD)
        with self.assertRaises(PermissionError):
            self.core.profile('alice')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.core.register('alice', PASSWORD)
        self.assertEqual(self.delete(), result)  # Duplicate submit is harmless.
        self.assertEqual([r['event'] for r in self.core.audit('operator')], ['customer_deleted'])
        with self.core._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_restore_preserves_balance_no_signup_gift_or_old_session(self):
        token = self.core.login('alice', PASSWORD)
        self.delete()
        self.core.set_registration_bonus('operator', 3000, 0)
        self.core.restore_customer('operator', 'alice')
        self.core.restore_customer('operator', 'alice')
        self.assertEqual(self.core.profile('alice')['balanceCents'], 2000)
        self.assertEqual(len(self.core.ledger('alice')), 1)
        self.assertEqual(self.core.customers('operator', 'deleted'), [])
        with self.assertRaises(PermissionError):
            self.core.authenticate(token)
        self.assertEqual(self.core.authenticate(self.core.login('alice', PASSWORD)), 'alice')
        self.assertEqual(sum(r['event'] == 'customer_restored' for r in self.core.audit('operator')), 1)

    def test_only_admin_can_delete_restore_or_list_accounts(self):
        for operation in (
            lambda: self.core.delete_customer('alice', 'alice', 'alice'),
            lambda: self.core.restore_customer('alice', 'alice'),
            lambda: self.core.customers('alice', 'deleted'),
        ):
            with self.assertRaises(PermissionError): operation()
        self.core.ensure_admin('second-admin', PASSWORD)
        for target in ('operator', 'second-admin'):
            with self.assertRaisesRegex(ValueError, '管理员'):
                self.core.delete_customer('operator', target, target)
        self.assertTrue(self.core.is_admin('operator'))

    def test_exact_confirmation_invalid_filter_and_missing_target(self):
        for confirmation in (None, '', 'Alice', ' alice', 'bob', True, ['alice']):
            with self.subTest(value=confirmation), self.assertRaises(ValueError):
                self.core.delete_customer('operator', 'alice', confirmation)
        with self.assertRaises(KeyError): self.core.delete_customer('operator', 'unknown', 'unknown')
        with self.assertRaises(KeyError): self.core.restore_customer('operator', 'unknown')
        for value in ('invalid', [], None):
            with self.assertRaises(ValueError): self.core.customers('operator', value)
        self.assertEqual(self.core.profile('alice')['balanceCents'], 2000)

    def test_all_unreleased_instance_states_block_without_mutating_vm(self):
        for mode in ('gpu', 'headless'):
            vm = self.core.order('alice', {'mode': mode, 'data': 50}, 'blocked-' + mode)
            for state in ('stopped', 'provisioning', 'running', 'stopping', 'deleting', 'quarantined', 'error'):
                with self.core._transaction() as db:
                    db.execute('UPDATE instances SET state=? WHERE id=?', (state, vm['id']))
                before = self.core.list_instances('alice')
                with self.subTest(mode=mode, state=state), self.assertRaisesRegex(RuntimeError, '先在实例管理中释放'):
                    self.delete()
                self.assertEqual(self.core.list_instances('alice'), before)
                self.assertIsNone(self.core.customers('operator')[0]['deletedAt'])
            self.core.action('alice', vm['id'], 'delete')
            self.core.mark_off(vm['id'])
        self.assertEqual(self.delete()['balanceCents'], 2000)

    def test_deleted_instance_with_live_endpoint_or_meter_blocks_deletion(self):
        vm = self.core.order('alice', {}, 'inconsistent-worker')
        with self.core._transaction() as db:
            db.execute("UPDATE instances SET state='deleted',endpoint=1 WHERE id=?", (vm['id'],))
        with self.assertRaises(RuntimeError): self.delete()
        with self.core._transaction() as db:
            db.execute('UPDATE instances SET endpoint=NULL WHERE id=?', (vm['id'],))
            db.execute("INSERT INTO usage_ledger(instance_id,owner,opened_at) VALUES (?,'alice','2026-01-01T00:00:00+00:00')", (vm['id'],))
        with self.assertRaisesRegex(RuntimeError, '计费'): self.delete()

    def test_deleted_customer_cannot_order_replay_act_recharge_or_redeem(self):
        code = self.core.issue_recharge_codes('operator', 300, 1, 'gift', 'alice', '', 'gift-code')['codes'][0]['code']
        vm = self.core.order('alice', {}, 'old-order')
        self.core.action('alice', vm['id'], 'delete')
        self.core.mark_off(vm['id'])
        self.delete()
        for key in ('old-order', 'new-order'):
            with self.assertRaises(PermissionError): self.core.order('alice', {}, key)
        for action in ('start', 'stop', 'delete'):
            with self.assertRaises(PermissionError): self.core.action('alice', vm['id'], action)
        for key in ('initial-credit', 'new-credit'):
            with self.assertRaises(KeyError): self.core.recharge('operator', 'alice', 2000, idempotency=key)
        with self.assertRaises(PermissionError): self.core.redeem_recharge_code('alice', code)
        with self.assertRaises(ValueError):
            self.core.issue_recharge_codes('operator', 300, 1, 'gift', 'alice', '', 'new-code')
        self.core.restore_customer('operator', 'alice')
        self.core.redeem_recharge_code('alice', code)
        self.assertEqual(self.core.profile('alice')['balanceCents'], 2300)

    def test_inflight_login_cannot_create_session_after_delete(self):
        hashing, release = threading.Event(), threading.Event()
        original = self.core._password
        results = []
        def paused(password, salt):
            hashing.set()
            if not release.wait(5): raise AssertionError('login test timeout')
            return original(password, salt)
        def login():
            try: results.append(self.core.login('alice', PASSWORD))
            except PermissionError: results.append('denied')
        with patch.object(self.core, '_password', side_effect=paused):
            worker = threading.Thread(target=login)
            worker.start()
            try:
                self.assertTrue(hashing.wait(5))
                self.delete()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, ['denied'])
        with self.core._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_concurrent_order_and_delete_cannot_leave_deleted_owner_with_vm(self):
        for i in range(5):
            owner = f'racing-{i}'
            self.core.register(owner, PASSWORD)
            barrier = threading.Barrier(2)
            outcomes = []
            def order():
                barrier.wait()
                try: self.core.order(owner, {}, 'race'); outcomes.append('ordered')
                except PermissionError: outcomes.append('order-denied')
            def delete():
                barrier.wait()
                try: self.core.delete_customer('operator', owner, owner); outcomes.append('deleted')
                except RuntimeError: outcomes.append('delete-denied')
            workers = [threading.Thread(target=order), threading.Thread(target=delete)]
            for worker in workers: worker.start()
            for worker in workers: worker.join(5)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertIn(set(outcomes), ({'ordered', 'delete-denied'}, {'deleted', 'order-denied'}))
            with self.core._connection() as db:
                self.assertFalse(db.execute("SELECT 1 FROM users u JOIN instances i ON i.owner=u.name WHERE u.name=? AND u.deleted_at IS NOT NULL AND i.state!='deleted'", (owner,)).fetchone())

    def test_additive_migration_and_restart_preserve_credentials_and_tombstone(self):
        # Model the previous release's table with existing credentials and credit.
        with self.core._transaction() as db:
            db.execute('ALTER TABLE users DROP COLUMN deleted_at')
            db.execute('ALTER TABLE users DROP COLUMN deleted_by')
        self.core = Core(self.tmp.name)
        self.assertEqual(self.core.profile('alice')['balanceCents'], 2000)
        token = self.core.login('alice', PASSWORD)
        self.delete()
        self.core = Core(self.tmp.name)
        with self.assertRaises(PermissionError): self.core.authenticate(token)
        with self.assertRaises(PermissionError): self.core.login('alice', PASSWORD)
        self.assertEqual(self.core.customers('operator', 'deleted')[0]['balanceCents'], 2000)


class AccountDeletionHttpTests(unittest.TestCase):
    def setUp(self):
        from test_server_http import server
        self.server = server
        self.previous_service = server.SERVICE
        self.tmp = tempfile.TemporaryDirectory(prefix='account-http-')
        self.root_patch = patch.object(server, 'ROOT', Path(self.tmp.name))
        self.root_patch.start()
        server.SERVICE = server.Service(start_background=False)
        self.core = server.SERVICE.core
        self.core.ensure_admin('operator', PASSWORD)
        self.httpd = server.ReusableServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.anonymous, self.admin, self.customer = self.client(), self.client(), self.client()
        self.assertEqual(self.request(self.admin, '/api/auth/login', {'name': 'operator', 'password': PASSWORD})[0], 200)
        self.assertEqual(self.request(self.customer, '/api/auth/register', {'name': 'alice', 'password': PASSWORD})[0], 201)

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(3)
        self.server.SERVICE.close()
        self.server.SERVICE = self.previous_service
        self.root_patch.stop(); self.tmp.cleanup()

    @staticmethod
    def client():
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, client, path, payload=None, headers=None):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with client.open(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_permissions_confirmation_admin_guard_and_cross_origin(self):
        path = '/api/admin/customers/alice/delete'
        for client in (self.anonymous, self.customer):
            self.assertEqual(self.request(client, '/api/admin/customers?status=all')[0], 401)
            self.assertEqual(self.request(client, path, {'confirmName': 'alice'})[0], 401)
            self.assertEqual(self.request(client, '/api/admin/customers/alice/restore', {})[0], 401)
        self.assertEqual(self.request(self.admin, path, {})[0], 409)
        self.assertEqual(self.request(self.admin, path, {'confirmName': 'bob'})[0], 409)
        self.assertEqual(self.request(self.admin, path, {'confirmName': 'alice'}, {'Origin': 'https://untrusted.invalid'})[0], 401)
        self.assertEqual(self.request(self.admin, '/api/admin/customers/operator/delete', {'confirmName': 'operator'})[0], 409)
        self.assertEqual(self.request(self.admin, '/api/admin/customers?status=invalid')[0], 400)
        self.assertEqual(self.request(self.admin, '/api/admin/customers/nobody/delete', {'confirmName': 'nobody'})[0], 404)
        self.assertEqual(self.request(self.customer, '/api/auth/me')[0], 200)

    def test_delete_restore_full_http_flow_and_account_filters(self):
        self.core.recharge('operator', 'alice', 1234, idempotency='http-credit')
        path = '/api/admin/customers/alice/delete'
        self.assertEqual(self.request(self.admin, path, {'confirmName': 'alice'})[0], 200)
        self.assertEqual(self.request(self.admin, path, {'confirmName': 'alice'})[0], 200)
        self.assertEqual(self.request(self.admin, '/api/admin/customers')[1]['customers'], [])
        deleted = self.request(self.admin, '/api/admin/customers?status=deleted')[1]['customers']
        self.assertEqual(deleted[0]['balanceCents'], 1234)
        for path in ('/api/auth/me', '/api/rental/state', '/api/rental/ledger'):
            self.assertEqual(self.request(self.customer, path)[0], 401)
        self.assertEqual(self.request(self.customer, '/api/rental/order', {})[0], 401)
        self.assertEqual(self.request(self.customer, '/api/rental/redeem-code', {'code': 'test'})[0], 401)
        self.assertEqual(self.request(self.customer, '/api/auth/login', {'name': 'alice', 'password': PASSWORD})[0], 401)
        self.assertEqual(self.request(self.customer, '/api/auth/register', {'name': 'alice', 'password': PASSWORD})[0], 409)
        self.assertEqual(self.request(self.admin, '/api/admin/customers/alice/restore', {})[0], 200)
        self.assertEqual(self.request(self.customer, '/api/auth/me')[0], 401)
        self.assertEqual(self.request(self.customer, '/api/auth/login', {'name': 'alice', 'password': PASSWORD})[0], 200)
        self.assertEqual(self.request(self.customer, '/api/auth/me')[1]['account']['balanceCents'], 1234)
        self.assertEqual(len(self.request(self.admin, '/api/admin/ledger')[1]['entries']), 1)

    def test_existing_instance_blocks_delete_without_worker_or_disk_actions(self):
        vm = self.core.order('alice', {'data': 200}, 'http-vm')
        with patch.object(self.server.SERVICE, 'refresh_workers') as worker:
            status, result = self.request(self.admin, '/api/admin/customers/alice/delete', {'confirmName': 'alice'})
            self.assertEqual(status, 409)
            self.assertIn('先在实例管理中释放', result['message'])
            worker.assert_not_called()
        self.assertEqual(self.core.list_instances('alice'), [vm])
        self.assertEqual(self.request(self.admin, '/api/admin/customers')[1]['customers'][0]['instanceCount'], 1)


if __name__ == '__main__':
    unittest.main()
