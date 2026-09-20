"""Headless scheduling, billing and passthrough isolation regressions."""
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from core import Core, COMPUTE_UNITS_PER_CENT
import test_backend


class HeadlessCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Core(self.tmp.name, storage_pool_budget=3000, rate_cents_per_hour=400)
        self.core.register('operator', 'test-only-password', role='admin')
        self.core.register('alice', 'test-only-password')
        self.core.recharge('operator', 'alice', 1000, idempotency='initial')

    def tearDown(self):
        self.tmp.cleanup()

    def order(self, key, mode='headless'):
        return self.core.order('alice', {'mode': mode}, key)

    def test_multiple_headless_and_one_gpu_coexist(self):
        a, b, gpu = self.order('a'), self.order('b'), self.order('gpu', 'gpu')
        for row in (a, b, gpu):
            self.core.action('alice', row['id'], 'start')
            self.core.mark_running(row['id'])
        live = self.core.active_instances()
        self.assertEqual(len(live), 3)
        self.assertEqual([r['slot'] for r in live], [None, None, 1])
        self.assertEqual(len({r['endpoint'] for r in live}), 3)
        self.assertEqual(self.core.metrics()['used']['cpu'], 20)
        self.assertEqual(self.core.metrics()['used']['memoryMB'], 70500)
        self.assertEqual(self.core.metrics()['active_slots'], 1)
        self.assertEqual(self.core.metrics()['active_headless'], 2)
        extra = self.order('gpu2', 'gpu')
        with self.assertRaisesRegex(RuntimeError, 'one GPU'):
            self.core.action('alice', extra['id'], 'start')

    def test_fixed_specs_and_mode_validation(self):
        for spec in ({'mode': 'other'}, {'mode': 'headless', 'cpu': 16}, {'mode': 'headless', 'ram': 4096}, {'mode': 'headless', 'gpu': 'Gaudi2'}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.core.order('alice', spec, str(spec))
        a = self.order('mode')
        with self.assertRaises(ValueError):
            self.core.action('alice', a['id'], 'start', mode='invalid')

    def test_concurrent_reservations_and_quarantine_hold_cpu_and_endpoint(self):
        self.core.cpu_budget = 4
        rows = [self.order(str(i)) for i in range(3)]
        barrier = threading.Barrier(3)
        accepted = []
        def start(row):
            barrier.wait()
            try:
                accepted.append(self.core.action('alice', row['id'], 'start'))
            except RuntimeError:
                pass
        workers = [threading.Thread(target=start, args=(r,)) for r in rows]
        for t in workers: t.start()
        for t in workers: t.join()
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len({r['endpoint'] for r in accepted}), 2)
        self.core.mark_error(accepted[0]['id'], 'not proven off')
        self.assertEqual(self.core.metrics()['used']['cpu'], 4)
        self.assertEqual(self.core.list_instances('alice')[accepted[0]['id'] - 1]['state'], 'quarantined')
        self.core.mark_failed(accepted[0]['id'])
        self.assertEqual(self.core.metrics()['used']['cpu'], 2)

    def test_rate_is_fixed_and_account_exhaustion_stops_all_including_booting(self):
        a, b, c = [self.order(k) for k in ('a', 'b', 'c')]
        for row in (a, b, c): self.core.action('alice', row['id'], 'start')
        for row in (a, b): self.core.mark_running(row['id'])
        self.core.set_price('operator', 999)
        now = datetime.now(timezone.utc)
        with self.core._transaction() as db:
            db.execute('UPDATE usage_ledger SET opened_at=?', ((now - timedelta(hours=1)).isoformat(),))
        self.core.bill_running(now)
        self.assertEqual(self.core.profile('alice')['balanceCents'], 984)
        self.core.bill_running(now)
        self.assertEqual(self.core.profile('alice')['balanceCents'], 984)
        with self.core._transaction() as db:
            self.assertEqual({r[0] for r in db.execute('SELECT rate_cents_per_hour FROM usage_ledger')}, {8})
            db.execute("UPDATE users SET balance_cents=1 WHERE name='alice'")
        self.core.bill_running(now + timedelta(hours=1))
        self.assertEqual(self.core.profile('alice')['balanceCents'], 0)
        self.assertEqual({r['state'] for r in self.core.list_instances('alice')}, {'stopping'})

    def test_fraction_survives_controller_and_guest_restart(self):
        a = self.order('fraction')
        for _ in range(2):
            self.core.action('alice', a['id'], 'start')
            self.core.mark_running(a['id'])
            now = datetime.now(timezone.utc)
            with self.core._transaction() as db:
                db.execute('UPDATE usage_ledger SET opened_at=? WHERE closed_at IS NULL', ((now - timedelta(seconds=240)).isoformat(),))
            self.core.bill_running(now)
            self.core.action('alice', a['id'], 'stop')
            self.core.mark_off(a['id'])
            self.core = Core(self.tmp.name, storage_pool_budget=3000, rate_cents_per_hour=400)
        self.assertEqual(self.core.profile('alice')['balanceCents'], 999)
        with self.core._connection() as db:
            self.assertGreater(db.execute('SELECT compute_remainder FROM instances').fetchone()[0], 0)
            self.assertEqual(sum(r[0] for r in db.execute('SELECT charged_cents FROM usage_ledger')), 1)

    def test_mode_switch_has_new_interval_and_same_instance_storage(self):
        a = self.order('switch', 'gpu')
        self.core.action('alice', a['id'], 'start', mode='headless')
        self.core.mark_running(a['id'])
        with self.assertRaises(ValueError):
            self.core.action('alice', a['id'], 'start', mode='gpu')
        self.core.action('alice', a['id'], 'stop')
        stopped = self.core.mark_off(a['id'])
        self.assertIsNone(stopped['endpoint'])
        gpu = self.core.action('alice', a['id'], 'start', mode='gpu')
        self.core.mark_running(a['id'])
        self.assertEqual((gpu['id'], gpu['cpu'], gpu['ram'], gpu['sys'], gpu['data']), (a['id'], 16, 62500, 50, 0))
        with self.core._connection() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT rate_cents_per_hour FROM usage_ledger ORDER BY id')], [8, 400])

    def test_idle_cleanup_and_zero_balance(self):
        a = self.order('idle')
        with self.core._transaction() as db:
            db.execute("UPDATE users SET balance_cents=0 WHERE name='alice'")
        with self.assertRaisesRegex(RuntimeError, 'balance is zero'):
            self.core.action('alice', a['id'], 'start')
        with self.core._transaction() as db:
            db.execute('UPDATE instances SET last_activity_at=?', ((datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(),))
        self.assertEqual(self.core.reap_idle(), [a['id']])
        self.assertEqual(self.core.mark_off(a['id'])['state'], 'deleted')

    def test_upgrade_preserves_gpu_interval_and_wallet(self):
        a = self.order('legacy', 'gpu')
        self.core.action('alice', a['id'], 'start')
        self.core.mark_running(a['id'])
        with self.core._transaction() as db:
            db.execute('DROP INDEX one_active_endpoint')
            for column in ('mode', 'endpoint', 'compute_remainder'):
                db.execute('ALTER TABLE instances DROP COLUMN ' + column)
            db.execute('ALTER TABLE usage_ledger DROP COLUMN metered_units')
        self.core = Core(self.tmp.name, storage_pool_budget=3000, rate_cents_per_hour=99)
        live = self.core.active_instances()[0]
        self.assertEqual((live['mode'], live['slot'], live['endpoint']), ('gpu', 1, 1))
        self.assertEqual(self.core.profile('alice')['balanceCents'], 1000)
        self.assertEqual(self.core.instance_costs('alice')[a['id']]['rateCentsPerHour'], 400)


class HeadlessBackendTests(unittest.TestCase):
    setUp = test_backend.BackendStorageTests.setUp
    tearDown = test_backend.BackendStorageTests.tearDown
    def test_headless_xml_has_no_passthrough_or_gpu_mmio(self):
        instance = {'id': '101', 'mode': 'headless', 'slot': None, 'vcpu': 2, 'memory_mb': 4000, 'data_disk': 0}
        directory = self.backend.path(instance)
        directory.mkdir()
        for name in ('system.qcow2', 'seed.iso', 'VARS.fd'): (directory / name).write_bytes(b'qa')
        with patch.object(self.backend, 'state', return_value='off'), patch.object(self.backend, 'preflight') as preflight, patch.object(self.backend, 'virsh', return_value='d4cc25b6-b546-438e-b54d-89e983698902'):
            self.backend.start(instance)
            preflight.assert_not_called()
        root = ET.fromstring((directory / 'domain.xml').read_text())
        self.assertEqual(root.findtext('vcpu'), '2')
        self.assertEqual(root.findtext('memory'), '4000')
        self.assertEqual(root.findall('./devices/hostdev'), [])
        self.assertEqual(root.findall('.//pcihole64'), [])
        self.assertEqual(root.findall('.//{http://libvirt.org/schemas/domain/qemu/1.0}commandline'), [])
        self.assertNotEqual(self.backend.mac_address(instance), self.backend.mac_address(dict(instance, id='102')))

    def test_headless_health_never_calls_hl_smi(self):
        instance = {'id': '101', 'mode': 'headless', 'slot': None}
        payload = {'return': [{'hardware-address': self.backend.mac_address(instance), 'ip-addresses': [{'ip-address': '192.168.124.101'}]}]}
        with patch.object(self.backend, 'virsh', return_value=json.dumps(payload)) as virsh, patch('backend.socket.create_connection') as sock, patch.object(self.backend, 'ensure_shared_guest', return_value=True):
            sock.return_value.__enter__.return_value.recv.return_value = b'SSH-2.0-OpenSSH'
            self.assertEqual(self.backend.healthy(instance), '192.168.124.101')
            self.assertEqual(virsh.call_count, 1)
            self.assertNotIn('hl-smi', str(virsh.call_args_list))


if __name__ == '__main__':
    unittest.main()
