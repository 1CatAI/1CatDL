"""Per-customer GPU VM concurrency, including cross-node atomic admission."""
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


class CustomerGpuLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Core(self.temp.name, nodes={'G2-003': {}})
        self.core.ensure_admin('operator', 'test-only-password')
        self.core.register('alice', 'test-only-password')
        self.core.recharge('operator', 'alice', 10000, idempotency='test-funds')

    def order(self, key, node='G2-002', mode='gpu'):
        return self.core.order('alice', {'mode': mode}, key, node_id=node)

    def test_migration_and_default_unlimited(self):
        first = self.order('first')
        with self.core._transaction() as db:
            db.execute('ALTER TABLE users DROP COLUMN gpu_instance_limit')
        self.core = Core(self.temp.name, nodes={'G2-003': {}})
        self.assertIsNone(self.core.profile('alice')['gpuInstanceLimit'])
        second = self.order('second', 'G2-003')
        self.core.action('alice', first['id'], 'start')
        self.core.action('alice', second['id'], 'start')
        self.assertEqual(self.core.profile('alice')['gpuActiveCount'], 2)
        listed = self.core.customers('operator')[0]
        self.assertEqual((listed['gpuInstanceLimit'], listed['gpuActiveCount']), (None, 2))

    def test_limit_zero_one_two_and_reopen(self):
        rows = [self.order(f'vm-{n}') for n in range(3)]
        self.core.set_customer_gpu_limit('operator', 'alice', 0, None)
        with self.assertRaisesRegex(RuntimeError, '并发配额已满'):
            self.core.action('alice', rows[0]['id'], 'start')
        self.core.set_customer_gpu_limit('operator', 'alice', 1, 0)
        self.core.action('alice', rows[0]['id'], 'start')
        with self.assertRaisesRegex(RuntimeError, '1/1'):
            self.core.action('alice', rows[1]['id'], 'start')
        self.core.set_customer_gpu_limit('operator', 'alice', 2, 1)
        self.core.action('alice', rows[1]['id'], 'start')
        self.core.set_customer_gpu_limit('operator', 'alice', 1, 2)
        self.assertEqual(self.core.profile('alice')['gpuActiveCount'], 2)
        with self.assertRaisesRegex(RuntimeError, '2/1'):
            self.core.action('alice', rows[2]['id'], 'start')
        self.core.set_customer_gpu_limit('operator', 'alice', None, 1)
        self.core.action('alice', rows[2]['id'], 'start')
        self.assertEqual(self.core.profile('alice')['gpuActiveCount'], 3)

    def test_headless_ignores_limit_and_quarantined_gpu_counts(self):
        self.core.set_customer_gpu_limit('operator', 'alice', 1, None)
        gpu = self.order('gpu')
        self.core.action('alice', gpu['id'], 'start')
        self.core.mark_error(gpu['id'], 'test fault')
        for n in range(2):
            headless = self.order(f'headless-{n}', mode='headless')
            self.core.action('alice', headless['id'], 'start')
        self.assertEqual(self.core.profile('alice')['gpuActiveCount'], 1)
        with self.assertRaisesRegex(RuntimeError, '并发配额已满'):
            self.core.action('alice', self.order('second-gpu')['id'], 'start')

    def test_validation_conflict_permissions_and_audit(self):
        for invalid in (True, -1, 10001, 1.5, '2'):
            with self.assertRaises(ValueError):
                self.core.set_customer_gpu_limit('operator', 'alice', invalid, None)
        with self.assertRaises(PermissionError):
            self.core.set_customer_gpu_limit('alice', 'alice', 2, None)
        with self.assertRaises(KeyError):
            self.core.set_customer_gpu_limit('operator', 'operator', 2, None)
        self.core.set_customer_gpu_limit('operator', 'alice', 2, None)
        with self.assertRaisesRegex(RuntimeError, '其他管理员修改'):
            self.core.set_customer_gpu_limit('operator', 'alice', 3, None)
        self.core.set_customer_gpu_limit('operator', 'alice', 2, 2)
        db = sqlite3.connect(self.core.db_path)
        try:
            events = db.execute("SELECT detail FROM audit_events WHERE event='customer_gpu_limit_changed'").fetchall()
        finally:
            db.close()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0][0]), {'previousLimit': None, 'newLimit': 2})

    def test_cross_node_concurrent_starts_obey_limit(self):
        self.core.set_customer_gpu_limit('operator', 'alice', 1, None)
        rows = [self.order('local'), self.order('remote', 'G2-003')]
        barrier = threading.Barrier(2)
        success, errors = [], []

        def start(row):
            barrier.wait()
            try:
                success.append(self.core.action('alice', row['id'], 'start'))
            except RuntimeError as error:
                errors.append(str(error))

        threads = [threading.Thread(target=start, args=(row,)) for row in rows]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual(len(success), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn('配额已满', errors[0])


class CustomerGpuLimitHttpTests(unittest.TestCase):
    def setUp(self):
        from test_server_http import server
        self.server = server
        self.previous_service = server.SERVICE
        self.temp = tempfile.TemporaryDirectory(prefix='gpu-limit-http-')
        self.addCleanup(self.temp.cleanup)
        self.root_patch = patch.object(server, 'ROOT', Path(self.temp.name))
        self.root_patch.start()
        server.SERVICE = server.Service(start_background=False)
        server.SERVICE.core.ensure_admin('operator', 'test-only-password')
        self.httpd = server.ReusableServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.admin, self.customer = self.client(), self.client()
        self.assertEqual(self.request(self.admin, '/api/auth/login', {'name': 'operator', 'password': 'test-only-password'})[0], 200)
        self.assertEqual(self.request(self.customer, '/api/auth/register', {'name': 'alice', 'password': 'test-only-password'})[0], 201)

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(timeout=3)
        self.server.SERVICE.close()
        self.server.SERVICE = self.previous_service
        self.root_patch.stop()

    @staticmethod
    def client():
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, client, path, payload=None):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Content-Type': 'application/json'})
        try:
            with client.open(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_admin_endpoint_and_customer_state(self):
        path = '/api/admin/customers/alice/gpu-limit'
        self.assertEqual(self.request(self.customer, path, {'limit': 2, 'expectedLimit': None})[0], 401)
        self.assertEqual(self.request(self.admin, path, {'limit': 2})[0], 409)
        self.assertEqual(self.request(self.admin, path, {'limit': True, 'expectedLimit': None})[0], 409)
        status, body = self.request(self.admin, path, {'limit': 2, 'expectedLimit': None})
        self.assertEqual((status, body['customer']['gpuInstanceLimit']), (200, 2))
        self.assertEqual(self.request(self.admin, path, {'limit': 3, 'expectedLimit': None})[0], 409)
        self.assertEqual(self.request(self.admin, '/api/admin/customers')[1]['customers'][0]['gpuInstanceLimit'], 2)
        self.assertEqual(self.request(self.customer, '/api/rental/state')[1]['account']['gpuInstanceLimit'], 2)
