import hashlib

import pytest

from backend.app.model_control.files import FileLock
from backend.app.model_control.investigation_archive import InvestigationArchive
from backend.app.model_control.investigation_checkpoint import InvestigationCheckpoint
from backend.app.model_control.investigation_diagnosis import diagnose_archived_investigation
from backend.app.model_control.investigation_log import _encode
from backend.app.model_control.investigation_records import observation_digest
from test_investigation_records import records


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(FileLock, '_check_trusted_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_directory', staticmethod(lambda _: None))
    monkeypatch.setattr(InvestigationCheckpoint, '_check_file', staticmethod(lambda _: None))
    monkeypatch.setattr('backend.app.model_control.investigation_archive.protected_xattrs', lambda _: {})
    events = records()
    archive = InvestigationArchive(tmp_path / 'archive')
    events[0]['evidence_digest'] = archive.write('operation', events[0]['operation_id'], {'journal': b'{}'})
    digest = observation_digest(events[0])
    for record in events[1:]: record['observation_digest'] = digest
    review = tmp_path / 'review'
    data = _encode(events[1]) + b'\n'
    review.write_bytes(data)
    return events, archive, review, hashlib.sha256(data).hexdigest()


def test_diagnosis_preserves_unresolved_outcome(case):
    events, archive, review, anchor = case
    assert diagnose_archived_investigation(events, archive, review, anchor).status == 'reconciliation_required'
    assert diagnose_archived_investigation(events[:3], archive, review, anchor).status == 'outcome_unknown'


@pytest.mark.parametrize('change', ['archive', 'review', 'record'])
def test_diagnosis_rejects_broken_evidence_binding(case, change):
    events, archive, review, anchor = case
    if change == 'archive': archive.store.path.write_bytes(b'{}\n')
    elif change == 'review': review.write_bytes(b'{}\n')
    else: events[0]['evidence_digest'] = 'f' * 64
    assert diagnose_archived_investigation(events, archive, review, anchor).status == 'unverified'
