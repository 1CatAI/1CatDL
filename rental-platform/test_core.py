import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path

from core import Core


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Core(Path(self.tmp.name))
        self.core.ensure_admin("test-operator", "correct horse battery staple")
        for name in ("alice", "bob"):
            self.core.register(name, "correct horse battery staple", self.core.create_invite())
            self.core.recharge("test-operator", name, 2000, idempotency=f"setup-credit:{name}")

    def tearDown(self):
        self.tmp.cleanup()

    def test_auth_invite_and_hashed_session(self):
        token = self.core.login("alice", "correct horse battery staple")
        self.assertEqual(self.core.authenticate(token), "alice")
        con = sqlite3.connect(self.core.db_path)
        try:
            stored = con.execute("SELECT token_hash FROM sessions").fetchone()[0]
            self.assertNotEqual(stored, token.encode())
            self.assertNotIn(b"correct horse", con.execute("SELECT password_hash FROM users WHERE name='alice'").fetchone()[0])
        finally:
            con.close()

    def test_customer_and_admin_registration_start_at_zero(self):
        core = Core(Path(self.tmp.name) / "no-registration-credit", rate_cents_per_hour=400)
        core.register("customer", "correct horse battery staple")
        core.register("admin2", "correct horse battery staple", role="admin")
        self.assertEqual(core.profile("customer")["balanceCents"], 0)
        self.assertEqual(core.ledger("customer"), [])
        self.assertEqual(core.profile("admin2")["balanceCents"], 0)
        vm = core.order("customer", {"cpu": 16, "ram": 62500}, "zero-balance")
        with self.assertRaisesRegex(RuntimeError, "balance is zero"):
            core.action("customer", vm["id"], "start")

    def test_concurrency_eight_orders_yields_all_eight_slots(self):
        owners = []
        orders = []
        for n in range(8):
            owner = f"user{n}"
            self.core.register(owner, "correct horse battery staple")
            self.core.recharge("test-operator", owner, 2000, idempotency=f"concurrency-credit:{owner}")
            owners.append(owner)
            orders.append(self.core.order(owner, {"cpu": 16, "ram": 62500}, f"k{n}"))
        barrier = threading.Barrier(8)
        results = []
        lock = threading.Lock()

        def place(n):
            barrier.wait()
            try:
                result = self.core.action(owners[n], orders[n]["id"], "start")
            except RuntimeError:
                result = None
            with lock:
                results.append(result)

        threads = [threading.Thread(target=place, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        accepted = [item for item in results if item]
        self.assertEqual(len(accepted), 8)
        self.assertEqual({item["slot"] for item in accepted}, set(range(1, 9)))

    def test_tenant_isolation(self):
        vm = self.core.order("alice", {"cpu": 16, "ram": 62500}, "one")
        self.assertEqual(self.core.list_instances("bob"), [])
        with self.assertRaises(PermissionError):
            self.core.action("bob", vm["id"], "stop")

    def test_fixed_compute_spec_and_integer_data_disk(self):
        for spec in (
            {"cpu": 8, "ram": 62500},
            {"cpu": 16, "ram": 64000},
            {"cpu": 16, "ram": 62500, "data": 0.5},
            {"cpu": True, "ram": 62500},
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.core.order("alice", spec, repr(spec))

    def test_operator_memory_override_survives_headless_round_trip(self):
        vm = self.core.order("alice", {"cpu": 16, "ram": 62500}, "custom-memory")
        with self.assertRaises(PermissionError):
            self.core.set_gpu_memory_override("alice", vm["id"], 120000, expected_mb=62500)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.core.set_gpu_memory_override("test-operator", vm["id"], 120000, expected_mb=64000)
        resized = self.core.set_gpu_memory_override("test-operator", vm["id"], 120000, expected_mb=62500)
        self.assertEqual(resized["ram"], 120000)
        self.assertEqual(self.core.action("alice", vm["id"], "start")["ram"], 120000)
        self.assertEqual(self.core.metrics()["used"]["memoryMB"], 120000)
        with self.assertRaisesRegex(ValueError, "fully stopped"):
            self.core.set_gpu_memory_override("test-operator", vm["id"], 125000, expected_mb=120000)
        self.core.mark_running(vm["id"])
        self.core.action("alice", vm["id"], "stop")
        self.core.mark_off(vm["id"])
        self.assertEqual(self.core.action("alice", vm["id"], "start", mode="headless")["ram"], 4000)
        self.core.mark_running(vm["id"])
        self.core.action("alice", vm["id"], "stop")
        self.core.mark_off(vm["id"])
        self.assertEqual(self.core.action("alice", vm["id"], "start", mode="gpu")["ram"], 120000)
        reopened = Core(Path(self.tmp.name))
        self.assertEqual(reopened.list_instances("alice")[0]["ram"], 120000)
        self.assertEqual(reopened.rate_for_mode("gpu", 1), self.core.rate_for_mode("gpu", 1))

    def test_operator_memory_override_respects_host_budget(self):
        core = Core(Path(self.tmp.name) / "limited-override", memory_budget=170000)
        core.ensure_admin("test-operator", "correct horse battery staple")
        for owner in ("alice", "bob"):
            core.register(owner, "correct horse battery staple")
            core.recharge("test-operator", owner, 2000, idempotency=f"override-credit:{owner}")
        reserved = core.order("bob", {"cpu": 16, "ram": 62500}, "reserved")
        core.action("bob", reserved["id"], "start")
        target = core.order("alice", {"cpu": 16, "ram": 62500}, "target")
        with self.assertRaisesRegex(RuntimeError, "memory capacity"):
            core.set_gpu_memory_override("test-operator", target["id"], 120000, expected_mb=62500)
        self.assertEqual(core.list_instances("alice")[0]["ram"], 62500)

    def test_existing_database_adds_memory_override_column_without_changing_instances(self):
        vm = self.core.order("alice", {"cpu": 16, "ram": 62500}, "old-schema")
        con = sqlite3.connect(self.core.db_path)
        try:
            con.execute("ALTER TABLE instances DROP COLUMN gpu_memory_override_mb")
        finally:
            con.close()
        reopened = Core(Path(self.tmp.name))
        self.assertEqual(reopened.list_instances("alice")[0]["id"], vm["id"])
        self.assertEqual(reopened.list_instances("alice")[0]["ram"], 62500)
        con = sqlite3.connect(reopened.db_path)
        try:
            self.assertIn("gpu_memory_override_mb", {row[1] for row in con.execute("PRAGMA table_info(instances)")})
        finally:
            con.close()

    def test_memory_budget_blocks_eighth_fixed_instance_when_reserved(self):
        core = Core(Path(self.tmp.name) / "limited", memory_budget=7 * 62500)
        core.ensure_admin("test-operator", "correct horse battery staple")
        owners = []
        for n in range(8):
            owner = f"limited{n}"
            core.register(owner, "correct horse battery staple")
            core.recharge("test-operator", owner, 2000, idempotency=f"limited-credit:{owner}")
            owners.append(owner)
        for n, owner in enumerate(owners[:7]):
            vm = core.order(owner, {"cpu": 16, "ram": 62500}, str(n))
            core.action(owner, vm["id"], "start")
        with self.assertRaisesRegex(RuntimeError, "memory"):
            vm = core.order(owners[7], {"cpu": 16, "ram": 62500}, "eighth")
            core.action(owners[7], vm["id"], "start")

    def test_safe_default_caps_and_paid_orders(self):
        self.assertEqual(
            self.core.metrics()["capacity"],
            {"slots": 8, "cpu": 128, "memoryMB": 500000, "systemDiskGiB": 700, "dataDiskGiB": 3300},
        )
        vm = self.core.order("alice", {"cpu": 16, "ram": 62500, "free_test": False}, "paid")
        self.assertTrue(vm["free_test"])  # legacy field; billing is rate-based
        self.assertEqual(vm["state"], "stopped")

    def test_idempotency_is_per_owner(self):
        first = self.core.order("alice", {"cpu": 16, "ram": 62500}, "same")
        again = self.core.order("alice", {"cpu": 16, "ram": 62500}, "same")
        other = self.core.order("bob", {"cpu": 16, "ram": 62500}, "same")
        self.assertEqual(first["id"], again["id"])
        self.assertNotEqual(first["id"], other["id"])

    def test_stopped_releases_compute_and_restart_reacquires(self):
        vm = self.core.order("alice", {"cpu": 16, "ram": 62500, "data": 10}, "vm")
        self.core.action("alice", vm["id"], "start")
        self.core.mark_running(vm["id"])
        self.core.action("alice", vm["id"], "stop")
        self.assertEqual(self.core.metrics()["active_slots"], 1)
        stopped = self.core.mark_off(vm["id"])
        self.assertIsNone(stopped["slot"])
        self.assertEqual(self.core.metrics()["used"], {"cpu": 0, "memoryMB": 0, "systemDiskGiB": 50, "dataDiskGiB": 10})
        restarted = self.core.action("alice", vm["id"], "start")
        self.assertIsNotNone(restarted["slot"])
        self.assertEqual(self.core.metrics()["used"]["memoryMB"], 62500)

    def test_duplicate_close_is_idempotent(self):
        vm = self.core.order("alice", {"cpu": 16, "ram": 62500}, "ledger")
        self.core.action("alice", vm["id"], "start")
        self.core.mark_running(vm["id"])
        self.core.action("alice", vm["id"], "stop")
        self.core.mark_off(vm["id"])
        self.core.mark_off(vm["id"])
        con = sqlite3.connect(self.core.db_path)
        try:
            rows = con.execute(
                "SELECT opened_at, closed_at FROM usage_ledger WHERE instance_id=?", (vm["id"],)
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0][1])

    def test_quarantine_holds_slot_until_recovery(self):
        vm = self.core.order("alice", {"cpu": 16, "ram": 62500}, "q")
        self.core.action("alice", vm["id"], "start")
        self.core.mark_running(vm["id"])
        quarantined = self.core.mark_error(vm["id"], "health failed")
        self.assertEqual(quarantined["state"], "quarantined")
        self.assertIsNotNone(quarantined["slot"])
        self.assertEqual(self.core.mark_recovered(vm["id"])["state"], "running")
        self.assertEqual(self.core.metrics()["open_usage_intervals"], 1)

    def test_mark_off_clears_stale_shutdown_error(self):
        vm = self.core.order("alice", {"cpu": 16, "ram": 62500}, "shutdown-error")
        self.core.action("alice", vm["id"], "start")
        self.core.mark_error(vm["id"], "guest-agent timeout")
        stopped = self.core.mark_off(vm["id"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertIsNone(stopped["error"])

    def test_default_customer_can_start_multiple_gpu_instances(self):
        first = self.core.order("alice", {"cpu": 16, "ram": 62500}, "first")
        second = self.core.order("alice", {"cpu": 16, "ram": 62500}, "second")
        self.core.action("alice", first["id"], "start")
        self.core.action("alice", second["id"], "start")
        self.assertIsNone(self.core.profile("alice")["gpuInstanceLimit"])
        self.assertEqual(self.core.profile("alice")["gpuActiveCount"], 2)

    def test_admin_recharge_and_balance_metering(self):
        core = Core(Path(self.tmp.name) / "billing", rate_cents_per_hour=3600)
        core.register("admin", "admin-password", role="admin")
        core.register("customer", "customer-password")
        self.assertEqual(core.recharge("admin", "customer", 5000, idempotency="credit-1")["balanceCents"], 5000)
        self.assertEqual(core.recharge("admin", "customer", 5000, idempotency="credit-1")["balanceCents"], 5000)
        with self.assertRaises(ValueError):
            core.recharge("admin", "customer", 4000, idempotency="credit-1")
        vm = core.order("customer", {"cpu": 16, "ram": 62500}, "meter")
        core.action("customer", vm["id"], "start")
        core.mark_running(vm["id"])
        now = datetime.now(timezone.utc) + timedelta(seconds=1800)
        self.assertEqual(core.bill_running(now), [])
        self.assertEqual(core.profile("customer")["balanceCents"], 3200)

    def test_base_bundle_and_extra_storage_discount(self):
        core = Core(Path(self.tmp.name) / "bundle", rate_cents_per_hour=400)
        core.register("admin", "admin-password", role="admin")
        core.register("customer", "customer-password")
        core.recharge("admin", "customer", 10000)
        self.assertEqual(core.pricing()["includedCpu"], 16)
        self.assertEqual(core.pricing()["includedMemoryGB"], 62.5)
        self.assertEqual(core.pricing()["includedMemoryMB"], 62500)
        self.assertEqual(core.pricing()["freeDataDiskGiB"], 0)
        vm = core.order("customer", {"cpu": 16, "ram": 62500, "data": 100}, "storage")
        with self.assertRaises(ValueError):
            core.order("customer", {"cpu": 17, "ram": 62500}, "too-many-cpu")
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        con = sqlite3.connect(core.db_path)
        try:
            con.execute("UPDATE instances SET storage_started_at=? WHERE id=?", (old, vm["id"]))
            con.commit()
        finally:
            con.close()
        core.bill_running(datetime.now(timezone.utc))
        # All 100 GiB are billable: 100 * 0.0066 CNY * 70% = 0.462 CNY/day.
        self.assertEqual(core.profile("customer")["balanceCents"], 9954)
        self.assertEqual(core.ledger("customer")[0]["reason"], "storage:100GiB")

    def test_zero_balance_requests_stop_and_idle_reap(self):
        core = Core(Path(self.tmp.name) / "billing-zero", rate_cents_per_hour=3600)
        core.register("admin", "admin-password", role="admin")
        core.register("customer", "customer-password")
        con = sqlite3.connect(core.db_path)
        try:
            con.execute("UPDATE users SET balance_cents=0 WHERE name='customer'")
            con.commit()
        finally:
            con.close()
        core.recharge("admin", "customer", 1)
        vm = core.order("customer", {"cpu": 16, "ram": 62500}, "meter")
        core.action("customer", vm["id"], "start")
        core.mark_running(vm["id"])
        now = datetime.now(timezone.utc) + timedelta(seconds=2)
        self.assertEqual(core.bill_running(now), [vm["id"]])
        core.mark_off(vm["id"])
        self.assertEqual(core.reap_idle(0), [vm["id"]])

    def test_migrates_previous_production_schema(self):
        legacy = tempfile.TemporaryDirectory()
        try:
            db = sqlite3.connect(Path(legacy.name) / "core.sqlite3")
            db.executescript(
                """
                CREATE TABLE users(name TEXT PRIMARY KEY, salt BLOB NOT NULL, password_hash BLOB NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE invites(code_hash BLOB PRIMARY KEY, created_at TEXT NOT NULL, used_at TEXT, used_by TEXT);
                CREATE TABLE sessions(token_hash BLOB PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE instances(id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, idempotency TEXT NOT NULL, cpu INTEGER NOT NULL, ram INTEGER NOT NULL, sys_disk INTEGER NOT NULL, data_disk INTEGER NOT NULL, free_test INTEGER NOT NULL DEFAULT 1 CHECK(free_test=1), slot INTEGER, state TEXT NOT NULL, desired_action TEXT, opaque_secret BLOB, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE usage_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id INTEGER NOT NULL, owner TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT);
                CREATE TABLE billing_entries(id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, instance_id INTEGER, cents INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
                """
            )
            db.commit()
            db.close()
            migrated = Core(legacy.name)
            check = sqlite3.connect(migrated.db_path)
            try:
                users = {row[1] for row in check.execute("PRAGMA table_info(users)")}
                instances = {row[1] for row in check.execute("PRAGMA table_info(instances)")}
                usage = {row[1] for row in check.execute("PRAGMA table_info(usage_ledger)")}
                billing = {row[1] for row in check.execute("PRAGMA table_info(billing_entries)")}
                instance_schema = check.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='instances'"
                ).fetchone()[0]
                foreign_key_violations = check.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                check.close()
            self.assertTrue({"role", "balance_cents", "last_activity_at"}.issubset(users))
            self.assertIn("last_activity_at", instances)
            self.assertTrue({"rate_cents_per_hour", "charged_cents", "metered_cents", "last_billed_at"}.issubset(usage))
            self.assertTrue({"actor", "idempotency"}.issubset(billing))
            self.assertIn("BETWEEN 1 AND 8", instance_schema)
            self.assertEqual(foreign_key_violations, [])
        finally:
            legacy.cleanup()


if __name__ == "__main__":
    unittest.main()
