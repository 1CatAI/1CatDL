"""Transactional, tenant-aware scheduling state for single-Gaudi2 VMs.

This module deliberately performs no host or hypervisor operations.  A worker
consumes ``pending()`` and reports outcomes through the ``mark_*`` methods.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from recharge_codes import RechargeCodeMixin


ACTIVE_STATES = ("provisioning", "running", "stopping", "deleting", "quarantined")
IDLE_RELEASE_SECONDS = 48 * 60 * 60
MAX_SLOTS = 8
FIXED_VCPU = 16
# The product specification is decimal GB. Keep the scheduler and libvirt
# boundary in integer MB so 62.5 GB is represented exactly, without a float or
# an accidental 62.5 GiB allocation.
FIXED_MEMORY_MB = 62_500
FIXED_MEMORY_GB = 62.5
HEADLESS_VCPU = 2
HEADLESS_MEMORY_MB = 4_000
HEADLESS_RATE_CENTS = 8
MAX_REGISTRATION_BONUS_CENTS = 1_000_000
COMPUTE_UNITS_PER_CENT = 3_600_000_000
SYSTEM_DISK_GIB = 50
FREE_DATA_DISK_GIB = 0
MAX_DATA_DISK_GIB = 200
# AutoDL reference: 0.0066 CNY/GB/day. This service applies the user's 70%
# policy: 0.00462 CNY/GB/day = 0.462 cents/GB/day. Keep four decimal places
# of a cent so small disks accumulate accurately before charging whole cents.
EXTRA_DATA_DISK_MICROCENTS_PER_GIB_DAY = 4620
MICROCENTS_PER_CENT = 10000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Core(RechargeCodeMixin):
    """SQLite-backed scheduling core with atomic capacity allocation."""

    def __init__(
        self,
        root: str | Path,
        memory_budget: int = 500_000,
        cpu_budget: int = 128,
        system_disk_budget: int = 700,
        data_disk_budget: int = 3_300,
        slot_count: int = MAX_SLOTS,
        rate_cents_per_hour: int = 0,
        storage_pool_budget: int = 0,
        headless_count: int = 16,
    ) -> None:
        for name, value in (
            ("memory_budget", memory_budget),
            ("cpu_budget", cpu_budget),
            ("system_disk_budget", system_disk_budget),
            ("data_disk_budget", data_disk_budget),
            ("slot_count", slot_count),
            ("rate_cents_per_hour", rate_cents_per_hour),
            ("storage_pool_budget", storage_pool_budget),
            ("headless_count", headless_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not 1 <= slot_count <= MAX_SLOTS:
            raise ValueError(f"slot_count must be from 1 to {MAX_SLOTS}")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "core.sqlite3"
        self.memory_budget = memory_budget
        self.cpu_budget = cpu_budget
        self.system_disk_budget = system_disk_budget
        self.data_disk_budget = data_disk_budget
        self.slot_count = slot_count
        self.rate_cents_per_hour = rate_cents_per_hour
        self.storage_pool_budget = storage_pool_budget
        if headless_count > 64:
            raise ValueError("headless_count must not exceed 64")
        self.headless_count = headless_count
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 30000")
        return con

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _initialize(self) -> None:
        with self._transaction() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    name TEXT PRIMARY KEY,
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'customer',
                    balance_cents INTEGER NOT NULL DEFAULT 0,
                    last_activity_at TEXT
                );
                CREATE TABLE IF NOT EXISTS invites (
                    code_hash BLOB PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    used_at TEXT,
                    used_by TEXT REFERENCES users(name)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash BLOB PRIMARY KEY,
                    owner TEXT NOT NULL REFERENCES users(name),
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL REFERENCES users(name),
                    idempotency TEXT NOT NULL,
                    cpu INTEGER NOT NULL,
                    ram INTEGER NOT NULL,
                    sys_disk INTEGER NOT NULL,
                    data_disk INTEGER NOT NULL,
                    free_test INTEGER NOT NULL DEFAULT 1 CHECK(free_test = 1),
                    slot INTEGER,
                    state TEXT NOT NULL,
                    desired_action TEXT,
                    opaque_secret BLOB,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_activity_at TEXT,
                    storage_started_at TEXT,
                    storage_metered_microcents INTEGER NOT NULL DEFAULT 0,
                    storage_charged_cents INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(owner, idempotency),
                    CHECK(slot IS NULL OR slot BETWEEN 1 AND 8)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_instance_per_slot
                    ON instances(slot) WHERE slot IS NOT NULL;
                CREATE TABLE IF NOT EXISTS usage_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id INTEGER NOT NULL REFERENCES instances(id),
                    owner TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    rate_cents_per_hour INTEGER NOT NULL DEFAULT 0,
                    charged_cents INTEGER NOT NULL DEFAULT 0,
                    metered_cents INTEGER NOT NULL DEFAULT 0,
                    last_billed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_usage_interval
                    ON usage_ledger(instance_id) WHERE closed_at IS NULL;
                CREATE TABLE IF NOT EXISTS billing_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL REFERENCES users(name),
                    instance_id INTEGER REFERENCES instances(id),
                    cents INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL, event TEXT NOT NULL, target TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(con, "users", "role", "TEXT NOT NULL DEFAULT 'customer'")
            self._ensure_column(con, "users", "balance_cents", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(con, "users", "last_activity_at", "TEXT")
            self._ensure_column(con, "instances", "last_activity_at", "TEXT")
            self._ensure_column(con, "instances", "storage_started_at", "TEXT")
            self._ensure_column(con, "instances", "storage_metered_microcents", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(con, "instances", "storage_charged_cents", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(con, "usage_ledger", "rate_cents_per_hour", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(con, "usage_ledger", "charged_cents", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(con, "usage_ledger", "last_billed_at", "TEXT")
            self._ensure_column(con, "usage_ledger", "metered_cents", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(con, "billing_entries", "actor", "TEXT")
            self._ensure_column(con, "billing_entries", "idempotency", "TEXT")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS credit_once ON billing_entries(actor, idempotency) WHERE idempotency IS NOT NULL")
            con.execute("UPDATE instances SET last_activity_at=COALESCE(last_activity_at, updated_at, created_at)")
            con.execute("UPDATE instances SET storage_started_at=COALESCE(storage_started_at, created_at)")
            self._initialize_recharge_codes(con)
        self._migrate_slot_constraint()
        # Add after the legacy table rebuild, which only knows the old columns.
        with self._transaction() as con:
            self._ensure_column(con, "instances", "mode", "TEXT NOT NULL DEFAULT 'gpu'")
            self._ensure_column(con, "instances", "endpoint", "INTEGER")
            self._ensure_column(con, "instances", "compute_remainder", "INTEGER NOT NULL DEFAULT 0")
            new_units_column = "metered_units" not in {r[1] for r in con.execute("PRAGMA table_info(usage_ledger)")}
            self._ensure_column(con, "usage_ledger", "metered_units", "INTEGER NOT NULL DEFAULT 0")
            con.execute("UPDATE instances SET endpoint=slot WHERE slot IS NOT NULL AND endpoint IS NULL")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_endpoint ON instances(endpoint) WHERE endpoint IS NOT NULL")
            if new_units_column:
                con.execute("UPDATE usage_ledger SET metered_units=MAX(metered_cents,charged_cents)*?", (COMPUTE_UNITS_PER_CENT,))

    def _migrate_slot_constraint(self) -> None:
        """Rebuild the legacy 4-slot table without rewinding business data."""
        con = self._connect()
        try:
            schema_row = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='instances'"
            ).fetchone()
            schema = "" if schema_row is None else str(schema_row[0] or "")
            normalized = re.sub(r"\s+", " ", schema.upper())
            if "BETWEEN 1 AND 8" in normalized:
                return
            con.execute("PRAGMA foreign_keys = OFF")
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DROP TABLE IF EXISTS instances_v8_migration")
                con.execute(
                    """
                    CREATE TABLE instances_v8_migration (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        owner TEXT NOT NULL REFERENCES users(name),
                        idempotency TEXT NOT NULL,
                        cpu INTEGER NOT NULL,
                        ram INTEGER NOT NULL,
                        sys_disk INTEGER NOT NULL,
                        data_disk INTEGER NOT NULL,
                        free_test INTEGER NOT NULL DEFAULT 1 CHECK(free_test = 1),
                        slot INTEGER,
                        state TEXT NOT NULL,
                        desired_action TEXT,
                        opaque_secret BLOB,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_activity_at TEXT,
                        storage_started_at TEXT,
                        storage_metered_microcents INTEGER NOT NULL DEFAULT 0,
                        storage_charged_cents INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(owner, idempotency),
                        CHECK(slot IS NULL OR slot BETWEEN 1 AND 8)
                    )
                    """
                )
                columns = (
                    "id, owner, idempotency, cpu, ram, sys_disk, data_disk, free_test, "
                    "slot, state, desired_action, opaque_secret, error, created_at, updated_at, "
                    "last_activity_at, storage_started_at, storage_metered_microcents, "
                    "storage_charged_cents"
                )
                con.execute(
                    f"INSERT INTO instances_v8_migration ({columns}) SELECT {columns} FROM instances"
                )
                con.execute("DROP TABLE instances")
                con.execute("ALTER TABLE instances_v8_migration RENAME TO instances")
                con.execute(
                    "CREATE UNIQUE INDEX one_active_instance_per_slot "
                    "ON instances(slot) WHERE slot IS NOT NULL"
                )
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.execute("PRAGMA foreign_keys = ON")
            violations = con.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("foreign-key validation failed after slot migration")
        finally:
            con.close()

    @staticmethod
    def _ensure_column(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _hash_secret(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    @staticmethod
    def _password(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )

    @staticmethod
    def _owner(owner: str) -> str:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be a non-empty string")
        return owner.strip()

    def create_invite(self) -> str:
        code = secrets.token_urlsafe(24)
        with self._transaction() as con:
            con.execute(
                "INSERT INTO invites(code_hash, created_at) VALUES (?, ?)",
                (self._hash_secret(code), _now()),
            )
        return code

    def register(self, name: str, password: str, invite: str | None = None, role: str = "customer") -> str:
        name = self._owner(name)
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,31}", name):
            raise ValueError("账户名须为 3–32 位字母、数字、下划线或横线")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ValueError("密码须为 8–128 位")
        if role not in ("customer", "admin"):
            raise ValueError("invalid role")
        salt = secrets.token_bytes(16)
        password_hash = self._password(password, salt)
        now = _now()
        with self._transaction() as con:
            if invite is not None:
                row = con.execute(
                    "SELECT used_at FROM invites WHERE code_hash = ?",
                    (self._hash_secret(invite),),
                ).fetchone()
                if row is None or row["used_at"] is not None:
                    raise PermissionError("invalid or already-used invite")
            try:
                con.execute(
                    "INSERT INTO users(name, salt, password_hash, created_at, role, last_activity_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, salt, password_hash, now, role, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("user already exists") from exc
            # Registration, gift credit and ledger entry commit together. Read
            # the policy under the same write lock as account creation so a
            # concurrent policy change cannot produce a partial/duplicate gift.
            bonus = self._registration_bonus(con) if role == "customer" else 0
            if bonus:
                con.execute("UPDATE users SET balance_cents=balance_cents+? WHERE name=?", (bonus, name))
                con.execute(
                    "INSERT INTO billing_entries(owner,cents,reason,created_at,actor,idempotency) VALUES (?,?,?,?,?,?)",
                    (name, bonus, "registration_bonus", now, "system:registration", f"registration_bonus:{name}"),
                )
                self._audit(con, "system:registration", "registration_bonus_granted", name, {"cents": bonus})
            if invite is not None:
                con.execute(
                    "UPDATE invites SET used_at = ?, used_by = ? WHERE code_hash = ?",
                    (now, name, self._hash_secret(invite)),
                )
        return name

    def login(self, name: str, password: str) -> str:
        name = self._owner(name)
        if not isinstance(password, str) or len(password) > 128:
            raise PermissionError("invalid credentials")
        with self._connection() as con:
            row = con.execute(
                "SELECT salt, password_hash FROM users WHERE name = ?", (name,)
            ).fetchone()
        if row is None or not hmac.compare_digest(
            self._password(password, row["salt"]), row["password_hash"]
        ):
            raise PermissionError("invalid credentials")
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        with self._transaction() as con:
            con.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(),)
            )
            con.execute(
                "INSERT INTO sessions(token_hash, owner, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (
                    self._hash_secret(token),
                    name,
                    (now + timedelta(hours=24)).isoformat(),
                    now.isoformat(),
                ),
            )
            con.execute("UPDATE users SET last_activity_at=? WHERE name=?", (now.isoformat(), name))
        return token

    def authenticate(self, token: str) -> str:
        if not isinstance(token, str) or not token:
            raise PermissionError("invalid or expired session")
        with self._connection() as con:
            row = con.execute(
                "SELECT owner, expires_at FROM sessions WHERE token_hash = ?",
                (self._hash_secret(token),),
            ).fetchone()
            if row is None or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
                raise PermissionError("invalid or expired session")
        return str(row["owner"])

    def logout(self, token: str) -> None:
        with self._transaction() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (self._hash_secret(token),))

    def profile(self, owner: str) -> dict[str, Any]:
        owner = self._owner(owner)
        with self._connection() as con:
            row = con.execute(
                "SELECT name, role, balance_cents, created_at, last_activity_at FROM users WHERE name=?",
                (owner,),
            ).fetchone()
        if row is None:
            raise PermissionError("unknown user")
        return {
            "name": row["name"],
            "role": row["role"],
            "balanceCents": row["balance_cents"],
            "createdAt": row["created_at"],
            "lastActivityAt": row["last_activity_at"],
        }

    def is_admin(self, owner: str) -> bool:
        return self.profile(owner)["role"] == "admin"

    def recharge(self, admin: str, customer: str, cents: int, note: str = "", idempotency: str | None = None) -> dict[str, Any]:
        if not self.is_admin(admin):
            raise PermissionError("admin required")
        customer = self._owner(customer)
        if isinstance(cents, bool) or not isinstance(cents, int) or not 0 < cents <= 100000000:
            raise ValueError("cents must be a positive integer")
        if idempotency is not None and (not isinstance(idempotency, str) or not 1 <= len(idempotency) <= 128):
            raise ValueError("invalid idempotency key")
        with self._transaction() as con:
            row = con.execute(
                "SELECT balance_cents FROM users WHERE name=? AND role='customer'", (customer,)
            ).fetchone()
            if row is None:
                raise KeyError("customer not found")
            previous = con.execute("SELECT owner,cents FROM billing_entries WHERE actor=? AND idempotency=?", (admin, idempotency)).fetchone() if idempotency else None
            if previous:
                if previous["owner"] != customer or previous["cents"] != cents:
                    raise ValueError("idempotency key already used for a different recharge")
                return {"user": customer, "balanceCents": row["balance_cents"]}
            balance = row["balance_cents"] + cents
            con.execute("UPDATE users SET balance_cents=? WHERE name=?", (balance, customer))
            con.execute(
                "INSERT INTO billing_entries(owner, cents, reason, created_at, actor, idempotency) VALUES (?, ?, ?, ?, ?, ?)",
                (customer, cents, "recharge:" + note[:120], _now(), admin, idempotency),
            )
        return {"user": customer, "balanceCents": balance}

    def customers(self, admin: str) -> list[dict[str, Any]]:
        if not self.is_admin(admin):
            raise PermissionError("admin required")
        with self._connection() as con:
            return [dict(row) for row in con.execute("SELECT name, balance_cents AS balanceCents, created_at AS createdAt FROM users WHERE role='customer' ORDER BY created_at DESC LIMIT 500")]

    def price(self) -> int:
        with self._connection() as con:
            row = con.execute("SELECT value FROM settings WHERE key='rate_cents_per_hour'").fetchone()
            return int(row[0]) if row else self.rate_cents_per_hour

    @staticmethod
    def _registration_bonus(con) -> int:
        row = con.execute("SELECT value FROM settings WHERE key='registration_bonus_cents'").fetchone()
        cents = int(row[0]) if row else 0  # Preserve the existing no-gift default.
        if not 0 <= cents <= MAX_REGISTRATION_BONUS_CENTS:
            raise ValueError("invalid registration bonus setting")
        return cents

    def registration_bonus(self) -> int:
        with self._connection() as con:
            return self._registration_bonus(con)

    def registration_settings(self, admin: str) -> dict[str, int]:
        if not self.is_admin(admin):
            raise PermissionError("admin required")
        return {"bonusCents": self.registration_bonus(), "maxBonusCents": MAX_REGISTRATION_BONUS_CENTS}

    def set_registration_bonus(self, admin: str, cents: int, expected_cents: int) -> dict[str, int]:
        if not self.is_admin(admin):
            raise PermissionError("admin required")
        if type(cents) is not int or not 0 <= cents <= MAX_REGISTRATION_BONUS_CENTS:
            raise ValueError("注册赠金须为 0–10000 元，精确到分；0 表示关闭")
        if type(expected_cents) is not int or not 0 <= expected_cents <= MAX_REGISTRATION_BONUS_CENTS:
            raise ValueError("请先读取当前赠金设置后再保存")
        with self._transaction() as con:
            previous = self._registration_bonus(con)
            if previous != expected_cents:
                # A retry of a successfully applied request is harmless.
                if previous == cents:
                    return {"bonusCents": previous, "maxBonusCents": MAX_REGISTRATION_BONUS_CENTS}
                raise RuntimeError("赠金设置已被其他管理员修改，请重新加载后再保存")
            if previous != cents:
                con.execute(
                    "INSERT INTO settings(key,value) VALUES ('registration_bonus_cents',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(cents),),
                )
                self._audit(con, admin, "registration_bonus_changed", "new_customer", {
                    "oldCents": previous, "newCents": cents, "effective": "future_registrations",
                })
        return {"bonusCents": cents, "maxBonusCents": MAX_REGISTRATION_BONUS_CENTS}

    def set_price(self, admin: str, cents: int) -> None:
        if not self.is_admin(admin):
            raise PermissionError("admin required")
        if isinstance(cents, bool) or not isinstance(cents, int) or not 1 <= cents <= 1000000:
            raise ValueError("小时单价须为 1–1000000 分的整数")
        with self._transaction() as con:
            old = con.execute("SELECT value FROM settings WHERE key='rate_cents_per_hour'").fetchone()
            con.execute("INSERT INTO settings(key,value) VALUES ('rate_cents_per_hour',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(cents),))
            self._audit(con, admin, "price_changed", "card_hour", {
                "oldCents": int(old[0]) if old else self.rate_cents_per_hour, "newCents": cents,
                "effective": "next_start",
            })

    @staticmethod
    def _audit(con, actor, event, target, detail):
        con.execute("INSERT INTO audit_events(actor,event,target,detail,created_at) VALUES (?,?,?,?,?)",
                    (actor, event, str(target), json.dumps(detail, ensure_ascii=False), _now()))

    def audit(self, admin):
        if not self.is_admin(admin):
            raise PermissionError("admin required")
        with self._connection() as con:
            return [dict(r) for r in con.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 200")]

    def statement(self, owner, all_customers=False):
        if all_customers and not self.is_admin(owner):
            raise PermissionError("admin required")
        with self._connection() as con:
            return [dict(r) for r in con.execute("""
                SELECT MAX(id) id, owner, instance_id, SUM(cents) cents, reason,
                       MAX(created_at) created_at, MIN(created_at) period_start, MAX(actor) actor
                FROM billing_entries WHERE (? OR owner=?)
                GROUP BY owner, CASE WHEN reason='usage' OR reason LIKE 'storage:%'
                  THEN 'cost:'||substr(created_at,1,10)||':'||instance_id||':'||reason
                  ELSE 'entry:'||id END
                ORDER BY MAX(id) DESC LIMIT 200
                """, (int(all_customers), owner))]

    def instance_costs(self, owner, all_customers=False):
        if all_customers and not self.is_admin(owner):
            raise PermissionError("admin required")
        with self._connection() as con:
            return {r["instance_id"]: dict(r) for r in con.execute("""
                SELECT instance_id, SUM(charged_cents) computeCents,
                       MAX(CASE WHEN closed_at IS NULL THEN rate_cents_per_hour END) rateCentsPerHour,
                       MAX(CASE WHEN closed_at IS NULL THEN opened_at END) billableAt
                FROM usage_ledger WHERE (? OR owner=?) GROUP BY instance_id
                """, (int(all_customers), owner))}

    def admin_instances(self, admin):
        if not self.is_admin(admin):
            raise PermissionError("admin required")
        with self._connection() as con:
            return [self._public(r) for r in con.execute("SELECT * FROM instances WHERE state!='deleted' ORDER BY id DESC")]

    def admin_action(self, admin, instance_id, verb, mode=None):
        if not self.is_admin(admin):
            raise PermissionError("admin required")
        with self._connection() as con:
            row = self._worker_row(con, instance_id)
            owner = row["owner"]
        return self.action(owner, instance_id, verb, actor=admin, mode=mode)

    def ledger(self, owner: str) -> list[dict[str, Any]]:
        with self._connection() as con:
            return [dict(row) for row in con.execute("SELECT id,instance_id,cents,reason,created_at FROM billing_entries WHERE owner=? ORDER BY id DESC LIMIT 100", (owner,))]

    def admin_ledger(self, admin: str) -> list[dict[str, Any]]:
        if not self.is_admin(admin):
            raise PermissionError("admin required")
        with self._connection() as con:
            return [dict(row) for row in con.execute(
                """SELECT id, owner, instance_id, cents, reason, created_at, actor
                   FROM billing_entries ORDER BY id DESC LIMIT 500"""
            )]

    def ensure_admin(self, name: str, password: str) -> None:
        name = self._owner(name)
        with self._connection() as con:
            row = con.execute("SELECT role FROM users WHERE name=?", (name,)).fetchone()
        if row is None:
            self.register(name, password, role="admin")
        elif row["role"] != "admin":
            raise ValueError("configured admin name belongs to a customer")

    @staticmethod
    def _spec(spec: Mapping[str, Any]) -> tuple[int, int, int, int, bool, bytes | None]:
        if not isinstance(spec, Mapping):
            raise ValueError("spec must be a mapping")
        allowed = {"cpu", "ram", "sys", "data", "gpu", "mode", "free_test", "opaque_secret"}
        unknown = set(spec) - allowed
        if unknown:
            raise ValueError(f"unknown spec fields: {', '.join(sorted(unknown))}")
        mode = spec.get("mode", "gpu")
        if mode not in ("gpu", "headless"):
            raise ValueError("mode must be gpu or headless")
        if "gpu" in spec and spec["gpu"] not in (("SINGLE_GAUDI2", "Gaudi2") if mode == "gpu" else (None, "none")):
            raise ValueError("GPU does not match instance mode")
        free_test = spec.get("free_test", False)
        if not isinstance(free_test, bool):
            raise ValueError("free_test must be boolean")
        values = []
        for key, expected in (("cpu", HEADLESS_VCPU if mode == "headless" else FIXED_VCPU),
                              ("ram", HEADLESS_MEMORY_MB if mode == "headless" else FIXED_MEMORY_MB)):
            value = spec.get(key, expected)
            if isinstance(value, bool) or not isinstance(value, int) or value != expected:
                raise ValueError(f"{key} is fixed at {expected}")
            values.append(value)
        for key, low, high, default in (
            ("sys", SYSTEM_DISK_GIB, SYSTEM_DISK_GIB, SYSTEM_DISK_GIB),
            ("data", 0, MAX_DATA_DISK_GIB, 0),
        ):
            value = spec.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} must be an integer from {low} to {high}")
            values.append(value)
        opaque = spec.get("opaque_secret")
        if opaque is not None and not isinstance(opaque, bytes):
            raise ValueError("opaque_secret must be encrypted bytes")
        return values[0], values[1], values[2], values[3], free_test, opaque

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "owner": row["owner"],
            "cpu": row["cpu"],
            "ram": row["ram"],
            "sys": row["sys_disk"],
            "data": row["data_disk"],
            "free_test": bool(row["free_test"]),
            "slot": row["slot"],
            "mode": row["mode"],
            "endpoint": row["endpoint"],
            "state": row["state"],
            "desired_action": row["desired_action"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_activity_at": row["last_activity_at"],
            "storage_charged_cents": row["storage_charged_cents"],
        }

    def _capacity(
        self,
        con: sqlite3.Connection,
        cpu: int,
        ram: int,
        system_disk: int,
        data_disk: int,
        allocate: bool = True,
        mode: str = "gpu",
    ) -> int | None:
        totals = con.execute(
            """SELECT COALESCE(SUM(CASE WHEN endpoint IS NOT NULL THEN cpu ELSE 0 END), 0) cpu,
                      COALESCE(SUM(CASE WHEN endpoint IS NOT NULL THEN ram ELSE 0 END), 0) ram,
                      COALESCE(SUM(CASE WHEN state != 'deleted' THEN sys_disk ELSE 0 END), 0) system_disk,
                      COALESCE(SUM(CASE WHEN state != 'deleted' THEN data_disk ELSE 0 END), 0) data_disk
                 FROM instances"""
        ).fetchone()
        if totals["cpu"] + cpu > self.cpu_budget:
            raise RuntimeError("CPU capacity unavailable")
        if totals["ram"] + ram > self.memory_budget:
            raise RuntimeError("memory capacity unavailable")
        if self.storage_pool_budget and totals["system_disk"] + totals["data_disk"] + system_disk + data_disk > self.storage_pool_budget:
            raise RuntimeError("storage pool capacity unavailable")
        if not self.storage_pool_budget and totals["system_disk"] + system_disk > self.system_disk_budget:
            raise RuntimeError("system disk capacity unavailable")
        if not self.storage_pool_budget and totals["data_disk"] + data_disk > self.data_disk_budget:
            raise RuntimeError("data disk capacity unavailable")
        if not allocate:
            return None
        if mode == "headless":
            used = {r[0] for r in con.execute("SELECT endpoint FROM instances WHERE endpoint IS NOT NULL")}
            available = next((n for n in range(MAX_SLOTS + 1, MAX_SLOTS + self.headless_count + 1) if n not in used), None)
            if available is None:
                raise RuntimeError("无头资源不足，暂时无法开机")
            return available
        used = {row[0] for row in con.execute("SELECT slot FROM instances WHERE slot IS NOT NULL")}
        try:
            return next(slot for slot in range(1, self.slot_count + 1) if slot not in used)
        except StopIteration as exc:
            raise RuntimeError("no active Gaudi2 slot available") from exc

    def order(self, owner: str, spec: Mapping[str, Any], idempotency: str) -> dict[str, Any]:
        owner = self._owner(owner)
        if not isinstance(idempotency, str) or not idempotency.strip():
            raise ValueError("idempotency must be a non-empty string")
        cpu, ram, sys_disk, data_disk, free_test, opaque = self._spec(spec)
        mode = spec.get("mode", "gpu")
        with self._transaction() as con:
            existing = con.execute(
                "SELECT * FROM instances WHERE owner = ? AND idempotency = ?",
                (owner, idempotency),
            ).fetchone()
            if existing is not None:
                if (existing["cpu"], existing["ram"], existing["sys_disk"], existing["data_disk"]) != (cpu, ram, sys_disk, data_disk):
                    raise ValueError("idempotency key already used for a different instance specification")
                return self._public(existing)
            if con.execute("SELECT 1 FROM users WHERE name = ?", (owner,)).fetchone() is None:
                raise PermissionError("unknown owner")
            self._capacity(con, 0, 0, sys_disk, data_disk, allocate=False)
            now = _now()
            cur = con.execute(
                """INSERT INTO instances
                   (owner, idempotency, cpu, ram, sys_disk, data_disk, free_test, slot, state,
                    desired_action, opaque_secret, created_at, updated_at, last_activity_at, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'stopped', NULL, ?, ?, ?, ?, ?)""",
                # Keep the legacy column value true for existing databases; billing is
                # controlled by rate_cents_per_hour, not this historical flag.
                (owner, idempotency, cpu, ram, sys_disk, data_disk, 1, opaque, now, now, now, mode),
            )
            row = con.execute("SELECT * FROM instances WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._public(row)

    def _owned(self, con: sqlite3.Connection, owner: str, instance_id: int) -> sqlite3.Row:
        row = con.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
        if row is None:
            raise KeyError("instance not found")
        if row["owner"] != owner:
            raise PermissionError("instance belongs to another tenant")
        return row

    def action(self, owner: str, instance_id: int, verb: str, actor: str | None = None, mode: str | None = None) -> dict[str, Any]:
        owner = self._owner(owner)
        if actor is not None and actor != owner and not self.is_admin(actor):
            raise PermissionError("admin required")
        if verb not in ("start", "stop", "delete"):
            raise ValueError("action must be start, stop, or delete")
        if mode is not None and (verb != "start" or mode not in ("gpu", "headless")):
            raise ValueError("start mode must be gpu or headless")
        with self._transaction() as con:
            row = self._owned(con, owner, instance_id)
            state = row["state"]
            if verb == "start":
                selected_mode = row["mode"] if mode is None else mode
                if state == "provisioning" and row["desired_action"] == "start":
                    if selected_mode != row["mode"]:
                        raise ValueError("关机后才能切换启动模式")
                    return self._public(row)
                if state not in ("stopped", "error") or row["endpoint"] is not None:
                    raise ValueError(f"cannot start instance in {state}")
                if self.rate_for_mode(selected_mode) > 0:
                    balance = con.execute("SELECT balance_cents FROM users WHERE name=?", (owner,)).fetchone()[0]
                    if balance <= 0:
                        raise RuntimeError("balance is zero; recharge before starting")
                if selected_mode == "gpu" and con.execute(
                    "SELECT 1 FROM instances WHERE owner=? AND slot IS NOT NULL",
                    (owner,),
                ).fetchone():
                    raise RuntimeError("each customer may run only one GPU instance")
                if row["mode"] == "gpu" and (row["cpu"] != FIXED_VCPU or row["ram"] != FIXED_MEMORY_MB):
                    raise RuntimeError("legacy variable-spec instance must be recreated")
                cpu = HEADLESS_VCPU if selected_mode == "headless" else FIXED_VCPU
                ram = HEADLESS_MEMORY_MB if selected_mode == "headless" else FIXED_MEMORY_MB
                endpoint = self._capacity(con, cpu, ram, 0, 0, mode=selected_mode)
                slot = endpoint if selected_mode == "gpu" else None
                con.execute(
                    "UPDATE instances SET slot=?, endpoint=?, mode=?, cpu=?, ram=?, state='provisioning', desired_action='start', error=NULL, updated_at=?, last_activity_at=? WHERE id=?",
                    (slot, endpoint, selected_mode, cpu, ram, _now(), _now(), instance_id),
                )
            elif verb == "stop":
                if state == "stopped":
                    return self._public(row)
                if state not in ("provisioning", "running", "quarantined", "stopping"):
                    raise ValueError(f"cannot stop instance in {state}")
                con.execute(
                    "UPDATE instances SET state='stopping', desired_action='stop', updated_at=?, last_activity_at=? WHERE id=?",
                    (_now(), _now(), instance_id),
                )
            else:
                if state == "deleted":
                    return self._public(row)
                con.execute(
                    "UPDATE instances SET state='deleting', desired_action='delete', updated_at=?, last_activity_at=? WHERE id=?",
                    (_now(), _now(), instance_id),
                )
            result = con.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
            self._audit(con, actor or owner, "instance_" + verb, instance_id, {"owner": owner, "previousState": state, "mode": result["mode"]})
        return self._public(result)

    def list_instances(self, owner: str) -> list[dict[str, Any]]:
        owner = self._owner(owner)
        with self._connection() as con:
            rows = con.execute(
                "SELECT * FROM instances WHERE owner = ? AND state != 'deleted' ORDER BY id", (owner,)
            ).fetchall()
        return [self._public(row) for row in rows]

    def active_slots(self) -> list[dict[str, Any]]:
        """Return only scheduler-safe occupancy data, without tenant names."""
        with self._connection() as con:
            rows = con.execute(
                "SELECT id, slot, state FROM instances WHERE slot IS NOT NULL ORDER BY slot"
            ).fetchall()
        return [{"id": row["id"], "slot": row["slot"], "state": row["state"]} for row in rows]

    def active_instances(self) -> list[dict[str, Any]]:
        """Return worker-owned details for instances holding a GPU slot."""
        with self._connection() as con:
            rows = con.execute(
                "SELECT * FROM instances WHERE endpoint IS NOT NULL AND state IN ('provisioning','running','stopping','deleting','quarantined') ORDER BY id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def pending(self) -> list[dict[str, Any]]:
        with self._connection() as con:
            rows = con.execute(
                "SELECT * FROM instances WHERE desired_action IS NOT NULL ORDER BY id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def _worker_row(self, con: sqlite3.Connection, instance_id: int) -> sqlite3.Row:
        row = con.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
        if row is None:
            raise KeyError("instance not found")
        return row

    def mark_running(self, instance_id: int) -> dict[str, Any]:
        with self._transaction() as con:
            row = self._worker_row(con, instance_id)
            if row["state"] == "running":
                return self._public(row)
            if row["state"] != "provisioning" or row["endpoint"] is None:
                raise ValueError(f"cannot mark running from {row['state']}")
            if self.rate_for_mode(row["mode"]) > 0 and con.execute("SELECT balance_cents FROM users WHERE name=?", (row["owner"],)).fetchone()[0] <= 0:
                raise RuntimeError("balance is zero; start cancelled")
            now = _now()
            con.execute(
                "UPDATE instances SET state='running', desired_action=NULL, error=NULL, updated_at=?, last_activity_at=? WHERE id=?",
                (now, now, instance_id),
            )
            con.execute(
                "INSERT INTO usage_ledger(instance_id, owner, opened_at, rate_cents_per_hour, last_billed_at) VALUES (?, ?, ?, ?, ?)",
                (instance_id, row["owner"], now, self.rate_for_mode(row["mode"]), now),
            )
            result = con.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        return self._public(result)

    def mark_off(self, instance_id: int) -> dict[str, Any]:
        with self._transaction() as con:
            row = self._worker_row(con, instance_id)
            if row["state"] == "deleted":
                return self._public(row)
            if row["state"] == "stopped" and row["slot"] is None and row["desired_action"] != "delete":
                return self._public(row)
            if row["state"] not in ("stopping", "deleting", "quarantined", "error"):
                raise ValueError(f"cannot mark off from {row['state']}")
            now = _now()
            new_state = "deleted" if row["state"] == "deleting" or row["desired_action"] == "delete" else "stopped"
            self._settle(con, datetime.fromisoformat(now), instance_id)
            con.execute(
                "UPDATE usage_ledger SET closed_at=? WHERE instance_id=? AND closed_at IS NULL",
                (now, instance_id),
            )
            con.execute(
                "UPDATE instances SET slot=NULL, endpoint=NULL, state=?, desired_action=NULL, updated_at=?, last_activity_at=? WHERE id=?",
                (new_state, now, now, instance_id),
            )
            result = con.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        return self._public(result)

    def mark_recovered(self, instance_id: int) -> dict[str, Any]:
        with self._transaction() as con:
            row = self._worker_row(con, instance_id)
            if row["state"] == "running":
                return self._public(row)
            if row["state"] != "quarantined" or row["endpoint"] is None:
                raise ValueError(f"cannot recover from {row['state']}")
            con.execute(
                "UPDATE instances SET state='running', desired_action=NULL, error=NULL, updated_at=? WHERE id=?",
                (_now(), instance_id),
            )
            result = con.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        return self._public(result)

    def mark_error(self, instance_id: int, error: Any = None) -> dict[str, Any]:
        message = "worker error" if error is None else str(error)
        with self._transaction() as con:
            row = self._worker_row(con, instance_id)
            state = "quarantined" if row["endpoint"] is not None else "error"
            con.execute(
                "UPDATE instances SET state=?, desired_action=NULL, error=?, updated_at=? WHERE id=?",
                (state, message, _now(), instance_id),
            )
            result = con.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        return self._public(result)

    def mark_failed(self, instance_id: int, error: Any = None) -> dict[str, Any]:
        """Release a slot after the worker proved the guest is safely off."""
        message = "worker error" if error is None else str(error)
        with self._transaction() as con:
            row = self._worker_row(con, instance_id)
            if row["state"] == "deleted":
                return self._public(row)
            if row["slot"] is None and row["state"] == "error":
                return self._public(row)
            if row["state"] not in ("provisioning", "running", "stopping", "quarantined", "error"):
                raise ValueError(f"cannot mark failed from {row['state']}")
            now = _now()
            self._settle(con, datetime.fromisoformat(now), instance_id)
            con.execute(
                "UPDATE usage_ledger SET closed_at=? WHERE instance_id=? AND closed_at IS NULL",
                (now, instance_id),
            )
            con.execute(
                """UPDATE instances
                   SET slot=NULL, endpoint=NULL, state='error', desired_action=NULL, error=?,
                       updated_at=?, last_activity_at=?
                   WHERE id=?""",
                (message, now, now, instance_id),
            )
            result = con.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        return self._public(result)

    def bill_running(self, now: datetime | None = None) -> list[int]:
        """Settle compute and persistent extra-disk charges without losing fractions."""
        with self._transaction() as con:
            return self._settle(con, now or datetime.now(timezone.utc))

    def observe_poweroff(self, instance_id: int):
        """Close compute metering as soon as libvirt proves the guest is off.

        Keep the slot until the worker confirms that the physical card has
        returned; guest shutdown and card recovery are separate facts.
        """
        with self._transaction() as con:
            row = self._worker_row(con, instance_id)
            if row["state"] != "running":
                return
            now = _now()
            self._settle(con, datetime.fromisoformat(now), instance_id)
            con.execute("UPDATE usage_ledger SET closed_at=? WHERE instance_id=? AND closed_at IS NULL", (now, instance_id))
            con.execute("UPDATE instances SET state='stopping', desired_action='stop', updated_at=?, error=? WHERE id=?",
                        (now, "检测到实例已关机，正在回收资源", instance_id))
            self._audit(con, "system", "guest_poweroff_detected", instance_id, {"owner": row["owner"]})

    def _settle(self, con: sqlite3.Connection, now: datetime, instance_id: int | None = None) -> list[int]:
        now_text = now.isoformat(timespec="microseconds")
        stop_ids: list[int] = []
        rows = con.execute(
            """SELECT l.*, i.state FROM usage_ledger l JOIN instances i ON i.id=l.instance_id
               WHERE l.closed_at IS NULL AND (? IS NULL OR l.instance_id=?)""", (instance_id, instance_id)
        ).fetchall()
        for row in rows:
            rate = row["rate_cents_per_hour"]
            if rate <= 0:
                continue
            delta = now - datetime.fromisoformat(row["opened_at"])
            micros = max(0, (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds)
            total_units = micros * rate
            total = total_units // COMPUTE_UNITS_PER_CENT
            previous = max(row["metered_cents"], row["charged_cents"])
            remainder = con.execute("SELECT compute_remainder FROM instances WHERE id=?", (row["instance_id"],)).fetchone()[0]
            pending_units = remainder + max(0, total_units - row["metered_units"])
            due, remainder = divmod(pending_units, COMPUTE_UNITS_PER_CENT)
            balance = con.execute("SELECT balance_cents FROM users WHERE name=?", (row["owner"],)).fetchone()[0]
            charge = min(due, max(0, balance))
            if charge:
                con.execute("UPDATE users SET balance_cents=balance_cents-? WHERE name=?", (charge, row["owner"]))
                con.execute(
                    "INSERT INTO billing_entries(owner, instance_id, cents, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                    (row["owner"], row["instance_id"], -charge, "usage", now_text),
                )
            if total_units > row["metered_units"]:
                con.execute("UPDATE instances SET compute_remainder=? WHERE id=?", (remainder, row["instance_id"]))
                con.execute("UPDATE usage_ledger SET charged_cents=charged_cents+?, metered_cents=?, metered_units=?, last_billed_at=? WHERE id=?", (charge, max(total, previous), total_units, now_text, row["id"]))
            if balance - charge <= 0 and row["state"] in ("running", "quarantined"):
                con.execute("UPDATE instances SET state='stopping', desired_action='stop', updated_at=? WHERE id=?", (now_text, row["instance_id"]))
                stop_ids.append(row["instance_id"])
        storage_rows = con.execute(
            """SELECT i.*, u.balance_cents FROM instances i JOIN users u ON u.name=i.owner
               WHERE i.state != 'deleted' AND (? IS NULL OR i.id=?)""", (instance_id, instance_id)
        ).fetchall()
        for row in storage_rows:
            extra_gib = max(0, row["data_disk"] - FREE_DATA_DISK_GIB)
            if extra_gib <= 0:
                continue
            started = datetime.fromisoformat(row["storage_started_at"] or row["created_at"])
            elapsed_us = max(0, int((now - started).total_seconds() * 1_000_000))
            total_microcents = elapsed_us * extra_gib * EXTRA_DATA_DISK_MICROCENTS_PER_GIB_DAY // (86_400 * 1_000_000)
            previous_microcents = row["storage_metered_microcents"]
            previous_charged = row["storage_charged_cents"]
            due = max(0, total_microcents // MICROCENTS_PER_CENT - previous_charged)
            # A customer can retain many disks. Re-read the balance after EACH
            # debit in this transaction; rows fetched above share a stale value.
            balance = con.execute("SELECT balance_cents FROM users WHERE name=?", (row["owner"],)).fetchone()[0]
            charge = min(due, max(0, balance))
            if charge:
                con.execute("UPDATE users SET balance_cents=balance_cents-? WHERE name=?", (charge, row["owner"]))
                con.execute(
                    "INSERT INTO billing_entries(owner, instance_id, cents, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                    (row["owner"], row["id"], -charge, f"storage:{extra_gib}GiB", now_text),
                )
            if total_microcents > previous_microcents or charge:
                con.execute(
                    "UPDATE instances SET storage_metered_microcents=?, storage_charged_cents=storage_charged_cents+? WHERE id=?",
                    (max(total_microcents, previous_microcents), charge, row["id"]),
                )
        # Storage can consume the last cent after compute settlement. Stop in
        # the same pass instead of letting another interval run unpaid.
        exhausted = con.execute("""SELECT i.id FROM instances i JOIN users u ON u.name=i.owner
            WHERE i.state IN ('provisioning','running','quarantined') AND u.balance_cents<=0
              AND (? IS NULL OR i.owner=(SELECT owner FROM instances WHERE id=?))""", (instance_id, instance_id)).fetchall()
        for item in exhausted:
            con.execute("UPDATE instances SET state='stopping', desired_action='stop', updated_at=? WHERE id=?", (now_text, item["id"]))
            if item["id"] not in stop_ids:
                stop_ids.append(item["id"])
        return stop_ids

    def rate_for_mode(self, mode):
        return HEADLESS_RATE_CENTS if mode == "headless" else self.price()

    def pricing(self) -> dict[str, Any]:
        return {
            "cardCentsPerHour": self.price(),
            "headlessCentsPerHour": HEADLESS_RATE_CENTS,
            "includedCpu": FIXED_VCPU,
            "includedMemoryGB": FIXED_MEMORY_GB,
            "includedMemoryMB": FIXED_MEMORY_MB,
            "systemDiskGiB": SYSTEM_DISK_GIB,
            "freeDataDiskGiB": FREE_DATA_DISK_GIB,
            "maxDataDiskGiB": MAX_DATA_DISK_GIB,
            "extraDataDiskCnyPerGiBDay": EXTRA_DATA_DISK_MICROCENTS_PER_GIB_DAY / MICROCENTS_PER_CENT / 100,
        }

    def reap_idle(self, older_than_seconds: int = IDLE_RELEASE_SECONDS) -> list[int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        with self._transaction() as con:
            rows = con.execute(
                "SELECT id FROM instances WHERE state IN ('stopped','error') AND slot IS NULL AND last_activity_at IS NOT NULL AND last_activity_at < ?",
                (cutoff,),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for instance_id in ids:
                con.execute(
                    "UPDATE instances SET state='deleting', desired_action='delete', updated_at=? WHERE id=?",
                    (_now(), instance_id),
                )
        return ids

    def metrics(self) -> dict[str, Any]:
        with self._connection() as con:
            totals = con.execute(
                """SELECT COUNT(*) FILTER (WHERE slot IS NOT NULL) active,
                          COALESCE(SUM(CASE WHEN endpoint IS NOT NULL AND mode='headless' THEN 1 ELSE 0 END), 0) headless,
                          COALESCE(SUM(CASE WHEN endpoint IS NOT NULL THEN cpu ELSE 0 END), 0) cpu,
                          COALESCE(SUM(CASE WHEN endpoint IS NOT NULL THEN ram ELSE 0 END), 0) ram,
                          COALESCE(SUM(CASE WHEN state != 'deleted' THEN sys_disk ELSE 0 END), 0) system_disk,
                          COALESCE(SUM(CASE WHEN state != 'deleted' THEN data_disk ELSE 0 END), 0) data_disk
                     FROM instances"""
            ).fetchone()
            states = {row["state"]: row["n"] for row in con.execute(
                "SELECT state, COUNT(*) n FROM instances GROUP BY state"
            )}
            open_intervals = con.execute(
                "SELECT COUNT(*) FROM usage_ledger WHERE closed_at IS NULL"
            ).fetchone()[0]
        return {
            "active_slots": totals["active"],
            "active_headless": totals["headless"],
            "used": {
                "cpu": totals["cpu"],
                "memoryMB": totals["ram"],
                "systemDiskGiB": totals["system_disk"],
                "dataDiskGiB": totals["data_disk"],
            },
            "capacity": {
                "slots": self.slot_count,
                "cpu": self.cpu_budget,
                "memoryMB": self.memory_budget,
                "systemDiskGiB": self.system_disk_budget,
                "dataDiskGiB": self.data_disk_budget,
            },
            "states": states,
            "open_usage_intervals": open_intervals,
        }
