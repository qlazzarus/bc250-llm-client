#!/usr/bin/env python3
"""Capture a drained legacy gate and provision a new startup-held state offline.

The administrator must separately exclude clients and other controllers from
capture until the replacement is ready. This tool does not stop services.
"""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys

from legacy_gate_status import read_status
from gate_candidate.migration import provision_from_legacy_snapshot


def save_snapshot(path, status):
    if (not isinstance(status, dict) or set(status) != {'pid', 'paused', 'active_requests', 'waiting_requests'}
            or type(status['pid']) is not int or status['pid'] <= 0
            or type(status['paused']) is not bool
            or type(status['active_requests']) is not int or status['active_requests'] != 0
            or type(status['waiting_requests']) is not int or status['waiting_requests'] != 0):
        raise ValueError('A drained authenticated legacy status is required')
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(status, file)
        file.flush()
        os.fsync(file.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def read_snapshot(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
            raise RuntimeError('Untrusted snapshot')
        with os.fdopen(fd, 'r', closefd=False) as file:
            result = json.load(file)
    finally:
        os.close(fd)
    if (not isinstance(result, dict) or set(result) != {'pid', 'paused', 'active_requests', 'waiting_requests'}
            or type(result['pid']) is not int or result['pid'] <= 0):
        raise ValueError('Invalid snapshot')
    return result


def require_stopped():
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'bc250-llm-gate.service',
                             '--property=ActiveState,SubState,MainPID,Job'], check=True,
                            text=True, capture_output=True, timeout=10)
    rows = [line.partition('=') for line in result.stdout.splitlines()]
    if any(not sep for _, sep, _ in rows) or len({k for k, _, _ in rows}) != len(rows):
        raise RuntimeError('Invalid gate status')
    state = {k: value for k, _, value in rows}
    if (state.get('ActiveState') not in {'inactive', 'failed'} or state.get('MainPID') != '0'
            or state.get('Job')):
        raise RuntimeError('Gate must be stopped and settled')


def initialize(snapshot_path, state_path):
    snapshot = read_snapshot(snapshot_path)
    require_stopped()
    state_path = Path(state_path)
    if any(os.path.lexists(str(state_path) + suffix) for suffix in ('', '.lock', '.uncertain')):
        raise FileExistsError('Gate state or its marker already exists')
    status = {key: snapshot[key] for key in ('paused', 'active_requests', 'waiting_requests')}
    provision_from_legacy_snapshot(state_path, status)
    os.chown(state_path, pwd.getpwnam('ai-studio').pw_uid, grp.getgrnam('ai-studio').gr_gid)
    with state_path.open('rb') as file:
        os.fsync(file.fileno())
    require_stopped()
    return status['paused']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('capture'); capture.add_argument('snapshot')
    create = sub.add_parser('initialize'); create.add_argument('snapshot'); create.add_argument('state')
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise RuntimeError('Root required')
        if args.command == 'capture':
            status = read_status()
            save_snapshot(args.snapshot, status)
            print(json.dumps({'captured': True, 'pid': status['pid'], 'legacy_paused': status['paused']}))
        else:
            paused = initialize(args.snapshot, args.state)
            print(json.dumps({'created_startup_hold': True, 'legacy_paused': paused}))
    except Exception:
        print('Migration check failed; preserve any existing files and investigate.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
