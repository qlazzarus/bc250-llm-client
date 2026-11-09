"""Experimental protected checkpoint, requiring an external singleton lock.

Provision the checkpoint out of band in a separate root-controlled directory.
This detects a log/checkpoint mismatch, not simultaneous rollback of both files.
It does not authorize recovery and is not wired into the production worker.
"""
import json
import os
import secrets
import stat
from pathlib import Path

from .files import FileLock
from .investigation_log import _HEX64, _encode

_MAX_CHECKPOINT_BYTES = 256


def _check_value(value):
    if (not isinstance(value, tuple) or len(value) != 2
            or type(value[0]) is not int or value[0] < 0
            or not isinstance(value[1], str) or not _HEX64.fullmatch(value[1])
            or (value[0] == 0 and value[1] != '0' * 64)):
        raise ValueError('Invalid checkpoint')


def _serialize(value):
    _check_value(value)
    return _encode({'sequence': value[0], 'digest': value[1]}) + b'\n'


class InvestigationCheckpoint:
    """Replace a pre-provisioned checkpoint while the worker singleton is held."""
    def __init__(self, path):
        self.path = Path(path)
        self.failed = False

    @staticmethod
    def _check_directory(info):
        FileLock._check_trusted_directory(info)
        if info.st_mode & 0o077:
            raise RuntimeError('Checkpoint directory must be root private')

    @staticmethod
    def _check_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o077):
            raise RuntimeError('Untrusted checkpoint file')

    def _directory(self):
        if not self.path.is_absolute() or '..' in self.path.parts or not self.path.name:
            raise RuntimeError('Invalid checkpoint path')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            FileLock._check_trusted_directory(os.fstat(fd))
            for part in self.path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
                FileLock._check_trusted_directory(os.fstat(fd))
            self._check_directory(os.fstat(fd))
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _read(self, directory):
        fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            self._check_file(before)
            if before.st_size <= 0 or before.st_size > _MAX_CHECKPOINT_BYTES:
                raise RuntimeError('Invalid checkpoint file size')
            data = b''
            while len(data) < before.st_size:
                chunk = os.read(fd, before.st_size - len(data))
                if not chunk:
                    raise RuntimeError('Checkpoint file changed during read')
                data += chunk
            if os.read(fd, 1):
                raise RuntimeError('Checkpoint file changed during read')
            after = os.fstat(fd)
            self._check_file(after)
            current = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                              dir_fd=directory)
            try:
                path_info = os.fstat(current)
                self._check_file(path_info)
                identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                         info.st_mtime_ns, info.st_ctime_ns)
                if identity(before) != identity(after) or identity(after) != identity(path_info):
                    raise RuntimeError('Checkpoint file changed during read')
            finally:
                os.close(current)
        finally:
            os.close(fd)
        try:
            value = json.loads(data)
            if not isinstance(value, dict) or set(value) != {'sequence', 'digest'}:
                raise ValueError()
            result = (value['sequence'], value['digest'])
            if _serialize(result) != data:
                raise ValueError()
            return result
        except (ValueError, TypeError, UnicodeError):
            raise RuntimeError('Invalid checkpoint file') from None

    def read(self):
        if self.failed:
            raise RuntimeError('Checkpoint unavailable after failure')
        try:
            directory = self._directory()
            try:
                return self._read(directory)
            finally:
                os.close(directory)
        except BaseException:
            self.failed = True
            raise

    def advance(self, expected, updated):
        """Atomically replace, then fsync the containing directory.

        Must run under the same lifetime lock as log append. On any failure the
        instance is poisoned; do not retry or infer whether replacement happened.
        """
        if self.failed:
            raise RuntimeError('Checkpoint unavailable after failure')
        directory = None
        temporary = None
        try:
            _check_value(expected)
            _check_value(updated)
            if updated[0] != expected[0] + 1:
                raise ValueError('Checkpoint must advance by one')
            directory = self._directory()
            if self._read(directory) != expected:
                raise RuntimeError('Checkpoint changed')
            temporary = '.investigation-checkpoint-' + secrets.token_hex(16)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                self._check_file(os.fstat(fd))
                remaining = memoryview(_serialize(updated))
                while remaining:
                    count = os.write(fd, remaining)
                    if count <= 0:
                        raise OSError('Checkpoint write failed')
                    remaining = remaining[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            os.fsync(directory)
        except BaseException:
            self.failed = True
            raise
        finally:
            if directory is not None:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                os.close(directory)


def append_checkpointed(log, checkpoint, event):
    """The caller must hold the singleton lock across this entire operation."""
    old = checkpoint.read()
    try:
        new = log.append(event, checkpoint=old)
        checkpoint.advance(old, new)
        return new
    except BaseException:
        checkpoint.failed = True
        raise
