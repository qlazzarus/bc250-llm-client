"""Combine protected evidence and pinned-review checks for diagnosis only.

Caller must obtain records from the checkpointed typed log under the trusted
singleton lock, and independently pin the review digest. These checks do not
verify evidence sources, freshness, human identity, or authorize mutations.
"""
from .investigation_records import observation_digest
from .investigation_review_anchor import replay_with_pinned_review
from .recovery_progress import RecoveryProgress


def diagnose_archived_investigation(records, archive, review_path, review_digest):
    blocked = RecoveryProgress('unverified')
    progress = replay_with_pinned_review(records, review_path, review_digest)
    if progress.status == 'unverified':
        return blocked
    try:
        observation = records[0]
        observation_digest(observation)
        identifier = (observation['operation_id'] if observation['association'] == 'operation'
                      else observation['investigation_id'])
        archive.read(observation['association'], identifier, observation['evidence_digest'])
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return blocked
    return progress
