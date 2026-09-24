import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core import Core, LOCAL_NODE
from backend import SimulationBackend
from node_agent import NodeAgent
from remote_backend import BackendRouter, RemoteBackend, NodeUnavailable


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core(self.temp.name,nodes={'G2-003':{}},rate_cents_per_hour=400)
        self.core.ensure_admin('operator','testing-password-only')
        for owner in ('alice','bobby','carol'):
            self.core.register(owner,'testing-password-only')
            self.core.recharge('operator',owner,10000,idempotency='setup-'+owner)

    def tearDown(self): self.temp.cleanup()

    def order(self,owner,node):
        return self.core.order(owner,{},owner+'-'+node,node_id=node)

    def test_same_slot_number_is_independent_and_default_limit_is_unlimited(self):
        a,b = self.order('alice',LOCAL_NODE),self.order('bobby','G2-003')
        self.assertEqual(self.core.action('alice',a['id'],'start')['slot'],1)
        self.assertEqual(self.core.action('bobby',b['id'],'start')['slot'],1)
        c = self.order('alice','G2-003')
        self.core.action('alice',c['id'],'start')
        self.assertEqual(self.core.metrics()['capacity']['slots'],16)
        self.assertEqual(self.core.metrics('G2-003')['used']['cpu'],32)

    def test_home_node_is_durable_and_idempotency_does_not_rehome(self):
        row = self.order('alice','G2-003')
        with self.assertRaisesRegex(ValueError,'different node'):
            self.core.order('alice',{},'alice-G2-003',node_id=LOCAL_NODE)
        restarted = Core(self.temp.name,nodes={'G2-003':{}})
        self.assertEqual(restarted.list_instances('alice')[0]['node_id'],'G2-003')
        self.assertEqual(row['generation'],0)

    def test_stale_poweroff_observation_cannot_stop_new_generation(self):
        row = self.order('alice','G2-003')
        first = self.core.action('alice',row['id'],'start')
        self.core.mark_running(row['id'])
        self.core.action('alice',row['id'],'stop')
        self.core.mark_off(row['id'])
        second = self.core.action('alice',row['id'],'start')
        self.core.mark_running(row['id'])
        self.assertFalse(self.core.observe_poweroff(row['id'],generation=first['generation']))
        self.assertEqual(self.core.list_instances('alice')[0]['state'],'running')
        self.assertTrue(self.core.observe_poweroff(row['id'],generation=second['generation']))

    def test_remote_billing_never_advances_without_confirmed_runtime(self):
        row = self.order('alice','G2-003')
        row = self.core.action('alice',row['id'],'start')
        self.core.mark_running(row['id'])
        before = self.core.profile('alice')['balanceCents']
        self.core.bill_running(datetime.now(timezone.utc)+timedelta(hours=2))
        self.assertEqual(self.core.profile('alice')['balanceCents'],before)
        with self.core._transaction() as con:
            started = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
            con.execute('UPDATE usage_ledger SET opened_at=?',(started,))
            con.execute('UPDATE instances SET confirmed_through=?',(started,))
        self.core.confirm_remote_runtime(row['id'],row['generation'],datetime.now(timezone.utc).isoformat())
        self.core.bill_running()
        self.assertEqual(before-self.core.profile('alice')['balanceCents'],400)
        self.core.bill_running(datetime.now(timezone.utc)+timedelta(hours=3))
        self.assertEqual(before-self.core.profile('alice')['balanceCents'],400)

    def test_concurrent_cross_node_start_for_same_owner_has_one_winner(self):
        self.core.set_customer_gpu_limit('operator', 'alice', 1, None)
        rows = [self.order('alice',LOCAL_NODE),self.order('alice','G2-003')]
        barrier = threading.Barrier(2)
        results = []
        def start(row):
            barrier.wait()
            try: results.append(self.core.action('alice',row['id'],'start'))
            except RuntimeError: pass
        threads = [threading.Thread(target=start,args=(row,)) for row in rows]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(results),1)

    def test_remote_unknown_never_releases_slot(self):
        row = self.order('alice','G2-003')
        self.core.action('alice',row['id'],'start')
        self.core.mark_error(row['id'],'connection lost')
        active = self.core.active_instances()[0]
        self.assertEqual((active['state'],active['slot']),('quarantined',1))
        self.assertEqual(active['node_id'],'G2-003')


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {'node_id':'G2-003','state_root':self.temp.name,'data_root':self.temp.name,'safety_gib':0}
        class NodeSimulation(SimulationBackend):
            def state(self,instance):
                return self.states.get(instance['id'],'absent')
            def recovered(self,instance):
                return self.state(instance) in ('off','absent')
        self.backend = NodeSimulation()
        self.forwarders = {n:Mock() for n in range(1,25)}
        self.telemetry = Mock()
        self.telemetry.snapshot.return_value = {'status':'live','observedAt':'2026-09-22T08:00:00+00:00'}
        self.node = NodeAgent(self.config,self.backend,self.forwarders,self.telemetry)
        self.instance = dict(id='901',node_id='G2-003',generation=1,slot=1,endpoint=1,mode='gpu',vcpu=16,memory_mb=62500,data_disk=0)

    def tearDown(self): self.temp.cleanup()

    def call(self,method,instance=None,**fields):
        return self.node.dispatch({'node_id':'G2-003','method':method,'instance':instance or self.instance,**fields})

    def boot(self):
        self.call('prepare',password='test-guest-secret')
        self.call('start')

    def test_generation_fences_duplicate_start_after_stop_and_delete(self):
        self.boot()
        self.call('start')
        self.call('stop')
        with self.assertRaises(RuntimeError): self.call('start')
        self.assertTrue(self.call('recovered'))
        self.call('release')
        with self.assertRaises(ValueError): self.call('prepare',password='test-guest-secret')
        self.assertTrue(self.node.records['901']['released'])
        self.instance = {**self.instance,'generation':2}
        self.boot()
        with self.assertRaises(ValueError): self.call('stop',{**self.instance,'generation':1})

    def test_no_secret_persisted_and_reservation_survives_restart(self):
        self.boot()
        data = self.node.path.read_text()
        self.assertNotIn('test-guest-secret',data)
        restarted = NodeAgent(self.config,self.backend,self.forwarders)
        self.assertTrue(restarted.records['901']['held'])

    def test_proxy_loss_after_node_restart_is_reported_and_repairable(self):
        self.boot()
        self.call('healthy')
        self.assertTrue(self.node.heartbeat([self.instance])['instances']['901']['sshReady'])
        restarted = NodeAgent(self.config,self.backend,self.forwarders)
        self.assertFalse(restarted.heartbeat([self.instance])['instances']['901']['sshReady'])
        self.assertEqual(self.backend.state(self.instance),'running')
        restarted.dispatch({'node_id':'G2-003','method':'healthy','instance':self.instance})
        self.assertTrue(restarted.heartbeat([self.instance])['instances']['901']['sshReady'])

    def test_heartbeat_exposes_bounded_host_telemetry(self):
        result = self.node.heartbeat([])
        self.assertEqual(result['telemetry'], self.telemetry.snapshot.return_value)
        self.telemetry.snapshot.assert_called_once_with()

    def test_duplicate_slot_on_node_rejected(self):
        self.boot()
        with self.assertRaisesRegex(RuntimeError,'already reserved'):
            self.call('prepare',{**self.instance,'id':'902'},password='test-guest-secret')

    def test_expired_lease_shuts_off_without_freeing_unrecovered_card(self):
        self.boot()
        self.node.records['901']['lease_until'] = time.time()-1
        self.node.expire_once()
        self.assertEqual(self.backend.state(self.instance),'off')
        self.assertTrue(self.node.records['901']['closed'])
        self.assertTrue(self.node.records['901']['held'])
        self.node.expire_once()
        self.assertFalse(self.node.records['901']['held'])
        self.node.heartbeat([self.instance])
        with self.assertRaises(RuntimeError): self.call('start')

    def test_timeout_after_start_can_retry_without_second_launch(self):
        self.call('prepare',password='test-guest-secret')
        original = self.backend.start
        def lost_reply(instance):
            original(instance)
            raise TimeoutError('response lost')
        with patch.object(self.backend,'start',side_effect=lost_reply):
            with self.assertRaises(TimeoutError): self.call('start')
        self.assertTrue(self.call('start'))

    def test_no_arbitrary_paths_shell_or_wrong_host(self):
        for modified in ({**self.instance,'id':'../../1'},{**self.instance,'node_id':'G2-002'},
                         {**self.instance,'vcpu':32},{**self.instance,'path':'/etc'},
                         {**self.instance,'endpoint':9}):
            with self.assertRaises(ValueError): self.call('prepare',modified,password='test-guest-secret')
        with self.assertRaises(ValueError): self.call('exec',command='whoami')

    def test_stopped_instance_can_delete_using_no_slot_but_generation_matches(self):
        self.boot()
        self.call('stop')
        self.call('recovered')
        self.call('release',{**self.instance,'slot':None,'endpoint':None})
        self.assertTrue(self.node.records['901']['released'])

    def test_never_started_instance_can_release_idempotently(self):
        stopped = {**self.instance,'generation':0,'slot':None,'endpoint':None}
        self.assertTrue(self.call('release',stopped))
        self.assertTrue(self.call('release',stopped))

    def test_old_release_cannot_disconnect_reassigned_ssh_or_probe_new_owner_gpu(self):
        self.boot()
        self.call('healthy')
        self.call('stop')
        self.call('recovered')
        newer = {**self.instance,'id':'902'}
        self.call('prepare',newer,password='test-guest-secret')
        self.call('start',newer)
        self.call('healthy',newer)
        self.forwarders[1].set_target.reset_mock()
        with patch.object(self.backend,'recovered',side_effect=AssertionError('old slot is now occupied')):
            self.call('release',{**self.instance,'slot':None,'endpoint':None})
        self.forwarders[1].set_target.assert_not_called()
        self.assertEqual(self.backend.state(newer),'running')


class TransportTests(unittest.TestCase):
    def test_ssh_is_pinned_fixed_command_and_timeout_is_unknown(self):
        remote = RemoteBackend(dict(id='G2-003',enabled=True,known_hosts='/pins',identity_file='/key',host='192.168.1.44',user='onecat-node'))
        with patch('remote_backend.subprocess.run',side_effect=OSError) as run:
            self.assertEqual(remote.state({'id':'12'}),'unknown')
            argv = run.call_args.args[0]
            self.assertIn('StrictHostKeyChecking=yes',argv)
            self.assertEqual(argv[-1],'node-rpc')
            self.assertFalse(run.call_args.kwargs.get('shell',False))
        router = BackendRouter(SimulationBackend(),[])
        with self.assertRaises(NodeUnavailable): router.state({'id':'12','node_id':'missing-node'})


if __name__ == '__main__': unittest.main()
