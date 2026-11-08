"""Verify the cooperating Studio and owned-hold gate before mutation."""
import os
import hashlib
import json
import grp
import pwd
import re
import subprocess
from pathlib import Path

import httpx

from .client_trust import verify_client_process, verify_operator_frontend


def exact_protocol_response(value, expected):
    return (isinstance(value, dict) and value.keys() == expected.keys()
            and all(type(value[key]) is type(expected_value) and value[key] == expected_value
                    for key, expected_value in expected.items()))


class CoordinationPreflight:
    def __init__(self, studio_key, gate_key, lock_path, journal_path, *, held_lock, transport=None):
        if not studio_key or not gate_key:
            raise ValueError('Coordination credentials required')
        self.studio_key, self.gate_key = studio_key, gate_key
        self.paths = (Path(lock_path), Path(journal_path))
        self.transport = transport
        self.held_lock = held_lock

    @staticmethod
    def service_identity(unit, account):
        result = subprocess.run(['/usr/bin/systemctl', 'show', '--all',
            '--property=LoadState,ActiveState,MainPID,Job,KillMode,User,Group,NoNewPrivileges', unit],
            check=True, capture_output=True, text=True, timeout=10)
        rows = [line.partition('=') for line in result.stdout.splitlines()]
        if any(not sep for _, sep, _ in rows) or len({key for key, _, _ in rows}) != len(rows):
            raise RuntimeError('Invalid service identity')
        data = {key: value for key, _, value in rows}
        if (data.get('LoadState') != 'loaded' or data.get('ActiveState') != 'active'
                or data.get('Job') or data.get('KillMode') != 'control-group'
                or data.get('User') != account or data.get('Group') != account
                or data.get('NoNewPrivileges') != 'yes'
                or not re.fullmatch(r'[1-9][0-9]*', data.get('MainPID', ''))):
            raise RuntimeError('Service identity is not settled')
        pid = int(data['MainPID'])
        expected_uid, expected_gid = pwd.getpwnam(account).pw_uid, grp.getgrnam(account).gr_gid
        raw_status = Path(f'/proc/{pid}/status').read_text()
        status = dict(line.split(':', 1) for line in raw_status.splitlines() if ':' in line)
        if (status.get('Uid', '').split() != [str(expected_uid)] * 4
                or status.get('Gid', '').split() != [str(expected_gid)] * 4
                or status.get('NoNewPrivs', '').strip() != '1'):
            raise RuntimeError('Service process credentials do not match its unit')
        raw = Path(f'/proc/{pid}/stat').read_text()
        rest = raw[raw.rfind(')') + 2:].split()
        if len(rest) < 20 or not rest[19].isdigit():
            raise RuntimeError('Cannot identify service process')
        return pid, rest[19]

    def same_studio_files(self, pid):
        for index, path in enumerate(self.paths):
            if not path.is_absolute() or '..' in path.parts:
                raise RuntimeError('Invalid coordination path')
            local = os.stat(path, follow_symlinks=False)
            remote = os.stat(f'/proc/{pid}/root{path}', follow_symlinks=False)
            if index == 0:
                if self.held_lock.handle is None:
                    raise RuntimeError('GPU lock is not held')
                held = os.fstat(self.held_lock.handle.fileno())
                if (held.st_dev, held.st_ino) != (local.st_dev, local.st_ino):
                    raise RuntimeError('GPU lock inode changed')
            if (local.st_dev, local.st_ino) != (remote.st_dev, remote.st_ino):
                raise RuntimeError('Studio sees a different coordination file')

    def __call__(self):
        client_identity = self.service_identity('bc250-llm-client.service', 'ai-client')
        verify_client_process(client_identity[0])
        verify_operator_frontend()
        studio = self.service_identity('bc250-ai-studio.service', 'ai-studio')
        gate = self.service_identity('bc250-llm-gate.service', 'ai-gate')
        self.same_studio_files(studio[0])

        with httpx.Client(timeout=5, trust_env=False, transport=self.transport) as client:
            response = client.get('http://127.0.0.1:8081/control/identity',
                                  headers={'Authorization': 'Bearer ' + self.gate_key})
            response.raise_for_status()
            if not exact_protocol_response(response.json(), {'protocol': 3, 'role': 'switch'}):
                raise RuntimeError('Gate identity mismatch')
            response = client.get('http://127.0.0.1:8090/internal/model-switch/coordination',
                                  headers={'Authorization': 'Bearer ' + self.studio_key})
            response.raise_for_status()
            if not exact_protocol_response(response.json(), {'protocol': 1, 'enabled': True, 'transition_clear': True}):
                raise RuntimeError('Studio coordination unavailable')
        if (client_identity != self.service_identity('bc250-llm-client.service', 'ai-client')
                or studio != self.service_identity('bc250-ai-studio.service', 'ai-studio')
                or gate != self.service_identity('bc250-llm-gate.service', 'ai-gate')):
            raise RuntimeError('Service identity changed during preflight')
        verify_client_process(client_identity[0])
        verify_operator_frontend()
        self.same_studio_files(studio[0])


class RecoveryPreflight(CoordinationPreflight):
    """Recovery-only Studio contract, evaluated while the worker holds GPU lock.

    Studio must independently verify its gate identity and block GPU admission;
    the digest binds its observation to the worker's validated journal snapshot.
    """
    def __init__(self, *args, journal, **kwargs):
        super().__init__(*args, **kwargs)
        self.journal = journal

    def __call__(self):
        studio = self.service_identity('bc250-ai-studio.service', 'ai-studio')
        gate = self.service_identity('bc250-llm-gate.service', 'ai-gate')
        self.same_studio_files(studio[0])
        state = self.journal.read()
        encoded = json.dumps(state, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        digest = hashlib.sha256(encoded).hexdigest()
        with httpx.Client(timeout=5, trust_env=False, transport=self.transport) as client:
            response = client.get('http://127.0.0.1:8081/control/identity',
                                  headers={'Authorization': 'Bearer ' + self.gate_key})
            response.raise_for_status()
            if not exact_protocol_response(response.json(), {'protocol': 3, 'role': 'switch'}):
                raise RuntimeError('Gate identity mismatch')
            response = client.get('http://127.0.0.1:8090/internal/model-switch/recovery',
                                  headers={'Authorization': 'Bearer ' + self.studio_key})
            response.raise_for_status()
            if not exact_protocol_response(response.json(), {'protocol': 1, 'enabled': True, 'gpu_blocked': True,
                                   'studio_recovery_clear': True,
                                   'gate_verified': True, 'journal_sha256': digest}):
                raise RuntimeError('Studio recovery coordination unavailable')
        if (studio != self.service_identity('bc250-ai-studio.service', 'ai-studio')
                or gate != self.service_identity('bc250-llm-gate.service', 'ai-gate')):
            raise RuntimeError('Service identity changed during recovery verification')
        self.same_studio_files(studio[0])
        if self.journal.read() != state:
            raise RuntimeError('Journal changed during recovery verification')
