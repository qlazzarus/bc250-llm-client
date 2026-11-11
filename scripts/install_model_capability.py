#!/usr/bin/env python3
"""Install one verified model record only while coordinated state is stable."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

from backend.app.model_control.client import ModelControlClient
from backend.app.model_control.files import FileLock
from backend.app.model_control.registry import ModelRegistry
from coordination_status import read_status
from draft_model_capabilities import observe

LOCK = Path('/run/bc250-gpu/gpu.lock')
SOCKET = '/run/bc250-model-control/control.sock'
DIRECTORY = Path('/opt/ai/config/capabilities')


def read_candidate(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        raw = os.read(fd, 16385)
    finally:
        os.close(fd)
    if len(raw) > 16384:
        raise ValueError('Candidate record too large')
    value = json.loads(raw)
    if (not isinstance(value, dict)
            or value.get('capabilities') != {'reasoning_control': 'supported', 'json_schema': 'supported'}):
        raise ValueError('Incomplete capability candidate')
    return value


def snapshot():
    with urllib.request.urlopen('http://127.0.0.1:8081/model-snapshot?include_context=true', timeout=5) as response:
        value = json.load(response)
    if (not isinstance(value, dict) or value.get('ready') is not True
            or not isinstance(value.get('epoch'), str) or len(value['epoch']) != 32
            or type(value.get('context_tokens')) is not int or value['context_tokens'] <= 0
            or not isinstance(value.get('models'), dict)
            or not isinstance(value['models'].get('data'), list) or len(value['models']['data']) != 1):
        raise RuntimeError('Model snapshot unavailable')
    return value


async def worker_state():
    client = ModelControlClient(SOCKET)
    current = await client.request('current')
    profile = await client.request('profile')
    if current != {'operation': None} or 'error' in profile:
        raise RuntimeError('Worker not idle')
    return profile


def write_once(directory, name, value):
    directory = Path(directory)
    if not directory.exists():
        directory.mkdir(mode=0o755)
        os.chmod(directory, 0o755)
        subprocess.run(['/usr/bin/restorecon', '-F', str(directory)], check=True, capture_output=True)
    FileLock._check_trusted_directory(directory.lstat())
    fd, temporary = tempfile.mkstemp(prefix='.capability-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as file:
            os.fchmod(file.fileno(), 0o644)
            json.dump(value, file, sort_keys=True, separators=(',', ':'))
            file.flush()
            os.fsync(file.fileno())
        target = directory / (name + '.json')
        os.link(temporary, target, follow_symlinks=False)
        subprocess.run(['/usr/bin/restorecon', '-F', str(target)], check=True, capture_output=True)
        os.unlink(temporary)
        temporary = None
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def install(path):
    candidate = read_candidate(path)
    lock = FileLock(LOCK, secure_precreated=True)
    lock.acquire()
    try:
        coordination = read_status()
        if coordination['holds']:
            raise RuntimeError('Independent hold present')
        profile = asyncio.run(worker_state())
        name, expected = observe(ModelRegistry())
        first = snapshot()
        expected['capabilities'] = {'reasoning_control': 'supported', 'json_schema': 'supported'}
        if (candidate != expected or profile.get('name') != name
                or profile.get('model_id') != candidate['model_id']
                or first['models']['data'][0].get('id') != candidate['model_id']):
            raise RuntimeError('Candidate does not match loaded model')
        after_name, after = observe(ModelRegistry())
        after['capabilities'] = expected['capabilities']
        second = snapshot()
        if (after_name != name or after != expected or first['epoch'] != second['epoch']
                or first['context_tokens'] != second['context_tokens']
                or second['models']['data'][0].get('id') != candidate['model_id']
                or asyncio.run(worker_state()) != profile or read_status()['holds']):
            raise RuntimeError('Model or coordination changed')
        write_once(DIRECTORY, name, candidate)
        return name
    finally:
        lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate')
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise RuntimeError('Root required')
        name = install(args.candidate)
        print('capability_record_installed=' + name)
    except Exception:
        print('Capability record not confirmed; preserve any existing record and investigate.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
