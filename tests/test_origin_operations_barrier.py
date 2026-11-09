import os
import sqlite3

import pytest

from backend.app.model_control import origin_deployment_barrier as barrier
from backend.app.model_control.operations import SwitchOperations


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / 'operations.sqlite3'
    operations = SwitchOperations(path)
    path.chmod(0o600)
    monkeypatch.setattr(barrier, 'OPERATIONS', path)
    monkeypatch.setattr(barrier, '_open_parent', lambda path: os.open(path.parent, os.O_RDONLY))
    monkeypatch.setattr(barrier, '_check_journal_file', lambda fd: os.fstat(fd))
    return path, operations


def test_empty_and_terminal_operations_are_clear(db):
    _, operations = db
    barrier._require_clear_operations()
    operation, _ = operations.submit('admin', 'new', 'test')
    operations.transition(operation['id'], 'waiting_for_gpu')
    operations.transition(operation['id'], 'failed', error_code='admission_blocked')
    barrier._require_clear_operations()


def test_unresolved_operation_in_live_wal_blocks(db):
    path, operations = db
    with sqlite3.connect(path) as live:
        live.execute('PRAGMA journal_mode=WAL')
        operations.submit('admin', 'new', 'test')
        wal = path.with_name(path.name + '-wal')
        assert wal.exists() and wal.stat().st_size > 0
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in path.parent.iterdir()}
        with pytest.raises(RuntimeError, match='Nonempty operation WAL'):
            barrier._require_clear_operations()
        assert before == {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in path.parent.iterdir()}


def test_missing_or_corrupt_database_blocks(db):
    path, _ = db
    path.unlink()
    with pytest.raises(FileNotFoundError):
        barrier._require_clear_operations()
    path.write_bytes(b'not a database')
    path.chmod(0o600)
    with pytest.raises(sqlite3.DatabaseError):
        barrier._require_clear_operations()


def test_terminal_database_with_corrupt_wal_blocks(db):
    path, operations = db
    operation, _ = operations.submit('admin', 'new', 'terminal')
    operations.transition(operation['id'], 'waiting_for_gpu')
    operations.transition(operation['id'], 'failed', error_code='admission_blocked')
    wal = path.with_name(path.name + '-wal')
    wal.write_bytes(b'corrupt WAL frame')
    wal.chmod(0o600)
    before = wal.read_bytes()
    with pytest.raises(RuntimeError, match='Nonempty operation WAL'):
        barrier._require_clear_operations()
    assert wal.read_bytes() == before
