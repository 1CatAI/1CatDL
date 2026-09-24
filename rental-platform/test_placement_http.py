"""Customer/admin HTTP enforcement; never touches a real compute node."""
import copy
import http.cookiejar
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

import test_server_http as fixture

server = fixture.server


class PlacementHttpTests(unittest.TestCase):
    request = fixture.ServerHttpTests.request

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for context in (patch.object(server, 'ROOT', Path(directory.name)),
                        patch.object(server, 'SIMULATION', True),
                        patch.dict(server.CONFIG, {'enabled': True}),
                        patch.dict(os.environ, {'RENTAL_NODES_CONFIG': '', 'RENTAL_LOCAL_NODE_ID': 'G2-002'})):
            context.start(); self.addCleanup(context.stop)
        self.service = server.Service(start_background=False)
        self.addCleanup(self.service.close)
        context = patch.object(server, 'SERVICE', self.service)
        context.start(); self.addCleanup(context.stop)
        self.service.core.nodes['G2-003'] = dict(self.service.core.nodes['G2-002'])
        self.observed = {name: {'online': True, 'imageReady': True,
                              'storage': {'freeGiB': 3000, 'safetyGiB': 128, 'lowSpace': False}}
                         for name in self.service.core.nodes}
        context = patch.object(self.service, 'placement_observations', side_effect=lambda: copy.deepcopy(self.observed))
        context.start(); self.addCleanup(context.stop)
        self.httpd = server.ReusableServer(('127.0.0.1', 0), server.Handler)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.service.core.ensure_admin('operator', 'http-placement-test-password')
        self.assertEqual(self.request('/api/auth/login', 'POST', {'name': 'operator', 'password': 'http-placement-test-password'}, client=self.admin)[0], 200)
        self.assertEqual(self.request('/api/auth/register', 'POST', {'name': 'customer', 'password': 'http-placement-test-password'})[0], 201)

    def test_customer_preview_requires_auth_and_valid_spec(self):
        self.assertEqual(self.request('/api/rental/placement', client=urllib.request.build_opener())[0], 401)
        status, quote = self.request('/api/rental/placement?gpuCount=4&dataDiskGiB=200')
        self.assertEqual(status, 200)
        self.assertEqual(quote['recommendedNode'], 'G2-002')
        self.assertFalse(quote['nodes'][1]['allowed'])
        self.assertEqual(self.service.core.list_instances('customer'), [])
        for query in ('gpuCount=2', 'gpuCount=true', 'dataDiskGiB=600', 'mode=other'):
            self.assertEqual(self.request('/api/rental/placement?' + query)[0], 400)

    def test_direct_secondary_request_and_forged_observations_cannot_bypass(self):
        status, body = self.request('/api/rental/order', 'POST', {
            'nodeId': 'G2-003', 'fallbackAllowed': True, 'observations': {'G2-002': {'online': False}}})
        self.assertEqual(status, 409)
        self.assertIn('优先使用G2-002', body['message'])
        self.assertEqual(self.service.core.list_instances('customer'), [])
        status, body = self.request('/api/rental/order', 'POST', {'nodeId': 'auto'})
        self.assertEqual(status, 202)
        self.assertEqual(body['instance']['node_id'], 'G2-002')
        self.assertIsNone(body['instance']['slot'])

    def test_offline_fallback_and_recovery_preserve_idempotent_order(self):
        self.observed['G2-002']['online'] = False
        headers = {'X-Idempotency-Key': 'http-fallback-retry'}
        status, first = self.request('/api/rental/order', 'POST', {}, headers=headers)
        self.assertEqual(status, 202)
        self.assertEqual(first['instance']['node_id'], 'G2-003')
        self.observed['G2-002']['online'] = True
        status, repeated = self.request('/api/rental/order', 'POST', {}, headers=headers)
        self.assertEqual(status, 202)
        self.assertEqual(first['instance']['id'], repeated['instance']['id'])
        self.assertEqual(len(self.service.core.list_instances('customer')), 1)

    def test_admin_only_policy_with_conflict_and_same_origin_guards(self):
        old = {'enabled': True, 'preferredNode': 'G2-002'}
        free = {**old, 'enabled': False}
        payload = {'policy': free, 'expected': old}
        self.assertEqual(self.request('/api/admin/placement-policy')[0], 401)
        self.assertEqual(self.request('/api/admin/placement-policy', 'POST', payload)[0], 401)
        self.assertEqual(self.request('/api/admin/placement-policy', 'POST', payload, client=self.admin,
                                      headers={'Origin': 'https://untrusted.example'})[0], 401)
        self.assertEqual(self.request('/api/admin/placement-policy', client=self.admin)[1]['policy'], old)
        self.assertEqual(self.request('/api/admin/placement-policy', 'POST', payload, client=self.admin)[0], 200)
        conflict = {'policy': {'enabled': True, 'preferredNode': 'G2-003'}, 'expected': old}
        self.assertEqual(self.request('/api/admin/placement-policy', 'POST', conflict, client=self.admin)[0], 409)
        self.assertEqual(self.request('/api/rental/order', 'POST', {'nodeId': 'G2-003'})[0], 202)


if __name__ == '__main__':
    unittest.main()
