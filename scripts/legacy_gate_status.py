#!/usr/bin/env python3
"""Read legacy gate status without printing its credential; no state changes."""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path


def service_identity():
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'bc250-llm-gate.service',
                             '--property=ActiveState,SubState,MainPID,Job'],
                            check=True, text=True, capture_output=True, timeout=10)
    entries = [line.partition('=') for line in result.stdout.splitlines()]
    if any(not sep for _, sep, _ in entries) or len({key for key, _, _ in entries}) != len(entries):
        raise RuntimeError('Invalid service status')
    values = {key: value for key, _, value in entries}
    pid = int(values['MainPID'])
    if values.get('ActiveState') != 'active' or values.get('SubState') != 'running' or values.get('Job') or pid <= 0:
        raise RuntimeError('Gate is not settled')
    raw = Path(f'/proc/{pid}/stat').read_text()
    rest = raw[raw.rfind(')') + 2:].split()
    if len(rest) < 20 or not rest[19].isdigit():
        raise RuntimeError('Gate process identity unavailable')
    return pid, rest[19]


def read_status():
    before = service_identity()
    entries = Path(f'/proc/{before[0]}/environ').read_bytes().split(b'\0')
    env = dict(entry.split(b'=', 1) for entry in entries if b'=' in entry)
    key = env.get(b'GATE_API_KEY', b'').decode('utf-8')
    if not key:
        raise RuntimeError('Gate credential unavailable')
    request = urllib.request.Request('http://127.0.0.1:8081/control',
                                     headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(request, timeout=5) as response:
        state = json.load(response)
    if (not isinstance(state, dict) or set(state) != {'paused', 'active_requests', 'waiting_requests'}
            or type(state['paused']) is not bool
            or any(type(state[k]) is not int or state[k] < 0 for k in ('active_requests', 'waiting_requests'))):
        raise RuntimeError('Unexpected gate response')
    if service_identity() != before:
        raise RuntimeError('Gate process changed during check')
    return {'pid': before[0], **state}


def main():
    try:
        print(json.dumps(read_status(), sort_keys=True))
    except Exception:
        print('Gate status unavailable; no change made.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
