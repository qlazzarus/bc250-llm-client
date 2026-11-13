"""Conservative, side-effect-free shared-memory classification and comparison.

A match is a candidate, not authorization to write. Callers must verify source
identity, legacy coverage, current revision and any approval before writing.
"""
from dataclasses import dataclass
import re
from typing import Literal

from .memory_repository import SHARED_SLOTS, _fingerprint


@dataclass(frozen=True)
class Candidate:
    subject: str
    predicate: str
    value: str
    start: int
    end: int
    kind: str = 'fact'
    scope: str = 'shared'


# Whole-message templates deliberately reject conversational ambiguity. The
# exact captured value and offsets remain relative to the original message.
_SUBJECT_SERVER = r'(?:BC[ -]?250\s*서버|BC[ -]?250)'
_SUBJECT_CLIENT = r'(?:BC[ -]?250\s*(?:클라이언트|client))'
_TEMPLATES = (
    (_SUBJECT_SERVER, r'(?:운영체제|OS)', 'bc250_server', 'operating_system', r'[A-Za-z][A-Za-z0-9._-]*(?:\s+[0-9][A-Za-z0-9._-]*)?'),
    (_SUBJECT_SERVER, r'백업\s*경로', 'bc250_server', 'backup_path', r'/[A-Za-z0-9._/-]+'),
    (_SUBJECT_SERVER, r'백업\s*주기', 'bc250_server', 'backup_frequency', r'(?:매일|매주|매월|[0-9]+\s*(?:시간|일|주|개월))'),
    (_SUBJECT_CLIENT, r'(?:서비스\s*)?포트', 'bc250_client', 'service_port', r'[0-9]{1,5}'),
)


def map_shared_fact(text: str) -> Candidate | None:
    """Map only explicitly scoped, affirmative, unambiguous statements.

    No context inference, negation handling, question answering, or semantic
    extraction is attempted; unknown expressions return None.
    """
    if not isinstance(text, str) or len(text) > 800:
        return None
    for subject_pattern, predicate_pattern, subject, predicate, value_pattern in _TEMPLATES:
        pattern = (rf'\s*{subject_pattern}\s*(?:의\s*|의\s+)?{predicate_pattern}'
                   rf'\s*(?:은|는|이|가|:|=)\s*(?P<value>{value_pattern})'
                   rf'\s*(?:입니다|이다|이야|야)?[.!]?\s*')
        match = re.fullmatch(pattern, text, re.IGNORECASE)
        if match:
            value = match.group('value')
            if predicate == 'service_port' and not 1 <= int(value) <= 65535:
                return None
            return Candidate(subject, predicate, value, match.start('value'), match.end('value'))
    return None


@dataclass(frozen=True)
class ActiveMemory:
    record_id: str
    scope: str
    subject: str
    predicate: str
    kind: str
    value: str
    fingerprint: str
    revision: str


@dataclass(frozen=True)
class Comparison:
    action: Literal['create_candidate', 'duplicate', 'confirm_conflict', 'supersede_candidate', 'hold']
    reason: str
    record_id: str | None = None


@dataclass(frozen=True)
class VerifiedApproval:
    """Binding from a separately authenticated and validated approval flow."""
    record_id: str
    revision: str
    candidate_fingerprint: str


def compare_memory(candidate: Candidate, active: ActiveMemory | None, *,
                   lookup_complete: bool, legacy_clear: bool = False,
                   explicit_correction: bool = False,
                   approval: VerifiedApproval | None = None) -> Comparison:
    """Compare an authoritative slot result; never authorize a Wiki write.

    lookup_complete means the caller verified the authoritative active slot,
    not merely a bounded Wiki search. legacy_clear is set only after a caller
    independently resolves legacy uncertainty, never from an empty top-k
    search. This function never proves global duplicate absence.
    explicit_correction must be independently established from the user source.
    Approval authentication, expiry and one-time consumption belong to W14.
    """
    if (candidate.scope != 'shared' or candidate.kind != 'fact'
            or candidate.predicate not in SHARED_SLOTS.get(candidate.subject, ())
            or not isinstance(candidate.value, str) or not candidate.value.strip()
            or type(candidate.start) is not int or type(candidate.end) is not int
            or candidate.start < 0 or candidate.end <= candidate.start):
        return Comparison('hold', 'unsupported_candidate')
    if lookup_complete is not True:
        return Comparison('hold', 'lookup_incomplete')
    if active is None:
        if approval is not None:
            return Comparison('hold', 'approval_target_missing')
        if explicit_correction:
            return Comparison('hold', 'correction_target_missing')
        if legacy_clear is not True:
            return Comparison('hold', 'legacy_uncertain')
        return Comparison('create_candidate', 'no_active_slot')
    if (not active.record_id or not isinstance(active.revision, str) or not active.revision.strip()
            or not isinstance(active.value, str) or not active.value.strip()
            or (active.scope, active.subject, active.predicate, active.kind)
            != (candidate.scope, candidate.subject, candidate.predicate, candidate.kind)):
        return Comparison('hold', 'active_record_mismatch')
    current_normalized, current_hash = _fingerprint(active.scope, active.subject, active.predicate, active.kind, active.value)
    candidate_normalized, candidate_hash = _fingerprint(candidate.scope, candidate.subject, candidate.predicate, candidate.kind, candidate.value)
    if active.fingerprint != current_hash:
        return Comparison('hold', 'fingerprint_mismatch', active.record_id)
    if current_hash == candidate_hash:
        if current_normalized != candidate_normalized:
            return Comparison('hold', 'fingerprint_collision', active.record_id)
        return Comparison('duplicate', 'same_value', active.record_id)
    if current_normalized == candidate_normalized:
        return Comparison('hold', 'fingerprint_mismatch', active.record_id)
    if approval is not None:
        if (not isinstance(approval, VerifiedApproval) or approval.record_id != active.record_id
                or approval.revision != active.revision or approval.candidate_fingerprint != candidate_hash):
            return Comparison('hold', 'approval_stale', active.record_id)
        return Comparison('supersede_candidate', 'verified_approval', active.record_id)
    if explicit_correction is True:
        return Comparison('supersede_candidate', 'explicit_correction', active.record_id)
    return Comparison('confirm_conflict', 'different_value', active.record_id)
