"""Experimental checkpoint-bound typed investigation snapshots.

Uses a separately pre-provisioned protected log and checkpoint under one external
lifetime lock. Never mix this format with legacy investigation events. The
snapshots contain only the strict typed schema; callers must not put secrets in
identifiers (including admin hold names). They must separately protect raw evidence.
The chain does not authenticate observations or reviews, prove completeness,
prevent simultaneous rollback of log and checkpoint, or authorize recovery.
A directory-fsync failure after replacement cannot be diagnosed after restart.
This is deliberately not connected to the worker.
"""
import fcntl
import json
import os

from .investigation_checkpoint import append_checkpointed
from .investigation_log import InvestigationLog, _MAX_BYTES
from .investigation_records import observation_digest, replay_investigation


class TypedInvestigationLog(InvestigationLog):
    """Store a complete, strictly validated snapshot as one chain entry."""

    @staticmethod
    def _validate_event(event):
        if (not isinstance(event, dict) or set(event) != {'version', 'kind', 'records'}
                or type(event['version']) is not int or event['version'] != 1
                or event['kind'] != 'investigation_snapshot'
                or not isinstance(event['records'], list)
                or not event['records']):
            raise ValueError('Invalid typed investigation snapshot')
        if len(event['records']) == 1:
            observation_digest(event['records'][0])
        elif replay_investigation(event['records']).status == 'unverified':
            raise ValueError('Invalid typed investigation snapshot')

    @staticmethod
    def _validate_history(events):
        latest = {}
        attempts = {}
        for event in events:
            records = event['records']
            investigation_id = records[0]['investigation_id']
            previous = latest.get(investigation_id)
            if previous is not None and (len(records) <= len(previous)
                                         or records[:len(previous)] != previous):
                raise RuntimeError('Investigation snapshot does not extend previous state')
            if len(records) >= 3:
                attempt = records[2]['attempt']
                owner = attempts.setdefault(attempt, investigation_id)
                if owner != investigation_id:
                    raise RuntimeError('Recovery attempt reused across investigations')
            latest[investigation_id] = records

    @classmethod
    def _verify(cls, data):
        tail = super()._verify(data)
        cls._validate_history([json.loads(line)['event'] for line in data.splitlines()])
        return tail

    @classmethod
    def _validate_append(cls, data, event):
        cls._validate_history([json.loads(line)['event'] for line in data.splitlines()] + [event])

    def append_snapshot(self, checkpoint, records):
        """Caller must hold the singleton lock until log and checkpoint finish."""
        return append_checkpointed(self, checkpoint,
                                   {'version': 1, 'kind': 'investigation_snapshot', 'records': records})

    def read_snapshots(self, checkpoint):
        """Read only after matching the trusted checkpoint, under singleton lock."""
        try:
            expected = checkpoint.read()
            fd = self._open()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._check_current_file(fd)
                data = b''
                while len(data) <= _MAX_BYTES:
                    chunk = os.read(fd, min(65536, _MAX_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
                if self._verify(data) != expected:
                    raise RuntimeError('Investigation checkpoint mismatch')
                self._check_current_file(fd)
                return [json.loads(line)['event']['records'] for line in data.splitlines()]
            finally:
                os.close(fd)
        except BaseException:
            checkpoint.failed = True
            raise
