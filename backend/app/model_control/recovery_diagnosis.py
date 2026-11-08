"""Pure classification of already-read recovery records; never authorizes repair.

The caller must serialize reads and enforce root-only access. This module does
not establish a consistent snapshot, inspect gate state, or mutate records.
"""

import json
import re

from .barrier import SwitchRecoveryRequired, decode_switch_journal
from .registry import MODEL_NAME


def classify_recovery_records(operation, hold_id, journal, journal_error):
    """Return a conservative category and mismatch fields, without record contents."""
    if journal_error is not None:
        return {"category": "journal_unverified", "mismatches": []}
    try:
        # Revalidate callers' data rather than treating an arbitrary mapping as trusted.
        journal = decode_switch_journal(json.dumps(journal).encode("utf-8"))
    except (SwitchRecoveryRequired, ValueError, TypeError, UnicodeError):
        return {"category": "journal_unverified", "mismatches": []}
    if operation is not None:
        if (not isinstance(operation, dict)
                or not isinstance(operation.get("id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", operation["id"])
                or not isinstance(operation.get("model"), str)
                or not MODEL_NAME.fullmatch(operation["model"])
                or (hold_id is not None and (not isinstance(hold_id, str)
                    or not re.fullmatch(r"switch-[0-9a-f]{32}", hold_id)))):
            return {"category": "operation_unverified", "mismatches": []}
    elif hold_id is not None:
        return {"category": "operation_unverified", "mismatches": []}
    if not journal:
        return {"category": "empty_journal", "mismatches": []}
    if operation is None:
        return {"category": "orphan_journal", "mismatches": []}
    mismatches = []
    if journal.get("operation_id") != operation["id"]:
        mismatches.append("operation_id")
    if journal["target"] != operation["model"]:
        mismatches.append("target")
    if journal["hold_id"] != hold_id:
        mismatches.append("hold_id")
    return {"category": "records_mismatch" if mismatches else "records_match",
            "mismatches": mismatches}
