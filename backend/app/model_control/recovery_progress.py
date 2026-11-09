"""Read-only replay of conditional recovery attempts.

This is a bookkeeping primitive, not a repair coordinator.  In particular,
its output never authorizes a write, a retry, or terminal success.  It neither
establishes that a log is complete/authentic nor verifies an external system's
receipt.  M4-04-05 must provide protected evidence and M5 must implement the
conditional external contracts before any executor can use such information.
"""

import re
from dataclasses import dataclass

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ACTIONS = frozenset({"db", "journal", "gate"})


@dataclass(frozen=True)
class RecoveryProgress:
    # All outcomes remain unresolved, even when the recorded attempt completed.
    status: str
    action: str | None = None
    attempt: str | None = None


def replay_recovery_progress(events):
    """Classify a single attempt's ordered intent and observation records.

    The intent binds a unique attempt and a digest of the complete conditional
    request. A matching observation records the *reported* result only. Missing,
    malformed, reordered, duplicated, or conflicting records are unresolved.
    A successful observation still requires independent reconciliation; no
    record here is proof that a remote operation did or did not occur.
    """
    blocked = RecoveryProgress("unverified")
    if not isinstance(events, (list, tuple)) or len(events) > 2:
        return blocked
    if not events:
        return RecoveryProgress("no_attempt")
    intent = events[0]
    if (not isinstance(intent, dict)
            or set(intent) != {"kind", "action", "attempt", "request_digest"}
            or intent["kind"] != "intent"
            or not isinstance(intent["action"], str)
            or intent["action"] not in _ACTIONS
            or not isinstance(intent["attempt"], str)
            or not _HEX.fullmatch(intent["attempt"])
            or not isinstance(intent["request_digest"], str)
            or not _HEX.fullmatch(intent["request_digest"])):
        return blocked
    action, attempt = intent["action"], intent["attempt"]
    if len(events) == 1:
        return RecoveryProgress("outcome_unknown", action, attempt)
    observation = events[1]
    if (not isinstance(observation, dict)
            or set(observation) != {"kind", "action", "attempt", "request_digest", "result"}
            or observation["kind"] != "observation"
            or observation["action"] != action
            or observation["attempt"] != attempt
            or observation["request_digest"] != intent["request_digest"]
            or observation["result"] not in ("applied", "not_applied", "unknown")):
        return blocked
    if observation["result"] == "unknown":
        return RecoveryProgress("outcome_unknown", action, attempt)
    return RecoveryProgress("reconciliation_required", action, attempt)
