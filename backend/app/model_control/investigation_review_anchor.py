"""Read a pinned root-controlled review record; never grants authorization.

The expected digest must come from an independently provisioned trusted anchor,
not the record or the investigation log. This checks bytes and filesystem
protection only: it does not authenticate a person, establish source or freshness,
or prevent rollback of both the record and its external anchor. No worker uses it.
"""
import hashlib
import hmac
import json
import os

from .investigation_checkpoint import InvestigationCheckpoint
from .investigation_log import _encode
from .investigation_records import _HEX64, _common, _hex, replay_investigation
from .recovery_progress import RecoveryProgress

_MAX_BYTES = 4096


def read_pinned_review(path, expected_digest):
    """Read one canonical typed review pinned by an independent SHA-256 digest.

    Root-controlled directories and a private, root-owned regular file are
    required. A trusted singleton lock must exclude all legitimate writers
    during this read; privileged adversaries and later changes are not excluded.
    The digest alone is not proof of review or permission to perform recovery.
    """
    if not _hex(expected_digest, _HEX64):
        raise ValueError('Invalid review anchor')
    store = InvestigationCheckpoint(path)
    directory = store._directory()
    try:
        fd = os.open(store.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            store._check_file(before)
            if not 0 < before.st_size <= _MAX_BYTES:
                raise RuntimeError('Invalid review record size')
            data = b''
            while len(data) < before.st_size:
                part = os.read(fd, before.st_size - len(data))
                if not part:
                    raise RuntimeError('Review record changed during read')
                data += part
            if os.read(fd, 1):
                raise RuntimeError('Review record changed during read')
            after = os.fstat(fd)
            store._check_file(after)
            current = os.open(store.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
            try:
                current_info = os.fstat(current)
                store._check_file(current_info)
                def identity(info):
                    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if identity(before) != identity(after) or identity(after) != identity(current_info):
                    raise RuntimeError('Review record changed during read')
            finally:
                os.close(current)
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_digest):
        raise RuntimeError('Review anchor mismatch')
    try:
        record = json.loads(data)
        if (not _common(record, 'review', {'observation_digest', 'review_digest'})
                or not _hex(record['observation_digest'], _HEX64)
                or not _hex(record['review_digest'], _HEX64)
                or _encode(record) + b'\n' != data):
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise RuntimeError('Invalid review record') from None
    return record


def replay_with_pinned_review(records, path, expected_digest):
    """Diagnose replay only when its review equals the pinned protected record.

    Every result remains unresolved and grants no authorization. An anchor or
    filesystem failure, missing review, or mismatch yields ``unverified``.
    The caller must hold the same singleton lock required by read_pinned_review.
    """
    blocked = RecoveryProgress('unverified')
    progress = replay_investigation(records)
    if progress.status == 'unverified':
        return blocked
    try:
        review = read_pinned_review(path, expected_digest)
        if review != records[1]:
            return blocked
    except (OSError, RuntimeError, ValueError, TypeError):
        return blocked
    return progress
