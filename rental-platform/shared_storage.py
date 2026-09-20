"""Host-enforced, optional public virtio-fs storage. No customer-supplied paths."""
from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

TAG = 'onecat-public'
TARGET = '/shared'
MARKER = 'Managed by 1Cat shared-storage v1'
ASSETS = Path(__file__).with_name('shared-storage')


def load_config(config):
    if 'shared_storage' in config:
        return dict(config['shared_storage'] or {})
    name = os.environ.get('RENTAL_SHARED_STORAGE_CONFIG')
    if not name:
        return {}
    value = json.loads(Path(name).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('invalid shared storage configuration')
    return value


def find_mount(path):
    result = subprocess.run(['findmnt', '--json', '--mountpoint', str(path),
                             '-o', 'TARGET,UUID,FSTYPE,OPTIONS,FSROOT'],
                            capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError('public storage is not mounted')
    rows = json.loads(result.stdout).get('filesystems', [])
    if len(rows) != 1:
        raise RuntimeError('ambiguous public storage mount')
    return rows[0]


def validate_export(config):
    if config.get('enabled') is not True:
        return None
    source = Path(config['source'])
    export = Path(config['export'])
    mount = Path(config['mount'])
    if any(not p.is_absolute() or p == Path('/') or p.is_symlink()
           or p.resolve() != p for p in (source, export, mount)):
        raise RuntimeError('unsafe public storage path')
    if source.parent != mount or export == source or not source.is_dir() or not export.is_dir():
        raise RuntimeError('invalid public storage directory')
    actual = find_mount(mount)
    bound = find_mount(export)
    expected_root = '/' + source.name
    if (actual.get('uuid') != config['uuid'] or actual.get('fstype') != 'ext4'
            or bound.get('uuid') != config['uuid'] or bound.get('fstype') != 'ext4'
            or bound.get('fsroot') != expected_root or bound.get('target') != str(export)):
        raise RuntimeError('public storage mount identity mismatch')
    if not {'ro', 'nodev', 'nosuid'}.issubset(set(bound.get('options', '').split(','))):
        raise RuntimeError('public storage export is not host-enforced read-only')
    if os.stat(source).st_dev != os.stat(export).st_dev:
        raise RuntimeError('public storage device mismatch')
    binary = Path(config.get('binary', '/usr/libexec/virtiofsd'))
    if not binary.is_file():
        raise RuntimeError('virtiofsd is missing')
    return str(export)


def guest_install_script():
    """Identical managed files for new images and older customer guests.

    Refuse to overwrite unrelated guest files and never rerun cloud-init or
    perform disk initialization. Paths and file payloads are server-owned.
    """
    files = [
        ('/usr/local/sbin/1cat-mount-shared', 0o755, '1cat-mount-shared'),
        ('/etc/systemd/system/1cat-shared-storage.service', 0o644, '1cat-shared-storage.service'),
    ]
    payload = [(dest, mode, (ASSETS / filename).read_text(encoding='utf-8'))
               for dest, mode, filename in files]
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return f'''import base64,json,os,subprocess
from pathlib import Path
files=json.loads(base64.b64decode({data!r}))
for name,mode,content in files:
    p=Path(name)
    if p.is_symlink() or (p.exists() and {MARKER!r} not in p.read_text()):
        raise RuntimeError('shared storage managed file conflict')
for name,mode,content in files:
    p=Path(name); p.parent.mkdir(parents=True,exist_ok=True)
    if not p.exists() or p.read_text()!=content:
        temp=p.with_name(p.name+'.1cat-new')
        if temp.exists() or temp.is_symlink():
            raise RuntimeError('shared storage temporary file conflict')
        with temp.open('x') as f: f.write(content)
        temp.chmod(mode); os.replace(temp,p)
    p.chmod(mode)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','1cat-shared-storage.service'],check=True,capture_output=True)
subprocess.run(['/usr/local/sbin/1cat-mount-shared'],check=True,timeout=25)
result=subprocess.run(['findmnt','-rn','-M','{TARGET}','-o','SOURCE,FSTYPE,OPTIONS'],check=True,capture_output=True,text=True).stdout.strip().split()
assert len(result)==3 and result[0]=={TAG!r} and result[1]=='virtiofs' and 'ro' in result[2].split(',')
print('ONECAT_SHARED_READY')
'''
