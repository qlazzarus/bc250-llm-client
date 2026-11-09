import os
import stat
from types import SimpleNamespace

import pytest

from backend.app.model_control.files import FileLock
from backend.app.model_control.investigation_checkpoint import InvestigationCheckpoint, append_checkpointed, _serialize
from backend.app.model_control.investigation_log import InvestigationLog
from backend.app.model_control.investigation_evidence import evidence_digest, verify_evidence

EMPTY = (0, '0' * 64)
EVENT = {'kind': 'evidence_observed', 'operation_id': 'a' * 32, 'evidence_digest': 'b' * 64}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    # Filesystem owner/path checks are tested separately; this environment is not root.
    monkeypatch.setattr(FileLock, '_check_trusted_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_file', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationLog, '_check_file', staticmethod(lambda _: None))
    logpath = tmp_path / 'events'
    logpath.touch(mode=0o600)
    checkpointpath = tmp_path / 'checkpoint'
    checkpointpath.write_bytes(_serialize(EMPTY))
    checkpointpath.chmod(0o600)
    return InvestigationLog(logpath), InvestigationCheckpoint(checkpointpath)


def test_checkpoint_permissions():
    info = {'st_mode': stat.S_IFREG | 0o600, 'st_uid': 0, 'st_nlink': 1}
    InvestigationCheckpoint._check_file(SimpleNamespace(**info))
    for change in ({'st_uid': 1}, {'st_nlink': 2}, {'st_mode': stat.S_IFREG | 0o640},
                   {'st_mode': stat.S_IFLNK | 0o600}):
        with pytest.raises(RuntimeError):
            InvestigationCheckpoint._check_file(SimpleNamespace(**(info | change)))


def test_success_and_reopen(stores):
    log, checkpoint = stores
    first = append_checkpointed(log, checkpoint, EVENT)
    reopened = InvestigationCheckpoint(checkpoint.path)
    assert reopened.read() == first
    assert append_checkpointed(log, reopened, EVENT)[0] == 2


def test_evidence_digest_is_checkpointed_without_raw_snapshot(stores):
    log, checkpoint = stores
    evidence = {'journal': b'private journal snapshot', 'gate': b'private gate snapshot'}
    digest = evidence_digest(EVENT['operation_id'], evidence)
    tail = append_checkpointed(log, checkpoint, {**EVENT, 'evidence_digest': digest})
    data = log.path.read_bytes()
    assert InvestigationLog._verify(data) == tail == InvestigationCheckpoint(checkpoint.path).read()
    assert b'private journal snapshot' not in data
    assert verify_evidence(EVENT['operation_id'], evidence, digest)
    assert not verify_evidence(EVENT['operation_id'], {**evidence, 'gate': b'changed'}, digest)


def test_crash_after_log_fsync_before_checkpoint_replace_fails_closed(stores, monkeypatch):
    log, checkpoint = stores
    def fail(*args, **kwargs):
        raise OSError('replace failed')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        append_checkpointed(log, checkpoint, EVENT)
    with pytest.raises(RuntimeError, match='after failure'):
        checkpoint.read()
    reopened = InvestigationCheckpoint(checkpoint.path)
    assert reopened.read() == EMPTY
    with pytest.raises(RuntimeError, match='mismatch'):
        append_checkpointed(log, reopened, EVENT)


def test_directory_fsync_failure_poisoned_and_no_automatic_retry(stores, monkeypatch):
    log, checkpoint = stores
    actual_fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory fsync failed')
        actual_fsync(fd)
    monkeypatch.setattr(os, 'fsync', fail_directory)
    with pytest.raises(OSError):
        append_checkpointed(log, checkpoint, EVENT)
    with pytest.raises(RuntimeError, match='after failure'):
        append_checkpointed(log, checkpoint, EVENT)
    # Replacement may already have happened; never claim it failed or undo it.
    assert InvestigationCheckpoint(checkpoint.path).read()[0] == 1


def test_corrupt_checkpoint_and_symlink_fail(stores, tmp_path):
    _, checkpoint = stores
    checkpoint.path.write_bytes(b'{}\n')
    with pytest.raises(RuntimeError):
        checkpoint.read()
    checkpoint.path.unlink()
    checkpoint.path.symlink_to(tmp_path / 'target')
    with pytest.raises(OSError):
        InvestigationCheckpoint(checkpoint.path).read()


def test_log_fsync_failure_preserves_checkpoint_and_restart_detects_tail(stores, monkeypatch):
    log, checkpoint = stores
    def fail(_):
        raise OSError('log fsync failed')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError):
        append_checkpointed(log, checkpoint, EVENT)
    reopened = InvestigationCheckpoint(checkpoint.path)
    assert reopened.read() == EMPTY
    with pytest.raises(RuntimeError, match='mismatch'):
        append_checkpointed(log, reopened, EVENT)


def test_checkpoint_file_fsync_failure_keeps_old_checkpoint(stores, monkeypatch):
    log, checkpoint = stores
    actual_fsync = os.fsync
    count = 0
    def fail_second(fd):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('checkpoint fsync failed')
        actual_fsync(fd)
    monkeypatch.setattr(os, 'fsync', fail_second)
    with pytest.raises(OSError):
        append_checkpointed(log, checkpoint, EVENT)
    assert checkpoint.path.read_bytes() == _serialize(EMPTY)
    assert not list(checkpoint.path.parent.glob('.investigation-checkpoint-*'))
    with pytest.raises(RuntimeError, match='mismatch'):
        append_checkpointed(log, InvestigationCheckpoint(checkpoint.path), EVENT)


def test_checkpoint_rejects_canonical_prefix_with_trailing_data(stores):
    _, checkpoint = stores
    checkpoint.path.write_bytes(_serialize(EMPTY) + b' ' * 2048)
    with pytest.raises(RuntimeError):
        checkpoint.read()
    checkpoint.path.write_bytes(_serialize(EMPTY) + b'garbage')
    with pytest.raises(RuntimeError):
        InvestigationCheckpoint(checkpoint.path).read()


def test_checkpoint_handles_short_reads(stores, monkeypatch):
    _, checkpoint = stores
    actual_read = os.read
    monkeypatch.setattr(os, 'read', lambda fd, count: actual_read(fd, min(count, 3)))
    assert checkpoint.read() == EMPTY


@pytest.mark.parametrize('mutation', ['append', 'replace', 'truncate'])
def test_checkpoint_changed_during_read_fails_closed(stores, monkeypatch, mutation):
    _, checkpoint = stores
    actual_read = os.read
    changed = False
    def mutate_after_read(fd, count):
        nonlocal changed
        result = actual_read(fd, count)
        if not changed and result:
            changed = True
            if mutation == 'append':
                with checkpoint.path.open('ab') as stream:
                    stream.write(b'extra')
            elif mutation == 'truncate':
                checkpoint.path.write_bytes(b'')
            else:
                replacement = checkpoint.path.with_name('replacement')
                replacement.write_bytes(_serialize(EMPTY))
                os.replace(replacement, checkpoint.path)
        return result
    monkeypatch.setattr(os, 'read', mutate_after_read)
    with pytest.raises(RuntimeError, match='changed'):
        checkpoint.read()
