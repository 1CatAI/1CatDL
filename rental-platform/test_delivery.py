"""Regression coverage for live-service billing, forwarding and shared storage."""
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core import Core
from test_server_http import server
from backend import LibvirtBackend


class DeliveryCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Core(Path(self.tmp.name), storage_pool_budget=3000)
        self.core.ensure_admin('operator', 'correct horse battery staple')
        self.core.register('alice', 'correct horse battery staple')
        self.core.register('bob', 'correct horse battery staple')
        self.core.recharge('operator', 'alice', 2000, idempotency='setup-credit:alice')
        self.core.recharge('operator', 'bob', 2000, idempotency='setup-credit:bob')

    def tearDown(self):
        self.tmp.cleanup()

    def order(self, key, data=100):
        return self.core.order('alice', {'cpu': 16, 'ram': 62500, 'data': data}, key)

    def test_multiple_storage_debits_never_overdraw_or_refund(self):
        self.order('a'); self.order('b'); self.order('c')
        with self.core._transaction() as db:
            db.execute("UPDATE users SET balance_cents=10 WHERE name='alice'")
        later = datetime.now(timezone.utc) + timedelta(days=1)
        self.core.bill_running(later)
        self.assertEqual(self.core.profile('alice')['balanceCents'], 0)
        self.core.bill_running(later + timedelta(hours=1))
        rows = [r for r in self.core.ledger('alice') if r['reason'].startswith('storage:')]
        self.assertEqual(sum(r['cents'] for r in rows), -10)
        self.assertTrue(all(r['cents'] < 0 for r in rows))

    def test_storage_exhaustion_stops_running_guest_same_pass(self):
        vm = self.order('a')
        self.core.action('alice', vm['id'], 'start')
        self.core.mark_running(vm['id'])
        with self.core._transaction() as db:
            db.execute("UPDATE users SET balance_cents=1 WHERE name='alice'")
        self.core.bill_running(datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual(self.core.list_instances('alice')[0]['state'], 'stopping')

    def test_poweroff_closes_compute_but_keeps_slot_until_recovered(self):
        self.core.set_price('operator', 400)
        vm = self.order('a', 0)
        self.core.action('alice', vm['id'], 'start'); self.core.mark_running(vm['id'])
        self.core.observe_poweroff(vm['id'])
        self.assertEqual(self.core.metrics()['open_usage_intervals'], 0)
        self.assertEqual(self.core.metrics()['active_slots'], 1)
        self.core.mark_off(vm['id'])
        self.assertEqual(self.core.metrics()['active_slots'], 0)

    def test_shared_pool_more_than_fourteen_system_disks(self):
        for n in range(20): self.order(str(n), 0)
        self.assertEqual(len(self.core.list_instances('alice')), 20)
        self.core.storage_pool_budget = 1000
        with self.assertRaisesRegex(RuntimeError, 'storage pool'):
            self.order('over', 0)

    def test_idempotency_cannot_change_configuration(self):
        original = self.order('a', 0)
        self.assertEqual(self.order('a', 0)['id'], original['id'])
        with self.assertRaises(ValueError): self.order('a', 100)

    def test_ledger_costs_admin_scope_and_audit(self):
        vm = self.order('a', 0)
        self.core.set_price('operator', 100)
        self.core.action('alice', vm['id'], 'start'); self.core.mark_running(vm['id'])
        self.core.set_price('operator', 400)
        self.assertEqual(self.core.instance_costs('alice')[vm['id']]['rateCentsPerHour'], 100)
        self.core.bill_running(datetime.now(timezone.utc) + timedelta(minutes=5))
        self.core.bill_running(datetime.now(timezone.utc) + timedelta(minutes=10))
        self.assertEqual(len([r for r in self.core.statement('alice') if r['reason'] == 'usage']), 1)
        self.assertTrue(all(r['owner'] == 'bob' for r in self.core.statement('bob')))
        self.core.admin_action('operator', vm['id'], 'stop')
        self.assertEqual(self.core.audit('operator')[0]['actor'], 'operator')
        self.assertEqual(len(self.core.admin_instances('operator')), 1)
        with self.assertRaises(PermissionError): self.core.admin_instances('bob')
        with self.assertRaises(PermissionError): self.core.statement('bob', True)


class ForwarderTests(unittest.TestCase):
    def test_idle_half_close_large_transfer_and_retarget_cleanup(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0)); listener.listen(1); listener.settimeout(3)
        forwarder = server.Forwarder(0)
        client, accepted = socket.socketpair()
        forwarder.set_target(listener.getsockname())
        threading.Thread(target=forwarder.handle, args=(accepted,), daemon=True).start()
        peer, _ = listener.accept()
        client.settimeout(3); peer.settimeout(3)
        try:
            # This is precisely the old five-second idle disconnect failure.
            time.sleep(5.3)
            peer.sendall(b'after-idle'); self.assertEqual(client.recv(10), b'after-idle')
            payload = b'x' * (2 * 1024 * 1024)
            def send():
                client.sendall(payload); client.shutdown(socket.SHUT_WR)
            sender = threading.Thread(target=send); sender.start()
            received = bytearray()
            while chunk := peer.recv(65536): received.extend(chunk)
            self.assertEqual(bytes(received), payload)
            # Client EOF must not truncate a response from the upstream.
            peer.sendall(b'done'); self.assertEqual(client.recv(4), b'done')
            forwarder.set_target(None)
            self.assertEqual(client.recv(1), b'')
            sender.join(3)
            deadline = time.time() + 2
            while forwarder.connections and time.time() < deadline: time.sleep(.02)
            self.assertFalse(forwarder.connections)
        finally:
            forwarder.close(); listener.close(); client.close(); peer.close()


class DeliveryServiceTests(unittest.TestCase):
    def test_shared_status_visible_without_claiming_legacy_guest_is_mounted(self):
        service = server.Service(start_background=False)
        self.addCleanup(service.close)
        name = 'shared' + str(time.time_ns())
        service.core.ensure_admin('delivery-admin', 'correct horse battery staple')
        service.core.register(name, 'correct horse battery staple')
        service.core.recharge('delivery-admin', name, 2000, idempotency='shared-test-credit')
        vm = service.core.order(name, {'cpu': 16, 'ram': 62500}, 'one')
        service.core.action(name, vm['id'], 'start')
        service.core.mark_running(vm['id'])
        service.backend.states[str(vm['id'])] = 'running'
        for value, expected in [('restart_required', '正常关机后再开机'), ('mounted', '/shared 已接入')]:
            status = {'enabled': True, 'path': '/shared', 'readOnly': True, 'state': value}
            with patch.object(service, 'refresh_workers'), patch.object(service.backend, 'shared_status', return_value=status, create=True):
                row = service.state(name)['instances'][0]
                self.assertEqual(row['sharedStorage'], status)
                self.assertIn(expected, row['message'])

    def test_runtime_reconcile_and_private_admin_state(self):
        service = server.Service(start_background=False)
        name = 'reconcile' + str(time.time_ns())
        service.core.register(name, 'correct horse battery staple')
        service.core.ensure_admin('delivery-admin', 'correct horse battery staple')
        service.core.recharge('delivery-admin', name, 2000, idempotency='reconcile-test-credit')
        vm = service.core.order(name, {'cpu':16, 'ram':62500}, 'one')
        row = service.core.action(name, vm['id'], 'start')
        service.core.mark_running(vm['id'])
        service.forwarded.add(vm['id'])
        service.secrets[str(vm['id'])] = 'not-for-administrator'
        service.backend.states[str(vm['id'])] = 'running'
        with patch.object(service, 'refresh_workers'):
            self.assertEqual(service.state(name)['instances'][0]['password'], 'not-for-administrator')
            all_rows = service.state('delivery-admin', admin_view=True)['instances']
            self.assertTrue(all(r['password'] is None for r in all_rows))
        service.backend.states[str(vm['id'])] = 'off'
        service.reconcile_runtime()
        self.assertEqual(service.core.list_instances(name)[0]['state'], 'stopping')
        self.assertIsNone(service.forwarders[row['slot']].target)
        self.assertEqual(service.core.metrics()['open_usage_intervals'], 0)
        service.core.mark_off(vm['id']); service.close()

    def test_system_disk_root_is_separate_from_control_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'control'; root.mkdir()
            data = Path(temp)/'data'; data.mkdir()
            systems = Path(temp)/'systems'; systems.mkdir()
            backend = LibvirtBackend(root, {'data_root': str(data), 'system_root': str(systems)})
            instance = {'id':'99'}
            self.assertEqual(backend.system_path(instance), systems/'instances'/'99'/'system.qcow2')
            self.assertEqual(backend.path(instance), root/'instances'/'99')
            with self.assertRaises(ValueError): backend.system_path({'id':'../../escape'})


if __name__ == '__main__': unittest.main()
