#!/usr/bin/env python3
"""Append headless tunnel presets without printing or replacing credentials.

This stages the config only. Restart the tunnel once, inspect assigned ports,
then configure RENTAL_PUBLIC_PORT_9 onward before activating the service.
"""
import argparse
import datetime
import json
import os
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--count', type=int, default=16)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.count <= 64:
        raise ValueError('count must be 1..64')
    path = args.config
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('config must be an existing regular file')
    before_bytes = path.read_bytes()
    before = json.loads(before_bytes)
    if not before.get('token') or not isinstance(before.get('presets'), list):
        raise RuntimeError('existing enrolled node config required')
    presets = list(before['presets'])
    added = []
    for endpoint in range(9, 9 + args.count):
        item = {'name': f'rental-headless-{endpoint - 8}-ssh', 'local_addr': f'127.0.0.1:{2220 + endpoint}',
                'description': f'1CatDL headless SSH {endpoint - 8}', 'protocol': 'tcp'}
        existing = [p for p in presets if p.get('name') == item['name'] or p.get('local_addr') == item['local_addr']]
        if existing:
            if existing != [item]:
                raise RuntimeError('conflicting headless tunnel preset')
        else:
            presets.append(item)
            added.append(item['name'])
    if not args.apply or not added:
        print(json.dumps({'applied': False, 'wouldAdd': added, 'count': args.count}))
        return
    backup = path.with_name(path.name + '.bak-headless-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    if backup.exists():
        raise RuntimeError('backup already exists')
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    updated = dict(before, presets=presets)
    temp = path.with_name(path.name + f'.headless-{os.getpid()}.tmp')
    try:
        with open(temp, 'x', encoding='utf-8') as stream:
            os.chmod(temp, 0o600)
            json.dump(updated, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != before_bytes:
            raise RuntimeError('config changed concurrently')
        os.replace(temp, path)
        print(json.dumps({'applied': True, 'added': added, 'backup': str(backup), 'credentialsPreserved': True}))
    finally:
        if temp.exists(): temp.unlink()


if __name__ == '__main__': main()
