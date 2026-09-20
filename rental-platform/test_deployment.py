"""Execute the real Bash installer functions in a disposable filesystem.

Only host-facing commands are mocked; cp/mv/install/chmod and the failure traps
are real. No production root, libvirt, systemd or network endpoint is touched.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


BASH = shutil.which("bash")
if not BASH and Path("C:/Program Files/Git/bin/bash.exe").is_file():
    BASH = "C:/Program Files/Git/bin/bash.exe"
SOURCE = Path(__file__).with_name("deploy-production.sh")

DRIVER = r"""
set -Eeuo pipefail
source "$QA_SOURCE"
BASE=$(cd "$QA_BASE" && pwd -P)
EXPECTED_DATA_ROOT="$BASE/intel-data"
EXPECTED_DATA_DEVICE=/dev/test-intel
ROOT="$BASE/program"
DATA="$BASE/data"
BACKUPS="$DATA/backups"
ENV_FILE="$BASE/rental.env"
UNIT_FILE="$BASE/service.unit"
SCRIPT_DIR="$BASE/release"
HEALTH_ATTEMPTS=3
systemctl() {
  printf '%s\n' "$*" >>"$BASE/commands.log"
  case "$1" in
    is-active)
      if [[ "$*" == *1cattunnel* || "$*" == *libvirtd* ]]; then return 0; fi
      [[ -f "$BASE/active" ]] ;;
    stop) rm -f "$BASE/active" ;;
    start)
      if [[ "$QA_MODE" == start-fail && -f "$ROOT/new-version" ]]; then return 51; fi
      touch "$BASE/active"
      if [[ -f "$ROOT/new-version" && ! -f "$BASE/use-real-db" ]]; then
        printf 'new-business-events\n' >"$DATA/state/core.sqlite3"
      fi ;;
    daemon-reload)
      if [[ "$QA_MODE" == reload-fail && ! -f "$BASE/injected" ]]; then
        touch "$BASE/injected"; return 52
      fi ;;
    *) return 0 ;;
  esac
}
curl() {
  local n=0
  if [[ -f "$BASE/health-count" ]]; then n=$(<"$BASE/health-count"); fi
  n=$((n+1)); printf '%s' "$n" >"$BASE/health-count"
  if [[ -f "$ROOT/new-version" ]]; then
    if [[ "$QA_MODE" == health-fail || "$QA_MODE" == restore-fail ]]; then return 7; fi
    if [[ "$QA_MODE" == bad-health ]]; then printf '{"ok":false,"service":"other"}'; return; fi
    if [[ "$QA_MODE" == slow-health && "$n" -le 2 ]]; then return 7; fi
  fi
  printf '{"ok":true,"service":"1cat-rental"}'
}
cp() {
  if [[ "$QA_MODE" == stage-fail && "$2" == "$SCRIPT_DIR/." ]]; then return 53; fi
  if [[ "$QA_MODE" == backup-fail && "$*" == *backups*/program* ]]; then return 54; fi
  if [[ "$QA_MODE" == restore-fail && "$*" == *.1cat-rental-restore* ]]; then return 55; fi
  command cp "$@"
}
mv() {
  if [[ "$QA_MODE" == rename-fail && "$*" == *.1cat-rental-stage* ]]; then return 56; fi
  command mv "$@"
}
install() {
  if [[ "$QA_MODE" == unit-fail && "$*" == *"$ROOT/1cat-rental.service"* ]]; then return 57; fi
  if [[ "$1" == -d ]]; then
    command mkdir -p "$4"
    command chmod "${3#-}" "$4"
    return 0
  fi
  command install "$@"
}
chown() {
  # Non-root test runner: ownership transition is not claimed as verified.
  if [[ "$QA_MODE" == permissions-fail ]]; then return 58; fi
}
runuser() {
  # The production preflight executes the real read check as libvirt-qemu.
  # Disposable tests model both outcomes without requiring that system user.
  if [[ "$QA_MODE" == image-unreadable ]]; then return 59; fi
  return 0
}
findmnt() {
  if [[ "$*" == *"$EXPECTED_DATA_ROOT"* ]]; then
    printf '%s\n' "$EXPECTED_DATA_DEVICE"
  else
    printf '%s\n' /dev/test-root
  fi
}
python3() { "$QA_PYTHON" "$@"; }
sleep() { :; }
# The real config validator runs; use operator-owned fixture values.
cat >"$ENV_FILE" <<EOF
RENTAL_ROOT=$DATA
RENTAL_STATIC_ROOT=$ROOT/public
RENTAL_IMAGE_ENABLED=1
RENTAL_RATE_CENTS_PER_HOUR=500
RENTAL_BASE_IMAGE=$BASE/image.qcow2
RENTAL_DATA_ROOT=$BASE/intel-data
RENTAL_CPU_BUDGET=128
RENTAL_MEMORY_BUDGET_MB=500000
RENTAL_SYSTEM_DISK_BUDGET_GIB=700
RENTAL_DATA_DISK_BUDGET_GIB=3300
RENTAL_PUBLIC_PORT_1=50101
RENTAL_PUBLIC_PORT_2=50102
RENTAL_PUBLIC_PORT_3=50103
RENTAL_PUBLIC_PORT_4=50104
RENTAL_PUBLIC_PORT_5=50105
RENTAL_PUBLIC_PORT_6=50106
RENTAL_PUBLIC_PORT_7=50107
RENTAL_PUBLIC_PORT_8=50108
EOF
case "$QA_MODE" in
  duplicate-port) printf 'RENTAL_PUBLIC_PORT_8=50101\n' >>"$ENV_FILE" ;;
  high-port) printf 'RENTAL_PUBLIC_PORT_8=65536\n' >>"$ENV_FILE" ;;
  simulation) printf 'RENTAL_SIMULATION=1\n' >>"$ENV_FILE" ;;
esac
if [[ "$QA_MODE" == rollback ]]; then
  # First install, then roll back while preserving newer state.
  install_release
  requested=$RECOVERY_BACKUP
  printf 'post-install-credit\n' >"$DATA/state/core.sqlite3"
  PROGRAM_CHANGED=0
  rollback_release "$requested"
elif [[ "$QA_MODE" == invalid-backup ]]; then
  rollback_release "$BASE/outside"
elif [[ "$QA_MODE" == account-rollback || "$QA_MODE" == account-rollback-empty ]]; then
  install_release
  requested=$RECOVERY_BACKUP
  printf 'def delete_customer(\n' >"$ROOT/core.py"
  python3 - "$DATA/state/core.sqlite3" "$QA_MODE" <<'PY'
from pathlib import Path
import sqlite3,sys
path=Path(sys.argv[1])  # Exact disposable fixture, not production.
path.unlink()
with sqlite3.connect(path) as db:
    db.execute('CREATE TABLE users(name TEXT, deleted_at TEXT)')
    db.execute('INSERT INTO users VALUES (?,?)', ('alice', '2026-09-20' if sys.argv[2]=='account-rollback' else None))
PY
  if [[ "$QA_MODE" == account-rollback ]]; then
    if rollback_release "$requested"; then exit 90; fi
    # The recovery path rechecks the same guard after a service stop, too.
    if restore_backup "$requested"; then exit 91; fi
    [[ -f "$ROOT/new-version" && -f "$BASE/active" ]]
  else
    PROGRAM_CHANGED=0
    rollback_release "$requested"
    [[ -f "$ROOT/old-version" && -f "$BASE/active" ]]
  fi
elif [[ "$QA_MODE" == multinode-rollback || "$QA_MODE" == multinode-rollback-empty ]]; then
  install_release
  requested=$RECOVERY_BACKUP
  printf 'multi-node transport\n' >"$ROOT/remote_backend.py"
  python3 - "$DATA/state/core.sqlite3" "$QA_MODE" <<'PY'
from pathlib import Path
import sqlite3,sys
path=Path(sys.argv[1])
path.unlink()
with sqlite3.connect(path) as db:
    db.execute('CREATE TABLE instances(node_id TEXT)')
    db.execute('INSERT INTO instances VALUES (?)',('G2-003' if sys.argv[2]=='multinode-rollback' else 'G2-002',))
PY
  if [[ "$QA_MODE" == multinode-rollback ]]; then
    if rollback_release "$requested"; then exit 90; fi
    if restore_backup "$requested"; then exit 91; fi
    [[ -f "$ROOT/new-version" && -f "$BASE/active" ]]
  else
    PROGRAM_CHANGED=0
    rollback_release "$requested"
    [[ -f "$ROOT/old-version" && -f "$BASE/active" ]]
  fi
elif [[ "$QA_MODE" == migrated-rollback ]]; then
  install_release
  requested=$RECOVERY_BACKUP
  printf 'RENTAL_SYSTEM_ROOT=/mnt/nvme1/1cat-rental-system\n' >>"$ENV_FILE"
  if rollback_release "$requested"; then exit 90; fi
  [[ -f "$ROOT/new-version" && -f "$BASE/active" ]]
elif [[ "$QA_MODE" == ui-rollback ]]; then
  mkdir -p "$ROOT/public/rental"
  printf 'old UI' >"$ROOT/public/rental/index.html"
  install_release
  requested=$RECOVERY_BACKUP
  printf 'post-install-credit\n' >"$DATA/state/core.sqlite3"
  rollback_ui "$requested"
  [[ "$(<"$ROOT/public/rental/index.html")" == 'old UI' ]]
  [[ -f "$ROOT/new-version" && -f "$BASE/active" ]]
elif [[ "$QA_MODE" == inplace ]]; then
  SCRIPT_DIR=$ROOT
  install_release
else
  install_release
fi
"""


@unittest.skipUnless(BASH, "Bash required for deployment fault injection")
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rental-deploy-test-")
        self.base = Path(self.tmp.name)
        for name in ("program", "release/public/rental", "data/state", "data/backups", "intel-data/instances"):
            (self.base / name).mkdir(parents=True, exist_ok=True)
        (self.base / "program/old-version").write_text("old")
        # Legacy deployments deliberately have no deploy-production.sh.
        (self.base / "program/server.py").write_text("old code")
        # Other fault-injection cases use text sentinels for the database and
        # already-compatible code. Only the multi-node rollback cases below
        # model a single-node backup, using a real SQLite database.
        (self.base / "program/remote_backend.py").write_text("prior compatible transport")
        (self.base / "service.unit").write_text("old unit")
        (self.base / "active").touch()
        (self.base / "data/state/core.sqlite3").write_text("old-business-events\n")
        release = self.base / "release"
        for name in ("core.py", "recharge_codes.py", "backend.py", "shared_storage.py", "remote_backend.py", "shared-storage/1cat-mount-shared", "shared-storage/1cat-shared-storage.service", "server.py", "adminctl.py",
                     "deploy-production.sh", "prepare-image.sh", "1cat-rental.service",
                     "public/index.html", "public/rental/index.html", "new-version"):
            (release / name).parent.mkdir(parents=True, exist_ok=True)
            (release / name).write_text("new")
        (self.base / "image.qcow2").touch()
        (self.base / "image.manifest.json").write_text(json.dumps({"validated": True}))
        self.driver = self.base / "driver.sh"
        self.driver.write_text(DRIVER, encoding="utf-8", newline="\n")

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, mode):
        if mode in ('multinode-rollback','multinode-rollback-empty'):
            (self.base / 'program/remote_backend.py').unlink()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("RENTAL_")}
        env.update(QA_SOURCE=SOURCE.resolve().as_posix(), QA_BASE=self.base.as_posix(),
                   QA_PYTHON=Path(sys.executable).as_posix(), QA_MODE=mode)
        result = subprocess.run([BASH, self.driver.as_posix()], env=env,
                                capture_output=True, timeout=30)
        self.last_output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        return result

    def assert_old_healthy(self):
        commands = (self.base / "commands.log").read_text(errors="replace") if (self.base / "commands.log").exists() else "<no commands>"
        evidence = self.last_output + "\nCOMMANDS:\n" + commands
        self.assertTrue((self.base / "program/old-version").exists(), evidence)
        self.assertFalse((self.base / "program/new-version").exists())
        self.assertTrue((self.base / "active").exists(), evidence)
        self.assertEqual((self.base / "service.unit").read_text(), "old unit")

    def test_success_uses_fresh_tree_and_complete_snapshot(self):
        result = self.invoke("ok")
        self.assertEqual(result.returncode, 0, self.last_output)
        self.assertFalse((self.base / "program/old-version").exists())
        self.assertTrue((self.base / "program/new-version").exists())
        backups = list((self.base / "data/backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "complete").read_text(), "v2\n")
        self.assertEqual((backups[0] / "service.unit").read_text(), "old unit")
        self.assertEqual((backups[0] / "state/core.sqlite3").read_text(), "old-business-events\n")
        if os.name != "nt":
            self.assertEqual((self.base / "program/server.py").stat().st_mode & 0o777, 0o644)
            self.assertEqual((self.base / "program/deploy-production.sh").stat().st_mode & 0o777, 0o755)

    def test_staging_and_permission_failures_never_stop_old_service(self):
        for mode in ("stage-fail", "permissions-fail"):
            with self.subTest(mode=mode):
                self.assertNotEqual(self.invoke(mode).returncode, 0)
                self.assert_old_healthy()
                self.assertNotIn("stop ", (self.base / "commands.log").read_text())

    def test_missing_remote_transport_rejected_before_stopping(self):
        (self.base / 'release/remote_backend.py').unlink()
        self.assertNotEqual(self.invoke('ok').returncode,0)
        self.assertIn('release file missing: remote_backend.py',self.last_output)
        self.assert_old_healthy()

    def test_backup_failure_restarts_old_service_without_replacement(self):
        self.assertNotEqual(self.invoke("backup-fail").returncode, 0)
        self.assert_old_healthy()
        self.assertEqual(list((self.base / "data/backups").glob("*/complete")), [])

    def test_activation_failures_restore_legacy_code_and_unit(self):
        for mode in ("rename-fail", "unit-fail", "reload-fail", "start-fail"):
            with self.subTest(mode=mode):
                self.assertNotEqual(self.invoke(mode).returncode, 0)
                self.assert_old_healthy()
                self.assertIn("previous deployment recovered", self.last_output)

    def test_health_failure_keeps_new_business_events(self):
        self.assertNotEqual(self.invoke("health-fail").returncode, 0)
        self.assert_old_healthy()
        self.assertEqual((self.base / "data/state/core.sqlite3").read_text(), "new-business-events\n")

    def test_wrong_health_payload_is_not_success(self):
        self.assertNotEqual(self.invoke("bad-health").returncode, 0)
        self.assert_old_healthy()

    def test_delayed_readiness_is_retried(self):
        self.assertEqual(self.invoke("slow-health").returncode, 0, self.last_output)
        self.assertEqual((self.base / "health-count").read_text(), "3")

    def test_failed_recovery_does_not_restart_broken_program(self):
        self.assertNotEqual(self.invoke("restore-fail").returncode, 0)
        self.assertIn("RECOVERY FAILED", self.last_output)
        self.assertFalse((self.base / "active").exists())

    def test_manual_rollback_preserves_post_deployment_recharge(self):
        self.assertEqual(self.invoke("rollback").returncode, 0, self.last_output)
        self.assert_old_healthy()
        self.assertEqual((self.base / "data/state/core.sqlite3").read_text(), "post-install-credit\n")

    def test_invalid_backup_is_rejected_before_stopping(self):
        self.assertNotEqual(self.invoke("invalid-backup").returncode, 0)
        self.assertTrue((self.base / "active").exists())
        self.assertFalse((self.base / "commands.log").exists())

    def test_deleted_accounts_block_legacy_rollback_and_recovery(self):
        self.assertEqual(self.invoke("account-rollback").returncode, 0, self.last_output)
        self.assertEqual(self.last_output.count('this backup would re-enable them'), 2)
        self.assertTrue((self.base / "program/new-version").exists())
        commands = (self.base / "commands.log").read_text()
        self.assertEqual(commands.count('stop 1cat-rental.service'), 1)  # Initial install only.

    def test_legacy_rollback_allowed_without_deleted_accounts(self):
        self.assertEqual(self.invoke("account-rollback-empty").returncode, 0, self.last_output)
        self.assert_old_healthy()

    def test_migrated_storage_rejects_legacy_controller_rollback(self):
        self.assertEqual(self.invoke('migrated-rollback').returncode, 0, self.last_output)
        self.assertIn('cannot safely restore the controller', self.last_output)

    def test_remote_instances_block_single_node_rollback_and_failure_recovery(self):
        self.assertEqual(self.invoke('multinode-rollback').returncode,0,self.last_output)
        self.assertEqual(self.last_output.count('could operate on the wrong host'),2)
        self.assertTrue((self.base/'program/new-version').exists())

    def test_single_node_rollback_allowed_before_any_remote_instance(self):
        self.assertEqual(self.invoke('multinode-rollback-empty').returncode,0,self.last_output)
        self.assert_old_healthy()

    def test_ui_rollback_keeps_controller_and_wallet(self):
        self.assertEqual(self.invoke('ui-rollback').returncode, 0, self.last_output)
        self.assertEqual((self.base / 'data/state/core.sqlite3').read_text(), 'post-install-credit\n')

    def test_invalid_public_ports_and_simulation_are_rejected(self):
        for mode in ("duplicate-port", "high-port", "simulation"):
            with self.subTest(mode=mode):
                self.assertNotEqual(self.invoke(mode).returncode, 0)
                self.assertTrue((self.base / "active").exists())
                self.assertFalse((self.base / "commands.log").exists())

    def test_unvalidated_manifest_is_rejected(self):
        (self.base / "image.manifest.json").write_text('{"validated":false}')
        self.assertNotEqual(self.invoke("ok").returncode, 0)
        self.assertTrue((self.base / "active").exists())

    def test_unreadable_image_is_rejected_before_stopping(self):
        self.assertNotEqual(self.invoke("image-unreadable").returncode, 0)
        self.assertTrue((self.base / "active").exists())
        commands = ((self.base / "commands.log").read_text()
                    if (self.base / "commands.log").exists() else "")
        self.assertNotIn("stop ", commands)
        self.assertIn("not readable by libvirt-qemu", self.last_output)

    def test_first_install_failure_restores_absence_of_program_and_unit(self):
        # Exact disposable test paths only.
        shutil.rmtree(self.base / "program")
        (self.base / "service.unit").unlink()
        (self.base / "active").unlink()
        (self.base / 'use-real-db').touch()
        (self.base / 'data/state/core.sqlite3').unlink()
        with sqlite3.connect(self.base / 'data/state/core.sqlite3') as db:
            db.execute('CREATE TABLE instances(node_id TEXT)')
        db.close()
        self.assertNotEqual(self.invoke("health-fail").returncode, 0)
        self.assertFalse((self.base / "program").exists())
        self.assertFalse((self.base / "service.unit").exists())
        self.assertFalse((self.base / "active").exists())


if __name__ == "__main__":
    unittest.main()
