"""Eight-card launch, gift storage, billing and lifecycle regressions."""
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from backend import LibvirtBackend, SimulationBackend
from core import Core
from gpu_plans import gpu_count, gpu_slots
from node_agent import NodeAgent
from server import Service


def customer(core, name, balance=10001):
    with core._transaction() as con:
        con.execute("INSERT INTO users(name,salt,password_hash,created_at,role,balance_cents) VALUES (?,?,?,?,?,?)",
                    (name, b'fixture', b'fixture', datetime.now(timezone.utc).isoformat(), 'customer', balance))


class EightGpuCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = Core(self.tmp.name, rate_cents_per_hour=400, nodes={'G2-003': {}})
        customer(self.core, 'alice')
        customer(self.core, 'bob')

    def test_full_node_and_fixed_gift(self):
        self.assertEqual(gpu_count(8), 8)
        self.assertEqual(gpu_slots(1, 8), list(range(1, 9)))
        with self.assertRaises(ValueError):
            gpu_slots(5, 8)
        with self.assertRaises(ValueError):
            self.core.order('alice', {'gpu_count': 8, 'data': 1}, 'adjust-gift')
        row = self.core.order('alice', {'gpu_count': 8}, 'eight', node_id='G2-002')
        self.assertEqual((row['cpu'], row['ram'], row['sys'], row['data'], row['gift_data_disk_gib']),
                         (128, 480000, 50, 0, 600))
        self.assertEqual(self.core.metrics('G2-002')['used']['dataDiskGiB'], 600)
        self.assertEqual(self.core.pricing()['gpuPlans'][-1]['rateCentsPerHour'], 3200)
        self.assertEqual(self.core.pricing()['gpuPlans'][-1]['minCreateBalanceExclusiveCents'], 10000)
        started = self.core.action('alice', row['id'], 'start')
        self.assertEqual(started['slots'], list(range(1, 9)))
        self.assertEqual(self.core.metrics('G2-002')['active_slots'], 8)
        other = self.core.order('bob', {'gpu_count': 8}, 'eight-other', node_id='G2-002')
        with self.assertRaises(RuntimeError):
            self.core.action('bob', other['id'], 'start')
        remote = self.core.order('bob', {'gpu_count': 8}, 'eight-remote', node_id='G2-003')
        self.assertEqual(self.core.action('bob', remote['id'], 'start')['slots'], list(range(1, 9)))

    def test_creation_requires_more_than_one_hundred_but_restarts_do_not(self):
        for balance in (0, 10000):
            with self.core._transaction() as con:
                con.execute("UPDATE users SET balance_cents=? WHERE name='alice'", (balance,))
            with self.assertRaisesRegex(RuntimeError, '创建八卡实例要求余额大于 ¥100'):
                self.core.order('alice', {'gpu_count': 8}, 'eight')
            with self.assertRaisesRegex(RuntimeError, '创建八卡实例要求余额大于 ¥100'):
                self.core.order('alice', {'gpu_count': 8, 'mode': 'headless'}, 'eight-headless')
        self.assertEqual(self.core.list_instances('alice'), [])
        with self.core._transaction() as con:
            con.execute("UPDATE users SET balance_cents=10001 WHERE name='alice'")
        row = self.core.order('alice', {'gpu_count': 8}, 'eight')
        with self.core._transaction() as con:
            con.execute("UPDATE users SET balance_cents=10000 WHERE name='alice'")
        self.assertEqual(self.core.order('alice', {'gpu_count': 8}, 'eight')['id'], row['id'])
        self.core.action('alice', row['id'], 'start')
        with self.core._transaction() as con:
            con.execute("UPDATE users SET balance_cents=0 WHERE name='alice'")
        self.assertEqual(self.core.mark_running(row['id'])['state'], 'running')
        self.core.action('alice', row['id'], 'stop')
        self.core.mark_off(row['id'])
        self.assertEqual(self.core.action('alice', row['id'], 'start')['state'], 'provisioning')
        self.assertEqual(self.core.mark_running(row['id'])['state'], 'running')
        self.assertEqual(self.core.bill_running(datetime.now(timezone.utc)), [row['id']])
        self.assertEqual(self.core.list_instances('alice')[0]['state'], 'stopping')

    def test_eight_card_gift_storage_has_no_storage_charge(self):
        row = self.core.order('alice', {'gpu_count': 8}, 'eight')
        self.core.action('alice', row['id'], 'start')
        self.core.mark_running(row['id'])
        with self.core._transaction() as con:
            con.execute("UPDATE users SET balance_cents=10000 WHERE name='alice'")
        now = datetime.now(timezone.utc)
        with self.core._transaction() as con:
            con.execute('UPDATE usage_ledger SET opened_at=?,last_billed_at=? WHERE instance_id=?',
                        ((now-timedelta(hours=1)).isoformat(), (now-timedelta(hours=1)).isoformat(), row['id']))
            con.execute('UPDATE instances SET storage_started_at=? WHERE id=?',
                        ((now-timedelta(days=1)).isoformat(), row['id']))
        self.core.bill_running(now)
        costs = self.core.instance_costs('alice')[row['id']]
        self.assertEqual(costs['rateCentsPerHour'], 3200)
        self.assertEqual(self.core.list_instances('alice')[0]['storage_charged_cents'], 0)

    def test_fallback_to_second_node_when_primary_cannot_fit_eight(self):
        first = self.core.order('alice', {'gpu_count': 1}, 'one')
        self.core.action('alice', first['id'], 'start')
        observations = {ident: {'online': True, 'imageReady': True,
                                'storage': {'freeGiB': 3000, 'safetyGiB': 100, 'lowSpace': False}}
                        for ident in ('G2-002', 'G2-003')}
        quote = self.core.placement_quote({'gpu_count': 8}, observations)
        self.assertEqual(quote['recommendedNode'], 'G2-003')
        self.assertTrue(quote['fallbackAllowed'])
        self.assertEqual(self.core.order('bob', {'gpu_count': 8}, 'fallback', node_id=None,
                                         observations=observations)['node_id'], 'G2-003')

    def test_existing_instance_survives_legacy_check_migration(self):
        import sqlite3
        row = self.core.order('alice', {'gpu_count': 1}, 'legacy')
        db = self.core.db_path
        con = sqlite3.connect(db, isolation_level=None)
        try:
            schema = con.execute("SELECT sql FROM sqlite_master WHERE name='instances'").fetchone()[0]
            legacy = schema.replace('CHECK(gpu_count IN (1,4,8))', 'CHECK(gpu_count IN (1,4))')
            self.assertNotEqual(legacy, schema)
            indexes = [r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='instances' AND sql IS NOT NULL")]
            con.execute('PRAGMA foreign_keys=OFF')
            con.execute('BEGIN IMMEDIATE')
            header = re.match(r'^CREATE\s+TABLE\s+(?:"instances"|instances)\s*(?=\()', legacy, re.I)
            self.assertIsNotNone(header)
            con.execute('CREATE TABLE instances_legacy ' + legacy[header.end():])
            con.execute('INSERT INTO instances_legacy SELECT * FROM instances')
            con.execute('DROP TABLE instances')
            con.execute('ALTER TABLE instances_legacy RENAME TO instances')
            for index in indexes:
                con.execute(index)
            con.commit()
        finally:
            con.close()
        restarted = Core(self.tmp.name, rate_cents_per_hour=400, nodes={'G2-003': {}})
        self.assertEqual(restarted.list_instances('alice')[0]['id'], row['id'])
        self.assertEqual(restarted.order('bob', {'gpu_count': 8}, 'after-migration')['gpu_count'], 8)
        with restarted._connection() as con:
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])


class EightGpuAdapterTests(unittest.TestCase):
    def test_agent_rejects_nonfixed_eight_card_resources(self):
        with tempfile.TemporaryDirectory() as root:
            node = NodeAgent({'node_id': 'G2-003', 'state_root': root, 'data_root': root}, SimulationBackend())
            item = dict(id='1', node_id='G2-003', generation=1, slot=1, endpoint=1,
                        mode='gpu', vcpu=128, memory_mb=480000, data_disk=600, gpu_count=8)
            self.assertEqual(node.validate(item)['gpu_count'], 8)
            for change in ({'data_disk': 0}, {'memory_mb': 500000}, {'slot': 5, 'endpoint': 5}):
                with self.assertRaises(ValueError):
                    node.validate({**item, **change})
            self.assertEqual(node.validate({**item, 'mode': 'headless', 'slot': None,
                                            'endpoint': 9, 'vcpu': 2, 'memory_mb': 4000})['data_disk'], 600)

    def test_backend_xml_matches_accepted_two_socket_profile(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            bdfs = [f'0000:{n:02x}:00.0' for n in range(16, 24)]
            backend = LibvirtBackend(root, dict(data_root=data, bdfs=bdfs, network='qa'))
            item = dict(id='42', slot=1, gpu_count=8, mode='gpu', vcpu=128,
                        memory_mb=480000, data_disk=600)
            directory = backend.path(item)
            directory.mkdir()
            backend.system_path(item).write_bytes(b'system')
            backend.data_path(item).mkdir()
            (backend.data_path(item)/'data.qcow2').write_bytes(b'data')
            calls = []
            def virsh(*args, **kwargs):
                calls.append((args, kwargs))
                return 'qa-uuid'
            with patch.object(backend, 'state', return_value='off'), patch.object(backend, 'preflight'), \
                 patch.object(backend, 'eight_card_numa'), patch.object(backend, 'eight_card_memory_guard'), \
                 patch.object(backend, 'virsh', side_effect=virsh):
                backend.start(item)
            xml = ET.parse(directory/'domain.xml').getroot()
            self.assertEqual(xml.findtext('memory'), '480000')
            self.assertEqual(xml.find('cpu/topology').attrib, {'sockets': '2', 'cores': '64', 'threads': '1'})
            self.assertEqual([cell.get('memory') for cell in xml.findall('./cpu/numa/cell')], ['240000', '240000'])
            self.assertEqual(len(xml.findall('./cputune/vcpupin')), 128)
            self.assertEqual(len(xml.findall('./devices/controller[@model="pcie-root-port"]')), 16)
            self.assertEqual(len(xml.findall('./devices/hostdev')), 8)
            self.assertEqual(calls[-1][1]['timeout'], 900)
            self.assertEqual(Service.backend_instance({'id': 42, 'slot': 1, 'cpu': 128, 'ram': 480000,
                                                       'data': 0, 'gift_data_disk_gib': 600,
                                                       'gpu_count': 8})['data_disk'], 600)

    def test_stuck_shutdown_is_forceable(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            backend = LibvirtBackend(root, dict(data_root=data, bdfs=[], network='qa'))
            item = {'id': '42', 'gpu_count': 8}
            with patch.object(backend, 'state', return_value='stopping'), patch.object(backend, 'virsh') as virsh:
                backend.force_off(item)
            virsh.assert_called_once_with('destroy', 'cat-rental-42', timeout=600)


if __name__ == '__main__':
    unittest.main()
