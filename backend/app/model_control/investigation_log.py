"""Experimental investigation event chain; not an authorization source.

The protected file must be provisioned separately. A hash chain detects local
corruption, but cannot detect replacement by an older valid copy or deletion
without an independent trusted checkpoint. Callers must fail closed on errors.
"""
import fcntl
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .files import FileLock

_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_KINDS = {'investigation_started', 'evidence_observed', 'reviewed', 'action_intended', 'action_result'}
_MAX_BYTES = 16 * 1024 * 1024


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _validate_event(event):
    if (not isinstance(event, dict) or set(event) != {'kind', 'operation_id', 'evidence_digest'}
            or event['kind'] not in _KINDS
            or (event['operation_id'] is not None and
                (not isinstance(event['operation_id'], str) or not _HEX32.fullmatch(event['operation_id'])))
            or not isinstance(event['evidence_digest'], str) or not _HEX64.fullmatch(event['evidence_digest'])):
        raise ValueError('Invalid investigation event')


class InvestigationLog:
    """Append validated, secret-free references to an already provisioned file."""
    _validate_event = staticmethod(_validate_event)

    @staticmethod
    def _validate_append(data, event):
        pass

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def _check_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o077):
            raise RuntimeError('Untrusted investigation log')

    def _open(self):
        if not self.path.is_absolute() or '..' in self.path.parts:
            raise RuntimeError('Invalid investigation log path')
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            FileLock._check_trusted_directory(os.fstat(directory))
            for part in self.path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                os.close(directory)
                directory = child
                FileLock._check_trusted_directory(os.fstat(directory))
            fd = os.open(self.path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
            try:
                self._check_file(os.fstat(fd))
                return fd
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(directory)

    def _check_current_file(self, fd):
        """Reopen the protected path and compare it with the locked descriptor.

        This detects changes at the checkpoints, not a later replacement or
        a privileged actor racing between checks. Directory protection and
        coordination with all authorized mutators remain mandatory.
        """
        current = self._open()
        try:
            locked_info = os.fstat(fd)
            current_info = os.fstat(current)
            self._check_file(locked_info)
            self._check_file(current_info)
            if (locked_info.st_dev, locked_info.st_ino) != (current_info.st_dev, current_info.st_ino):
                raise RuntimeError('Investigation log path changed')
        finally:
            os.close(current)

    @classmethod
    def _verify(cls, data):
        if len(data) > _MAX_BYTES or (data and not data.endswith(b'\n')):
            raise RuntimeError('Invalid investigation log')
        previous = '0' * 64
        for seq, line in enumerate(data.splitlines(), 1):
            try:
                record = json.loads(line)
                if (not isinstance(record, dict) or set(record) != {'seq', 'previous', 'event', 'digest'}
                        or type(record['seq']) is not int or record['seq'] != seq
                        or record['previous'] != previous):
                    raise ValueError()
                cls._validate_event(record['event'])
                payload = {key: record[key] for key in ('seq', 'previous', 'event')}
                digest = hashlib.sha256(_encode(payload)).hexdigest()
                if record['digest'] != digest or _encode(record) != line:
                    raise ValueError()
                previous = digest
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise RuntimeError('Invalid investigation log') from None
        return len(data.splitlines()), previous

    def append(self, event, *, checkpoint):
        # The caller must obtain this checkpoint from an independently trusted
        # source and durably advance it after append. A crash between the two
        # writes causes a mismatch on re-entry, not automatic reconciliation.
        if (not isinstance(checkpoint, tuple) or len(checkpoint) != 2
                or type(checkpoint[0]) is not int or checkpoint[0] < 0
                or not isinstance(checkpoint[1], str) or not _HEX64.fullmatch(checkpoint[1])
                or (checkpoint[0] == 0 and checkpoint[1] != '0' * 64)):
            raise ValueError('Invalid trusted checkpoint')
        self._validate_event(event)
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
            seq, previous = self._verify(data)
            if (seq, previous) != checkpoint:
                raise RuntimeError('Investigation checkpoint mismatch')
            self._validate_append(data, event)
            payload = {'seq': seq + 1, 'previous': previous, 'event': event}
            record = {**payload, 'digest': hashlib.sha256(_encode(payload)).hexdigest()}
            encoded = _encode(record) + b'\n'
            if len(data) + len(encoded) > _MAX_BYTES:
                raise RuntimeError('Investigation log full')
            os.lseek(fd, 0, os.SEEK_END)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError('Investigation log write failed')
                remaining = remaining[written:]
            os.fsync(fd)
            self._check_current_file(fd)
            return seq + 1, record['digest']
        finally:
            os.close(fd)
