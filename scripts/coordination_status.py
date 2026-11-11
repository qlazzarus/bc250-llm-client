#!/usr/bin/env python3
"""Read-only post-install checks; credentials and job contents are never printed."""
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from legacy_gate_status import service_identity as gate_identity
from studio_quiescence_status import identity as studio_identity, read_status as studio_status

KEYS = Path('/etc/bc250-model-control')


def client_switch_enabled(value):
    enabled = value.lower()
    if enabled in {b'1', b'true', b'yes', b'on', b'y', b't'}:
        return True
    if enabled in {b'', b'0', b'false', b'no', b'off', b'n', b'f'}:
        return False
    raise RuntimeError('Invalid Client switch setting')


def get(url, key):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def valid_gate(state):
    return (isinstance(state, dict) and type(state.get('protocol')) is int and state['protocol'] == 3
            and state.get('durable') is True and isinstance(state.get('epoch'), str)
            and re.fullmatch(r'[0-9a-f]{32}', state['epoch']) is not None
            and isinstance(state.get('holds'), list) and all(isinstance(x, str) for x in state['holds'])
            and len(set(state['holds'])) == len(state['holds'])
            and type(state.get('paused')) is bool and state['paused'] == bool(state['holds'])
            and type(state.get('active_requests')) is int and state['active_requests'] == 0
            and type(state.get('waiting_requests')) is int and state['waiting_requests'] == 0
            and 'recovery_exclusive_hold' in state and state['recovery_exclusive_hold'] is None
            and 'deployment_generation' in state and state['deployment_generation'] is None)


def client_identity():
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'bc250-llm-client.service',
                             '--property=ActiveState,SubState,MainPID,Job'], check=True,
                            text=True, capture_output=True, timeout=10)
    rows = [line.partition('=') for line in result.stdout.splitlines()]
    if any(not sep for _, sep, _ in rows) or len({k for k, _, _ in rows}) != len(rows):
        raise RuntimeError('Invalid Client service state')
    data = {k: value for k, _, value in rows}
    pid = int(data['MainPID'])
    if data.get('ActiveState') != 'active' or data.get('SubState') != 'running' or data.get('Job') or pid <= 0:
        raise RuntimeError('Client is not settled')
    stat = Path(f'/proc/{pid}/stat').read_text()
    fields = stat[stat.rfind(')') + 2:].split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise RuntimeError('Client identity unavailable')
    return pid, fields[19]


def read_status():
    gate_before, studio_before, client_before = gate_identity(), studio_identity(), client_identity()
    roles = {'admin': 'gate-admin-key', 'client': 'gate-client-key',
             'studio': 'gate-studio-key', 'switch': 'switch-gate-key'}
    keys = {role: (KEYS / file).read_text().rstrip('\r\n') for role, file in roles.items()}
    if any(not key for key in keys.values()) or len(set(keys.values())) != len(keys):
        raise RuntimeError('Gate credentials unavailable or not distinct')
    entries = Path(f'/proc/{client_before[0]}/environ').read_bytes().split(b'\0')
    client_env = dict(item.split(b'=', 1) for item in entries if b'=' in item)
    if (client_env.get(b'LLM_GATE_API_KEY', b'').decode() != keys['client']
            or not re.fullmatch(rb'client-[0-9a-f]{32}', client_env.get(b'LLM_GATE_HOLD_ID', b''))
            or not client_env.get(b'OPERATOR_KEY')
            or (client_switch_enabled(client_env.get(b'MODEL_CONTROL_ENABLED', b''))
                and client_env.get(b'LLM_MODEL_ID', b''))):
        raise RuntimeError('Client configuration mismatch')
    base = 'http://127.0.0.1:8081'
    for role, key in keys.items():
        if get(base + '/control/identity', key) != {'protocol': 3, 'role': role}:
            raise RuntimeError('Gate role identity mismatch')
    state = get(base + '/control', keys['admin'])
    if not valid_gate(state):
        raise RuntimeError('Gate is not drained and settled')
    key = (KEYS / 'studio-coordination-key').read_text().rstrip('\r\n')
    studio = get('http://127.0.0.1:8090/internal/model-switch/coordination', key)
    if (not isinstance(studio, dict) or set(studio) != {'protocol', 'enabled', 'transition_clear'}
            or type(studio['protocol']) is not int or studio['protocol'] != 1
            or studio['enabled'] is not True or studio['transition_clear'] is not True):
        raise RuntimeError('Studio coordination unavailable')
    status = studio_status()
    after = get(base + '/control', keys['admin'])
    if (not status['quiescent'] or not valid_gate(after) or after != state
            or gate_identity() != gate_before or studio_identity() != studio_before
            or client_identity() != client_before):
        raise RuntimeError('Coordination changed while checking')
    return {'gate_protocol': 3, 'gate_drained': True, 'holds': state['holds'],
            'gate_roles_verified': True, 'client_configuration_verified': True, 'studio_coordination_verified': True,
            'studio_quiescent': True}


def main():
    try:
        print(json.dumps(read_status(), sort_keys=True))
    except Exception:
        print('Coordination check unavailable; preserve holds and investigate.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
