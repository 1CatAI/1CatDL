"""Four-card scheduling, billing, node fencing and real-adapter XML regression."""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from core import Core
from backend import LibvirtBackend, SimulationBackend
from gpu_plans import gpu_count, gpu_slots
from node_agent import NodeAgent


class FourCardCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Core(self.temp.name, rate_cents_per_hour=400, nodes={'G2-003': {}})
        # Authentication has its own suite. These isolated fixtures hold no real accounts.
        with self.core._transaction() as con:
            for owner in ('alice', 'bobby', 'carol', 'david', 'erica', 'frank', 'grace', 'henry', 'operator'):
                con.execute("INSERT INTO users(name,salt,password_hash,created_at,role,balance_cents) VALUES (?,?,?,?,?,?)",
                            (owner,b'fixture',b'fixture',datetime.now(timezone.utc).isoformat(),
                             'admin' if owner=='operator' else 'customer',10000))

    def order(self, owner, count=4, node='G2-002', mode='gpu', key=None, data=0):
        return self.core.order(owner, {'gpu_count':count,'mode':mode,'data':data}, key or owner+node+mode+str(count), node_id=node)

    def start(self, owner, count=4, **kwargs):
        row = self.order(owner,count,**kwargs)
        return self.core.action(owner,row['id'],'start')

    def test_fixed_resources_and_prices(self):
        one, four = self.order('alice',1), self.order('bobby')
        self.assertEqual((one['cpu'],one['ram'],one['sys'],one['data']),(16,62500,50,0))
        self.assertEqual((four['cpu'],four['ram'],four['sys'],four['data']),(64,250000,50,0))
        self.assertEqual(self.core.rate_for_mode('gpu',4),1600)
        self.assertEqual(self.core.rate_for_mode('headless',4),8)
        self.assertEqual(self.core.pricing()['gpuPlans'][1]['systemDiskGiB'],50)
        for value in (0,2,3,5,True,4.0,'4',None):
            with self.assertRaises(ValueError): gpu_count(value)
        with self.assertRaises(ValueError): gpu_slots(2,4)
        with self.assertRaises(ValueError): self.core.order('alice',{'gpu_count':4,'cpu':16},'bad')

    def test_two_groups_and_full_occupancy(self):
        a,b=self.start('alice'),self.start('bobby')
        self.assertEqual(a['slots'],[1,2,3,4])
        self.assertEqual(b['slots'],[5,6,7,8])
        self.assertEqual(self.core.metrics()['active_slots'],8)
        self.assertEqual(len(self.core.active_slots()),8)
        self.assertEqual(self.core.metrics()['used']['memoryMB'],500000)
        with self.assertRaises(RuntimeError): self.start('carol',1)

    def test_fragmentation_requires_whole_group(self):
        owners=('alice','bobby','carol','david','erica')
        rows=[self.start(owner,1) for owner in owners]
        for owner,row in zip(owners[1:4],rows[1:4]):
            self.core.mark_failed(row['id'],'fixture stopped')
        self.assertEqual(self.core.metrics()['active_slots'],2)
        with self.assertRaisesRegex(RuntimeError,'四卡资源不足'): self.start('frank')
        self.assertEqual(self.core.metrics()['active_slots'],2)

    def test_atomic_concurrent_four_and_singles(self):
        rows=[(owner,self.order(owner,count)) for owner,count in [('alice',4),('bobby',4),('carol',1),('david',1),('erica',1)]]
        barrier=threading.Barrier(len(rows)); successes=[]; failures=[]
        def start(owner,row):
            barrier.wait()
            try: successes.append(self.core.action(owner,row['id'],'start'))
            except RuntimeError as exc: failures.append(str(exc))
        threads=[threading.Thread(target=start,args=pair) for pair in rows]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        held=[slot for row in successes for slot in row['slots']]
        self.assertEqual(len(held),len(set(held)))
        self.assertLessEqual(len(held),8)
        self.assertTrue(failures)

    def test_single_customer_limit_global_but_many_headless(self):
        first=self.start('alice')
        other=self.order('alice',1,node='G2-003')
        with self.assertRaisesRegex(RuntimeError,'one GPU'): self.core.action('alice',other['id'],'start')
        for key in ('headless-a','headless-b'):
            self.start('alice',4,mode='headless',key=key)
        self.assertEqual(self.core.metrics()['active_headless'],2)
        self.assertEqual(self.core.metrics()['active_slots'],4)

    def test_headless_switch_keeps_plan_and_changes_rate(self):
        row=self.start('alice'); ident=row['id']
        self.core.mark_running(ident)
        self.core.action('alice',ident,'stop'); self.core.mark_off(ident)
        headless=self.core.action('alice',ident,'start',mode='headless')
        self.assertEqual((headless['cpu'],headless['ram'],headless['gpu_count'],headless['slots']),(2,4000,4,[]))
        self.core.mark_running(ident)
        self.assertEqual(self.core.instance_costs('alice')[ident]['rateCentsPerHour'],8)
        self.core.action('alice',ident,'stop'); self.core.mark_off(ident)
        resumed=self.core.action('alice',ident,'start',mode='gpu')
        self.assertEqual((resumed['cpu'],resumed['ram'],len(resumed['slots'])),(64,250000,4))

    def test_compute_times_four_storage_unchanged_price_locked(self):
        row=self.start('alice',data=100); ident=row['id']; self.core.mark_running(ident)
        self.core.set_price('operator',500)
        self.assertEqual(self.core.instance_costs('alice')[ident]['rateCentsPerHour'],1600)
        self.assertEqual(self.core.rate_for_mode('gpu',4),2000)
        now=datetime.now(timezone.utc)
        with self.core._transaction() as con:
            con.execute('UPDATE usage_ledger SET opened_at=? WHERE instance_id=?',((now-timedelta(hours=1)).isoformat(),ident))
        self.core.bill_running(now)
        self.assertEqual(self.core.instance_costs('alice')[ident]['computeCents'],1600)
        one=self.order('bobby',1,data=100)
        with self.core._transaction() as con:
            stamp=(now-timedelta(days=1)).isoformat()
            con.execute('UPDATE instances SET storage_started_at=? WHERE id IN (?,?)',(stamp,ident,one['id']))
        self.core.bill_running(now)
        self.assertEqual(self.core.list_instances('alice')[0]['storage_charged_cents'],self.core.list_instances('bobby')[0]['storage_charged_cents'])

    def test_zero_balance_and_quarantine_hold_all_four(self):
        row=self.start('alice'); self.core.mark_running(row['id'])
        with self.core._transaction() as con:
            con.execute("UPDATE users SET balance_cents=0 WHERE name='alice'")
        self.core.bill_running()
        self.assertEqual(self.core.list_instances('alice')[0]['state'],'stopping')
        self.core.mark_error(row['id'],'one card not recovered')
        self.assertEqual(len(self.core.active_slots()),4)
        self.assertEqual(self.start('bobby')['slots'],[5,6,7,8])

    def test_poweroff_closes_billing_before_all_slots_release(self):
        row=self.start('alice'); ident=row['id']; self.core.mark_running(ident)
        self.core.action('alice',ident,'delete')
        self.assertTrue(self.core.observe_poweroff(ident,row['generation']))
        self.assertEqual(self.core.metrics()['open_usage_intervals'],0)
        self.assertEqual(self.core.list_instances('alice')[0]['desired_action'],'delete')
        self.assertEqual(self.core.metrics()['active_slots'],4)
        self.core.mark_off(ident)
        self.assertEqual(self.core.metrics()['active_slots'],0)

    def test_restart_and_idempotency(self):
        row=self.start('alice')
        with self.assertRaises(ValueError): self.order('alice',1,key='aliceG2-002gpu4')
        restarted=Core(self.temp.name,nodes={'G2-003':{}})
        self.assertEqual(restarted.list_instances('alice')[0]['slots'],row['slots'])
        self.assertEqual(len(restarted.active_slots()),4)
        self.assertEqual(self.start('bobby',1)['slot'],5)


class FourCardNodeTests(unittest.TestCase):
    def test_slow_four_card_boot_still_renews_lease(self):
        with tempfile.TemporaryDirectory() as root:
            backend=SimulationBackend()
            node=NodeAgent({'node_id':'G2-003','state_root':root,'data_root':root,'safety_gib':0},backend)
            item=dict(id='1',node_id='G2-003',generation=1,slot=1,endpoint=1,mode='gpu',vcpu=64,memory_mb=250000,data_disk=0,gpu_count=4)
            node.reserve(item)
            entered,finish=threading.Event(),threading.Event()
            errors=[]
            def slow_start(instance):
                entered.set(); finish.wait(5); backend.states[instance['id']]='running'
            def start():
                try: node.dispatch({'node_id':'G2-003','method':'start','instance':item})
                except Exception as exc: errors.append(exc)
            with patch.object(backend,'start',side_effect=slow_start):
                thread=threading.Thread(target=start); thread.start()
                try:
                    self.assertTrue(entered.wait(2))
                    node.records['1']['lease_until']=time.time()+5
                    status=node.heartbeat([item])
                    self.assertGreater(node.records['1']['lease_until'],time.time()+60)
                    self.assertEqual(status['instances']['1']['state'],'unknown')
                    self.assertFalse(node.records['1']['closed'])
                finally:
                    finish.set(); thread.join(5)
            self.assertFalse(errors)

    def test_agent_rejects_secondary_slot_collision_and_preserves_old_protocol(self):
        with tempfile.TemporaryDirectory() as root:
            config={'node_id':'G2-003','state_root':root}
            node=NodeAgent(config,SimulationBackend())
            item=dict(id='1',node_id='G2-003',generation=1,slot=1,endpoint=1,mode='gpu',vcpu=64,memory_mb=250000,data_disk=0,gpu_count=4)
            node.reserve(item)
            single={**item,'id':'2','slot':3,'endpoint':3,'vcpu':16,'memory_mb':62500}; single.pop('gpu_count')
            with self.assertRaisesRegex(RuntimeError,'already reserved'): node.reserve(single)
            single.update(slot=5,endpoint=5); node.reserve(single)
            restarted=NodeAgent(config,SimulationBackend())
            self.assertEqual(restarted.records['1']['instance']['gpu_count'],4)
            self.assertEqual(restarted.records['2']['instance']['gpu_count'],1)
            with self.assertRaises(ValueError): node.validate({**item,'slot':2,'endpoint':2})
            with self.assertRaises(ValueError): node.validate({**item,'gpu_count':True})
            with self.assertRaises(ValueError): node.validate({**item,'memory_mb':62500})

    def test_backend_four_unique_buses_and_checks_every_card(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            bdfs=[f'0000:{n:02x}:00.0' for n in range(16,24)]
            backend=LibvirtBackend(root,dict(data_root=data,bdfs=bdfs,network='qa'))
            item=dict(id='1',slot=5,gpu_count=4,vcpu=64,memory_mb=250000,data_disk=0)
            directory=backend.path(item); directory.mkdir()
            (directory/'system.qcow2').touch()
            with patch.object(backend,'state',return_value='off'), patch.object(backend,'preflight') as checks, patch.object(backend,'virsh',return_value='qa-uuid'), patch.object(backend,'four_card_numa',return_value=(1,'32-63,96-127')):
                backend.start(item)
                self.assertEqual([call.args[0] for call in checks.call_args_list],[5,6,7,8])
            xml=ET.parse(directory/'domain.xml').getroot()
            self.assertEqual(xml.findtext('memory'),'250000')
            self.assertEqual(xml.findtext('vcpu'),'64')
            self.assertEqual([h.find('address').get('bus') for h in xml.findall('./devices/hostdev')],['0x05','0x06','0x07','0x08'])
            self.assertEqual(len(xml.findall('./devices/controller[@model="pcie-root-port"]')),12)
            self.assertEqual(len(xml.findall('./devices/disk[@device="disk"]')),1)
            with patch.object(backend,'state',return_value='off'), patch.object(backend,'preflight',side_effect=[None,None,None,RuntimeError('not ready')]) as checks:
                self.assertFalse(backend.recovered(item)); self.assertEqual(checks.call_count,4)


if __name__ == '__main__': unittest.main()
