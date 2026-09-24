"""Private fixed-command SSH transport. No remote shell or shared business DB."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

LOCAL_NODE = 'G2-002'


class NodeUnavailable(RuntimeError):
    pass


class RemoteBackend:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.snapshot = {}
        self.observed = 0.0

    def rpc(self, method, instance=None, **fields):
        request = {'method': method, 'node_id': self.config['id'], **fields}
        if instance is not None:
            request['instance'] = instance
        args = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                '-o', 'IdentitiesOnly=yes', '-o', 'LogLevel=ERROR',
                '-o', 'UserKnownHostsFile=' + self.config['known_hosts'],
                '-i', self.config['identity_file'], '-p', str(self.config.get('port', 22)),
                self.config['user'] + '@' + self.config['host'], 'node-rpc']
        try:
            result = subprocess.run(args, input=json.dumps(request) + '\n', text=True,
                                    capture_output=True, timeout=(720 if instance and instance.get('gpu_count',1)==4 else 150)
                                    if method in ('prepare', 'start', 'release', 'force_off') else
                                    (90 if instance and instance.get('gpu_count',1)==4 else 45))
            if result.returncode:
                raise NodeUnavailable('节点通信暂不可用，资源占用将保留')
            response = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise NodeUnavailable('节点通信暂不可用，资源占用将保留') from exc
        if not isinstance(response, dict) or response.get('node_id') != self.config['id']:
            raise NodeUnavailable('节点身份不匹配')
        if not response.get('ok'):
            raise RuntimeError(response.get('error', '节点拒绝操作'))
        return response.get('result')

    def heartbeat(self, instances):
        try:
            snapshot = self.rpc('heartbeat', instances=instances)
            with self.lock:
                self.snapshot, self.observed = snapshot, time.monotonic()
            return snapshot
        except (NodeUnavailable, RuntimeError):
            with self.lock:
                self.observed = 0
            return None

    def status(self):
        with self.lock:
            return dict(self.snapshot) if self.observed and time.monotonic() - self.observed < 20 else {}

    def ready(self):
        return self.config.get('enabled') is True and self.status().get('ready') is True

    def state(self, instance):
        try:
            return self.rpc('state', instance)
        except NodeUnavailable:
            return 'unknown'

    def prepare(self, instance, password):
        if not self.ready():
            raise NodeUnavailable('节点未通过在线检查')
        return self.rpc('prepare', instance, password=password)

    def start(self, instance): return self.rpc('start', instance)
    def healthy(self, instance): return self.rpc('healthy', instance)
    def stop(self, instance): return self.rpc('stop', instance)
    def force_off(self, instance): return self.rpc('force_off', instance)
    def recovered(self, instance): return self.rpc('recovered', instance)
    def release(self, instance): return self.rpc('release', instance)
    def disconnect(self, instance): return self.rpc('disconnect', instance)

    def shared_status(self, instance):
        return self.status().get('instances', {}).get(str(instance['id']), {}).get('shared',
                {'enabled': True, 'state': 'pending', 'path': '/shared', 'readonly': True})


class BackendRouter:
    def __init__(self, local, nodes, local_node_id=LOCAL_NODE):
        self.local = local
        self.local_node_id = local_node_id
        self.remotes = {node['id']: RemoteBackend(node) for node in nodes}

    def for_node(self, node_id):
        if self.local is not None and node_id == self.local_node_id:
            return self.local
        if node_id not in self.remotes:
            raise NodeUnavailable('实例所属节点未配置，禁止切换到其他机器')
        return self.remotes[node_id]

    def ready(self, node_id=LOCAL_NODE):
        return self.for_node(node_id).ready()

    def __getattr__(self, method):
        if method not in {'prepare','start','healthy','state','stop','force_off','recovered','release','shared_status'}:
            raise AttributeError(method)
        def call(instance, *args):
            backend = self.for_node(instance.get('node_id', LOCAL_NODE))
            if method == 'shared_status' and not hasattr(backend, method):
                return {'enabled': False}
            return getattr(backend, method)(instance, *args)
        return call


def load_nodes(path, local_node_id=LOCAL_NODE):
    if not path:
        return []
    nodes = json.loads(Path(path).read_text())
    if not isinstance(nodes, list):
        raise ValueError('nodes config must be a list')
    seen = {local_node_id} if local_node_id is not None else set()
    for node in nodes:
        if node['id'] in seen or not isinstance(node.get('enabled'), bool):
            raise ValueError('duplicate or invalid node config')
        seen.add(node['id'])
        # Configuration is root-owned, never accepted from a browser request.
        for key in ('host', 'user', 'identity_file', 'known_hosts'):
            if not isinstance(node.get(key), str) or not node[key] or node[key].startswith('-') or '\n' in node[key]:
                raise ValueError('invalid node transport config')
        if not Path(node['identity_file']).is_absolute() or not Path(node['known_hosts']).is_absolute():
            raise ValueError('node credentials require absolute paths')
    return nodes
