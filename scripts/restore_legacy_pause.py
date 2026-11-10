#!/usr/bin/env python3
"""Restore an observed legacy administrator pause during an isolated rollback."""
import json
import sys
import urllib.request
from pathlib import Path

from legacy_gate_status import read_status, service_identity


def restore():
    before = service_identity()
    status = read_status()
    if status['pid'] != before[0] or status['active_requests'] or status['waiting_requests']:
        raise RuntimeError('Legacy gate is not drained')
    if status['paused']:
        return
    entries = Path(f'/proc/{before[0]}/environ').read_bytes().split(b'\0')
    env = dict(item.split(b'=', 1) for item in entries if b'=' in item)
    key = env.get(b'GATE_API_KEY', b'').decode()
    if not key or service_identity() != before:
        raise RuntimeError('Legacy gate changed')
    request = urllib.request.Request('http://127.0.0.1:8081/control', method='PUT',
                                     data=b'{"paused":true}',
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=5) as response:
        value = json.load(response)
    if not isinstance(value, dict) or value.get('paused') is not True:
        raise RuntimeError('Pause was not verified')
    after = read_status()
    if (service_identity() != before or after['pid'] != before[0] or after['paused'] is not True
            or after['active_requests'] or after['waiting_requests']):
        raise RuntimeError('Legacy gate state changed')


def main():
    try:
        restore()
        print('legacy_pause_restored=True')
    except Exception:
        print('Pause outcome unavailable; keep ingress blocked and investigate.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
