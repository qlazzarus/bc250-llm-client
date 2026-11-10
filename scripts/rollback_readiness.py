#!/usr/bin/env python3
"""Read-only rollback assessment; it does not stop services or release holds."""
import asyncio
import json
import subprocess
import sys

from backend.app.model_control.client import ModelControlClient
from coordination_status import KEYS, get, read_status
from gate_candidate.migration import legacy_pause_for_rollback


def validate_worker(value):
    return (isinstance(value, dict) and value.get('operation', 'missing') is None
            and value.get('hold_id', 'missing') is None and value.get('journal') == {}
            and value.get('journal_error', 'missing') is None
            and value.get('reconciliation_error', 'missing') is None)


def require_settled_llama():
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'llama-server.service',
                             '--property=ActiveState,SubState,MainPID,Job'], check=True,
                            text=True, capture_output=True, timeout=10)
    rows = [line.partition('=') for line in result.stdout.splitlines()]
    if any(not sep for _, sep, _ in rows) or len({k for k, _, _ in rows}) != len(rows):
        raise RuntimeError('Invalid llama state')
    state = {k: value for k, _, value in rows}
    if (state.get('ActiveState') != 'active' or state.get('SubState') != 'running'
            or state.get('Job') or not state.get('MainPID', '').isdigit() or int(state['MainPID']) <= 0):
        raise RuntimeError('Llama is not settled')
    return state['MainPID']


async def assess():
    read_status()
    key = (KEYS / 'gate-admin-key').read_text().rstrip('\r\n')
    url = 'http://127.0.0.1:8081/control'
    before = get(url, key)
    paused = legacy_pause_for_rollback(before)
    pid = require_settled_llama()
    client = ModelControlClient('/run/bc250-model-control/control.sock')
    first = await client.request('inspect_recovery')
    if not validate_worker(first):
        raise RuntimeError('Worker recovery state is unresolved')
    read_status()
    second = await client.request('inspect_recovery')
    if (not validate_worker(second) or second != first or get(url, key) != before
            or require_settled_llama() != pid):
        raise RuntimeError('State changed during assessment')
    return {'legacy_pause_to_restore': paused, 'gate_representable': True,
            'worker_no_unresolved_operation': True, 'studio_quiescent': True, 'llama_settled': True}


def main():
    try:
        print(json.dumps(asyncio.run(assess()), sort_keys=True))
    except Exception:
        print('Rollback assessment unavailable; preserve state and investigate.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
