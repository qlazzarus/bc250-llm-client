"""Private, immutable evidence archive candidate; does not authorize recovery.

Provision a root-private directory out of band and hold the trusted singleton
lock throughout each operation. The caller supplies an independently trusted
expected digest and is responsible for authentic collection, freshness, secret
handling and an independent backup. This archive is not connected to the worker
and cannot detect rollback of both the archive and its external digest.
It accepts arbitrary caller-supplied bytes, including secrets and corrupt journal
contents. It provides no allowlisted evidence schema or sanitized collection;
do not connect it to operational collection or recovery until those exist.
"""
import base64
import binascii
import hmac
import json
import os

from .files import protected_xattrs
from .investigation_checkpoint import InvestigationCheckpoint
from .investigation_evidence import evidence_digest, unassociated_evidence_digest
from .investigation_log import _encode
from .investigation_records import _HEX32, _HEX64, _hex

_MAX_ARCHIVE_BYTES = 24 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024


def _digest(association, identifier, evidence):
    if association == 'operation':
        return evidence_digest(identifier, evidence)
    if association == 'unassociated':
        return unassociated_evidence_digest(identifier, evidence)
    raise ValueError('Invalid evidence association')


def _serialize(association, identifier, evidence):
    digest = _digest(association, identifier, evidence)
    if sum(map(len, evidence.values())) > _MAX_TOTAL_BYTES:
        raise ValueError('Evidence archive too large')
    payload = {'version': 1, 'association': association, 'identifier': identifier,
               'items': {name: base64.b64encode(content).decode('ascii')
                         for name, content in evidence.items()}}
    data = _encode(payload) + b'\n'
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise ValueError('Evidence archive too large')
    return data, digest


def _check_file(fd):
    InvestigationCheckpoint._check_file(os.fstat(fd))
    if 'system.posix_acl_access' in protected_xattrs(fd):
        raise RuntimeError('Evidence archive has an extended ACL')


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class InvestigationArchive:
    """Write-once file for one investigation ID; poison this instance on I/O failure."""
    def __init__(self, path):
        self.store = InvestigationCheckpoint(path)
        self.failed = False

    def write(self, association, identifier, evidence):
        if self.failed:
            raise RuntimeError('Evidence archive unavailable after failure')
        data, digest = _serialize(association, identifier, evidence)
        directory = None
        try:
            directory = self.store._directory()
            fd = os.open(self.store.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                _check_file(fd)
                remaining = memoryview(data)
                while remaining:
                    count = os.write(fd, remaining)
                    if count <= 0:
                        raise OSError('Evidence archive write failed')
                    remaining = remaining[count:]
                os.fsync(fd)
                _check_file(fd)
            finally:
                os.close(fd)
            os.fsync(directory)
            return digest
        except BaseException:
            # Do not remove a possibly complete file after any uncertain failure.
            self.failed = True
            raise
        finally:
            if directory is not None:
                os.close(directory)

    def read(self, association, identifier, expected_digest):
        if self.failed:
            raise RuntimeError('Evidence archive unavailable after failure')
        if not _hex(identifier, _HEX32) or not _hex(expected_digest, _HEX64):
            raise ValueError('Invalid evidence anchor')
        directory = None
        try:
            directory = self.store._directory()
            fd = os.open(self.store.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
            try:
                _check_file(fd)
                before = os.fstat(fd)
                if not 0 < before.st_size <= _MAX_ARCHIVE_BYTES:
                    raise RuntimeError('Invalid evidence archive size')
                data = b''
                while len(data) < before.st_size:
                    chunk = os.read(fd, min(65536, before.st_size - len(data)))
                    if not chunk:
                        raise RuntimeError('Evidence archive changed during read')
                    data += chunk
                if os.read(fd, 1):
                    raise RuntimeError('Evidence archive changed during read')
                _check_file(fd)
                after = os.fstat(fd)
                current = os.open(self.store.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
                try:
                    _check_file(current)
                    if _identity(before) != _identity(after) or _identity(after) != _identity(os.fstat(current)):
                        raise RuntimeError('Evidence archive changed during read')
                finally:
                    os.close(current)
            finally:
                os.close(fd)
            try:
                payload = json.loads(data)
                if (not isinstance(payload, dict) or set(payload) != {'version', 'association', 'identifier', 'items'}
                        or type(payload['version']) is not int or payload['version'] != 1
                        or payload['association'] != association or payload['identifier'] != identifier
                        or not isinstance(payload['items'], dict)):
                    raise ValueError()
                evidence = {name: base64.b64decode(value, validate=True)
                            for name, value in payload['items'].items()}
                canonical, actual = _serialize(association, identifier, evidence)
                if canonical != data or not hmac.compare_digest(actual, expected_digest):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError, binascii.Error):
                raise RuntimeError('Evidence archive verification failed') from None
            return evidence
        except BaseException:
            self.failed = True
            raise
        finally:
            if directory is not None:
                os.close(directory)
