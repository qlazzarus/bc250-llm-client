from dataclasses import replace

import pytest

from backend.app.memory_dedup import ActiveMemory, Candidate, VerifiedApproval, compare_memory, map_shared_fact
from backend.app.memory_repository import _fingerprint


@pytest.mark.parametrize(('text', 'subject', 'predicate', 'value'), [
    ('BC250 서버 운영체제는 Fedora입니다.', 'bc250_server', 'operating_system', 'Fedora'),
    ('BC-250의 OS: Fedora 43', 'bc250_server', 'operating_system', 'Fedora 43'),
    ('BC 250 서버 백업 경로: /srv/backup', 'bc250_server', 'backup_path', '/srv/backup'),
    ('BC250 서버 백업 주기는 매일', 'bc250_server', 'backup_frequency', '매일'),
    ('BC250 client 서비스 포트 = 8010', 'bc250_client', 'service_port', '8010'),
])
def test_map_only_explicit_shared_fact_with_exact_source_span(text, subject, predicate, value):
    result = map_shared_fact(text)
    assert result is not None
    assert (result.subject, result.predicate, result.value) == (subject, predicate, value)
    assert text[result.start:result.end] == value


@pytest.mark.parametrize('text', [
    '포트는 8010', '서버 운영체제는 Fedora', '다른 서버 운영체제는 Fedora',
    'BC250 서버 운영체제는 Fedora가 아니야', 'BC250 서버 운영체제는 Fedora야?',
    'BC250 서버 운영체제는 Ubuntu일 수도 있어', 'BC250 서버 운영체제는 Fedora로 바꿀 계획이야',
    'BC250 서버 운영체제는 Fedora이고 다른 서버는 Ubuntu야',
    'BC250 서버 백업 주기는 어제', 'BC250 client 포트는 0', 'BC250 client 포트는 65536',
    'BC250 client 포트는 8010 또는 8020', '내 서버 BC250 운영체제는 Fedora',
    'BC250 서버 운영체제는 Fedora라고 기억하지 마',
])
def test_ambiguous_negative_future_multiple_or_private_statements_are_held(text):
    assert map_shared_fact(text) is None


def candidate(value='Fedora'):
    return Candidate('bc250_server', 'operating_system', value, 0, len(value))


def active(value='Fedora'):
    digest = _fingerprint('shared', 'bc250_server', 'operating_system', 'fact', value)[1]
    return ActiveMemory('record-1', 'shared', 'bc250_server', 'operating_system', 'fact', value, digest, 'rev-1')


def test_authoritative_lookup_and_conflict_are_fail_closed():
    assert compare_memory(candidate(), None, lookup_complete=False).action == 'hold'
    assert compare_memory(candidate(), None, lookup_complete=True).reason == 'legacy_uncertain'
    assert compare_memory(candidate(), None, lookup_complete=True, legacy_clear=True).action == 'create_candidate'
    assert compare_memory(candidate(), None, lookup_complete=True, explicit_correction=True).action == 'hold'
    assert compare_memory(candidate('Ubuntu'), active(), lookup_complete=True).action == 'confirm_conflict'
    result = compare_memory(candidate('Ubuntu'), active(), lookup_complete=True, explicit_correction=True)
    assert (result.action, result.record_id) == ('supersede_candidate', 'record-1')
    assert compare_memory(candidate('Ubuntu'), active(), lookup_complete=True, explicit_correction='yes').action == 'confirm_conflict'


def test_nfc_whitespace_exact_duplicate_but_other_changes_conflict():
    assert compare_memory(candidate('Cafe\u0301  43'), active('Café 43'), lookup_complete=True).action == 'duplicate'
    for value in ['fedora', 'Fedora 43', 'Fedora 아님', 'Fedora 42']:
        assert compare_memory(candidate(value), active(), lookup_complete=True).action == 'confirm_conflict'


def test_mismatched_slot_kind_revision_and_invalid_fingerprint_are_held():
    for record in [replace(active(), subject='other'), replace(active(), kind='plan'),
                   replace(active(), revision=' '), replace(active(), fingerprint='incorrect')]:
        assert compare_memory(candidate(), record, lookup_complete=True).action == 'hold'
    assert compare_memory(replace(candidate(), scope='personal'), active(), lookup_complete=True).action == 'hold'
    assert compare_memory(replace(candidate(), kind='plan'), active(), lookup_complete=True).action == 'hold'


def test_verified_approval_is_bound_to_record_revision_and_candidate():
    proposed = candidate('Ubuntu')
    fingerprint = _fingerprint('shared', proposed.subject, proposed.predicate, 'fact', proposed.value)[1]
    approval = VerifiedApproval('record-1', 'rev-1', fingerprint)
    assert compare_memory(proposed, active(), lookup_complete=True, approval=approval).reason == 'verified_approval'
    for stale in [replace(approval, record_id='other'), replace(approval, revision='rev-2'),
                  replace(approval, candidate_fingerprint='different')]:
        assert compare_memory(proposed, active(), lookup_complete=True, approval=stale).reason == 'approval_stale'
    assert compare_memory(proposed, None, lookup_complete=True, legacy_clear=True, approval=approval).action == 'hold'


def test_hash_collision_does_not_create_false_duplicate(monkeypatch):
    import backend.app.memory_dedup as module
    monkeypatch.setattr(module, '_fingerprint', lambda scope, subject, predicate, kind, value: (value, 'same'))
    assert compare_memory(candidate('Ubuntu'), replace(active(), fingerprint='same'), lookup_complete=True).reason == 'fingerprint_collision'
