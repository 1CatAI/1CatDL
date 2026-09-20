#!/usr/bin/env python3
"""Single-host executor behind a restricted SSH command and a Unix socket.

The central controller owns customers/money. This service owns only durable VM
reservations, generation fences, bounded run leases and host-local SSH proxies.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from backend import LibvirtBackend, command

SOCKET = '/run/1cat-node/agent.sock'
MAX_MESSAGE = 131072
LEASE_SECONDS = 75


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class IsolatedNodeBackend(LibvirtBackend):
    def ready(self):
        try:
            command(['systemctl','is-active','--quiet','1cat-node-network-guard.service'])
            command(['nft','list','table','inet','onecat_node'])
            return super().ready()
        except Exception:
            return False

    def preflight(self, slot):
        bdf = self.config['bdfs'][int(slot)-1]
        group = Path('/sys/bus/pci/devices')/bdf/'iommu_group'
        if not group.exists() or sorted(path.name for path in (group/'devices').iterdir()) != [bdf]:
            raise RuntimeError('GPU no longer has an independent IOMMU group')
        return super().preflight(slot)


class NodeAgent:
    def __init__(self, config, backend=None, forwarders=None):
        self.config = config
        self.backend = backend or IsolatedNodeBackend(config['control_root'], config)
        self.directory = Path(config['state_root'])
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / 'reservations.json'
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.lock = threading.RLock()
        self.instance_locks = {}
        self.forwarders = forwarders or {}
        self.forwarder_owners = {}

    def save(self):
        # Call under lock. The directory and temp are root-only; no password is stored.
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(self.records, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)

    def validate(self, instance):
        if not isinstance(instance, dict) or instance.get('node_id') != self.config['node_id']:
            raise ValueError('wrong node')
        if set(instance) != {'id','node_id','generation','slot','endpoint','mode','vcpu','memory_mb','data_disk'}:
            raise ValueError('invalid instance fields')
        if not isinstance(instance['id'], str) or not re.fullmatch(r'[1-9][0-9]{0,11}', instance['id']):
            raise ValueError('invalid instance id')
        for key in ('generation','vcpu','memory_mb','data_disk'):
            if type(instance[key]) is not int:
                raise ValueError('invalid integer')
        if instance['generation'] < 0 or not 0 <= instance['data_disk'] <= 200:
            raise ValueError('invalid generation or disk')
        mode = instance['mode']
        if mode not in ('gpu', 'headless') or (instance['vcpu'],instance['memory_mb']) != ((16,62500) if mode == 'gpu' else (2,4000)):
            raise ValueError('invalid fixed resources')
        slot, endpoint = instance['slot'], instance['endpoint']
        # Unallocated, never-started instances can be safely deleted.
        if endpoint is None:
            if slot is not None:
                raise ValueError('invalid unallocated slot')
        elif type(endpoint) is not int or (mode == 'gpu' and (type(slot) is not int or not 1 <= slot <= 8 or slot != endpoint)) or (mode == 'headless' and (slot is not None or not 9 <= endpoint <= 8 + self.config.get('headless_count',16))):
            raise ValueError('invalid slot/endpoint')
        return dict(instance)

    def reserve(self, instance):
        ident = instance['id']
        with self.lock:
            previous = self.records.get(ident)
            if previous and instance['generation'] < previous['instance']['generation']:
                raise ValueError('stale generation')
            if previous and instance['generation'] == previous['instance']['generation']:
                if instance != previous['instance'] or previous.get('closed'):
                    raise ValueError('generation already closed or changed')
                return previous
            if instance['generation'] < 1 or instance['endpoint'] is None:
                raise ValueError('start requires allocation and generation')
            if previous:
                old = previous['instance']
                if self.backend.state(old) not in ('off','absent') or (previous.get('held') and not self.backend.recovered(old)):
                    raise RuntimeError('previous generation not recovered')
            held = [record['instance'] for key,record in self.records.items() if key != ident and record.get('held')]
            if any(item['endpoint'] == instance['endpoint'] or (instance['slot'] and item['slot'] == instance['slot']) for item in held):
                raise RuntimeError('node resource already reserved')
            if sum(item['vcpu'] for item in held) + instance['vcpu'] > self.config.get('cpu_budget',128) or sum(item['memory_mb'] for item in held) + instance['memory_mb'] > self.config.get('memory_budget',500000):
                raise RuntimeError('node capacity unavailable')
            record = {'instance': instance, 'held': True, 'closed': False, 'started': False,
                      'lease_until': time.time() + LEASE_SECONDS, 'last_running_at': None}
            self.records[ident] = record
            self.save()
            return record

    def matching(self, instance, allow_missing=False):
        with self.lock:
            record = self.records.get(instance['id'])
            if not record:
                if allow_missing:
                    return None
                raise ValueError('instance reservation is missing')
            if record['instance']['generation'] != instance['generation']:
                raise ValueError('stale generation')
            # Stopped instances have no allocated endpoint in the central DB.
            normalized = {**instance, 'slot':record['instance']['slot'], 'endpoint':record['instance']['endpoint']}
            if normalized != record['instance']:
                raise ValueError('instance specification changed')
            return record

    def clear(self, instance):
        with self.lock:
            endpoint = instance['endpoint']
            if self.forwarder_owners.get(endpoint) != (instance['id'],instance['generation']):
                return
            forwarder = self.forwarders.get(endpoint)
            if forwarder:
                forwarder.set_target(None)
            self.forwarder_owners.pop(endpoint,None)

    def dispatch(self, request):
        if not isinstance(request, dict) or request.get('node_id') != self.config['node_id']:
            raise ValueError('wrong node')
        method = request.get('method')
        if method == 'heartbeat':
            if set(request) != {'method','node_id','instances'}:
                raise ValueError('invalid heartbeat fields')
            return self.heartbeat(request['instances'])
        methods = {'prepare','start','healthy','state','stop','force_off','recovered','release','disconnect'}
        if method not in methods or set(request) != ({'method','node_id','instance','password'} if method == 'prepare' else {'method','node_id','instance'}):
            raise ValueError('unsupported node command')
        instance = self.validate(request['instance'])
        with self.lock:
            mutex = self.instance_locks.setdefault(instance['id'], threading.RLock())
        with mutex:
            if method == 'prepare':
                password = request['password']
                if not isinstance(password,str) or not 12 <= len(password) <= 128:
                    raise ValueError('invalid guest password')
                if not self.backend.ready() or shutil.disk_usage(self.config['data_root']).free < self.config.get('safety_gib',128)*1024**3:
                    raise RuntimeError('node image or storage not ready')
                self.reserve(instance)
                self.backend.prepare(instance, password)
                return True
            record = self.matching(instance, allow_missing=method in ('state','recovered','release','disconnect','stop','force_off'))
            if record:
                instance = record['instance']
            elif self.backend.state(instance) != 'absent':
                raise RuntimeError('untracked VM requires operator review')
            if method == 'state':
                return self.backend.state(instance)
            if method == 'start':
                with self.lock:
                    if record.get('closed') or record['lease_until'] <= time.time():
                        raise RuntimeError('run lease expired; restart with a new generation')
                    if record['started']:
                        if self.backend.state(instance) == 'running':
                            return True
                        raise RuntimeError('completed start cannot be replayed')
                    record['started'] = True
                    self.save()
                self.backend.start(instance)
                return True
            if method == 'healthy':
                if record.get('closed'):
                    raise RuntimeError('run lease closed')
                address = self.backend.healthy(instance)
                if address:
                    if instance['endpoint'] not in self.forwarders:
                        raise RuntimeError('node SSH proxy not configured')
                    with self.lock:
                        self.forwarder_owners[instance['endpoint']] = (instance['id'],instance['generation'])
                        self.forwarders[instance['endpoint']].set_target((address,22))
                return address
            if method in ('stop','force_off'):
                if record:
                    with self.lock:
                        record['closed'] = True
                        self.save()
                self.clear(instance)
                if self.backend.state(instance) == 'running':
                    getattr(self.backend,method)(instance)
                return True
            if method == 'disconnect':
                self.clear(instance)
                return True
            if method == 'recovered':
                recovered = (self.backend.state(instance) in ('off','absent') if record and not record.get('held') else self.backend.recovered(instance))
                if recovered and record:
                    with self.lock:
                        record.update(held=False, closed=True)
                        self.save()
                return recovered
            if method == 'release':
                recovered = self.backend.state(instance) in ('off','absent') if record and not record.get('held') else self.backend.recovered(instance)
                if not recovered:
                    raise RuntimeError('release requires recovered hardware')
                self.clear(instance)
                self.backend.release(instance)
                if record:
                    with self.lock:
                        record.update(held=False, closed=True, released=True)
                        self.save()  # Keep the generation tombstone; old requests cannot recreate it.
                return True

    def heartbeat(self, instances):
        if not isinstance(instances,list) or len(instances) > 80:
            raise ValueError('invalid heartbeat size')
        renew = {item['id']: self.validate(item) for item in instances}
        with self.lock:
            for ident, instance in renew.items():
                record = self.records.get(ident)
                if record and record['instance'] == instance and not record.get('closed') and record['lease_until'] > time.time():
                    record['lease_until'] = time.time() + LEASE_SECONDS
            records = list(self.records.items())
            self.save()
        results = {}
        for ident, record in records:
            if record.get('released'):
                continue  # Keep fences on disk without probing every historical deletion.
            with self.lock:
                mutex = self.instance_locks.setdefault(ident, threading.RLock())
            if not mutex.acquire(blocking=False):
                results[ident] = {'state':'unknown','generation':record['instance']['generation'], 'lastRunningAt':record.get('last_running_at')}
                continue
            try:
                state = self.backend.state(record['instance'])
                if state == 'running':
                    with self.lock:
                        record['last_running_at'] = stamp()
                elif state == 'off' and record['started']:
                    with self.lock:
                        record['closed'] = True
                shared = self.backend.shared_status(record['instance']) if hasattr(self.backend,'shared_status') else {'enabled':False}
                results[ident] = {'state':state,'generation':record['instance']['generation'],
                                  'lastRunningAt':record.get('last_running_at'), 'closed':record.get('closed'), 'shared':shared,
                                  'sshReady':self.forwarder_owners.get(record['instance']['endpoint']) == (ident,record['instance']['generation'])}
            finally:
                mutex.release()
        with self.lock:
            self.save()
        usage = shutil.disk_usage(self.config['data_root'])
        gib = 1024**3
        return {'nodeId':self.config['node_id'], 'ready':self.backend.ready(), 'observedAt':stamp(),
                'instances':results, 'storage':{'totalGiB':round(usage.total/gib,1), 'freeGiB':round(usage.free/gib,1),
                'usedGiB':round(usage.used/gib,1),'safetyGiB':self.config.get('safety_gib',128),
                'lowSpace':usage.free < self.config.get('safety_gib',128)*gib,'mode':'共享模板 · 稀疏增量盘'}}

    def expire_once(self):
        with self.lock:
            expired = [key for key, record in self.records.items() if record.get('held') and record['lease_until'] <= time.time()]
        for ident in expired:
            with self.lock:
                mutex = self.instance_locks.setdefault(ident, threading.RLock())
            if not mutex.acquire(blocking=False):
                continue
            try:
                with self.lock:
                    record = self.records[ident]
                    if record['lease_until'] > time.time():
                        continue
                    record['closed'] = True
                    self.save()
                instance = record['instance']
                self.clear(instance)
                state = self.backend.state(instance)
                if state == 'running':
                    if not record.get('shutdown_requested'):
                        record['shutdown_requested'] = time.time()
                        self.backend.stop(instance)
                    elif time.time() - record['shutdown_requested'] >= 20:
                        self.backend.force_off(instance)
                elif state in ('off','absent') and self.backend.recovered(instance):
                    with self.lock:
                        record['held'] = False
                with self.lock:
                    self.save()
            except Exception as exc:
                print(f'lease cleanup {ident}: {type(exc).__name__}', flush=True)
            finally:
                mutex.release()


def client():
    # authorized_keys forces this entrypoint regardless of SSH_ORIGINAL_COMMAND.
    request = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if len(request) > MAX_MESSAGE or not request.endswith(b'\n'):
        raise ValueError('invalid request length')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(145)
        connection.connect(SOCKET)
        connection.sendall(request)
        response = connection.makefile('rb').readline(MAX_MESSAGE + 1)
        if len(response) > MAX_MESSAGE or not response.endswith(b'\n'):
            raise ValueError('invalid response length')
        sys.stdout.buffer.write(response)


def serve():
    from server import Forwarder
    config = json.loads(Path('/etc/1cat-node/node.json').read_text())
    if socket.gethostname() != config['node_id']:
        raise RuntimeError('node hostname mismatch')
    forwarders = {endpoint:Forwarder(2220+endpoint) for endpoint in range(1,9+config.get('headless_count',16))}
    agent = NodeAgent(config, forwarders=forwarders)
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(150)
            try:
                data = self.rfile.readline(MAX_MESSAGE + 1)
                if len(data)>MAX_MESSAGE or not data.endswith(b'\n'):
                    raise ValueError('invalid request size')
                response = {'ok':True,'result':agent.dispatch(json.loads(data))}
            except Exception as exc:
                # Never return subprocess arguments, JSON input or guest credentials.
                response = {'ok':False,'error':f'node operation rejected ({type(exc).__name__})'}
                print(f'node operation rejected: {type(exc).__name__}',flush=True)
            response['node_id'] = config['node_id']
            try:
                self.wfile.write(json.dumps(response).encode()+b'\n')
            except (BrokenPipeError,ConnectionResetError):
                pass  # Completed operation remains durable even when the client disconnects.
    class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
        daemon_threads = True
    Path(SOCKET).unlink(missing_ok=True)
    with Server(SOCKET,Handler) as server:
        import grp
        os.chown(SOCKET,0,grp.getgrnam('onecat-node').gr_gid)
        os.chmod(SOCKET,0o660)
        for forwarder in forwarders.values(): forwarder.start()
        def watchdog():
            while True:
                agent.expire_once()
                time.sleep(2)
        threading.Thread(target=watchdog,daemon=True).start()
        server.serve_forever()


if __name__ == '__main__':
    if sys.argv[1:] == ['client']:
        client()
    elif sys.argv[1:] == ['serve'] and os.geteuid() == 0:
        serve()
    else:
        raise SystemExit('use client or root serve')
