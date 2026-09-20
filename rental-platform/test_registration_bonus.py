"""Registration gifts: permission, exact-once credit, persistence and HTTP."""
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

from core import Core, MAX_REGISTRATION_BONUS_CENTS

PASSWORD = 'test-only-registration-password'


class RegistrationBonusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='registration-policy-')
        self.core = Core(self.tmp.name)
        self.core.ensure_admin('operator', PASSWORD)
        self.core.register('existing-customer', PASSWORD)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_and_admin_accounts_receive_no_gift(self):
        self.assertEqual(self.core.registration_bonus(), 0)
        self.assertEqual(self.core.profile('existing-customer')['balanceCents'], 0)
        self.core.set_registration_bonus('operator', 2000, 0)
        self.core.register('another-admin', PASSWORD, role='admin')
        self.assertEqual(self.core.profile('another-admin')['balanceCents'], 0)
        self.assertEqual(self.core.ledger('another-admin'), [])

    def test_only_future_customers_get_current_gift_and_it_persists(self):
        self.core.recharge('operator', 'existing-customer', 1234, idempotency='test-previous-credit')
        self.core.set_registration_bonus('operator', 2000, 0)
        self.core.register('first-customer', PASSWORD)
        self.core.set_registration_bonus('operator', 3050, 2000)
        restored = Core(self.tmp.name)
        self.assertEqual(restored.registration_settings('operator')['bonusCents'], 3050)
        restored.register('second-customer', PASSWORD)
        self.assertEqual(restored.profile('first-customer')['balanceCents'], 2000)
        self.assertEqual(restored.profile('second-customer')['balanceCents'], 3050)
        self.assertEqual(restored.profile('existing-customer')['balanceCents'], 1234)
        ledger = restored.ledger('second-customer')
        self.assertEqual([(r['cents'], r['reason']) for r in ledger], [(3050, 'registration_bonus')])
        self.assertEqual(restored.price(), 0)
        self.assertEqual(restored.metrics()['active_slots'], 0)

    def test_zero_disables_without_removing_old_gifts(self):
        self.core.set_registration_bonus('operator', 2000, 0)
        self.core.register('before-disable', PASSWORD)
        self.core.set_registration_bonus('operator', 0, 2000)
        self.core.register('after-disable', PASSWORD)
        self.assertEqual(self.core.profile('before-disable')['balanceCents'], 2000)
        self.assertEqual(self.core.profile('after-disable')['balanceCents'], 0)
        self.assertEqual(self.core.ledger('after-disable'), [])

    def test_admin_only_strict_integer_limits_and_audit(self):
        with self.assertRaises(PermissionError):
            self.core.registration_settings('existing-customer')
        with self.assertRaises(PermissionError):
            self.core.set_registration_bonus('existing-customer', 2000, 0)
        for bad in (True, False, -1, 0.1, '2000', None, MAX_REGISTRATION_BONUS_CENTS + 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.core.set_registration_bonus('operator', bad, 0)
        for bad in (True, -1, '0', None):
            with self.subTest(expected=bad), self.assertRaises(ValueError):
                self.core.set_registration_bonus('operator', 2000, bad)
        self.core.set_registration_bonus('operator', MAX_REGISTRATION_BONUS_CENTS, 0)
        event = self.core.audit('operator')[0]
        self.assertEqual((event['actor'], event['event']), ('operator', 'registration_bonus_changed'))
        self.assertEqual(json.loads(event['detail']), {
            'oldCents': 0, 'newCents': MAX_REGISTRATION_BONUS_CENTS, 'effective': 'future_registrations',
        })

    def test_stale_editor_rejected_and_same_request_retry_does_not_repeat_audit(self):
        self.core.set_registration_bonus('operator', 2000, 0)
        self.core.set_registration_bonus('operator', 2000, 0)
        self.core.set_registration_bonus('operator', 2000, 2000)
        self.assertEqual(len(self.core.audit('operator')), 1)
        with self.assertRaisesRegex(RuntimeError, '重新加载'):
            self.core.set_registration_bonus('operator', 3000, 0)
        self.assertEqual(self.core.registration_bonus(), 2000)

    def test_duplicate_registration_and_login_cannot_repeat_gift(self):
        self.core.set_registration_bonus('operator', 2000, 0)
        self.core.register('new-customer', PASSWORD)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.core.register('new-customer', PASSWORD)
        for _ in range(2):
            self.core.login('new-customer', PASSWORD)
        self.assertEqual(self.core.profile('new-customer')['balanceCents'], 2000)
        self.assertEqual(len(self.core.ledger('new-customer')), 1)

    def test_concurrent_duplicate_registration_credits_exactly_once(self):
        self.core.set_registration_bonus('operator', 2000, 0)
        barrier = threading.Barrier(4)
        results = []
        def register():
            barrier.wait()
            try:
                self.core.register('concurrent-user', PASSWORD)
                results.append('ok')
            except ValueError:
                results.append('duplicate')
        threads = [threading.Thread(target=register) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(sorted(results), ['duplicate', 'duplicate', 'duplicate', 'ok'])
        self.assertEqual(self.core.profile('concurrent-user')['balanceCents'], 2000)
        self.assertEqual(len(self.core.ledger('concurrent-user')), 1)

    def test_account_credit_ledger_and_invite_roll_back_together(self):
        self.core.set_registration_bonus('operator', 2000, 0)
        invite = self.core.create_invite()
        with patch.object(self.core, '_audit', side_effect=sqlite3.OperationalError('injected audit failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.core.register('atomic-user', PASSWORD, invite)
        with self.assertRaises(PermissionError): self.core.profile('atomic-user')
        self.assertEqual(self.core.ledger('atomic-user'), [])
        self.core.register('atomic-user', PASSWORD, invite)
        self.assertEqual(self.core.profile('atomic-user')['balanceCents'], 2000)


class RegistrationBonusHttpTests(unittest.TestCase):
    def setUp(self):
        from test_server_http import server
        self.server = server
        self.previous_service = server.SERVICE
        self.tmp = tempfile.TemporaryDirectory(prefix='registration-http-')
        self.root_patch = patch.object(server, 'ROOT', Path(self.tmp.name))
        self.root_patch.start()
        server.SERVICE = server.Service(start_background=False)
        server.SERVICE.core.ensure_admin('operator', PASSWORD)
        self.httpd = server.ReusableServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.anonymous = self.client()
        self.admin = self.client()
        self.assertEqual(self.request(self.admin, '/api/auth/login', {'name': 'operator', 'password': PASSWORD})[0], 200)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.server.SERVICE.close()
        self.server.SERVICE = self.previous_service
        self.root_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def client():
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, client, path, payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, data=body,
                                         headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with client.open(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_public_policy_and_admin_customer_permissions(self):
        path = '/api/admin/registration-bonus'
        self.assertEqual(self.request(self.anonymous, '/api/auth/registration-policy'), (200, {'bonusCents': 0}))
        self.assertEqual(self.request(self.anonymous, path)[0], 401)
        self.assertEqual(self.request(self.anonymous, path, {'cents': 2000, 'expectedCents': 0})[0], 401)
        customer = self.client()
        self.assertEqual(self.request(customer, '/api/auth/register', {'name': 'customer', 'password': PASSWORD})[0], 201)
        self.assertEqual(self.request(customer, path)[0], 401)
        self.assertEqual(self.request(customer, path, {'cents': 2000, 'expectedCents': 0})[0], 401)
        self.assertEqual(self.request(self.admin, path)[1]['bonusCents'], 0)
        self.assertEqual(self.request(self.admin, path, {'cents': 2000, 'expectedCents': 0},
                                      {'Origin': 'https://untrusted.invalid'})[0], 401)
        self.assertEqual(self.server.SERVICE.core.registration_bonus(), 0)

    def test_full_save_register_disable_and_ledger(self):
        path = '/api/admin/registration-bonus'
        self.assertEqual(self.request(self.admin, path, {'cents': 2025, 'expectedCents': 0})[0], 200)
        self.assertEqual(self.request(self.anonymous, '/api/auth/registration-policy?fresh=1')[1], {'bonusCents': 2025})
        customer = self.client()
        status, result = self.request(customer, '/api/auth/register', {
            'name': 'gift-customer', 'password': PASSWORD, 'bonusCents': 999999, 'role': 'admin',
        })
        self.assertEqual(status, 201)
        self.assertEqual((result['account']['balanceCents'], result['account']['role']), (2025, 'customer'))
        entries = self.request(customer, '/api/rental/ledger')[1]['entries']
        self.assertEqual([(r['reason'], r['cents']) for r in entries], [('registration_bonus', 2025)])
        self.assertEqual(self.request(self.admin, path, {'cents': 0, 'expectedCents': 2025})[0], 200)
        after = self.request(self.client(), '/api/auth/register', {'name': 'no-gift-customer', 'password': PASSWORD})[1]
        self.assertEqual(after['account']['balanceCents'], 0)
        self.assertEqual(self.request(customer, '/api/auth/me')[1]['account']['balanceCents'], 2025)

    def test_validation_and_stale_update_rejected(self):
        path = '/api/admin/registration-bonus'
        for cents in (-1, True, '2000', 1.5, None, 1000001):
            self.assertEqual(self.request(self.admin, path, {'cents': cents, 'expectedCents': 0})[0], 409)
        self.assertEqual(self.request(self.admin, path, {'cents': 2000})[0], 409)
        self.assertEqual(self.request(self.admin, path, {'cents': 2000, 'expectedCents': 0})[0], 200)
        self.assertEqual(self.request(self.admin, path, {'cents': 3000, 'expectedCents': 0})[0], 409)
        self.assertEqual(self.request(self.admin, path)[1]['bonusCents'], 2000)


if __name__ == '__main__':
    unittest.main()
