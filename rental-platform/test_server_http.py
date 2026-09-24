import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path


ROOT = tempfile.TemporaryDirectory(prefix="rental-http-")
STATIC = tempfile.TemporaryDirectory(prefix="rental-static-")
os.environ.update({
    "RENTAL_ROOT": ROOT.name,
    "RENTAL_STATIC_ROOT": STATIC.name,
    "RENTAL_SIMULATION": "1",
    "RENTAL_IMAGE_ENABLED": "1",
    "RENTAL_RATE_CENTS_PER_HOUR": "0",
})

import server  # noqa: E402


class ServerHttpTests(unittest.TestCase):
    def setUp(self):
        server.SERVICE = server.Service(start_background=False)
        self.httpd = server.ReusableServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.SERVICE.close()

    def wait_for(self, predicate):
        deadline = time.time() + 3
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for service state")

    def test_static_response_declares_exact_content_length(self):
        rental = Path(STATIC.name) / "rental"
        rental.mkdir(parents=True, exist_ok=True)
        body = b"<html><body>rental</body></html>"
        (rental / "index.html").write_bytes(body)

        with urllib.request.urlopen(self.base + "/rental", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Length"], str(len(body)))
            self.assertEqual(response.read(), body)

    def test_gift_disk_shows_total_but_charges_only_purchased_size(self):
        service = server.SERVICE
        owner = 'gift-disk-http'
        self.request('/api/auth/register', 'POST', {'name': owner, 'password': 'test gift disk password'})
        service.core.ensure_admin('gift-disk-admin', 'test gift disk admin password')
        service.core.recharge('gift-disk-admin', owner, 10000)
        row = service.core.order(owner, {'data': 200}, 'gift-http')
        service.core.action(owner, row['id'], 'start')
        service.core.mark_running(row['id'])
        service.core.record_storage_gift('gift-disk-admin', owner, row['id'], 400,
                                        expected_gift_gib=0, verified_total_gib=600)
        status, body = self.request('/api/rental/state')
        self.assertEqual(status, 200)
        item = next(r for r in body['instances'] if r['id'] == str(row['id']))
        self.assertEqual((item['dataDiskGiB'], item['billableDataDiskGiB'], item['giftDataDiskGiB']), (600, 200, 400))
        self.assertEqual(item['storageCnyPerDay'], 0.924)
        self.assertIn('赠送400GiB', item['message'])
        self.assertEqual(server.Service.backend_instance(service.core.list_instances(owner)[0])['data_disk'], 200)

    def test_customer_can_downgrade_eight_card_instance_without_losing_gift_disk(self):
        service = server.SERVICE
        owner = 'plan-switch-http'
        self.request('/api/auth/register', 'POST', {'name': owner, 'password': 'test plan switch password'})
        service.core.ensure_admin('plan-switch-admin', 'test plan switch admin password')
        service.core.recharge('plan-switch-admin', owner, 10001, idempotency='plan-switch-funding')
        # The HTTP fixture may run on a developer disk with < 600 GiB free;
        # seed a valid existing eight-card instance, then exercise the API.
        ident = service.core.order(owner, {'gpu_count': 8}, 'eight-to-four')['id']
        path = f'/api/rental/instances/{ident}/gpu-plan'
        self.assertEqual(self.request(path, 'POST', {'gpuCount': 8})[0], 200)
        self.assertEqual(self.request(path, 'POST', {'gpuCount': 4})[0], 200)
        self.assertEqual(self.request(path, 'POST', {'gpuCount': 8})[0], 409)
        self.assertEqual(self.request(path, 'POST', {'gpuCount': True})[0], 409)
        self.assertEqual(self.request(path, 'POST', {'gpuCount': 1, 'dataDiskGiB': 0})[0], 409)
        _, state = self.request('/api/rental/state')
        item = next(row for row in state['instances'] if row['id'] == str(ident))
        self.assertEqual((item['gpuCount'], item['vcpu'], item['memoryGB']), (4, 64, 250))
        self.assertEqual((item['dataDiskGiB'], item['billableDataDiskGiB'], item['giftDataDiskGiB']),
                         (600, 0, 600))
        self.assertTrue(item['eightCardGiftDisk'])
        self.assertEqual(item['rateCentsPerHour'], service.core.price() * 4)
        other = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request('/api/auth/register', 'POST', {'name': 'other-plan-http', 'password': 'test other password'}, client=other)
        self.assertEqual(self.request(path, 'POST', {'gpuCount': 1}, client=other)[0], 401)

    def test_four_gpu_public_api_full_lifecycle(self):
        service = server.SERVICE
        old_price = service.core.price()
        def restore_fixture_price():
            with service.core._transaction() as con:
                con.execute("UPDATE settings SET value=? WHERE key='rate_cents_per_hour'", (str(old_price),))
        self.addCleanup(restore_fixture_price)
        owner = 'four-plan-http'
        self.request('/api/auth/register','POST',{'name':owner,'password':'test four gpu password'})
        service.core.ensure_admin('four-plan-admin','test four gpu admin password')
        service.core.recharge('four-plan-admin',owner,10000,idempotency='four-http-funding')
        service.core.set_price('four-plan-admin',400)
        status,body=self.request('/api/rental/order','POST',{'name':'four-card-vm','gpuCount':4,'vcpu':64,'memoryGB':250})
        self.assertEqual(status,202)
        ident=body['instance']['id']
        self.assertEqual(service.core.list_instances(owner)[0]['gpu_count'],4)
        self.assertEqual(self.request('/api/rental/order','POST',{'gpuCount':4,'vcpu':16})[0],409)
        self.assertEqual(self.request('/api/rental/order','POST',{'gpuCount':True})[0],409)
        self.assertEqual(self.request(f'/api/rental/instances/{ident}/start','POST',{})[0],202)
        self.wait_for(lambda: service.core.list_instances(owner)[0]['state']=='running')
        status,body=self.request('/api/rental/state')
        row=next(x for x in body['instances'] if int(x['id'])==ident)
        self.assertEqual((row['gpuCount'],row['vcpu'],row['memoryGB'],row['rateCentsPerHour']),(4,64,250,1600))
        self.assertEqual(len(row['slots']),4)
        self.assertEqual(sum(x['instanceId']==str(ident) for x in body['slots']),4)
        self.assertEqual(body['billing']['gpuPlans'][1]['rateCentsPerHour'],1600)
        self.request(f'/api/rental/instances/{ident}/stop','POST',{})
        self.wait_for(lambda: service.core.list_instances(owner)[0]['state']=='stopped')
        self.request(f'/api/rental/instances/{ident}/start','POST',{'mode':'headless'})
        self.wait_for(lambda: service.core.list_instances(owner)[0]['state']=='running')
        _,body=self.request('/api/rental/state')
        row=next(x for x in body['instances'] if int(x['id'])==ident)
        self.assertEqual((row['gpuCount'],row['vcpu'],row['memoryGB'],row['rateCentsPerHour'],row['slots']),(4,2,4,8,[]))
        self.request(f'/api/rental/instances/{ident}/delete','POST',{})
        self.wait_for(lambda: not service.core.list_instances(owner))
        self.assertNotIn(str(ident),service.secrets)

    def request(self, path, method="GET", payload=None, client=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=data,
            headers=request_headers,
        )
        try:
            with (client or self.client).open(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_registration_cookie_state_and_order(self):
        status, body = self.request(
            "/api/auth/register",
            "POST",
            {"name": "alice", "password": "correct horse battery staple"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["account"]["name"], "alice")

        status, body = self.request("/api/rental/state?poll=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["account"]["name"], "alice")
        self.assertEqual(body["service"], "ready")

        status, body = self.request(
            "/api/rental/order",
            "POST",
            {"name": "dev", "vcpu": 16, "memoryGB": 62.5, "dataDiskGiB": 0},
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["instance"]["state"], "stopped")

        status, body = self.request("/api/rental/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["instances"]), 1)
        self.assertEqual(len(body["slots"]), 8)
        self.assertEqual(body["limits"]["memoryGB"], [62.5, 62.5])
        self.assertEqual(body["instances"][0]["memoryGB"], 62.5)
        self.assertEqual(body["instances"][0]["dataDiskGiB"], 0)

        status, body = self.request(
            "/api/rental/order",
            "POST",
            {"name": "variable", "vcpu": 8, "memoryGB": 62.5, "dataDiskGiB": 0},
        )
        self.assertEqual(status, 409)

        status, body = self.request(
            "/api/rental/order",
            "POST",
            {"name": "bad name", "vcpu": 16, "memoryGB": 62.5, "dataDiskGiB": 0},
        )
        self.assertEqual(status, 409)

    def test_admin_endpoints_and_query_strings(self):
        server.SERVICE.core.ensure_admin("rootadmin", "correct horse battery staple")
        server.SERVICE.core.register("customer1", "correct horse battery staple")
        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status, body = self.request(
            "/api/auth/login?from=admin",
            "POST",
            {"name": "rootadmin", "password": "correct horse battery staple"},
            client=admin,
        )
        self.assertEqual(status, 200)
        status, body = self.request("/api/admin/customers?refresh=1", client=admin)
        self.assertEqual(status, 200)
        self.assertEqual(body["customers"][0]["name"], "customer1")
        status, body = self.request(
            "/api/admin/price?save=1",
            "POST",
            {"cents": 500},
            client=admin,
        )
        self.assertEqual(status, 200)
        status, body = self.request(
            "/api/admin/recharge?save=1",
            "POST",
            {"user": "customer1", "cents": 1000},
            client=admin,
            headers={"X-Idempotency-Key": "http-credit-1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["balanceCents"], 1000)
        status, body = self.request("/api/admin/ledger?refresh=1", client=admin)
        self.assertEqual(status, 200)
        self.assertEqual(body["scopeOwner"], "")
        self.assertEqual(body["entries"][0]["owner"], "customer1")
        status, body = self.request("/api/admin/nodes", client=admin)
        self.assertEqual(status, 200)
        self.assertEqual(body['nodes'][0]['id'], 'G2-002')
        self.assertEqual(body['nodes'][0]['telemetry']['status'], 'partial')

    def test_admin_ledger_exact_owner_before_limit(self):
        core = server.SERVICE.core
        password = "ledger-test-only-password"
        core.ensure_admin("ledgeradmin", password)
        core.register("ledger-target", password)
        core.register("ledger-target-extra", password)
        core.recharge("ledgeradmin", "ledger-target", 123, idempotency="ledger-target-1")
        core.recharge("ledgeradmin", "ledger-target", 456, idempotency="ledger-target-2")
        # More than 200 newer entries for another, similarly named customer.
        for index in range(205):
            core.recharge("ledgeradmin", "ledger-target-extra", 1, idempotency=f"ledger-other-{index}")
        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status, _ = self.request(
            "/api/auth/login", "POST", {"name": "ledgeradmin", "password": password}, client=admin,
        )
        self.assertEqual(status, 200)

        status, body = self.request("/api/admin/ledger?owner=ledger-target", client=admin)
        self.assertEqual(status, 200)
        self.assertEqual(body["scopeOwner"], "ledger-target")
        self.assertEqual([row["owner"] for row in body["entries"]], ["ledger-target"] * 2)
        self.assertEqual([row["cents"] for row in body["entries"]], [456, 123])
        for suffix in ("", "?refresh=1", "?owner="):
            with self.subTest(default_query=suffix):
                status, body = self.request("/api/admin/ledger" + suffix, client=admin)
                self.assertEqual(status, 200)
                self.assertEqual(body["scopeOwner"], "")
                self.assertEqual(len(body["entries"]), 200)
                self.assertEqual({row["owner"] for row in body["entries"]}, {"ledger-target-extra"})
        for owner in ("ledger", "LEDGER-TARGET", "missing-ledger-customer"):
            with self.subTest(exact_owner=owner):
                status, body = self.request(f"/api/admin/ledger?owner={owner}", client=admin)
                self.assertEqual(status, 200)
                self.assertEqual(body["scopeOwner"], owner)
                self.assertEqual(body["entries"], [])
        status, body = self.request("/api/rental/ledger?owner=ledger-target", client=admin)
        self.assertEqual(status, 200)
        self.assertNotIn("scopeOwner", body)
        self.assertEqual(body["entries"], [])  # Customer endpoint still scopes to the logged-in admin.

    def test_ledger_owner_query_cannot_bypass_identity_or_admin_role(self):
        core = server.SERVICE.core
        password = "ledger-test-only-password"
        core.ensure_admin("ledger-guard-admin", password)
        core.register("ledger-guard-user", password)
        core.register("ledger-guard-other", password)
        core.recharge("ledger-guard-admin", "ledger-guard-user", 111, idempotency="ledger-guard-own")
        core.recharge("ledger-guard-admin", "ledger-guard-other", 222, idempotency="ledger-guard-other")
        for suffix in ("", "?owner=ledger-guard-other"):
            with self.subTest(anonymous_query=suffix):
                status, body = self.request("/api/admin/ledger" + suffix)
                self.assertEqual(status, 401)
                self.assertEqual(body["error"], "unauthorized")
                self.assertNotIn("entries", body)
                self.assertNotIn("scopeOwner", body)
        status, _ = self.request(
            "/api/auth/login", "POST", {"name": "ledger-guard-user", "password": password},
        )
        self.assertEqual(status, 200)
        for suffix in ("", "?owner=", "?owner=ledger-guard-user", "?owner=ledger-guard-other", "?owner=ledger-guard-admin"):
            with self.subTest(customer_admin_query=suffix):
                status, body = self.request("/api/admin/ledger" + suffix)
                self.assertEqual(status, 401)
                self.assertEqual(body["error"], "unauthorized")
                self.assertNotIn("entries", body)
                self.assertNotIn("scopeOwner", body)
        for suffix in ("", "?owner=ledger-guard-other", "?owner=ledger-guard-other&all_customers=1"):
            with self.subTest(customer_query=suffix):
                status, body = self.request("/api/rental/ledger" + suffix)
                self.assertEqual(status, 200)
                self.assertNotIn("scopeOwner", body)
                self.assertEqual([row["owner"] for row in body["entries"]], ["ledger-guard-user"])
                self.assertEqual([row["cents"] for row in body["entries"]], [111])

    def test_state_requires_login(self):
        status, body = self.request("/api/rental/state", client=urllib.request.build_opener())
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_headless_api_multi_instance_restore_and_mode_switch(self):
        service = server.SERVICE
        service.core.ensure_admin('headlessadmin', 'test-only-password')
        status, _ = self.request('/api/auth/register', 'POST', {'name': 'headlessuser', 'password': 'test-only-password'})
        self.assertEqual(status, 201)
        service.core.recharge('headlessadmin', 'headlessuser', 100, idempotency='headless-credit')
        ids = []
        for name in ('headless-a', 'headless-b'):
            status, body = self.request('/api/rental/order', 'POST', {'name': name, 'mode': 'headless', 'vcpu': 2, 'memoryGB': 4})
            self.assertEqual(status, 202)
            ids.append(body['instance']['id'])
            status, _ = self.request(f'/api/rental/instances/{ids[-1]}/start', 'POST', {'mode': 'headless'})
            self.assertEqual(status, 202)  # GPU price is intentionally zero in this test.
        self.wait_for(lambda: all(r['state'] == 'running' for r in service.core.list_instances('headlessuser')))
        rows = service.core.list_instances('headlessuser')
        self.assertEqual(len({r['endpoint'] for r in rows}), 2)
        for row in rows:
            self.assertIsNone(row['slot'])
            self.assertEqual(service.forwarders[row['endpoint']].target, ('127.0.0.1', 22))
            service.disconnect(row)
        service.refresh_workers()
        self.wait_for(lambda: set(ids).issubset(service.forwarded))
        status, state = self.request('/api/rental/state')
        self.assertEqual(status, 200)
        self.assertEqual([(i['vcpu'], i['memoryGB'], i['rateCentsPerHour'], i['mode']) for i in state['instances']], [(2, 4, 8, 'headless')] * 2)
        self.assertTrue(all(s['state'] == 'available' for s in state['slots']))
        self.assertEqual(state['headless']['running'], 2)
        first = ids[0]
        for ident in ids:
            self.request(f'/api/rental/instances/{ident}/stop', 'POST', {})
        self.wait_for(lambda: all(r['state'] == 'stopped' for r in service.core.list_instances('headlessuser')))
        self.assertTrue(all(service.forwarders[r['endpoint']].target is None for r in rows))
        service.core.set_price('headlessadmin', 400)
        status, _ = self.request(f'/api/rental/instances/{first}/start', 'POST', {'mode': 'gpu'})
        self.assertEqual(status, 202)
        self.wait_for(lambda: service.core.list_instances('headlessuser')[0]['state'] == 'running')
        self.assertEqual(service.core.list_instances('headlessuser')[0]['cpu'], 16)
        for ident in ids:
            self.request(f'/api/rental/instances/{ident}/delete', 'POST', {})
        self.wait_for(lambda: not service.core.list_instances('headlessuser'))

    def test_headless_boot_failure_clears_endpoint_and_compute(self):
        from unittest.mock import patch
        service = server.SERVICE
        service.core.ensure_admin('hfailadmin', 'test-only-password')
        service.core.register('hfailuser', 'test-only-password')
        service.core.recharge('hfailadmin', 'hfailuser', 100, idempotency='hfail-credit')
        row = service.order('hfailuser', {'name': 'headless-failure', 'mode': 'headless'}, 'hfail')
        started = service.core.action('hfailuser', row['id'], 'start')
        with patch.object(service.backend, 'healthy', side_effect=RuntimeError('injected headless failure')):
            service.worker(row['id'])
        result = service.core.list_instances('hfailuser')[0]
        self.assertEqual(result['state'], 'error')
        self.assertIsNone(result['endpoint'])
        self.assertIsNone(service.forwarders[started['endpoint']].target)
        self.assertEqual(service.core.instance_costs('hfailuser'), {})

    def test_simulation_worker_start_stop_delete_and_secret_cleanup(self):
        service = server.SERVICE
        service.core.ensure_admin("rootadmin", "correct horse battery staple")
        service.core.set_price("rootadmin", 500)
        service.core.register("workeruser", "correct horse battery staple")
        service.core.recharge("rootadmin", "workeruser", 1000, idempotency="credit-1")
        ordered = service.order("workeruser", {"name": "qa-vm", "vcpu": 16, "memoryGB": 62.5, "dataDiskGiB": 0}, "worker-1")
        instance_id = ordered["id"]
        self.assertEqual(service.core.action("workeruser", instance_id, "start")["state"], "provisioning")
        service.refresh_workers()
        self.wait_for(lambda: service.core.list_instances("workeruser")[0]["state"] == "running")
        self.assertEqual(service.state("workeruser")["slots"][0]["state"], "running")
        service.core.action("workeruser", instance_id, "stop")
        service.refresh_workers()
        self.wait_for(lambda: service.core.list_instances("workeruser")[0]["state"] == "stopped")
        service.core.action("workeruser", instance_id, "delete")
        service.refresh_workers()
        self.wait_for(lambda: service.core.list_instances("workeruser") == [])
        self.assertNotIn(str(instance_id), service.secrets)
        self.assertNotIn(str(instance_id), service.meta)

    def test_running_instance_rebinds_forwarder_after_service_restart(self):
        service = server.SERVICE
        service.core.ensure_admin("rootadmin", "correct horse battery staple")
        service.core.set_price("rootadmin", 500)
        service.core.register("restartuser", "correct horse battery staple")
        service.core.recharge("rootadmin", "restartuser", 1000, idempotency="restart-credit")
        ordered = service.order("restartuser", {"name": "restart-vm", "vcpu": 16, "memoryGB": 62.5, "dataDiskGiB": 0}, "restart-1")
        instance_id = ordered["id"]
        service.core.action("restartuser", instance_id, "start")
        service.refresh_workers()
        self.wait_for(lambda: service.core.list_instances("restartuser")[0]["state"] == "running")
        self.assertEqual(service.forwarders[1].target, ("127.0.0.1", 22))

        service.forwarders[1].set_target(None)
        service.forwarded.clear()
        service.refresh_workers()
        self.wait_for(lambda: service.forwarders[1].target == ("127.0.0.1", 22))

        service.core.action("restartuser", instance_id, "stop")
        service.refresh_workers()
        self.wait_for(lambda: service.core.list_instances("restartuser")[0]["state"] == "stopped")

    def test_missing_vm_after_restart_is_stopped_without_continuing_billing(self):
        service = server.SERVICE
        service.core.ensure_admin("rootadmin", "correct horse battery staple")
        service.core.set_price("rootadmin", 500)
        service.core.register("missinguser", "correct horse battery staple")
        service.core.recharge("rootadmin", "missinguser", 1000, idempotency="missing-credit")
        ordered = service.order("missinguser", {"name": "missing-vm", "vcpu": 16, "memoryGB": 62.5, "dataDiskGiB": 0}, "missing-1")
        instance_id = ordered["id"]
        service.core.action("missinguser", instance_id, "start")
        service.refresh_workers()
        self.wait_for(lambda: service.core.list_instances("missinguser")[0]["state"] == "running")
        service.backend.states.pop(str(instance_id), None)
        service.forwarded.clear()
        service.refresh_workers()
        self.wait_for(lambda: service.core.list_instances("missinguser")[0]["state"] == "stopped")
        self.assertEqual(service.core.profile("missinguser")["balanceCents"], 1000)

    def test_stop_or_delete_during_start_is_reconciled(self):
        service = server.SERVICE
        service.core.ensure_admin("rootadmin", "correct horse battery staple")
        service.core.set_price("rootadmin", 500)
        for owner, verb in (("cancelstop", "stop"), ("canceldelete", "delete")):
            service.core.register(owner, "correct horse battery staple")
            service.core.recharge("rootadmin", owner, 1000, idempotency=f"{owner}-credit")
            ordered = service.order(owner, {"name": f"{owner}-vm", "vcpu": 16, "memoryGB": 62.5, "dataDiskGiB": 0}, f"{owner}-order")
            fired = False
            original_healthy = service.backend.healthy

            def healthy(instance):
                nonlocal fired
                if not fired:
                    fired = True
                    service.core.action(owner, ordered["id"], verb)
                return original_healthy(instance)

            service.backend.healthy = healthy
            try:
                service.core.action(owner, ordered["id"], "start")
                service.refresh_workers()
                if verb == "stop":
                    self.wait_for(lambda: service.core.list_instances(owner)[0]["state"] == "stopped")
                    self.assertEqual(service.backend.state({"id": str(ordered["id"]) }), "off")
                else:
                    self.wait_for(lambda: service.core.list_instances(owner) == [])
                    self.assertEqual(service.backend.state({"id": str(ordered["id"]) }), "off")
            finally:
                service.backend.healthy = original_healthy

    def test_start_failure_stops_guest_and_releases_slot(self):
        service = server.SERVICE
        service.core.ensure_admin("rootadmin", "correct horse battery staple")
        service.core.set_price("rootadmin", 500)
        service.core.register("failedstart", "correct horse battery staple")
        service.core.recharge("rootadmin", "failedstart", 1000, idempotency="failedstart-credit")
        ordered = service.order(
            "failedstart",
            {"name": "failed-start-vm", "vcpu": 16, "memoryGB": 62.5, "dataDiskGiB": 0},
            "failedstart-order",
        )
        original_start = service.backend.start

        def start_then_fail(instance):
            service.backend.states[instance["id"]] = "running"
            raise RuntimeError("simulated post-start failure")

        service.backend.start = start_then_fail
        try:
            service.core.action("failedstart", ordered["id"], "start")
            service.refresh_workers()
            self.wait_for(lambda: service.core.list_instances("failedstart")[0]["state"] == "error")
            self.assertEqual(service.backend.state({"id": str(ordered["id"])}), "off")
            self.assertEqual(service.core.active_slots(), [])
            self.assertEqual(service.core.metrics()["open_usage_intervals"], 0)
        finally:
            service.backend.start = original_start


if __name__ == "__main__":
    unittest.main()
