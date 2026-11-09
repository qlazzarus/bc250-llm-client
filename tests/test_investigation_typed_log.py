import os

import pytest

from backend.app.model_control.files import FileLock
from backend.app.model_control.investigation_checkpoint import InvestigationCheckpoint, _serialize
from backend.app.model_control.investigation_log import InvestigationLog
from backend.app.model_control.investigation_typed_log import TypedInvestigationLog
from test_investigation_records import records

EMPTY = (0, '0' * 64)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(FileLock, '_check_trusted_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_file', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationLog, '_check_file', staticmethod(lambda _: None))
    logpath = tmp_path / 'typed'
    logpath.touch(mode=0o600)
    path = tmp_path / 'checkpoint'
    path.write_bytes(_serialize(EMPTY))
    return TypedInvestigationLog(logpath), InvestigationCheckpoint(path)


def test_write_restart_and_replay(stores):
    log, checkpoint = stores
    snapshots = [records()[:1], records()[:2], records()[:3], records()]
    for snapshot in snapshots:
        log.append_snapshot(checkpoint, snapshot)
    assert log.read_snapshots(InvestigationCheckpoint(checkpoint.path)) == snapshots


def test_rejects_partial_invalid_and_secret_fields(stores):
    log, checkpoint = stores
    for invalid in ([], [*records()[:1], {**records()[1], 'secret': 'value'}]):
        with pytest.raises(ValueError):
            log.append_snapshot(checkpoint, invalid)
        checkpoint = InvestigationCheckpoint(checkpoint.path)
    assert log.path.read_bytes() == b''


def test_legacy_and_partial_log_fail_closed(stores):
    log, checkpoint = stores
    legacy = InvestigationLog(log.path)
    legacy.append({'kind': 'reviewed', 'operation_id': 'a' * 32, 'evidence_digest': 'b' * 64}, checkpoint=EMPTY)
    with pytest.raises(RuntimeError):
        log.read_snapshots(checkpoint)
    log.path.write_bytes(b'{"partial":')
    with pytest.raises(RuntimeError):
        log.read_snapshots(InvestigationCheckpoint(checkpoint.path))


def test_restart_rejects_valid_old_tail_and_uncheckpointed_append(stores, monkeypatch):
    log, checkpoint = stores
    log.append_snapshot(checkpoint, records()[:2])
    old = log.path.read_bytes()
    log.append_snapshot(checkpoint, records()[:3])
    latest = log.path.read_bytes()
    log.path.write_bytes(old)
    with pytest.raises(RuntimeError, match='mismatch'):
        log.read_snapshots(InvestigationCheckpoint(checkpoint.path))
    log.path.write_bytes(latest)
    def fail(*args, **kwargs):
        raise OSError('checkpoint replace failed')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        log.append_snapshot(InvestigationCheckpoint(checkpoint.path), records())
    with pytest.raises(RuntimeError, match='mismatch'):
        log.read_snapshots(InvestigationCheckpoint(checkpoint.path))


@pytest.mark.parametrize('change', ['truncate', 'duplicate', 'replace_observation', 'replace_attempt'])
def test_snapshot_history_cannot_regress_or_replace(stores, change):
    log, checkpoint = stores
    initial = records()[:3]
    log.append_snapshot(checkpoint, initial)
    candidate = records()
    if change == 'truncate':
        candidate = candidate[:2]
    elif change == 'duplicate':
        candidate = candidate[:3]
    elif change == 'replace_observation':
        candidate[0]['observed_at'] += 1
        from backend.app.model_control.investigation_records import observation_digest
        for event in candidate[1:]:
            event['observation_digest'] = observation_digest(candidate[0])
    else:
        candidate[2]['attempt'] = candidate[3]['attempt'] = '4' * 64
    with pytest.raises(RuntimeError, match='extend'):
        log.append_snapshot(checkpoint, candidate)


def test_replay_checks_history_even_with_valid_chain_and_checkpoint(stores, monkeypatch):
    log, checkpoint = stores
    log.append_snapshot(checkpoint, records()[:3])
    monkeypatch.setattr(TypedInvestigationLog, '_validate_append', staticmethod(lambda *_: None))
    log.append_snapshot(checkpoint, records()[:2])
    with pytest.raises(RuntimeError, match='extend'):
        log.read_snapshots(InvestigationCheckpoint(checkpoint.path))


def test_attempt_cannot_be_reused_across_investigations(stores, monkeypatch):
    from backend.app.model_control.investigation_records import observation_digest

    log, checkpoint = stores
    first = records()[:3]
    second = records()[:3]
    for record in second:
        record['investigation_id'] = 'f' * 32
    digest = observation_digest(second[0])
    for record in second[1:]:
        record['observation_digest'] = digest

    log.append_snapshot(checkpoint, first)
    with pytest.raises(RuntimeError, match='attempt reused'):
        log.append_snapshot(checkpoint, second)

    # Even a valid hash chain and checkpoint cannot make the conflict valid.
    monkeypatch.setattr(TypedInvestigationLog, '_validate_append', staticmethod(lambda *_: None))
    log.append_snapshot(InvestigationCheckpoint(checkpoint.path), second)
    with pytest.raises(RuntimeError, match='attempt reused'):
        log.read_snapshots(InvestigationCheckpoint(checkpoint.path))
