#!/usr/bin/env python3
"""HTTP/static gateway for the Gaudi rental platform.

The browser receives an opaque session cookie. Host credentials and libvirt
details are never exposed to the client.
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import socketserver
import threading
import time
import urllib.parse
from http import cookies
from datetime import datetime, timedelta
from pathlib import Path

from backend import LibvirtBackend, SimulationBackend
from recharge_codes import RedemptionRateLimited
from gpu_plans import gpu_count
from remote_backend import BackendRouter, NodeUnavailable, load_nodes, LOCAL_NODE
from host_metrics import HostMetrics
from core import (
    Core,
    FIXED_MEMORY_GB,
    FIXED_MEMORY_MB,
    FIXED_VCPU,
    MAX_DATA_DISK_GIB,
    SYSTEM_DISK_GIB,
    HEADLESS_VCPU, HEADLESS_MEMORY_MB, HEADLESS_RATE_CENTS,
)


ROOT = Path(os.environ.get("RENTAL_ROOT", "/var/lib/1cat-rental"))
STATIC = Path(os.environ.get("RENTAL_STATIC_ROOT", "/opt/1cat-rental/public"))
PORT = int(os.environ.get("RENTAL_PORT", "8765"))
SIMULATION = os.environ.get("RENTAL_SIMULATION", "0") == "1"
DEFAULT_BDFS = [
    "0000:19:00.0", "0000:1a:00.0", "0000:43:00.0", "0000:44:00.0",
    "0000:b3:00.0", "0000:b4:00.0", "0000:cc:00.0", "0000:cd:00.0",
]
CONFIGURED_BDFS = [
    value.strip()
    for value in os.environ.get("RENTAL_BDFS", ",".join(DEFAULT_BDFS)).split(",")
    if value.strip()
]
CONFIG = {
    "enabled": os.environ.get("RENTAL_IMAGE_ENABLED", "0") == "1",
    "image": os.environ.get("RENTAL_BASE_IMAGE", str(ROOT / "images" / "gaudi-ubuntu24.04.qcow2")),
    "network": os.environ.get("RENTAL_LIBVIRT_NETWORK", "1cat-rental"),
    "bdfs": CONFIGURED_BDFS,
    "data_root": os.environ.get("RENTAL_DATA_ROOT", "/mnt/nvme1/1cat-rental-data"),
    "system_root": os.environ.get("RENTAL_SYSTEM_ROOT", str(ROOT)),
    "enforce_separate_data_device": not SIMULATION,
}
SLOT_COUNT = len(CONFIG["bdfs"])


class Forwarder:
    def __init__(self, port: int):
        self.port = port
        self.target: tuple[str, int] | None = None
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name=f"rental-forward-{port}")
        self.server: socket.socket | None = None
        self.connections: set[tuple[socket.socket, socket.socket]] = set()

    def start(self):
        self.thread.start()

    def set_target(self, target: tuple[str, int] | None):
        with self.lock:
            changed = self.target != target
            self.target = target
            if changed:
                # Reassigned GPU/port must never retain a previous tenant's SSH.
                for pair in tuple(self.connections):
                    self.close_pair(pair)

    @staticmethod
    def close_pair(pair):
        for sock in pair:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            sock.close()

    def run(self):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", self.port))
        self.server.listen(64)
        self.server.settimeout(1)
        while not self.closed.is_set():
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def handle(self, client: socket.socket):
        with self.lock:
            target = self.target
        if target is None:
            client.close()
            return
        try:
            upstream = socket.create_connection(target, timeout=5)
            # Five seconds is a CONNECT deadline, not an idle session limit.
            upstream.settimeout(None)
            client.settimeout(None)
            upstream.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            client.close()
            return
        pair = (client, upstream)
        with self.lock:
            if self.closed.is_set() or self.target != target:
                self.close_pair(pair)
                return
            self.connections.add(pair)
        remaining = [2]
        def pipe(src, dst):
            try:
                while data := src.recv(65536):
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                # EOF is directional. Continue carrying the opposite direction
                # until it too closes; then actually release both descriptors.
                try: dst.shutdown(socket.SHUT_WR)
                except OSError: pass
                with self.lock:
                    remaining[0] -= 1
                    if remaining[0] == 0:
                        self.connections.discard(pair)
                        self.close_pair(pair)
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    def close(self):
        self.closed.set()
        self.set_target(None)
        if self.server:
            self.server.close()


class Service:
    def __init__(self, start_background=True):
        self.lock = threading.RLock()
        self.rate_cents_per_hour = int(os.environ.get("RENTAL_RATE_CENTS_PER_HOUR", "0"))
        self.headless_count = int(os.environ.get("RENTAL_HEADLESS_COUNT", "16"))
        # An empty value means a controller-only host, not a G2-002 executor.
        self.local_node_id = os.environ.get('RENTAL_LOCAL_NODE_ID', LOCAL_NODE).strip() or None
        if self.local_node_id and SLOT_COUNT != 8:
            raise RuntimeError("exactly eight Gaudi2 PCI addresses must be configured for a local compute node")
        if self.local_node_id and not SIMULATION and socket.gethostname() != self.local_node_id:
            raise RuntimeError('local compute identity does not match this host; use controller-only mode')
        self.node_configs = load_nodes(os.environ.get('RENTAL_NODES_CONFIG'), self.local_node_id)
        self.core = Core(
            ROOT / "state",
            memory_budget=int(os.environ.get("RENTAL_MEMORY_BUDGET_MB", "500000")),
            cpu_budget=int(os.environ.get("RENTAL_CPU_BUDGET", "128")),
            system_disk_budget=int(os.environ.get("RENTAL_SYSTEM_DISK_BUDGET_GIB", "700")),
            data_disk_budget=int(os.environ.get("RENTAL_DATA_DISK_BUDGET_GIB", "3300")),
            storage_pool_budget=int(os.environ.get("RENTAL_STORAGE_POOL_BUDGET_GIB", "0")),
            # Default budget for remote nodes is eight; there is no local pool
            # at all when local_node_id is None (even with no BDFs configured).
            slot_count=SLOT_COUNT if self.local_node_id else 8,
            rate_cents_per_hour=self.rate_cents_per_hour,
            headless_count=self.headless_count,
            nodes={node['id']:node.get('budget',{}) for node in self.node_configs},
            local_node_id=self.local_node_id,
        )
        local_backend = (SimulationBackend() if SIMULATION else LibvirtBackend(ROOT, CONFIG)) if self.local_node_id else None
        self.backend = (BackendRouter(local_backend, self.node_configs, self.local_node_id)
                        if self.node_configs or self.local_node_id is None else local_backend)
        self.node_status = {}
        self.meta_path = ROOT / "state" / "metadata.json"
        self.secret_path = ROOT / "state" / "instance-secrets.json"
        self.public_host = os.environ.get("RENTAL_PUBLIC_HOST", "dx.1catai.com")
        self.meta = self.load_json(self.meta_path, {})
        self.secrets = self.load_json(self.secret_path, {})
        self.workers: set[int] = set()
        self.forwarded: set[int] = set()
        self.storage_cache = (0.0, {})
        self.local_online_cache = (0.0, False)
        self.host_metrics = HostMetrics() if self.local_node_id and not SIMULATION else None
        self.power_node_root = Path(os.environ.get('RENTAL_POWER_NODE_ROOT', '/var/lib/power-meter-nodes'))
        self.remote_power_metrics = {}
        admin_name = os.environ.get("RENTAL_ADMIN_USER")
        admin_password = os.environ.get("RENTAL_ADMIN_PASSWORD")
        if admin_name and admin_password:
            self.core.ensure_admin(admin_name, admin_password)
        self.forwarders = ({slot: Forwarder(2220 + slot) for slot in range(1, SLOT_COUNT + self.headless_count + 1)}
                           if self.local_node_id else {})
        self.closed = threading.Event()
        self.maintenance = threading.Thread(target=self.maintenance_loop, daemon=True, name="rental-maintenance")
        if start_background:
            for forwarder in self.forwarders.values():
                forwarder.start()
            self.maintenance.start()

    def close(self):
        self.closed.set()
        for forwarder in self.forwarders.values():
            forwarder.close()

    @staticmethod
    def load_json(path: Path, default):
        try: return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError): return default

    def save_json(self, path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".tmp-{os.getpid()}")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temp, path)
        os.chmod(path, 0o600)

    @staticmethod
    def cookie(token):
        cookie = cookies.SimpleCookie()
        cookie["rental_session"] = token
        cookie["rental_session"]["path"] = "/"
        cookie["rental_session"]["httponly"] = True
        cookie["rental_session"]["samesite"] = "Lax"
        return cookie.output(header="").strip()

    def register(self, name, password):
        self.core.register(name, password)
        return self.core.login(name, password)

    def login(self, name, password):
        return self.core.login(name, password)

    def owner(self, handler, create=False):
        jar = cookies.SimpleCookie(handler.headers.get("Cookie", ""))
        token = jar.get("rental_session")
        if token:
            try: return self.core.authenticate(token.value), None
            except PermissionError: pass
        if not create: raise PermissionError("login required")
        if os.environ.get("RENTAL_ALLOW_ANONYMOUS", "0") != "1":
            raise PermissionError("registration or login required")
        owner = "guest-" + secrets.token_hex(8)
        invite = self.core.create_invite()
        password = secrets.token_urlsafe(18)
        self.core.register(owner, password, invite)
        token = self.core.login(owner, password)
        return owner, self.cookie(token)

    def maintenance_loop(self):
        while not self.closed.is_set():
            try:
                self.reconcile_nodes()
                self.reconcile_runtime()
                self.core.bill_running()
                if not (ROOT / 'state' / 'storage-maintenance').exists():
                    self.core.reap_idle()
                self.refresh_workers()
            except Exception as exc:
                print(f"maintenance failed: {type(exc).__name__}: {exc}", flush=True)
            self.closed.wait(5)

    @staticmethod
    def backend_instance(row):
        result = {"id": str(row["id"]), "slot": row["slot"], "vcpu": row["cpu"],
                "gpu_count": row.get('gpu_count', 1),
                "memory_mb": row["ram"],
                "data_disk": row["data"] + (row.get('gift_data_disk_gib', 0) if row.get('gpu_count') == 8 or row.get('eight_card_gift_disk') else 0),
                "node_id": row.get('node_id',LOCAL_NODE), "generation": row.get('generation',0),
                "mode": row.get("mode", "gpu"), "endpoint": row.get("endpoint", row["slot"])}
        if row.get('eight_card_gift_disk') and row.get('gpu_count') != 8:
            result['eight_card_gift_disk'] = True
        return result

    def reconcile_nodes(self):
        if not isinstance(self.backend, BackendRouter):
            return
        rows = self.core.active_instances()
        # Do not let a failed remote node delay local billing or other nodes.
        def refresh(node_id, remote):
            wanted = [self.backend_instance(row) for row in rows if row['node_id'] == node_id
                      and (row['state'] == 'running' or row['desired_action'] == 'start')]
            snapshot = remote.heartbeat(wanted)
            if snapshot:
                for row in rows:
                    if row['node_id'] != node_id:
                        continue
                    observed = snapshot.get('instances',{}).get(str(row['id']),{})
                    if observed.get('generation') == row['generation'] and observed.get('lastRunningAt'):
                        self.core.confirm_remote_runtime(row['id'],row['generation'],observed['lastRunningAt'])
                    if (observed.get('generation') == row['generation'] and observed.get('state') == 'running'
                            and observed.get('sshReady') is False):
                        # A node process restart loses its in-memory proxy targets,
                        # not the VM. Retry only connectivity, without changing billing.
                        with self.lock: self.forwarded.discard(row['id'])
                    if (row['state'] == 'running' and observed.get('generation') == row['generation']
                            and observed.get('state') in ('off','absent')
                            and self.core.observe_poweroff(row['id'],generation=row['generation'])):
                        with self.lock: self.forwarded.discard(row['id'])
            with self.lock:
                self.node_status[node_id] = False
        for node_id, remote in self.backend.remotes.items():
            with self.lock:
                if self.node_status.get(node_id):
                    continue
                self.node_status[node_id] = True
            def run(node_id=node_id,remote=remote):
                try: refresh(node_id,remote)
                finally:
                    with self.lock: self.node_status[node_id] = False
            threading.Thread(target=run,daemon=True,name=f'node-status-{node_id}').start()

    def node_online(self,node_id):
        if node_id == self.local_node_id:
            if SIMULATION:
                return True
            with self.lock:
                if time.monotonic() - self.local_online_cache[0] > 5:
                    backend = self.backend.local if isinstance(self.backend, BackendRouter) else self.backend
                    self.local_online_cache = (time.monotonic(), backend.online())
                return self.local_online_cache[1]
        if node_id not in self.core.nodes or not isinstance(self.backend, BackendRouter):
            return False
        remote = self.backend.for_node(node_id)
        return remote.config.get('enabled') is True and bool(remote.status())

    def connect(self,row,address):
        # Remote nodes own their SSH proxy; no customer traffic crosses the controller.
        if row.get('node_id',LOCAL_NODE) == self.local_node_id:
            self.forwarders[int(row['endpoint'])].set_target((address,22))
        with self.lock:
            self.forwarded.add(int(row['id']))

    def disconnect(self, row):
        endpoint = row.get('endpoint') or row.get('slot')
        if row.get('node_id',LOCAL_NODE) == self.local_node_id and endpoint in self.forwarders:
            self.forwarders[endpoint].set_target(None)
        with self.lock:
            self.forwarded.discard(int(row['id']))

    def reconcile_runtime(self):
        for row in self.core.active_instances():
            if row.get('node_id',LOCAL_NODE) != self.local_node_id:
                continue  # Bounded batch observations, never N blocking SSH probes here.
            ident = int(row['id'])
            with self.lock:
                if row['state'] != 'running' or ident in self.workers:
                    continue
            if self.backend.state(self.backend_instance(row)) in ('off', 'absent'):
                self.core.observe_poweroff(ident)
                self.disconnect(row)

    def default_node(self):
        preferred = self.core.placement_policy()['preferredNode']
        return preferred if preferred in self.core.nodes else next(iter(self.core.nodes))

    def placement_observations(self):
        observations = {}
        for ident in self.core.nodes:
            online = self.node_online(ident)
            observations[ident] = {'online': online, 'imageReady': online and self.image_ready(ident),
                                   'storage': self.storage_status(ident)}
        return observations

    def host_telemetry(self, node_id):
        if node_id == self.local_node_id:
            if self.host_metrics is None:
                return {'status':'partial','observedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
            return self.host_metrics.snapshot()
        if node_id not in self.core.nodes or not isinstance(self.backend, BackendRouter):
            return {'status':'unavailable','observedAt':None}
        telemetry = dict(self.backend.for_node(node_id).status().get('telemetry') or
                         {'status':'unavailable','observedAt':None})
        # CPU/memory need the running node agent. BMC electricity does not:
        # the always-on controller samples it over the private management LAN.
        data_dir = self.power_node_root / node_id
        if not SIMULATION and (data_dir / 'state.json').is_file():
            with self.lock:
                if node_id not in self.remote_power_metrics:
                    self.remote_power_metrics[node_id] = HostMetrics(
                        power_state=data_dir / 'state.json',
                        power_history=data_dir / 'history',
                        power_config=Path('/etc/power-meter-nodes') / f'{node_id}.conf',
                    )
                meter = self.remote_power_metrics[node_id]
            telemetry['power'] = meter.power_snapshot()
        return telemetry

    def admin_nodes(self, owner):
        if not self.core.is_admin(owner):
            raise PermissionError('admin required')
        return {'nodes':[{'id':node_id,'online':self.node_online(node_id),
                          'telemetry':self.host_telemetry(node_id)}
                         for node_id in self.core.nodes],
                'updatedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}

    def storage_status(self,node_id=None):
        node_id = node_id or self.default_node()
        metrics = self.core.metrics(node_id)['used']
        if node_id != self.local_node_id:
            remote = self.backend.for_node(node_id)
            budget = self.core.nodes[node_id]
            value = dict(remote.status().get('storage',{'totalGiB':0,'usedGiB':0,'freeGiB':0,'safetyGiB':128,'lowSpace':True,'mode':'节点未连接'}))
            value.update(ready=remote.ready(), reservedGiB=metrics['systemDiskGiB']+metrics['dataDiskGiB'],
                         budgetGiB=budget['storage_pool_budget'] or budget['system_disk_budget']+budget['data_disk_budget'])
            return value
        with self.lock:
            if time.monotonic() - self.storage_cache[0] > 5:
                path = ROOT if SIMULATION else Path(CONFIG['data_root'])
                try:
                    usage = shutil.disk_usage(path)
                except OSError:
                    return {'totalGiB': 0, 'usedGiB': 0, 'freeGiB': 0, 'safetyGiB': 128,
                            'reservedGiB': metrics['systemDiskGiB'] + metrics['dataDiskGiB'],
                            'budgetGiB': self.core.storage_pool_budget or self.core.system_disk_budget + self.core.data_disk_budget,
                            'lowSpace': True, 'ready': False, 'mode': '存储暂不可用'}
                gib = 1024 ** 3
                value = {'totalGiB': round(usage.total / gib, 1),
                         'usedGiB': round(usage.used / gib, 1),
                         'freeGiB': round(usage.free / gib, 1),
                         'safetyGiB': 0 if SIMULATION else int(os.environ.get('RENTAL_STORAGE_SAFETY_GIB', '128')),
                         'mode': '共享模板 · 稀疏增量盘', 'ready': self.image_ready(node_id)}
                self.storage_cache = (time.monotonic(), value)
            value = dict(self.storage_cache[1])
        value.update({'reservedGiB': metrics['systemDiskGiB'] + metrics['dataDiskGiB'],
                      'budgetGiB': self.core.storage_pool_budget or self.core.system_disk_budget + self.core.data_disk_budget})
        value['lowSpace'] = value['freeGiB'] < value['safetyGiB']
        return value

    def check_mutation(self, requires_storage=False, node_id=LOCAL_NODE):
        if (ROOT / 'state' / 'storage-maintenance').exists():
            raise RuntimeError('存储正在维护迁移，现有实例继续运行，请稍后再操作')
        if requires_storage and (not self.node_online(node_id) or not self.image_ready(node_id)):
            raise RuntimeError('所属节点或镜像尚未就绪，暂不能创建或启动')
        if requires_storage and self.storage_status(node_id)['lowSpace']:
            raise RuntimeError('存储可用空间已触及安全余量，暂不能创建或启动，请联系管理员')

    def image_ready(self,node_id=None):
        node_id = node_id or self.default_node()
        try: return self.backend.ready(node_id) if isinstance(self.backend,BackendRouter) else self.backend.ready()
        except (OSError, json.JSONDecodeError, RuntimeError): return False

    def tunnel_ports(self,node_id=None):
        node_id = node_id or self.default_node()
        if node_id != self.local_node_id:
            config = next((node for node in self.node_configs if node['id'] == node_id),{})
            return {int(key):int(value) for key,value in config.get('public_ports',{}).items()}
        # A fixed port map can be supplied after 1CatTunnel publishes its
        # endpoints. Until then the instance is still usable through the
        # host-side proxy, but the UI does not claim a public SSH endpoint.
        result = {}
        for slot in range(1, SLOT_COUNT + self.headless_count + 1):
            value = os.environ.get(f"RENTAL_PUBLIC_PORT_{slot}")
            if value and value.isdigit() and 1 <= int(value) <= 65535:
                result[slot] = int(value)
        return result

    def refresh_workers(self):
        for row in self.core.pending():
            if not self.node_online(row.get('node_id',LOCAL_NODE)):
                continue
            instance_id = int(row["id"])
            with self.lock:
                if instance_id in self.workers: continue
                self.workers.add(instance_id)
            threading.Thread(target=self.worker, args=(instance_id,), daemon=True, name=f"rental-{instance_id}").start()
        # A process restart leaves the durable VM in `running` while the
        # in-memory localhost forwarder targets are empty. Re-probe the guest
        # once and restore the tunnel without changing billing state.
        for row in self.core.active_instances():
            if not self.node_online(row.get('node_id',LOCAL_NODE)):
                continue
            if row["state"] != "running":
                continue
            instance_id = int(row["id"])
            with self.lock:
                if instance_id in self.workers or instance_id in self.forwarded:
                    continue
                self.workers.add(instance_id)
            threading.Thread(target=self.restore_forwarder, args=(instance_id,), daemon=True, name=f"rental-restore-{instance_id}").start()

    def restore_forwarder(self, instance_id):
        try:
            row = next((item for item in self.core.active_instances() if int(item["id"]) == instance_id), None)
            if not row or row["state"] != "running":
                return
            instance = self.backend_instance(row)
            backend_state = self.backend.state(instance)
            if backend_state in ("off", "absent"):
                self.core.observe_poweroff(instance_id)
                if self.backend.recovered(instance):
                    # The controller restarted after the VM disappeared. Do
                    # not keep charging a tenant for a non-existent guest;
                    # release the slot but preserve its disks for restart.
                    self.core.mark_off(instance_id)
                return
            if backend_state != "running":
                return
            deadline = time.time() + 30
            address = None
            while time.time() < deadline and not address:
                address = self.backend.healthy(instance)
                if not address:
                    time.sleep(2)
            if not address:
                # SSH may be temporarily unavailable while tenant compute is
                # healthy. Retry, and surface connectivity separately from VM
                # power state; never shut down a workload merely for SSH delay.
                print(f'forwarder {instance_id}: guest SSH not ready; will retry', flush=True)
                return
            self.connect(row,address)
        except Exception as exc:
            print(f"forwarder restore {instance_id} failed: {type(exc).__name__}: {exc}", flush=True)
        finally:
            with self.lock:
                self.workers.discard(instance_id)

    def find(self, instance_id):
        rows = self.core.pending() + self.core.list_instances("__not-owner__")
        # Core intentionally has owner-filtered list APIs; inspect only the
        # pending queue for the worker. The worker never accepts user IDs.
        for row in self.core.pending():
            if int(row["id"]) == instance_id: return row
        return None

    def reconcile_cancelled_start(self, instance_id, instance, row):
        """Finish a stop/delete requested while the start worker was booting."""
        self.disconnect(row)
        if self.backend.state(instance) == "running":
            self.backend.stop(instance)
        self.wait_stopped(instance)
        if not self.wait_recovered(instance):
            raise RuntimeError("GPU recovery check failed after cancelled start")
        deleting = row.get("state") == "deleting" or row.get("desired_action") == "delete"
        if deleting:
            self.backend.release(instance)
        self.core.mark_off(instance_id)
        if deleting:
            self.forget(instance_id)

    def cleanup_failed_worker(self, instance_id, instance, reason):
        """Never leave a half-started guest consuming a GPU after an error."""
        self.disconnect(instance)
        try:
            if self.backend.state(instance) == "running":
                self.backend.stop(instance)
            self.wait_stopped(instance)
            if not self.wait_recovered(instance):
                self.core.mark_error(instance_id, reason)
                return
            latest = next((item for item in self.core.pending() if int(item["id"]) == instance_id), None)
            if latest and latest["desired_action"] == "delete":
                self.backend.release(instance)
                self.core.mark_off(instance_id)
                self.forget(instance_id)
            elif latest and latest["desired_action"] == "stop":
                self.core.mark_off(instance_id)
            else:
                self.core.mark_failed(instance_id, reason)
        except Exception as cleanup_exc:
            print(f"worker cleanup {instance_id} failed: {type(cleanup_exc).__name__}: {cleanup_exc}", flush=True)
            try:
                self.core.mark_error(instance_id, f"{reason}; cleanup: {cleanup_exc}")
            except Exception:
                pass

    def wait_stopped(self, instance):
        deadline = time.monotonic() + (420 if instance.get('gpu_count',1)==8 else 300 if instance.get('gpu_count',1)==4 else 120)
        while time.monotonic() < deadline and self.backend.state(instance) not in ('off', 'absent'):
            time.sleep(3)
        if self.backend.state(instance) in ('running', 'stopping'):
            print(f"instance {instance['id']}: graceful shutdown timed out; forcing power off", flush=True)
            self.backend.force_off(instance)

    def wait_recovered(self, instance):
        # Stop compute billing once QEMU is off, but retain every GPU until
        # all physical devices pass their host checks. Four rebinds take minutes.
        if self.backend.state(instance) in ('off', 'absent'):
            self.core.observe_poweroff(int(instance['id']), instance.get('generation'))
        deadline = time.monotonic() + (900 if instance.get('gpu_count',1)==8 else 600 if instance.get('gpu_count',1)==4 else 0)
        while True:
            if self.backend.recovered(instance):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(5)

    def worker(self, instance_id):
        instance = None
        try:
            row = next((r for r in self.core.pending() if int(r["id"]) == instance_id), None)
            if not row: return
            instance = self.backend_instance(row)
            password = self.secrets.get(str(instance_id))
            desired = row["desired_action"]
            if desired in ("create", "start"):
                if not password: raise RuntimeError("instance secret is missing")
                self.backend.prepare(instance, password)
                self.backend.start(instance)
                deadline = time.time() + (600 if instance.get('gpu_count',1)==8 else 420 if instance.get('gpu_count',1)==4 else 180)
                address = None
                while time.time() < deadline:
                    address = self.backend.healthy(instance)
                    if address: break
                    time.sleep(3)
                if not address: raise RuntimeError("guest health check failed")
                self.connect(row,address)
                latest = next((item for item in self.core.pending() if int(item["id"]) == instance_id), None)
                if not latest or latest["desired_action"] != "start" or latest["state"] != "provisioning":
                    if latest:
                        self.reconcile_cancelled_start(instance_id, instance, latest)
                    return
                try:
                    self.core.mark_running(instance_id)
                except ValueError:
                    latest = next((item for item in self.core.pending() if int(item["id"]) == instance_id), None)
                    if latest and latest["desired_action"] in ("stop", "delete"):
                        self.reconcile_cancelled_start(instance_id, instance, latest)
                        return
                    raise
                with self.lock:
                    self.meta.setdefault(str(instance_id), {})["billableAt"] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                    self.save_json(self.meta_path, self.meta)
            elif desired == "stop":
                self.backend.stop(instance)
                self.wait_stopped(instance)
                self.disconnect(row)
                if not self.wait_recovered(instance): raise RuntimeError("GPU recovery check failed")
                self.core.mark_off(instance_id)
            elif desired == "delete":
                if self.backend.state(instance) == "running": self.backend.stop(instance)
                self.wait_stopped(instance)
                if not self.wait_recovered(instance): raise RuntimeError("GPU recovery check failed")
                self.disconnect(row)
                self.backend.release(instance)
                self.core.mark_off(instance_id)
                self.forget(instance_id)
        except Exception as exc:
            print(f"worker {instance_id} failed: {type(exc).__name__}: {exc}", flush=True)
            if instance is not None:
                self.cleanup_failed_worker(instance_id, instance, str(exc))
            else:
                try: self.core.mark_error(instance_id, str(exc))
                except Exception: pass
        finally:
            with self.lock:
                self.workers.discard(instance_id)

    def forget(self, instance_id):
        """Remove credentials and UI metadata only after durable VM release."""
        ident = str(instance_id)
        with self.lock:
            changed = self.secrets.pop(ident, None) is not None
            changed = self.meta.pop(ident, None) is not None or changed
            if changed:
                self.save_json(self.meta_path, self.meta)
                self.save_json(self.secret_path, self.secrets)

    def state(self, owner, admin_view=False):
        self.refresh_workers()
        rows = self.core.admin_instances(owner) if admin_view else self.core.list_instances(owner)
        costs = self.core.instance_costs(owner, all_customers=admin_view)
        ports = self.tunnel_ports()
        instances = []
        for row in rows:
            ports = self.tunnel_ports(row['node_id'])
            instance_id = int(row["id"])
            mapped = {"provisioning":"creating","running":"running","stopping":"stopping","stopped":"stopped","error":"error","quarantined":"repair_required","deleting":"stopping"}
            current = mapped.get(row["state"], "error")
            if row["state"] == "provisioning" and row["desired_action"] == "start": current = "starting"
            cost = costs.get(instance_id, {})
            release_at = None
            if row['state'] in ('stopped', 'error') and not row['slot']:
                release_at = (datetime.fromisoformat(row['last_activity_at']) + timedelta(hours=48)).isoformat()
            online = self.node_online(row['node_id'])
            connected = instance_id in self.forwarded and current == 'running' and online
            message = row['error'] or {
                'running': '实例运行中' if connected else '实例运行中，SSH 入口正在检查',
                'stopped': '已停止算力计费，磁盘继续保留',
                'starting': '正在准备磁盘、启动系统并检查 SSH' if row['mode'] == 'headless' else '正在准备磁盘、启动系统并检查 GPU 与 SSH',
                'stopping': '正在关闭实例并回收资源',
            }.get(current, '等待调度器处理')
            if not online:
                message = '所属节点暂不可达；保留资源占用，算力计费不超过最后确认运行时间'
            gift_disk = row.get('gift_data_disk_gib', 0)
            if gift_disk:
                message += f' · 数据盘含赠送{gift_disk}GiB（赠送部分不计费）'
            shared = self.backend.shared_status(self.backend_instance(row)) if hasattr(self.backend, 'shared_status') else {'enabled': False}
            if shared.get('enabled'):
                shared_message = ('公共只读盘 /shared 已接入' if shared.get('state') == 'mounted' and current == 'running'
                                  else '公共盘待接入：正常关机后再开机' if current == 'running' and shared.get('state') == 'restart_required'
                                  else '公共只读盘 /shared：下次开机自动接入' if current in ('stopped', 'error')
                                  else '公共只读盘 /shared 正在检查')
                message += ' · ' + shared_message
            instances.append({
                "id": str(instance_id), "name": self.meta.get(str(instance_id), {}).get("name", f"gaudi-{instance_id}"),
                "slot": row["slot"] or 0, "state": current, "vcpu": row["cpu"], "memoryGB": row["ram"] / 1000,
                "mode": row["mode"],
                "gpuCount": row['gpu_count'], "slots": row['slots'],
                "nodeId": row['node_id'], "nodeOnline": online,
                "systemDiskGiB": row["sys"], "dataDiskGiB": row["data"] + gift_disk, "image": "gaudi-ubuntu24.04",
                "billableDataDiskGiB": row["data"], "giftDataDiskGiB": gift_disk,
                "eightCardGiftDisk": bool(row.get('eight_card_gift_disk')),
                "publicHost": self.public_host if row["endpoint"] in ports else None,
                "publicPort": ports.get(row["endpoint"]), "username": "gpu" if current == "running" else None,
                "password": self.secrets.get(str(instance_id)) if current == "running" and not admin_view else None,
                "billableAt": cost.get('billableAt'), "createdAt": row["created_at"],
                "releaseAt": release_at, "connectivity": 'ready' if connected else 'pending',
                "observedAt": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                "rateCentsPerHour": cost.get('rateCentsPerHour') if cost.get('rateCentsPerHour') is not None else self.core.rate_for_mode(row['mode'],row['gpu_count']),
                "computeCents": cost.get('computeCents', 0), "storageCents": row['storage_charged_cents'],
                "storageCnyPerDay": round(row['data'] * self.core.pricing()['extraDataDiskCnyPerGiBDay'], 5),
                "owner": row['owner'] if admin_view else None,
                "message": message,
                "sharedStorage": shared,
            })
        occupied = {(row['node_id'],int(row["slot"])): row for row in self.core.active_slots()}
        slot_states = {
            "provisioning": "creating",
            "running": "running",
            "stopping": "stopping",
            "quarantined": "repair_required",
            "deleting": "stopping",
        }
        slots, nodes = [], []
        headless_available = 0
        for node_id,budget in self.core.nodes.items():
            online = self.node_online(node_id) and self.image_ready(node_id)
            metrics = self.core.metrics(node_id)
            remaining = max(0,min(budget['headless_count']-metrics['active_headless'],
                                  (budget['cpu_budget']-metrics['used']['cpu'])//HEADLESS_VCPU,
                                  (budget['memory_budget']-metrics['used']['memoryMB'])//HEADLESS_MEMORY_MB)) if online else 0
            headless_available += remaining
            node = {'id':node_id,'online':online,'imageReady':self.image_ready(node_id),
                    'availableCpu': max(0,budget['cpu_budget']-metrics['used']['cpu']),
                    'availableMemoryMB': max(0,budget['memory_budget']-metrics['used']['memoryMB']),
                    'storage':self.storage_status(node_id),'headlessAvailable':remaining}
            if admin_view:
                node['telemetry'] = self.host_telemetry(node_id)
            nodes.append(node)
            for slot in range(1,budget['slot_count']+1):
                held = occupied.get((node_id,slot))
                slots.append({'slot':slot,'nodeId':node_id,
                              'state':slot_states.get(held['state'],'error') if held else ('available' if online else 'repair_required'),
                              'instanceId':str(held['id']) if held and (admin_view or any(r['id']==held['id'] for r in rows)) else None,
                              'gpu':f'{node_id} · Gaudi2 · HPU {slot}'})
        pricing = self.core.pricing()
        disk_price = pricing["extraDataDiskCnyPerGiBDay"]
        billing_detail = (
            f"¥{pricing['cardCentsPerHour'] / 100:.2f}/小时含 {pricing['includedCpu']} 核、"
            f"{pricing['includedMemoryGB']} GB 内存和 {pricing['systemDiskGiB']} GiB 系统盘；"
            f"数据盘按 ¥{disk_price:.5f}/GiB/天计费，关机保留期间仍计费"
        )
        image_ready = any(node['online'] and node['imageReady'] for node in nodes)
        manifest = self.load_json(Path(CONFIG['image']).with_suffix('.manifest.json'), {}) if self.local_node_id and not SIMULATION else {}
        storage = self.storage_status()
        maintenance = (ROOT / 'state' / 'storage-maintenance').exists()
        metrics = self.core.metrics()
        return {
            'service': 'maintenance' if maintenance else ('ready' if any(node['online'] and not node['storage']['lowSpace'] for node in nodes) else 'blocked'),
            'serviceMessage': '存储维护迁移中，现有实例不受影响' if maintenance else ('资源池就绪' if image_ready else '镜像或存储尚未通过检查'),
            'account': self.core.profile(owner), 'slots': slots, 'nodes':nodes, 'instances': instances, 'storage': storage,
            'placementPolicy': self.core.placement_policy(),
            'controller': {'mode': 'hybrid' if self.local_node_id else 'controller_only', 'localNodeId': self.local_node_id},
            'headless': {'available': headless_available, 'running': metrics['active_headless'],
                         'capacity': sum(b['headless_count'] for b in self.core.nodes.values()), 'vcpu': HEADLESS_VCPU,
                         'memoryGB': HEADLESS_MEMORY_MB / 1000, 'rateCentsPerHour': HEADLESS_RATE_CENTS},
            'image': {'id': 'gaudi-ubuntu24.04', 'name': 'Gaudi Ubuntu 24.04',
                      'version': str(manifest.get('version') or Path(CONFIG['image']).stem),
                      'ready': image_ready, 'detail': '已验收 · 驱动与计算环境' if image_ready else '等待镜像与存储检查'},
            'limits': {'vcpu': [FIXED_VCPU, FIXED_VCPU], 'memoryGB': [FIXED_MEMORY_GB, FIXED_MEMORY_GB],
                       'dataDiskGiB': [0, MAX_DATA_DISK_GIB], 'systemDiskGiB': SYSTEM_DISK_GIB},
            'billing': {'mode': 'balance_metered', 'currency': 'CNY', 'rateCentsPerHour': self.core.price(),
                        'gpuPlans': pricing['gpuPlans'],
                        'headlessRateCentsPerHour': HEADLESS_RATE_CENTS,
                        'includedCpu': pricing['includedCpu'], 'includedMemoryGB': pricing['includedMemoryGB'],
                        'freeDataDiskGiB': pricing['freeDataDiskGiB'], 'extraDataDiskCnyPerGiBDay': disk_price,
                        'detail': billing_detail},
            'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}

    def order(self, owner, body, idempotency):
        node_id = body.get('nodeId')
        if node_id == 'auto':
            node_id = None
        if node_id is not None and (not isinstance(node_id,str) or node_id not in self.core.nodes):
            raise ValueError('unknown node')
        self.check_mutation()
        raw_name = str(body.get("name") or "").strip()
        if raw_name and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,31}", raw_name):
            raise ValueError("实例名称须为 3–32 位字母、数字、下划线或横线")
        mode = body.get('mode', 'gpu')
        if mode not in ('gpu', 'headless'):
            raise ValueError('mode must be gpu or headless')
        count = gpu_count(body.get('gpuCount', 1))
        cpu = HEADLESS_VCPU if mode == 'headless' else FIXED_VCPU * count
        ram = HEADLESS_MEMORY_MB if mode == 'headless' else 480_000 if count == 8 else FIXED_MEMORY_MB * count
        requested_vcpu = body.get("vcpu", cpu)
        requested_memory = body.get("memoryGB", body.get("memoryGiB", ram / 1000))
        if isinstance(requested_vcpu, bool) or requested_vcpu != cpu:
            raise ValueError(f"vCPU 固定为 {cpu} 核")
        if isinstance(requested_memory, bool) or requested_memory != ram / 1000:
            raise ValueError(f"内存固定为 {ram / 1000:g} GB")
        with self.lock:
            result = self.core.order(owner, {"cpu":cpu,"ram":ram,"sys":SYSTEM_DISK_GIB,"data":body.get("dataDiskGiB", 0),"mode":mode,"gpu_count":count}, idempotency,node_id=node_id,
                                     observations=self.placement_observations())
            ident = str(result["id"])
            self.meta.setdefault(ident, {})["name"] = raw_name or f"gaudi-{ident}"
            self.secrets.setdefault(ident, secrets.token_urlsafe(12))
            self.save_json(self.meta_path,self.meta); self.save_json(self.secret_path,self.secrets)
        return result


SERVICE = None


class Handler(http.server.BaseHTTPRequestHandler):
    STATIC_CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".ico": "image/x-icon",
    }

    def log_message(self, fmt, *args): print(fmt % args, flush=True)
    def json(self, payload, status=200, set_cookie=None):
        raw=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(raw)))
        if set_cookie: self.send_header('Set-Cookie',set_cookie)
        self.end_headers(); self.wfile.write(raw)
    def identity(self): return SERVICE.owner(self)
    def body(self):
        size = int(self.headers.get('Content-Length','0'))
        if not 0 <= size <= 16384: raise ValueError('request too large')
        if self.headers.get('Transfer-Encoding'): raise ValueError('chunked requests not supported')
        if 'application/json' not in self.headers.get('Content-Type',''): raise ValueError('application/json required')
        origin = self.headers.get('Origin')
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host'):
            raise PermissionError('cross-origin mutation denied')
        value = json.loads(self.rfile.read(size) or b'{}')
        if not isinstance(value, dict): raise ValueError('JSON object required')
        return value
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/admin', '/admin/'):
            self.send_response(302)
            self.send_header('Location', '/rental?view=admin' if path.startswith('/admin') else '/rental')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path in ('/health','/healthz'):
            self.json({'ok':True,'service':'1cat-rental'}); return
        if path == '/api/auth/registration-policy':
            self.json({'bonusCents': SERVICE.core.registration_bonus()}); return
        if path == '/api/admin/registration-bonus':
            try:
                owner, _ = self.identity()
                self.json(SERVICE.core.registration_settings(owner))
            except PermissionError as exc:
                self.json({'error':'unauthorized','message':str(exc)},401)
            return
        if path == '/api/auth/me':
            try:
                owner, cookie = self.identity()
                self.json({'account': SERVICE.core.profile(owner)}, set_cookie=cookie)
            except PermissionError as exc:
                self.json({'error':'unauthorized','message':str(exc)},401)
            return
        if path in ('/api/admin/placement-policy', '/api/rental/placement'):
            try:
                owner, _ = self.identity()
                if path == '/api/admin/placement-policy':
                    if not SERVICE.core.is_admin(owner): raise PermissionError('admin required')
                    self.json({'policy': SERVICE.core.placement_policy(), 'nodes': list(SERVICE.core.nodes)})
                else:
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    spec = {'mode': query.get('mode', ['gpu'])[0], 'gpu_count': int(query.get('gpuCount', ['1'])[0]),
                            'data': int(query.get('dataDiskGiB', ['0'])[0])}
                    self.json(SERVICE.core.placement_quote(spec, SERVICE.placement_observations()))
            except PermissionError as exc: self.json({'error': 'unauthorized', 'message': str(exc)}, 401)
            except (ValueError, TypeError) as exc: self.json({'error': 'invalid_request', 'message': str(exc)}, 400)
            return
        if path in ('/api/admin/customers','/api/rental/ledger','/api/admin/ledger','/api/admin/instances','/api/admin/nodes','/api/admin/audit','/api/admin/recharge-codes'):
            try:
                owner, _ = self.identity()
                if path == '/api/admin/recharge-codes':
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    self.json(SERVICE.core.list_recharge_codes(owner,int(query.get('before',['0'])[0]),query.get('status',['all'])[0],query.get('q',[''])[0]))
                elif path == '/api/admin/customers':
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    self.json({'customers': SERVICE.core.customers(owner, query.get('status', ['active'])[0])})
                elif path == '/api/admin/instances':
                    self.json(SERVICE.state(owner, admin_view=True))
                elif path == '/api/admin/nodes':
                    self.json(SERVICE.admin_nodes(owner))
                elif path == '/api/admin/audit':
                    self.json({'events': SERVICE.core.audit(owner)})
                elif path == '/api/admin/ledger':
                    # Scoped statements do not check admin access; authorize the actor first.
                    if not SERVICE.core.is_admin(owner):
                        raise PermissionError('admin required')
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    ledger_owner = query.get('owner', [''])[0]
                    if ledger_owner:
                        entries = SERVICE.core.statement(ledger_owner, all_customers=False)
                    else:
                        entries = SERVICE.core.statement(owner, all_customers=True)
                    self.json({'entries': entries, 'scopeOwner': ledger_owner})
                else:
                    self.json({'entries': SERVICE.core.statement(owner)})
            except PermissionError as exc:
                self.json({'error':'unauthorized','message':str(exc)},401)
            except ValueError as exc:
                self.json({'error':'invalid_request','message':str(exc)},400)
            return
        if path == '/api/rental/state':
            try: owner,cookie=self.identity(); self.json(SERVICE.state(owner),set_cookie=cookie)
            except PermissionError as exc: self.json({'error':'unauthorized','message':str(exc)},401)
            except Exception: self.json({'error':'state_unavailable','message':'暂时无法读取状态，请稍后刷新'},500)
            return
        relative='index.html' if path in ('/','') else ('rental/index.html' if path.rstrip('/')=='/rental' else path.lstrip('/'))
        file=(STATIC/relative).resolve()
        if file.is_file() and STATIC.resolve() in file.parents:
            content_type = self.STATIC_CONTENT_TYPES.get(
                file.suffix.lower(),
                mimetypes.guess_type(file.name)[0] or "application/octet-stream",
            )
            raw = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            # 1cat-tunnel keeps the upstream connection alive.  Without an
            # explicit length, HTTP/1.0 clients have to wait for EOF even after
            # the complete asset has arrived, which makes the page appear hung.
            self.send_header("Content-Length", str(len(raw)))
            self.send_header('Cache-Control', 'no-cache' if file.suffix == '.html' else 'public, max-age=86400')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)
            return
        self.json({'error':'not_found'},404)
    def do_POST(self):
        try:
            path=urllib.parse.urlparse(self.path).path; body=self.body()
            if path == '/api/auth/register':
                name = str(body.get('name') or '').strip()
                password = str(body.get('password') or '')
                token = SERVICE.register(name, password)
                self.json({'ok':True,'account':SERVICE.core.profile(name)},201,set_cookie=SERVICE.cookie(token)); return
            if path == '/api/auth/login':
                name = str(body.get('name') or '').strip()
                token = SERVICE.login(name, str(body.get('password') or ''))
                self.json({'ok':True,'account':SERVICE.core.profile(name)},200,set_cookie=SERVICE.cookie(token)); return
            if path == '/api/auth/logout':
                jar = cookies.SimpleCookie(self.headers.get('Cookie',''))
                if jar.get('rental_session'): SERVICE.core.logout(jar['rental_session'].value)
                self.json({'ok':True},200,set_cookie='rental_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'); return
            owner,cookie=self.identity()
            if path == '/api/admin/recharge-codes':
                result = SERVICE.core.issue_recharge_codes(owner,body.get('cents'),body.get('count',1),body.get('kind'),body.get('boundOwner'),body.get('note',''),self.headers.get('X-Idempotency-Key'))
                self.json(result,200 if result['replayed'] else 201); return
            if path == '/api/rental/redeem-code':
                self.json({'result':SERVICE.core.redeem_recharge_code(owner,body.get('code'))}); return
            code_action = re.fullmatch(r'/api/admin/(recharge-codes|recharge-code-batches)/([a-z0-9]+)/revoke',path)
            if code_action:
                kwargs = {'code_id':int(code_action[2])} if code_action[1]=='recharge-codes' else {'batch_id':code_action[2]}
                self.json(SERVICE.core.revoke_recharge_codes(owner,**kwargs)); return
            if path == '/api/admin/recharge':
                key = self.headers.get('X-Idempotency-Key')
                if not key: raise ValueError('idempotency key required')
                result = SERVICE.core.recharge(owner, str(body.get('user') or body.get('customer') or ''), body.get('cents'), str(body.get('note') or ''), key)
                self.json({'ok':True,'result':result},200,set_cookie=cookie); return
            if path == '/api/admin/price':
                SERVICE.core.set_price(owner, body.get('cents'))
                self.json({'ok':True},200,set_cookie=cookie); return
            if path == '/api/admin/registration-bonus':
                self.json(SERVICE.core.set_registration_bonus(owner, body.get('cents'), body.get('expectedCents'))); return
            if path == '/api/admin/placement-policy':
                policy = SERVICE.core.set_placement_policy(owner, body.get('policy'), body.get('expected'))
                self.json({'policy': policy, 'nodes': list(SERVICE.core.nodes)}); return
            customer_action = re.fullmatch(r'/api/admin/customers/([a-zA-Z0-9][a-zA-Z0-9_-]{2,31})/(delete|restore)', path)
            if customer_action:
                if customer_action[2] == 'delete':
                    result = SERVICE.core.delete_customer(owner, customer_action[1], body.get('confirmName'))
                else:
                    result = SERVICE.core.restore_customer(owner, customer_action[1])
                self.json({'ok': True, 'customer': result}); return
            if path=='/api/rental/order':
                result=SERVICE.order(owner,body,self.headers.get('X-Idempotency-Key') or secrets.token_urlsafe(18)); self.json({'instance':result},202,set_cookie=cookie); return
            plan_change = re.fullmatch(r'/api/rental/instances/([1-9][0-9]*)/gpu-plan', path)
            if plan_change:
                SERVICE.check_mutation()
                if set(body) != {'gpuCount'}:
                    raise ValueError('只接受 gpuCount 配置项')
                result = SERVICE.core.change_gpu_plan(owner, int(plan_change[1]), body['gpuCount'])
                self.json({'ok': True, 'instance': result}, 200, set_cookie=cookie); return
            parts=[urllib.parse.unquote(x) for x in path.split('/') if x]
            if len(parts)==5 and parts[:3] in (['api','rental','instances'], ['api','admin','instances']):
                SERVICE.check_mutation()
                mode = body.get('mode')
                operation = SERVICE.core.admin_action if parts[1] == 'admin' else SERVICE.core.action
                if parts[4] == 'start':
                    rows = SERVICE.core.admin_instances(owner) if parts[1] == 'admin' else SERVICE.core.list_instances(owner)
                    item = next((r for r in rows if r['id'] == int(parts[3])), None)
                    if item is None: raise KeyError('instance not found')
                    SERVICE.check_mutation(requires_storage=True,node_id=item['node_id'])
                    if not SERVICE.image_ready(item['node_id']):
                        raise RuntimeError('所属节点镜像尚未就绪，暂不能开机')
                    selected_mode = mode if mode is not None else item['mode']
                    if selected_mode == 'gpu' and SERVICE.core.price() <= 0:
                        raise RuntimeError('GPU 计费价格未就绪，暂不能开机')
                    if selected_mode == 'headless' and not SIMULATION:
                        ports = SERVICE.tunnel_ports(item['node_id'])
                        if any(n not in ports for n in range(9, 9 + SERVICE.core.nodes[item['node_id']]['headless_count'])):
                            raise RuntimeError('无头 SSH 入口尚未配置完成，请联系管理员')
                result=operation(owner,int(parts[3]),parts[4],mode=mode); SERVICE.refresh_workers(); self.json({'ok':True,'instance':result},202,set_cookie=cookie); return
            self.json({'error':'not_found'},404)
        except RedemptionRateLimited as exc: self.json({'error':'rate_limited','message':str(exc),'retryAfter':exc.retry_after},429)
        except PermissionError as exc: self.json({'error':'unauthorized','message':str(exc)},401)
        except (ValueError,RuntimeError) as exc: self.json({'error':'operation_rejected','message':str(exc)},409)
        except KeyError: self.json({'error':'not_found'},404)
        except Exception: self.json({'error':'internal_error'},500)


class ReusableServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads=True; allow_reuse_address=True


if __name__ == '__main__':
    SERVICE = Service()
    print(f'1Cat rental HTTP listening on 127.0.0.1:{PORT} simulation={SIMULATION}',flush=True)
    ReusableServer(('127.0.0.1',PORT),Handler).serve_forever()
