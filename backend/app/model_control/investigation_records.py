"""Versioned, secret-free investigation records and conservative replay.

These records bind claimed observations; they do not authenticate their sources,
prove completeness or freshness, establish ownership, or authorize any mutation.
Callers must protect the original evidence separately. Unknown versions, fields,
and sequences are rejected rather than interpreted as an earlier format.
"""
import hashlib
import re

from .investigation_log import _encode
from .recovery_progress import RecoveryProgress, replay_recovery_progress

_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_HOLD = re.compile(r'(?:(?:switch|studio)-[0-9a-f]{32}|startup|legacy|admin-[A-Za-z0-9_.:-]{1,128})\Z')
_SOURCES = frozenset({'journal', 'gate', 'studio', 'operator'})
_COMMON = {'version', 'kind', 'investigation_id', 'association', 'operation_id'}


def _hex(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _common(record, kind, fields):
    return (isinstance(record, dict) and set(record) == _COMMON | fields
            and type(record['version']) is int and record['version'] == 1
            and record['kind'] == kind
            and _hex(record['investigation_id'], _HEX32)
            and ((record['association'] == 'operation' and _hex(record['operation_id'], _HEX32))
                 or (record['association'] == 'unassociated' and record['operation_id'] is None)))


def observation_digest(record):
    """Validate and hash one typed observation, without authenticating it."""
    fields = {'evidence_digest', 'journal_digest', 'service_generations', 'gate_epoch', 'holds',
              'reservation', 'observed_at', 'source'}
    if not _common(record, 'observation', fields):
        raise ValueError('Invalid observation')
    holds = record['holds']
    generations = record['service_generations']
    reservation = record['reservation']
    if (not isinstance(generations, dict) or set(generations) != {'gate', 'studio'}
            or any(not isinstance(generation, dict) or set(generation) != {'pid', 'starttime'}
                   or type(generation['pid']) is not int or not 0 < generation['pid'] < 2**31
                   or type(generation['starttime']) is not int or not 0 < generation['starttime'] < 2**64
                   for generation in generations.values())):
        raise ValueError('Invalid service generations')
    if (not _hex(record['evidence_digest'], _HEX64)
            or not _hex(record['journal_digest'], _HEX64)
            or not _hex(record['gate_epoch'], _HEX32)
            or not isinstance(holds, list) or len(holds) > 1024
            or any(not _hex(item, _HOLD) for item in holds)
            or holds != sorted(set(holds))
            or type(record['observed_at']) is not int or not 0 < record['observed_at'] < 2**63
            or not isinstance(record['source'], str) or record['source'] not in _SOURCES):
        raise ValueError('Invalid observation')
    if (reservation is not None and
            (not isinstance(reservation, dict) or set(reservation) != {'hold_id', 'epoch'}
             or not isinstance(reservation['hold_id'], str)
             or not re.fullmatch(r'switch-[0-9a-f]{32}', reservation['hold_id'])
             or reservation['hold_id'] not in holds or reservation['epoch'] != record['gate_epoch'])):
        raise ValueError('Invalid reservation')
    return hashlib.sha256(b'model-control-observation-v1\0' + _encode(record)).hexdigest()


def replay_investigation(records):
    """Validate one observation, review and at most one conditional attempt.

    Results only describe recorded state. Even a matching review and applied
    result are unresolved and must never be used as permission to retry, mutate,
    or close an investigation. observed_at is a claimed UTC Unix timestamp in
    seconds, not an authenticated clock. Empty and incomplete input fails closed.
    """
    blocked = RecoveryProgress('unverified')
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= 4:
        return blocked
    try:
        observation = records[0]
        digest = observation_digest(observation)
        if len(records) == 1:
            return blocked
        identity = (observation['investigation_id'], observation['association'], observation['operation_id'])
        review = records[1]
        if (not _common(review, 'review', {'observation_digest', 'review_digest'})
                or (review['investigation_id'], review['association'], review['operation_id']) != identity
                or review['observation_digest'] != digest
                or not _hex(review['review_digest'], _HEX64)):
            return blocked
        if len(records) == 2:
            return RecoveryProgress('no_attempt')
        intent = records[2]
        intent_fields = {'observation_digest', 'review_digest', 'action', 'attempt', 'request_digest'}
        if (not _common(intent, 'intent', intent_fields)
                or (intent['investigation_id'], intent['association'], intent['operation_id']) != identity
                or intent['observation_digest'] != digest
                or intent['review_digest'] != review['review_digest']):
            return blocked
        progress = [{'kind': 'intent', 'action': intent['action'], 'attempt': intent['attempt'],
                     'request_digest': intent['request_digest']}]
        if len(records) == 4:
            outcome = records[3]
            if (not _common(outcome, 'outcome', intent_fields | {'result'})
                    or (outcome['investigation_id'], outcome['association'], outcome['operation_id']) != identity
                    or any(outcome[key] != intent[key] for key in intent_fields)):
                return blocked
            progress.append({'kind': 'observation', 'action': outcome['action'],
                             'attempt': outcome['attempt'], 'request_digest': outcome['request_digest'],
                             'result': outcome['result']})
        return replay_recovery_progress(progress)
    except (ValueError, TypeError, KeyError):
        return blocked
