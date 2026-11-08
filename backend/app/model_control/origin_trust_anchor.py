"""Root-administered origin trust anchor and cooperative lock building blocks.

Neither function authorizes deployment or proves origin safety. All participants
must hold the same pre-created lock across their entire critical section.
"""
import contextlib
import fcntl
import os
import re
import stat
from pathlib import Path

ANCHOR = Path('/etc/bc250-model-control/origin-manifest.sha256')
LOCK = Path('/run/bc250-model-control/origin.lock')


def _check_entry(fd, *, directory):
    info = os.fstat(fd)
    if (info.st_uid != 0 or info.st_mode & 0o022
            or (not stat.S_ISDIR(info.st_mode) if directory else
                not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077)):
        raise RuntimeError('Untrusted origin control path')
    # Reject ACLs rather than trying to infer effective write permissions.
    if not hasattr(os, 'listxattr'):
        raise RuntimeError('Cannot inspect origin control ACLs')
    if set(os.listxattr(fd)) & {'system.posix_acl_access', 'system.posix_acl_default'}:
        raise RuntimeError('Origin control path has an ACL')
    return info


def _open_parent(path):
    if not path.is_absolute() or '..' in path.parts or path == Path('/'):
        raise ValueError('Invalid origin control path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _check_entry(fd, directory=True)
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            _check_entry(fd, directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_control(path, flags):
    parent = _open_parent(path)
    try:
        fd = os.open(path.name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            _check_entry(fd, directory=False)
            return parent, fd
        except BaseException:
            os.close(fd)
            raise
    except BaseException:
        os.close(parent)
        raise


def _same_file(parent, fd, name):
    opened = os.fstat(fd)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)):
        raise RuntimeError('Origin control file changed')
    _check_entry(fd, directory=False)


def read_origin_manifest_digest():
    """Read the fixed root-reviewed anchor; never promote a supplied digest."""
    parent, fd = _open_control(ANCHOR, os.O_RDONLY)
    try:
        before = os.fstat(fd)
        raw = os.read(fd, 66)
        after = os.fstat(fd)
        if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or not re.fullmatch(rb'[0-9a-f]{64}\n', raw)):
            raise RuntimeError('Invalid or changed origin trust anchor')
        _same_file(parent, fd, ANCHOR.name)
        return raw[:-1].decode('ascii')
    finally:
        os.close(fd)
        os.close(parent)


@contextlib.contextmanager
def origin_change_lock():
    """Hold the existing exclusive cooperative lock; do not create or replace it."""
    parent, fd = _open_control(LOCK, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Origin change lock is busy') from None
        _same_file(parent, fd, LOCK.name)
        try:
            yield
            _same_file(parent, fd, LOCK.name)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
        os.close(parent)
