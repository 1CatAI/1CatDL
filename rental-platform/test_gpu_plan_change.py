"""Changing the next GPU boot profile must preserve customer disks and tenancy."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backend import SimulationBackend
from core import Core
from node_agent import NodeAgent
from server import Service


def customer(core, name, balance=10001):
    with core._transaction() as con:
        con.execute("INSERT INTO users(name,salt,password_hash,created_at,role,balance_cents) VALUES (?,?,?,?,?,?)",
                    (name, b'fixture', b'fixture', datetime.now(timezone.utc).isoformat(), 'customer', balance))


class GpuPlanChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = Core(self.tmp.name, rate_cents_per_hour=400, nodes={'G2-003': {}})
        customer(self.core, 'alice')
        customer(self.core, 'bob')

    def test_one_four_switch_preserves_identity_and_blocks_eight_upgrade(self):
        row = self.core.order('alice', {'gpu_count': 1, 'data': 100}, 'one', node_id='G2-003')
        changed = self.core.change_gpu_plan('alice', row['id'], 4)
        self.assertEqual((changed['id'], changed['node_id'], changed['cpu'], changed['ram'], changed['data']),
                         (row['id'], 'G2-003', 64, 250000, 100))
        self.assertEqual(changed['gift_data_disk_gib'], 0)
        self.assertEqual(self.core.change_gpu_plan('alice', row['id'], 4), changed)
        with self.assertRaisesRegex(ValueError, '不可升级至八卡'):
            self.core.change_gpu_plan('alice', row['id'], 8)
        with self.assertRaises(PermissionError):
            self.core.change_gpu_plan('bob', row['id'], 1)
        self.assertEqual(self.core.change_gpu_plan('alice', row['id'], 1)['cpu'], 16)
        self.assertEqual(self.core.list_instances('alice')[0]['data'], 100)
        with self.core._connection() as con:
            events = con.execute("SELECT * FROM audit_events WHERE event='instance_gpu_plan_changed'").fetchall()
        self.assertEqual(len(events), 2)

    def test_running_or_unreleased_instance_cannot_change(self):
        row = self.core.order('alice', {'gpu_count': 1}, 'running')
        self.core.action('alice', row['id'], 'start')
        for target in (1, 4):
            with self.assertRaisesRegex(ValueError, '完全关机'):
                self.core.change_gpu_plan('alice', row['id'], target)
        self.core.mark_running(row['id'])
        self.core.action('alice', row['id'], 'stop')
        with self.assertRaisesRegex(ValueError, '完全关机'):
            self.core.change_gpu_plan('alice', row['id'], 4)
        self.core.mark_off(row['id'])
        self.assertEqual(self.core.change_gpu_plan('alice', row['id'], 4)['gpu_count'], 4)

    def test_eight_downgrade_keeps_free_six_hundred_gib_and_remote_payload(self):
        row = self.core.order('alice', {'gpu_count': 8}, 'eight', node_id='G2-003')
        self.assertTrue(row['eight_card_gift_disk'])
        self.assertEqual(Service.backend_instance(row)['data_disk'], 600)
        four = self.core.change_gpu_plan('alice', row['id'], 4)
        self.assertEqual((four['gpu_count'], four['cpu'], four['ram'], four['data'], four['gift_data_disk_gib']),
                         (4, 64, 250000, 0, 600))
        self.assertTrue(four['eight_card_gift_disk'])
        self.assertEqual(self.core.metrics('G2-003')['used']['dataDiskGiB'], 600)
        self.assertEqual(self.core.rate_for_mode('gpu', four['gpu_count']), 1600)
        with tempfile.TemporaryDirectory() as node_root:
            agent = NodeAgent({'node_id': 'G2-003', 'state_root': node_root, 'data_root': node_root}, SimulationBackend())
            start = self.core.action('alice', row['id'], 'start')
            payload = Service.backend_instance(start)
            self.assertEqual((payload['data_disk'], payload['eight_card_gift_disk']), (600, True))
            self.assertEqual(agent.validate(payload)['gpu_count'], 4)
            with self.assertRaises(ValueError):
                agent.validate({k: v for k, v in payload.items() if k != 'eight_card_gift_disk'})
            with self.assertRaises(ValueError):
                agent.validate({**payload, 'data_disk': 200})
            self.core.mark_running(row['id'])
            self.assertEqual(self.core.instance_costs('alice')[row['id']]['rateCentsPerHour'], 1600)
            with self.core._transaction() as con:
                con.execute('UPDATE instances SET storage_started_at=? WHERE id=?',
                            ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), row['id']))
            self.core.bill_running()
            self.assertEqual(self.core.list_instances('alice')[0]['storage_charged_cents'], 0)
            self.core.action('alice', row['id'], 'stop')
            self.core.mark_off(row['id'])
            one = self.core.change_gpu_plan('alice', row['id'], 1)
            self.assertEqual((one['cpu'], one['ram'], one['gift_data_disk_gib'], one['data']),
                             (16, 62500, 600, 0))
            self.assertEqual(Service.backend_instance(one)['data_disk'], 600)
            with self.assertRaisesRegex(ValueError, '不可升级至八卡'):
                self.core.change_gpu_plan('alice', row['id'], 8)

    def test_existing_eight_card_gift_is_backfilled_on_upgrade(self):
        row = self.core.order('alice', {'gpu_count': 8}, 'eight')
        with self.core._transaction() as con:
            con.execute('UPDATE instances SET eight_card_gift_disk=0 WHERE id=?', (row['id'],))
        reopened = Core(self.tmp.name, rate_cents_per_hour=400, nodes={'G2-003': {}})
        self.assertTrue(reopened.list_instances('alice')[0]['eight_card_gift_disk'])
        self.assertEqual(reopened.change_gpu_plan('alice', row['id'], 4)['gift_data_disk_gib'], 600)

    def test_headless_instance_keeps_headless_resources_until_gpu_boot(self):
        row = self.core.order('alice', {'gpu_count': 4, 'mode': 'headless'}, 'headless')
        changed = self.core.change_gpu_plan('alice', row['id'], 1)
        self.assertEqual((changed['mode'], changed['cpu'], changed['ram']), ('headless', 2, 4000))
        started = self.core.action('alice', row['id'], 'start', mode='gpu')
        self.assertEqual((started['gpu_count'], started['cpu'], started['ram']), (1, 16, 62500))


if __name__ == '__main__':
    unittest.main()
