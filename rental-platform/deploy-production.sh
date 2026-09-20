#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Safe production installer for the G2-002 single-card rental service.
# Run as root from the extracted release directory. It never writes admin
# passwords; bootstrap-admin and set-price keep those prompts hidden.

ROOT=/opt/1cat-rental
DATA=/var/lib/1cat-rental
ENV_FILE=/etc/1cat-rental/rental.env
SERVICE=1cat-rental
UNIT_FILE="/etc/systemd/system/$SERVICE.service"
BACKUPS="$DATA/backups"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RECOVERY_BACKUP=
WAS_ACTIVE=0
PROGRAM_CHANGED=0
HEALTH_ATTEMPTS=30
EXPECTED_DATA_ROOT=${EXPECTED_DATA_ROOT:-/mnt/nvme1/1cat-rental-data}
EXPECTED_DATA_DEVICE=${EXPECTED_DATA_DEVICE:-/dev/nvme1n1p1}

usage() {
  cat <<'EOF'
Usage:
  deploy-production.sh check
  deploy-production.sh backup
  deploy-production.sh install
  deploy-production.sh bootstrap-admin NAME
  deploy-production.sh set-price ADMIN CNY_PER_HOUR
  deploy-production.sh rollback /var/lib/1cat-rental/backups/TIMESTAMP
  deploy-production.sh ui-rollback /var/lib/1cat-rental/backups/TIMESTAMP
  deploy-production.sh status
EOF
}

need_root() {
  [[ "$(id -u)" == 0 ]] || { echo "run as root" >&2; exit 1; }
  [[ ! -L "$ROOT" && ! -L "$DATA" && ! -L "$BACKUPS" && ! -L "$DATA/state" && ! -L "$ENV_FILE" && ! -L "$UNIT_FILE" ]] || {
    echo "refusing symlinked deployment targets" >&2; exit 1;
  }
}

lock_deployment() {
  install -d "$DATA"
  exec 9>"$DATA/deployment.lock"
  flock -n 9 || { echo "another deployment operation is running" >&2; exit 1; }
}

package_check() {
  local required=(core.py recharge_codes.py backend.py shared_storage.py shared-storage/1cat-mount-shared shared-storage/1cat-shared-storage.service server.py adminctl.py deploy-production.sh prepare-image.sh 1cat-rental.service public/index.html public/rental/index.html)
  local item
  for item in "${required[@]}"; do
    [[ -f "$SCRIPT_DIR/$item" ]] || { echo "release file missing: $item" >&2; return 1; }
  done
  [[ -z "$(find "$SCRIPT_DIR" -type l -print -quit)" ]] || { echo "release contains symlinks" >&2; return 1; }
}

config_check() {
  [[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE; copy rental.env.example and configure it first" >&2; return 1; }
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  [[ "${RENTAL_ROOT:-}" == "$DATA" ]] || { echo "RENTAL_ROOT must be $DATA" >&2; return 1; }
  [[ "${RENTAL_STATE_ROOT:-$DATA/state}" == "$DATA/state" ]] || { echo "unexpected RENTAL_STATE_ROOT" >&2; return 1; }
  [[ "${RENTAL_SIMULATION:-0}" == 0 && "${RENTAL_PORT:-8765}" == 8765 ]] || { echo "real backend and port 8765 required" >&2; return 1; }
  [[ "${RENTAL_STATIC_ROOT:-}" == "$ROOT/public" ]] || { echo "RENTAL_STATIC_ROOT must be $ROOT/public" >&2; return 1; }
  [[ "${RENTAL_IMAGE_ENABLED:-0}" == 1 ]] || { echo "RENTAL_IMAGE_ENABLED must be 1" >&2; return 1; }
  [[ "${RENTAL_ALLOW_ANONYMOUS:-0}" == 0 ]] || { echo "anonymous access must remain disabled" >&2; return 1; }
  [[ "${RENTAL_RATE_CENTS_PER_HOUR:-0}" =~ ^[1-9][0-9]*$ ]] || { echo "set a positive RENTAL_RATE_CENTS_PER_HOUR before activation" >&2; return 1; }
  [[ -f "${RENTAL_BASE_IMAGE:-}" && -f "${RENTAL_BASE_IMAGE%.qcow2}.manifest.json" ]] || { echo "validated Gaudi image/manifest is missing" >&2; return 1; }
  python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1])).get("validated") is not True)' "${RENTAL_BASE_IMAGE%.qcow2}.manifest.json"
  runuser -u libvirt-qemu -- test -r "$RENTAL_BASE_IMAGE" || { echo "validated Gaudi image is not readable by libvirt-qemu" >&2; return 1; }
  [[ "${RENTAL_DATA_ROOT:-}" == "$EXPECTED_DATA_ROOT" ]] || { echo "RENTAL_DATA_ROOT must be $EXPECTED_DATA_ROOT" >&2; return 1; }
  [[ -d "$RENTAL_DATA_ROOT/instances" ]] || { echo "Intel data-disk instance directory is missing" >&2; return 1; }
  local root_source data_source
  root_source=$(findmnt -n -o SOURCE -T "$DATA") || return
  data_source=$(findmnt -n -o SOURCE -T "$RENTAL_DATA_ROOT") || return
  [[ "$data_source" == "$EXPECTED_DATA_DEVICE" && "$data_source" != "$root_source" ]] || { echo "data root is not on the verified Intel NVMe partition" >&2; return 1; }
  runuser -u libvirt-qemu -- test -x "$RENTAL_DATA_ROOT" || { echo "Intel data root is not traversable by libvirt-qemu" >&2; return 1; }
  if [[ -n "${RENTAL_SYSTEM_ROOT:-}" && "$RENTAL_SYSTEM_ROOT" != "$DATA" ]]; then
    [[ "$RENTAL_SYSTEM_ROOT" == /mnt/nvme1/1cat-rental-system && -d "$RENTAL_SYSTEM_ROOT/instances" ]] || { echo "unexpected/missing system disk pool" >&2; return 1; }
    [[ "$(findmnt -n -o SOURCE -T "$RENTAL_SYSTEM_ROOT")" == "$EXPECTED_DATA_DEVICE" ]] || { echo "system pool is not on Intel NVMe" >&2; return 1; }
    [[ "${RENTAL_STORAGE_POOL_BUDGET_GIB:-}" =~ ^[0-9]+$ && "$RENTAL_STORAGE_POOL_BUDGET_GIB" -ge 400 && "$RENTAL_STORAGE_POOL_BUDGET_GIB" -le 3200 ]] || { echo "shared pool budget must be 400-3200 GiB" >&2; return 1; }
  fi
  [[ "${RENTAL_CPU_BUDGET:-}" == 128 ]] || { echo "RENTAL_CPU_BUDGET must be 128" >&2; return 1; }
  [[ "${RENTAL_MEMORY_BUDGET_MB:-}" == 500000 ]] || { echo "RENTAL_MEMORY_BUDGET_MB must be 500000" >&2; return 1; }
  local system_budget=${RENTAL_SYSTEM_DISK_BUDGET_GIB:-} data_budget=${RENTAL_DATA_DISK_BUDGET_GIB:-}
  [[ "$system_budget" =~ ^[0-9]+$ && "$system_budget" -ge 400 ]] || { echo "system disk budget must cover eight fixed instances" >&2; return 1; }
  [[ "$data_budget" =~ ^[0-9]+$ && "$data_budget" -le 3300 ]] || { echo "data disk budget must be set and no greater than 3300 GiB" >&2; return 1; }
  local slot port_var port
  local -A used_ports=()
  for slot in 1 2 3 4 5 6 7 8; do
    port_var="RENTAL_PUBLIC_PORT_${slot}"
    port=${!port_var:-}
    [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && (( port <= 65535 )) || { echo "$port_var is missing or invalid" >&2; return 1; }
    [[ ! -v "used_ports[$port]" ]] || { echo "public ports must be distinct" >&2; return 1; }
    used_ports[$port]=1
  done
}

normalize_release_permissions() {
  # Release archives can be unpacked with a permissive umask.  Normalize the
  # program tree before it becomes executable so an unprivileged account
  # cannot replace the control-plane code or its static assets.
  local tree=$1 script
  [[ -z "$(find "$tree" -type l -print -quit)" ]] || return 1
  chown -R root:root "$tree" || return
  find "$tree" -type d -exec chmod 755 {} + || return
  find "$tree" -type f -exec chmod 644 {} + || return
  for script in deploy-production.sh prepare-image.sh; do
    if [[ -f "$tree/$script" ]]; then chmod 755 "$tree/$script" || return; fi
  done
  return 0
}

wait_healthy() {
  local attempt
  for (( attempt=0; attempt<HEALTH_ATTEMPTS; attempt++ )); do
    if systemctl is-active --quiet "$SERVICE.service" &&
       curl --fail --silent --max-time 2 http://127.0.0.1:8765/health |
       python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(not (d.get("ok") is True and d.get("service")=="1cat-rental"))' 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "bounded readiness check failed" >&2
  return 1
}

backup_current() {
  systemctl is-active --quiet "$SERVICE.service" && { echo "stop service before snapshot" >&2; return 1; }
  local backup
  install -d -m 700 "$BACKUPS" || return
  backup=$(mktemp -d "$BACKUPS/$(date -u +%Y%m%d-%H%M%S)-XXXXXX") || return
  if [[ -d "$ROOT" ]]; then cp -a "$ROOT" "$backup/program" || return; fi
  if [[ -f "$ENV_FILE" ]]; then install -m 600 "$ENV_FILE" "$backup/rental.env" || return; fi
  if [[ -f "$UNIT_FILE" ]]; then install -m 600 "$UNIT_FILE" "$backup/service.unit" || return; fi
  if [[ -d "$DATA/state" ]]; then cp -a "$DATA/state" "$backup/state" || return; fi
  printf 'v2\n' >"$backup/complete" || return
  printf '%s\n' "$backup"
}

backup_release() {
  WAS_ACTIVE=0
  systemctl is-active --quiet "$SERVICE.service" && WAS_ACTIVE=1
  trap 'recover_install_failure $?' ERR
  systemctl stop "$SERVICE.service"
  RECOVERY_BACKUP=$(backup_current)
  if (( WAS_ACTIVE )); then
    systemctl start "$SERVICE.service"
    wait_healthy
  fi
  trap - ERR INT TERM
  echo "$RECOVERY_BACKUP"
}

validate_backup() {
  local backup=$1 canonical
  canonical=$(realpath -e -- "$backup") || { echo "invalid backup path" >&2; return 1; }
  [[ "$canonical" == "$backup" && "$(dirname "$canonical")" == "$(realpath -e "$BACKUPS")" && -f "$backup/complete" ]] || { echo "invalid/incomplete backup path" >&2; return 1; }
  [[ "$(<"$backup/complete")" == v2 && -z "$(find "$backup" -type l -print -quit)" ]] || { echo "unsafe backup" >&2; return 1; }
}

displace_program() {
  local displaced
  if [[ -d "$ROOT" ]]; then
    displaced=$(mktemp -d "$(dirname "$ROOT")/.1cat-rental-previous.XXXXXX") || return
    rmdir "$displaced" || return
    mv -- "$ROOT" "$displaced" || return
    echo "previous program retained at $displaced" >&2
  fi
  return 0
}

guard_account_rollback() {
  local backup=$1
  if grep -q 'def delete_customer(' "$backup/program/core.py" 2>/dev/null; then return 0; fi
  if grep -q 'def delete_customer(' "$ROOT/core.py" "$SCRIPT_DIR/core.py" 2>/dev/null &&
     [[ -f "$DATA/state/core.sqlite3" ]]; then
    if ! python3 - "$DATA/state/core.sqlite3" <<'PY'
import sqlite3,sys
db=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)
columns={r[1] for r in db.execute('PRAGMA table_info(users)')}
sys.exit(1 if 'deleted_at' in columns and db.execute('SELECT 1 FROM users WHERE deleted_at IS NOT NULL LIMIT 1').fetchone() else 0)
PY
    then
      echo 'deleted customer accounts exist; this backup would re-enable them. Use ui-rollback or an account-deletion-compatible release.' >&2
      return 1
    fi
  fi
}

restore_backup() {
  local backup=$1 restored
  validate_backup "$backup" || return
  # Recheck after the service stops: a deletion could race the initial preflight.
  guard_account_rollback "$backup" || return
  if [[ -d "$backup/program" ]]; then
    restored=$(mktemp -d "$(dirname "$ROOT")/.1cat-rental-restore.XXXXXX") || return
    cp -a "$backup/program/." "$restored/" || return
    normalize_release_permissions "$restored" || return
    displace_program || return
    mv -- "$restored" "$ROOT" || return
  else
    displace_program || return
  fi
  if [[ -f "$backup/rental.env" ]]; then
    install -d -m 700 "$(dirname "$ENV_FILE")" || return
    install -m 600 "$backup/rental.env" "$ENV_FILE" || return
  else
    rm -f -- "$ENV_FILE" || return
  fi
  if [[ -f "$backup/service.unit" ]]; then
    install -m 644 "$backup/service.unit" "$UNIT_FILE" || return
  else
    rm -f -- "$UNIT_FILE" || return
  fi
  # Keep live business state intact. The copied state directory is retained
  # inside the backup for separately reviewed disaster recovery only; a code
  # rollback must never rewind balances, ledger entries, sessions or instance
  # lifecycle history.
  return 0
}

recover_install_failure() {
  local code=$1 recovery_ok=1
  trap - ERR INT TERM
  set +e
  echo "deployment failed (exit=$code); attempting recovery" >&2
  if (( PROGRAM_CHANGED )) && [[ -n "$RECOVERY_BACKUP" ]]; then
    if systemctl stop "$SERVICE.service"; then
      restore_backup "$RECOVERY_BACKUP" && systemctl daemon-reload || recovery_ok=0
    else
      recovery_ok=0
    fi
  fi
  if (( recovery_ok && WAS_ACTIVE )); then
    systemctl start "$SERVICE.service" && wait_healthy || recovery_ok=0
  fi
  if (( recovery_ok )); then
    echo "previous deployment recovered; business state preserved" >&2
  else
    echo "RECOVERY FAILED; inspect service before retrying. backup=$RECOVERY_BACKUP" >&2
  fi
  exit "$code"
}

install_release() {
  package_check
  config_check
  [[ "$SCRIPT_DIR" != "$ROOT" && "$SCRIPT_DIR" != "$ROOT/"* ]] || { echo "install from a separate release directory" >&2; return 1; }
  systemctl is-active --quiet 1cattunnel.service || { echo "1cattunnel.service is not active" >&2; return 1; }
  systemctl is-active --quiet libvirtd.service || { echo "libvirtd.service is not active" >&2; return 1; }
  local staged
  staged=$(mktemp -d "$(dirname "$ROOT")/.1cat-rental-stage.XXXXXX")
  cp -a "$SCRIPT_DIR/." "$staged/"
  normalize_release_permissions "$staged"
  WAS_ACTIVE=0
  systemctl is-active --quiet "$SERVICE.service" && WAS_ACTIVE=1
  trap 'recover_install_failure $?' ERR
  trap 'recover_install_failure 130' INT
  trap 'recover_install_failure 143' TERM
  systemctl stop "$SERVICE.service"
  RECOVERY_BACKUP=$(backup_current)
  # Fresh tree, same-filesystem rename: no stale modules and no recursive delete.
  PROGRAM_CHANGED=1
  displace_program
  mv -- "$staged" "$ROOT"
  chmod 600 "$ENV_FILE"
  install -m 644 "$ROOT/1cat-rental.service" "$UNIT_FILE"
  systemctl daemon-reload
  systemctl start "$SERVICE.service"
  wait_healthy
  trap - ERR INT TERM
  echo "installed and healthy; backup=$RECOVERY_BACKUP"
}

rollback_release() {
  local requested=$1
  validate_backup "$requested"
  guard_account_rollback "$requested" || return
  if [[ -f "$ROOT/core.py" ]] && grep -q 'HEADLESS_VCPU' "$ROOT/core.py" &&
     ! grep -q 'HEADLESS_VCPU' "$requested/program/core.py"; then
    if ! python3 - "$DATA/state/core.sqlite3" <<'PY'
import sqlite3,sys
db=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)
columns={r[1] for r in db.execute('PRAGMA table_info(instances)')}
sys.exit(1 if 'mode' in columns and db.execute("SELECT 1 FROM instances WHERE mode='headless' AND state!='deleted' LIMIT 1").fetchone() else 0)
PY
    then
      echo 'headless instances exist; this backup cannot schedule or meter them. Use ui-rollback or a headless-compatible release.' >&2
      return 1
    fi
  fi
  # A pre-migration controller/env would point at stale source disks. Never
  # treat an HTTP health check as proof that such a rollback is safe.
  if [[ -f "$ENV_FILE" ]] && grep -q '^RENTAL_SYSTEM_ROOT=/mnt/nvme1/1cat-rental-system$' "$ENV_FILE"; then
    if ! grep -q '^RENTAL_SYSTEM_ROOT=/mnt/nvme1/1cat-rental-system$' "$requested/rental.env" ||
       ! grep -q 'self.system_root' "$requested/program/backend.py"; then
      echo 'storage has migrated; this backup cannot safely restore the controller. Use ui-rollback or reverse-migrate the live disks first.' >&2
      return 1
    fi
  fi
  WAS_ACTIVE=0
  systemctl is-active --quiet "$SERVICE.service" && WAS_ACTIVE=1
  trap 'recover_install_failure $?' ERR
  systemctl stop "$SERVICE.service"
  RECOVERY_BACKUP=$(backup_current)
  PROGRAM_CHANGED=1
  restore_backup "$requested"
  systemctl daemon-reload
  if (( WAS_ACTIVE )); then
    systemctl start "$SERVICE.service"
    wait_healthy
  fi
  trap - ERR INT TERM
  echo "rollback healthy; roll-forward-backup=$RECOVERY_BACKUP"
}

rollback_ui() {
  local requested=$1 staged retained
  validate_backup "$requested"
  [[ -f "$requested/program/public/rental/index.html" ]] || { echo 'backup UI missing' >&2; return 1; }
  staged=$(mktemp -d "$(dirname "$ROOT")/.1cat-rental-ui-stage.XXXXXX")
  cp -a "$requested/program/public/." "$staged/"
  chown -R root:root "$staged"
  find "$staged" -type d -exec chmod 755 {} +
  find "$staged" -type f -exec chmod 644 {} +
  retained=$(mktemp -d "$(dirname "$ROOT")/.1cat-rental-ui-previous.XXXXXX")
  rmdir "$retained"
  mv -- "$ROOT/public" "$retained"
  if ! mv -- "$staged" "$ROOT/public"; then
    mv -- "$retained" "$ROOT/public"
    return 1
  fi
  echo "UI restored; previous UI retained=$retained; disks, wallets and controller unchanged"
}

status() {
  systemctl --no-pager --full status "$SERVICE.service" || true
  curl --fail --silent --max-time 3 http://127.0.0.1:8765/health || true
  echo
}

main() {
  need_root
  local action=${1:-}
  case "$action" in
    install|backup|rollback|ui-rollback|bootstrap-admin|set-price) lock_deployment ;;
  esac
  case "$action" in
    check) package_check; config_check; echo "release and configuration checks passed" ;;
    backup) package_check >/dev/null; backup_release ;;
    install) install_release ;;
    bootstrap-admin) [[ $# == 2 ]] || { usage; exit 2; }; python3 "$ROOT/adminctl.py" --root "$DATA/state" create-admin --name "$2" ;;
    set-price) [[ $# == 3 ]] || { usage; exit 2; }; python3 "$ROOT/adminctl.py" --root "$DATA/state" set-price --admin "$2" --cny-per-hour "$3" ;;
    rollback) [[ $# == 2 ]] || { usage; exit 2; }; rollback_release "$2" ;;
    ui-rollback) [[ $# == 2 ]] || { usage; exit 2; }; rollback_ui "$2" ;;
    status) status ;;
    *) usage; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
