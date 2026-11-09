"""Fail-closed deployment guard; callers must use it for the entire change.

This is a building block, not evidence that all origin writers participate.
The durable switch journal is independently maintained by the model worker.
"""
import contextlib
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

from .barrier import decode_switch_journal
from .origin_trust_anchor import _open_parent, origin_change_lock

JOURNAL = Path('/var/lib/bc250-model-control/switch.json')
MAX_JOURNAL = 8192
OPERATIONS = Path('/var/lib/bc250-model-control/operations.sqlite3')
MAX_DATABASE_COMPONENT = 64 * 1024 * 1024


def _clear_gate_epoch(state):
    """Validate the narrow diagnostic protocol; do not infer authenticity."""
    fields = {'protocol', 'durable', 'epoch', 'holds', 'paused', 'recovery_exclusive_hold'}
    if (not isinstance(state, dict) or set(state) != fields
            or type(state['protocol']) is not int or state['protocol'] != 3
            or state['durable'] is not True
            or not isinstance(state['epoch'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', state['epoch'])
            or type(state['holds']) is not list or state['holds'] != []
            or state['paused'] is not False
            or state['recovery_exclusive_hold'] is not None):
        raise RuntimeError('Gate state is not clear or supported')
    return state['epoch']


def inspect_clear_deployment_records(read_gate):
    """Compare read-only observations around journal and database checks.

    The caller must hold the origin lock. This diagnostic cannot authenticate
    the reader or prove serialization, completeness, or a durable exclusion;
    even successful observations never authorize deployment.
    """
    before = _clear_gate_epoch(read_gate())
    _require_clear_journal()
    _require_clear_operations()
    after = _clear_gate_epoch(read_gate())
    if before != after:
        raise RuntimeError('Gate epoch changed during deployment inspection')


def _check_journal_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or info.st_mode & 0o027):
        raise RuntimeError('Untrusted origin deployment barrier')
    if not hasattr(os, 'listxattr'):
        raise RuntimeError('Cannot inspect deployment barrier ACL')
    if set(os.listxattr(fd)) & {'system.posix_acl_access', 'system.posix_acl_default'}:
        raise RuntimeError('Deployment barrier has an ACL')
    return info


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _require_clear_journal():
    """Read the fixed root-managed journal while the origin lock is held."""
    parent = _open_parent(JOURNAL)
    try:
        fd = os.open(JOURNAL.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            before = _check_journal_file(fd)
            if before.st_size > MAX_JOURNAL:
                raise RuntimeError('Deployment barrier is too large')
            data = os.read(fd, MAX_JOURNAL + 1)
            after = _check_journal_file(fd)
            current = os.stat(JOURNAL.name, dir_fd=parent, follow_symlinks=False)
            if (len(data) != before.st_size or _identity(before) != _identity(after)
                    or _identity(before) != _identity(current)):
                raise RuntimeError('Deployment barrier changed')
            if decode_switch_journal(data) != {}:
                raise RuntimeError('Unresolved model switch blocks origin deployment')
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _database_component(parent, name, *, required):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    except FileNotFoundError:
        if required:
            raise
        return None
    try:
        info = _check_journal_file(fd)
        if info.st_mode & 0o077 or info.st_size > MAX_DATABASE_COMPONENT:
            raise RuntimeError('Untrusted operation database component')
        data = bytearray()
        while len(data) <= MAX_DATABASE_COMPONENT:
            chunk = os.read(fd, min(1024 * 1024, MAX_DATABASE_COMPONENT + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = _check_journal_file(fd)
        if len(data) != info.st_size or _identity(info) != _identity(after):
            raise RuntimeError('Operation database changed during read')
        return _identity(info), bytes(data)
    finally:
        os.close(fd)


def _require_clear_operations():
    """Inspect a stable private database copy; reject any nonempty WAL."""
    parent = _open_parent(OPERATIONS)
    names = (OPERATIONS.name, OPERATIONS.name + '-wal', OPERATIONS.name + '-shm')
    try:
        parts = [_database_component(parent, name, required=i == 0)
                 for i, name in enumerate(names)]
        # Check replacement, removal and creation after the snapshot, including
        # optional WAL/SHM. A changing source is not a reliable snapshot.
        for name, part in zip(names, parts):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if part is not None:
                    raise RuntimeError('Operation database changed') from None
                continue
            if part is None or _identity(info) != part[0]:
                raise RuntimeError('Operation database changed')
        # SQLite can silently stop at an invalid WAL frame. Without independent
        # full-frame and commit validation, accepting its recovered prefix can
        # miss a later unresolved operation. Require a checkpointed database.
        if parts[1] is not None and parts[1][1]:
            raise RuntimeError('Nonempty operation WAL blocks origin deployment')
        with tempfile.TemporaryDirectory(prefix='origin-barrier-') as directory:
            copy = Path(directory) / 'operations.sqlite3'
            copy.write_bytes(parts[0][1])
            # Never reuse the source's shared-memory index. SQLite reconstructs
            # it for this private copy; source files remain untouched.
            with closing(sqlite3.connect(copy.as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('PRAGMA trusted_schema=OFF')
                if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise RuntimeError('Invalid operation database')
                table = db.execute("SELECT type FROM sqlite_schema WHERE name='switch_operations'").fetchall()
                columns = {row[1] for row in db.execute("PRAGMA table_info(switch_operations)")}
                expected = {'id', 'principal', 'idempotency_key', 'model', 'state', 'error_code',
                            'created_at', 'updated_at', 'previous_model', 'current_model', 'hold_id'}
                if table != [('table',)] or columns != expected:
                    raise RuntimeError('Invalid operation database schema')
                if db.execute("SELECT 1 FROM switch_operations WHERE state IS NULL OR "
                              "typeof(state) != 'text' OR state NOT IN "
                              "('succeeded', 'failed', 'failed_rolled_back') LIMIT 1").fetchone():
                    raise RuntimeError('Unresolved operation blocks origin deployment')
    finally:
        os.close(parent)


@contextlib.contextmanager
def origin_deployment_guard():
    """Lock, check the journal, and hold the lock through an origin change.

    Failure to read or validate the journal must abort the change. The guard
    remains unavailable until gate hold consistency can be proved under a
    supported, durable exclusion contract; a snapshot alone is insufficient.
    """
    with origin_change_lock():
        _require_clear_journal()
        _require_clear_operations()
        raise RuntimeError('Gate hold consistency is not supported for origin deployment')
        yield  # pragma: no cover -- retains the context-manager interface
