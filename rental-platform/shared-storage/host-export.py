#!/usr/bin/python3
"""Idempotently establish the verified, host-enforced read-only public export."""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, '/opt/1cat-rental')
from shared_storage import find_mount, validate_export

config = json.loads(Path('/etc/1cat-rental/shared-storage.json').read_text())
if os.geteuid() != 0:
    raise SystemExit('root required')
mount = Path(config['mount'])
source = Path(config['source'])
export = Path(config['export'])
if source != mount / 'public' or export != Path('/srv/1cat-public-ro'):
    raise SystemExit('unexpected shared storage layout')
if any(p.is_symlink() or p.resolve() != p for p in (mount, source, export)):
    raise SystemExit('shared storage paths must not be symlinks')
actual = find_mount(mount)
if actual.get('uuid') != config['uuid'] or actual.get('fstype') != 'ext4':
    raise SystemExit('expected SATA RAID is not mounted')
if not source.is_dir():
    raise SystemExit('public source missing')
export.mkdir(mode=0o755, exist_ok=True)
if os.path.ismount(export):
    validate_export(config)
else:
    if any(export.iterdir()):
        raise SystemExit('export mountpoint is not empty')
    subprocess.run(['mount', '--bind', str(source), str(export)], check=True)
    try:
        subprocess.run(['mount', '-o', 'remount,bind,ro,nosuid,nodev', str(export)], check=True)
        validate_export(config)
    except Exception:
        subprocess.run(['umount', str(export)], check=True)
        raise
print('ONECAT_HOST_EXPORT_READY')
