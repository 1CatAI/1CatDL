"""Free physical extensions must not change billing or the active node spec."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core import Core
from backend import SimulationBackend
from node_agent import NodeAgent


class StorageGiftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Core(Path(self.temp.name) / 'core', nodes={'G2-003': {'storage_pool_budget': 800}})
        self.core.ensure_admin('operator', 'test-only-operator-password')
        self.core.register('customer', 'test-only-customer-password')
        self.core.recharge('operator', 'customer', 10000)
        self.row = self.core.order('customer', {'data': 200}, 'data', node_id='G2-003')
        self.row = self.core.action('customer', self.row['id'], 'start')
        self.core.mark_running(self.row['id'])

    def grant(self, amount=400, previous=0):
        return self.core.record_storage_gift('operator', 'customer', self.row['id'], amount,
                                            expected_gift_gib=previous, verified_total_gib=200 + amount)

    def test_no_retroactive_charge_and_no_extra_ongoing_storage_fee(self):
        when = datetime(2026, 9, 22, tzinfo=timezone.utc)
        with self.core._transaction() as con:
            con.execute('UPDATE instances SET storage_started_at=? WHERE id=?',
                        ((when - timedelta(days=1)).isoformat(), self.row['id']))
        self.core.bill_running(when)
        before = self.core.profile('customer')['balanceCents']
        with self.core._connection() as con:
            prior = dict(con.execute('SELECT * FROM instances WHERE id=?', (self.row['id'],)).fetchone())
        self.grant()
        with self.core._connection() as con:
            after = dict(con.execute('SELECT * FROM instances WHERE id=?', (self.row['id'],)).fetchone())
        self.assertEqual({k: v for k, v in prior.items() if k != 'gift_data_disk_gib'},
                         {k: v for k, v in after.items() if k != 'gift_data_disk_gib'})
        self.core.bill_running(when)
        self.assertEqual(self.core.profile('customer')['balanceCents'], before)
        self.core.bill_running(when + timedelta(days=1))
        self.assertEqual(before - self.core.profile('customer')['balanceCents'], 92)
        self.assertEqual(self.core.ledger('customer')[0]['reason'], 'storage:200GiB')

    def test_capacity_is_physical_and_freed_only_on_deletion(self):
        self.grant()
        self.assertEqual(self.core.metrics('G2-003')['used']['dataDiskGiB'], 600)
        with self.assertRaisesRegex(RuntimeError, 'storage pool'):
            self.core.order('customer', {'data': 200}, 'overflow', node_id='G2-003')
        self.core.action('customer', self.row['id'], 'stop')
        self.core.mark_off(self.row['id'])
        self.assertEqual(self.core.metrics('G2-003')['used']['dataDiskGiB'], 600)
        self.core.action('customer', self.row['id'], 'delete')
        self.core.mark_off(self.row['id'])
        self.assertEqual(self.core.metrics('G2-003')['used']['dataDiskGiB'], 0)

    def test_idempotence_migration_and_reject_stale_or_unauthorized_writes(self):
        first = self.grant()
        self.assertEqual(self.grant(), first)
        events = [e for e in self.core.audit('operator') if e['event'] == 'storage_gift_recorded']
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]['detail'])['billableGiB'], 200)
        restarted = Core(Path(self.temp.name) / 'core', nodes={'G2-003': {'storage_pool_budget': 800}})
        self.assertEqual(restarted.list_instances('customer')[0]['gift_data_disk_gib'], 400)
        with self.assertRaisesRegex(ValueError, 'refresh'):
            self.grant(450)
        with self.assertRaises(PermissionError):
            self.core.record_storage_gift('customer', 'customer', self.row['id'], 500,
                                         expected_gift_gib=400, verified_total_gib=700)
        with self.assertRaisesRegex(ValueError, 'cannot shrink'):
            self.grant(300, previous=400)
        with self.assertRaises(ValueError):
            self.grant(True)

    def test_existing_node_lease_and_restart_spec_are_unchanged(self):
        # Import here to avoid changing other test modules' simulation settings.
        from server import Service
        old = Service.backend_instance(self.row)
        cfg = {'node_id': 'G2-003', 'state_root': str(Path(self.temp.name) / 'node'),
               'data_root': self.temp.name, 'safety_gib': 0}
        node = NodeAgent(cfg, SimulationBackend(), {n: Mock() for n in range(1, 25)})
        node.reserve(old)
        new = Service.backend_instance(self.grant())
        self.assertEqual(new, old)
        self.assertEqual(node.matching(new)['instance'], old)
        node.heartbeat([new])
        self.assertFalse(node.records[str(self.row['id'])]['closed'])
        self.core.action('customer', self.row['id'], 'stop')
        self.core.mark_off(self.row['id'])
        headless = self.core.action('customer', self.row['id'], 'start', mode='headless')
        self.assertEqual(headless['gift_data_disk_gib'], 400)
        self.assertEqual(Service.backend_instance(headless)['data_disk'], 200)

    def test_reject_unproven_volume_size_and_insufficient_capacity(self):
        with self.assertRaisesRegex(ValueError, 'verified disk size'):
            self.core.record_storage_gift('operator', 'customer', self.row['id'], 400,
                                         expected_gift_gib=0, verified_total_gib=200)
        with self.assertRaisesRegex(RuntimeError, 'storage pool'):
            self.grant(600)
        self.assertEqual(self.core.list_instances('customer')[0]['gift_data_disk_gib'], 0)
        new = self.core.order('customer', {'data': 200}, 'never-booted', node_id='G2-003')
        with self.assertRaisesRegex(ValueError, 'initialized'):
            self.core.record_storage_gift('operator', 'customer', new['id'], 100,
                                         expected_gift_gib=0, verified_total_gib=300)

    def test_customer_cannot_order_a_gift_or_a_600_gib_paid_disk(self):
        for spec in ({'data': 200, 'gift_data_disk_gib': 400}, {'data': 600}):
            with self.assertRaises(ValueError):
                self.core.order('customer', spec, 'not-authorized', node_id='G2-003')


if __name__ == '__main__':
    unittest.main()
