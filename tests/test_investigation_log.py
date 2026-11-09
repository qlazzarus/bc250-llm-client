import os

import pytest

from backend.app.model_control.files import FileLock
from backend.app.model_control.investigation_log import InvestigationLog


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / 'investigation.log'
    path.touch(mode=0o600)
    # Local tests are not root and their temporary directory is not root owned.
    monkeypatch.setattr(FileLock, '_check_trusted_directory', lambda info: None)
    monkeypatch.setattr(InvestigationLog, '_check_file', staticmethod(lambda info: None))
    return InvestigationLog(path)


EMPTY = (0, '0' * 64)


def event(kind='evidence_observed'):
    return {'kind': kind, 'operation_id': 'a' * 32, 'evidence_digest': 'b' * 64}


def test_append_reopens_and_verifies_chain(log):
    first = log.append(event(), checkpoint=EMPTY)
    second = InvestigationLog(log.path).append(event('reviewed'), checkpoint=first)
    assert first != second
    assert InvestigationLog._verify(log.path.read_bytes()) == second


def test_corruption_and_partial_write_fail_closed(log):
    checkpoint = log.append(event(), checkpoint=EMPTY)
    original = log.path.read_bytes()
    log.path.write_bytes(original.replace(b'evidence_observed', b'evidence_observeD'))
    with pytest.raises(RuntimeError):
        log.append(event(), checkpoint=checkpoint)
    log.path.write_bytes(original[:-1])
    with pytest.raises(RuntimeError):
        log.append(event(), checkpoint=checkpoint)


def test_rejects_raw_evidence_and_fsync_failure(log, monkeypatch):
    with pytest.raises(ValueError):
        log.append({**event(), 'raw': 'secret'}, checkpoint=EMPTY)
    def fail(_):
        raise OSError('fsync failed')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError):
        log.append(event(), checkpoint=EMPTY)


def test_file_permission_check_rejects_untrusted_metadata():
    from types import SimpleNamespace
    import stat
    valid = dict(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
    InvestigationLog._check_file(SimpleNamespace(**valid))
    for change in ({'st_uid': 1}, {'st_nlink': 2}, {'st_mode': stat.S_IFREG | 0o640},
                   {'st_mode': stat.S_IFLNK | 0o600}):
        with pytest.raises(RuntimeError):
            InvestigationLog._check_file(SimpleNamespace(**(valid | change)))


def test_symlink_file_is_rejected(log, tmp_path):
    log.path.unlink()
    target = tmp_path / 'target'
    target.touch()
    log.path.symlink_to(target)
    with pytest.raises(OSError):
        log.append(event(), checkpoint=EMPTY)


def test_checkpoint_detects_tail_truncation_and_interrupted_advance(log):
    first = log.append(event(), checkpoint=EMPTY)
    prefix = log.path.read_bytes()
    second = log.append(event('reviewed'), checkpoint=first)
    # Crash before checkpoint is advanced: the file is ahead.
    with pytest.raises(RuntimeError, match='checkpoint mismatch'):
        log.append(event(), checkpoint=first)
    # A valid earlier prefix cannot satisfy a later trusted checkpoint.
    log.path.write_bytes(prefix)
    with pytest.raises(RuntimeError, match='checkpoint mismatch'):
        log.append(event(), checkpoint=second)


@pytest.mark.parametrize('change', ['replace', 'delete'])
def test_path_change_during_append_fails_closed(log, monkeypatch, change):
    real_fsync = os.fsync
    def change_path(fd):
        real_fsync(fd)
        if change == 'replace':
            replacement = log.path.with_name('replacement')
            replacement.touch(mode=0o600)
            os.replace(replacement, log.path)
        else:
            log.path.unlink()
    monkeypatch.setattr(os, 'fsync', change_path)
    with pytest.raises((RuntimeError, OSError)):
        log.append(event(), checkpoint=EMPTY)


def test_path_replacement_before_lock_fails_closed(log, monkeypatch):
    import fcntl
    real_flock = fcntl.flock
    def replace_then_lock(fd, operation):
        replacement = log.path.with_name('replacement')
        replacement.touch(mode=0o600)
        os.replace(replacement, log.path)
        real_flock(fd, operation)
    monkeypatch.setattr(fcntl, 'flock', replace_then_lock)
    with pytest.raises(RuntimeError, match='path changed'):
        log.append(event(), checkpoint=EMPTY)
    assert log.path.read_bytes() == b''
