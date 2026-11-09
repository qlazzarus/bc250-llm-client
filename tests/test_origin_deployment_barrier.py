import json
import os
from contextlib import contextmanager

import pytest

from backend.app.model_control import origin_deployment_barrier as barrier


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / 'switch.json'
    path.write_text('{}')
    monkeypatch.setattr(barrier, 'JOURNAL', path)
    monkeypatch.setattr(barrier, '_open_parent', lambda path: os.open(path.parent, os.O_RDONLY))
    monkeypatch.setattr(barrier, '_check_journal_file', lambda fd: os.fstat(fd))
    monkeypatch.setattr(barrier, '_require_clear_operations', lambda: None)
    return path


def test_deployment_guard_checks_journal_inside_lock(journal, monkeypatch):
    events = []
    @contextmanager
    def lock():
        events.append('lock')
        try:
            yield
        finally:
            events.append('unlock')
    monkeypatch.setattr(barrier, 'origin_change_lock', lock)
    with pytest.raises(RuntimeError, match='Gate hold consistency'):
        with barrier.origin_deployment_guard():
            pytest.fail('change ran without a gate consistency guarantee')
    assert events == ['lock', 'unlock']


@pytest.mark.parametrize('content', [b'', b'null', b'{broken', b'[]', b' ' * 8193,
    json.dumps({'hold_id': 'switch-' + 'a' * 32, 'old': 'old', 'target': 'new',
                'stage': 'switching', 'mutation_started': True}).encode()])
def test_nonclear_or_unreadable_journal_blocks_change(journal, monkeypatch, content):
    monkeypatch.setattr(barrier, 'origin_change_lock', lambda: contextmanager(lambda: (yield))())
    journal.write_bytes(content)
    with pytest.raises((RuntimeError, ValueError)):
        with barrier.origin_deployment_guard():
            pytest.fail('change ran')


def test_missing_or_symlink_journal_blocks_change(journal):
    journal.unlink()
    with pytest.raises(OSError):
        barrier._require_clear_journal()
    journal.symlink_to('/dev/null')
    with pytest.raises(OSError):
        barrier._require_clear_journal()


def test_journal_replacement_during_read_is_rejected(journal, monkeypatch):
    original = barrier.os.read
    def replace(fd, size):
        result = original(fd, size)
        replacement = journal.with_name('replacement')
        replacement.write_text('{}')
        replacement.replace(journal)
        return result
    monkeypatch.setattr(barrier.os, 'read', replace)
    with pytest.raises(RuntimeError, match='changed'):
        barrier._require_clear_journal()


def test_barrier_rejects_untrusted_owner_mode_and_acl(tmp_path, monkeypatch):
    from types import SimpleNamespace
    path = tmp_path / 'journal'
    path.write_text('{}')
    fd = os.open(path, os.O_RDONLY)
    try:
        monkeypatch.setattr(barrier.os, 'fstat', lambda _: SimpleNamespace(
            st_mode=0o100640, st_uid=0, st_nlink=1))
        monkeypatch.setattr(barrier.os, 'listxattr', lambda _: ['system.posix_acl_access'], raising=False)
        with pytest.raises(RuntimeError, match='ACL'):
            barrier._check_journal_file(fd)
        monkeypatch.setattr(barrier.os, 'fstat', lambda _: SimpleNamespace(
            st_mode=0o100666, st_uid=0, st_nlink=1))
        with pytest.raises(RuntimeError, match='Untrusted'):
            barrier._check_journal_file(fd)
        monkeypatch.setattr(barrier.os, 'fstat', lambda _: SimpleNamespace(
            st_mode=0o100600, st_uid=123, st_nlink=1))
        with pytest.raises(RuntimeError, match='Untrusted'):
            barrier._check_journal_file(fd)
    finally:
        os.close(fd)


def test_busy_lock_does_not_enter_change(journal, monkeypatch):
    @contextmanager
    def busy():
        raise RuntimeError('lock busy')
        yield
    monkeypatch.setattr(barrier, 'origin_change_lock', busy)
    with pytest.raises(RuntimeError, match='lock busy'):
        with barrier.origin_deployment_guard():
            pytest.fail('change ran')


def clear_gate():
    return {'protocol': 3, 'durable': True, 'epoch': 'a' * 32, 'holds': [],
            'paused': False, 'recovery_exclusive_hold': None}


def test_gate_diagnostic_reads_around_local_records(journal, monkeypatch):
    events = []
    monkeypatch.setattr(barrier, '_require_clear_journal', lambda: events.append('journal'))
    monkeypatch.setattr(barrier, '_require_clear_operations', lambda: events.append('operations'))
    def read_gate():
        events.append('gate')
        return clear_gate()
    barrier.inspect_clear_deployment_records(read_gate)
    assert events == ['gate', 'journal', 'operations', 'gate']


@pytest.mark.parametrize('change', [
    {'protocol': True}, {'durable': False}, {'durable': 1},
    {'epoch': 'invalid'}, {'epoch': 123}, {'holds': ()}, {'holds': None},
    {'holds': ['switch-' + 'a' * 32], 'paused': True},
    {'holds': ['admin-maintenance'], 'paused': True},
    {'paused': True}, {'paused': 0}, {'recovery_exclusive_hold': 'switch-' + 'a' * 32},
    {'extra': 1},
])
def test_gate_diagnostic_rejects_unsupported_state(journal, change):
    state = clear_gate()
    state.update(change)
    with pytest.raises(RuntimeError, match='Gate state'):
        barrier.inspect_clear_deployment_records(lambda: state)


def test_gate_diagnostic_rejects_epoch_change(journal):
    changed = clear_gate()
    changed['epoch'] = 'b' * 32
    observations = iter([clear_gate(), changed])
    with pytest.raises(RuntimeError, match='epoch changed'):
        barrier.inspect_clear_deployment_records(lambda: next(observations))


@pytest.mark.parametrize('missing', list(clear_gate()))
def test_gate_diagnostic_rejects_missing_fields(journal, missing):
    state = clear_gate()
    del state[missing]
    with pytest.raises(RuntimeError, match='Gate state'):
        barrier.inspect_clear_deployment_records(lambda: state)
