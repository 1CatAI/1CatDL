import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import Core
from remote_backend import BackendRouter, load_nodes, NodeUnavailable


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Core(self.temp.name, storage_pool_budget=3300, nodes={'G2-003': {'storage_pool_budget': 3300}})
        self.core.ensure_admin('operator', 'placement-test-only-password')
        self.core.register('customer', 'placement-test-only-password')
        self.core.recharge('operator', 'customer', 10000)
        self.observed = {ident: {'online': True, 'imageReady': True,
            'storage': {'freeGiB': 3000, 'safetyGiB': 128, 'lowSpace': False}} for ident in self.core.nodes}

    def order(self, key='order', target=None, spec=None):
        return self.core.order('customer', spec or {}, key, node_id=target, observations=self.observed)

    def occupy(self, slots, node='G2-002'):
        for slot in slots:
            row = self.core.order('customer', {}, f'held-{node}-{slot}', node_id=node)
            with self.core._transaction() as con:
                con.execute("UPDATE instances SET slot=?,endpoint=?,state='quarantined' WHERE id=?", (slot, slot, row['id']))

    def test_preferred_default_and_direct_secondary_request_rejected(self):
        self.assertEqual(self.order()['node_id'], 'G2-002')
        with self.assertRaisesRegex(RuntimeError, '优先使用G2-002'):
            self.order('bypass', 'G2-003')
        self.assertEqual(len(self.core.list_instances('customer')), 1)

    def test_full_gpu_pool_unlocks_and_recommends_other_node(self):
        self.occupy(range(1, 9))
        quote = self.core.placement_quote({}, self.observed)
        self.assertEqual(quote['reasonCodes'], ['primary_gpu_insufficient'])
        self.assertEqual(self.order()['node_id'], 'G2-003')

    def test_four_gpu_fragmentation_counts_real_groups_not_total_free(self):
        self.occupy([1, 5])
        self.assertFalse(self.core.placement_quote({}, self.observed)['fallbackAllowed'])
        self.assertTrue(self.core.placement_quote({'gpu_count': 4}, self.observed)['fallbackAllowed'])
        self.assertEqual(self.order(spec={'gpu_count': 4})['node_id'], 'G2-003')

    def test_offline_and_failed_image_have_different_policy_meanings(self):
        self.observed['G2-002']['online'] = False
        self.assertEqual(self.order()['node_id'], 'G2-003')
        self.observed['G2-002']['online'] = True
        self.observed['G2-002']['imageReady'] = False
        with self.assertRaisesRegex(RuntimeError, '优先使用'):
            self.order('image-not-an-exception', 'G2-003')

    def test_storage_checks_system_disk_data_disk_safety_and_gifts(self):
        self.observed['G2-002']['storage']['freeGiB'] = 327  # 128 reserve + 200 available minus 1
        self.assertEqual(self.order(spec={'data': 200})['node_id'], 'G2-003')
        self.observed['G2-002']['storage']['freeGiB'] = 3000
        self.core.storage_pool_budget = 650
        row = self.core.order('customer', {'data': 200}, 'gift')
        self.core.action('customer', row['id'], 'start')
        self.core.mark_running(row['id'])
        self.core.record_storage_gift('operator', 'customer', row['id'], 400, expected_gift_gib=0, verified_total_gib=600)
        quote = self.core.placement_quote({}, self.observed)
        self.assertIn('primary_storage_insufficient', quote['reasonCodes'])
        self.assertEqual(self.order('gift-full')['node_id'], 'G2-003')

    def test_missing_or_nan_storage_fails_closed(self):
        for storage in ({}, {'freeGiB': float('nan'), 'safetyGiB': 128, 'lowSpace': False}):
            self.observed['G2-002']['storage'] = storage
            self.assertEqual(self.core.placement_quote({}, self.observed)['recommendedNode'], 'G2-003')

    def test_all_gpus_busy_still_allows_creating_stopped_instance(self):
        self.occupy(range(1, 9)); self.occupy(range(1, 9), 'G2-003')
        row = self.order()
        self.assertEqual((row['node_id'], row['state'], row['slot']), ('G2-002', 'stopped', None))

    def test_headless_ignores_four_card_fragmentation_but_full_pool_unlocks(self):
        self.occupy([1, 5])
        self.assertFalse(self.core.placement_quote({'mode': 'headless', 'gpu_count': 4}, self.observed)['fallbackAllowed'])
        self.occupy([2, 3, 4, 6, 7, 8])
        self.assertTrue(self.core.placement_quote({'mode': 'headless'}, self.observed)['fallbackAllowed'])

    def test_auto_and_explicit_idempotent_retry_survive_primary_recovery(self):
        self.observed['G2-002']['online'] = False
        row = self.order()
        self.observed['G2-002']['online'] = True
        self.assertEqual(self.order()['id'], row['id'])
        self.assertEqual(self.order(target='G2-003')['id'], row['id'])
        self.assertEqual(len(self.core.list_instances('customer')), 1)
        with self.assertRaises(ValueError): self.order(target='G2-002')

    def test_storage_check_and_insert_are_one_atomic_transaction(self):
        self.core.storage_pool_budget = 150
        barrier = threading.Barrier(2)
        result, errors = [], []
        def create(key):
            barrier.wait()
            try: result.append(self.order(key, spec={'data': 50})['node_id'])
            except Exception as exc: errors.append(exc)
        threads = [threading.Thread(target=create, args=(str(n),)) for n in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result), ['G2-002', 'G2-003'])

    def test_cpu_memory_shortage_alone_does_not_add_a_fourth_exception(self):
        self.core.cpu_budget = 0
        self.assertFalse(self.core.placement_quote({}, self.observed)['fallbackAllowed'])

    def test_policy_admin_only_cas_audit_and_restart_persistence(self):
        original = self.core.placement_policy()
        free = {**original, 'enabled': False}
        with self.assertRaises(PermissionError): self.core.set_placement_policy('customer', free, original)
        self.core.set_placement_policy('operator', free, original)
        self.assertEqual(self.order(target='G2-003')['node_id'], 'G2-003')
        self.assertEqual(self.core.set_placement_policy('operator', free, original), free)
        with self.assertRaises(ValueError): self.core.set_placement_policy('operator', {'enabled': True, 'preferredNode': 'G2-003'}, original)
        with self.assertRaises(ValueError): self.core.set_placement_policy('operator', {'enabled': True, 'preferredNode': []}, free)
        restarted = Core(self.temp.name, nodes={'G2-003': {}})
        self.assertEqual(restarted.placement_policy(), free)
        events = [e for e in self.core.audit('operator') if e['event'] == 'placement_policy_changed']
        self.assertEqual(len(events), 1)

    def test_existing_secondary_restart_is_not_rehomed_when_primary_is_free(self):
        old = self.core.order('customer', {}, 'existing', node_id='G2-003')
        started = self.core.action('customer', old['id'], 'start')
        self.assertEqual(started['node_id'], 'G2-003')


class ControllerOnlyTests(unittest.TestCase):
    def test_remote_g2002_billing_capped_and_no_phantom_local_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Core(directory, nodes={'G2-002': {}, 'G2-003': {}}, local_node_id=None, rate_cents_per_hour=400)
            self.assertEqual(core.metrics()['capacity']['slots'], 16)
            core.ensure_admin('operator', 'controller-test-password')
            core.register('customer', 'controller-test-password')
            core.recharge('operator', 'customer', 10000)
            row = core.order('customer', {}, 'remote-primary')
            row = core.action('customer', row['id'], 'start')
            core.mark_running(row['id'])
            when = datetime.now(timezone.utc)
            with core._transaction() as con:
                con.execute('UPDATE usage_ledger SET opened_at=?', ((when - timedelta(hours=1)).isoformat(),))
                con.execute('UPDATE instances SET confirmed_through=? WHERE id=?', ((when - timedelta(hours=1)).isoformat(), row['id']))
            core.bill_running(when)
            self.assertEqual(core.profile('customer')['balanceCents'], 10000)
            core.confirm_remote_runtime(row['id'], row['generation'], when.isoformat())
            core.bill_running(when)
            self.assertEqual(core.profile('customer')['balanceCents'], 9600)
            core.bill_running(when + timedelta(hours=2))
            self.assertEqual(core.profile('customer')['balanceCents'], 9600)

    def test_controller_can_route_g2002_remotely_never_falls_back_to_local(self):
        router = BackendRouter(None, [{'id': 'G2-002'}], local_node_id=None)
        self.assertIs(router.for_node('G2-002'), router.remotes['G2-002'])
        with self.assertRaises(NodeUnavailable): router.for_node('unknown')

    def test_controller_mode_needs_no_libvirt_local_disks_or_ssh_listeners(self):
        import server
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = [{'id': name, 'enabled': True, 'host': '127.0.0.1', 'user': 'node',
                      'identity_file': str(root / 'key'), 'known_hosts': str(root / 'known'),
                      'public_ports': {'1': 54001 if name == 'G2-002' else 54290}} for name in ('G2-002', 'G2-003')]
            path = root / 'nodes.json'; path.write_text(json.dumps(nodes))
            self.assertEqual(len(load_nodes(path, None)), 2)
            with self.assertRaises(ValueError): load_nodes(path)
            with patch.dict(os.environ, {'RENTAL_LOCAL_NODE_ID': '', 'RENTAL_NODES_CONFIG': str(path)}), \
                 patch.object(server, 'ROOT', root), patch.object(server, 'SIMULATION', False), \
                 patch.object(server, 'SLOT_COUNT', 0), \
                 patch.object(server, 'LibvirtBackend', side_effect=AssertionError('must not initialize a local VM backend')), \
                 patch.object(server.shutil, 'disk_usage', side_effect=AssertionError('must not probe controller disk as a node')):
                service = server.Service(start_background=False)
                try:
                    self.assertEqual(service.forwarders, {})
                    self.assertEqual(set(service.core.nodes), {'G2-002', 'G2-003'})
                    for remote in service.backend.remotes.values():
                        remote.observed = time.monotonic()
                        remote.snapshot = {'ready': True, 'storage': {'totalGiB': 3300, 'freeGiB': 3000, 'usedGiB': 300,
                                          'safetyGiB': 128, 'lowSpace': False, 'mode': 'test'}}
                    service.core.register('customer', 'controller-test-password')
                    state = service.state('customer')
                    self.assertEqual(state['controller'], {'mode': 'controller_only', 'localNodeId': None})
                    self.assertEqual(len(state['slots']), 16)
                    self.assertEqual(service.tunnel_ports('G2-002'), {1: 54001})
                    self.assertEqual(service.order('customer', {}, 'remote-default')['node_id'], 'G2-002')
                finally: service.close()


if __name__ == '__main__': unittest.main()
