"""Verify the cooperating Studio and owned-hold gate before mutation."""
import os
import re
import subprocess
from pathlib import Path

import httpx


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
            '--property=LoadState,ActiveState,MainPID,Job,KillMode,User,Group', unit],
            check=True, capture_output=True, text=True, timeout=10)
        rows = [line.partition('=') for line in result.stdout.splitlines()]
        if any(not sep for _, sep, _ in rows) or len({key for key, _, _ in rows}) != len(rows):
            raise RuntimeError('Invalid service identity')
        data = {key: value for key, _, value in rows}
        if (data.get('LoadState') != 'loaded' or data.get('ActiveState') != 'active'
                or data.get('Job') or data.get('KillMode') != 'control-group'
                or data.get('User') != account or data.get('Group') != account
                or not re.fullmatch(r'[1-9][0-9]*', data.get('MainPID', ''))):
            raise RuntimeError('Service identity is not settled')
        pid = int(data['MainPID'])
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
        studio = self.service_identity('bc250-ai-studio.service', 'ai-studio')
        gate = self.service_identity('bc250-llm-gate.service', 'ai-gate')
        self.same_studio_files(studio[0])
        with httpx.Client(timeout=5, trust_env=False, transport=self.transport) as client:
            response = client.get('http://127.0.0.1:8081/control/identity',
                                  headers={'Authorization': 'Bearer ' + self.gate_key})
            response.raise_for_status()
            if response.json() != {'protocol': 2, 'role': 'switch'}:
                raise RuntimeError('Gate identity mismatch')
            response = client.get('http://127.0.0.1:8090/internal/model-switch/coordination',
                                  headers={'Authorization': 'Bearer ' + self.studio_key})
            response.raise_for_status()
            if response.json() != {'protocol': 1, 'enabled': True, 'transition_clear': True}:
                raise RuntimeError('Studio coordination unavailable')
        if (studio != self.service_identity('bc250-ai-studio.service', 'ai-studio')
                or gate != self.service_identity('bc250-llm-gate.service', 'ai-gate')):
            raise RuntimeError('Service identity changed during preflight')
        self.same_studio_files(studio[0])
