#!/usr/bin/env python3
"""Read-only Studio quiescence check; never print credentials or job contents."""
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
NONTERMINAL = {'queued', 'waiting_for_gpu', 'running'}


def identity():
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'bc250-ai-studio.service',
                             '--property=ActiveState,SubState,MainPID,Job'], check=True,
                            text=True, capture_output=True, timeout=10)
    rows = [line.partition('=') for line in result.stdout.splitlines()]
    if any(not sep for _, sep, _ in rows) or len({k for k, _, _ in rows}) != len(rows):
        raise RuntimeError('Invalid Studio service state')
    state = {k: value for k, _, value in rows}
    pid = int(state['MainPID'])
    if state.get('ActiveState') != 'active' or state.get('SubState') != 'running' or state.get('Job') or pid <= 0:
        raise RuntimeError('Studio is not settled')
    stat = Path(f'/proc/{pid}/stat').read_text()
    fields = stat[stat.rfind(')') + 2:].split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise RuntimeError('Studio process identity unavailable')
    return pid, fields[19]


def read_key(pid):
    entries = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    env = dict(entry.split(b'=', 1) for entry in entries if b'=' in entry)
    key = env.get(b'STUDIO_ADMIN_KEY', b'').decode('utf-8')
    if not key:
        raise RuntimeError('Studio credential unavailable')
    return key


def request(key, path):
    req = urllib.request.Request('http://127.0.0.1:8090/api/v1' + path,
                                 headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def validate_status(status):
    if (not isinstance(status, dict) or not {'gpu_owner', 'active_job'} <= status.keys()
            or type(status.get('recovery_required')) is not bool):
        raise RuntimeError('Invalid Studio status')
    if type(status.get('scheduler_running')) is not bool:
        raise RuntimeError('Invalid scheduler status')
    if any(not isinstance(status.get(k), dict) for k in ('transition', 'llm_recovery')):
        raise RuntimeError('Invalid recovery state')
    return (not status['recovery_required'] and status['scheduler_running']
            and status.get('gpu_owner') is None and status.get('active_job') is None
            and not status['transition'] and not status['llm_recovery'])


def jobs(key):
    seen = []
    for offset in range(0, 20000, 200):
        rows = request(key, '/jobs?' + urllib.parse.urlencode({'limit': 200, 'offset': offset}))
        if not isinstance(rows, list) or len(rows) > 200:
            raise RuntimeError('Invalid jobs page')
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('job_id'), str)
                    or row.get('status') not in TERMINAL | NONTERMINAL):
                raise RuntimeError('Invalid job state')
            seen.append((row['job_id'], row['status']))
        if len(rows) < 200:
            if len({job_id for job_id, _ in seen}) != len(seen):
                raise RuntimeError('Job pages changed')
            return seen
    raise RuntimeError('Too many jobs to verify')


def read_status():
    before = identity()
    key = read_key(before[0])
    first_status = validate_status(request(key, '/system/status'))
    first_jobs = jobs(key)
    second_jobs = jobs(key)
    second_status = validate_status(request(key, '/system/status'))
    if first_jobs != second_jobs or identity() != before:
        raise RuntimeError('Studio state changed while checking')
    count = sum(state in NONTERMINAL for _, state in second_jobs)
    return {'pid': before[0], 'scheduler_gpu_recovery_clear': first_status and second_status,
            'nonterminal_jobs': count, 'quiescent': first_status and second_status and count == 0}


def main():
    try:
        print(json.dumps(read_status(), sort_keys=True))
    except Exception:
        print('Studio status unavailable; no change made.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
