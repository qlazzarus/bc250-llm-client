"""Durable switch request records; no API or privileged worker is exposed.

Callers must authenticate the principal and validate installation separately.
The worker must reconcile these records with the GPU switch journal at startup.
"""

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .registry import MODEL_NAME

TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
ACTIVE = ("queued", "waiting_for_gpu", "pausing", "draining", "switching", "verifying",
          "rolling_back", "recovery_required")
TRANSITIONS = {
    "queued": {"waiting_for_gpu", "failed", "recovery_required"},
    "waiting_for_gpu": {"pausing", "succeeded", "failed", "recovery_required"},
    "pausing": {"draining", "failed", "rolling_back", "recovery_required"},
    "draining": {"switching", "failed", "rolling_back", "recovery_required"},
    "switching": {"verifying", "rolling_back", "recovery_required"},
    "verifying": {"succeeded", "rolling_back", "recovery_required"},
    "rolling_back": {"failed_rolled_back", "recovery_required"},
    "recovery_required": {"rolling_back", "failed"},
    "succeeded": set(), "failed": set(), "failed_rolled_back": set(),
}
ERROR_CODES = {"invalid_model", "drain_timeout", "restart_failed", "health_timeout",
               "recovery_required", "service_unavailable", "interrupted", "admission_blocked"}


class OperationConflict(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class SwitchOperations:
    def __init__(self, path):
        self.path = Path(path)
        # The caller provisions a protected parent directory and its permissions.
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS switch_operations (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                model TEXT NOT NULL, state TEXT NOT NULL, error_code TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, previous_model TEXT, current_model TEXT,
                UNIQUE(principal, idempotency_key)
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(switch_operations)")}
            for name in ("previous_model", "current_model", "hold_id"):
                if name not in columns:
                    db.execute(f"ALTER TABLE switch_operations ADD COLUMN {name} TEXT")
            # A single shared GPU permits only one unresolved switch globally.
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_switch
                ON switch_operations ((1)) WHERE state IN
                ('queued','waiting_for_gpu','pausing','draining','switching',
                 'verifying','rolling_back','recovery_required')""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ("id", "model", "state", "error_code",
                                          "created_at", "updated_at", "previous_model", "current_model")}

    @staticmethod
    def _validate_request(principal, model, idempotency_key):
        if (not isinstance(principal, str) or not TOKEN.fullmatch(principal)
                or not isinstance(idempotency_key, str) or not TOKEN.fullmatch(idempotency_key)
                or not isinstance(model, str) or not MODEL_NAME.fullmatch(model)):
            raise ValueError("Invalid switch request")

    def find_idempotent(self, principal, model, idempotency_key):
        self._validate_request(principal, model, idempotency_key)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM switch_operations WHERE principal=? AND idempotency_key=?",
                (principal, idempotency_key),
            ).fetchone()
        if row and row["model"] != model:
            raise IdempotencyConflict("Idempotency key already used for another model")
        return self._public(row) if row else None

    def submit(self, principal, model, idempotency_key):
        self._validate_request(principal, model, idempotency_key)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT * FROM switch_operations WHERE principal=? AND idempotency_key=?",
                    (principal, idempotency_key),
                ).fetchone()
                if existing:
                    if existing["model"] != model:
                        raise IdempotencyConflict("Idempotency key already used for another model")
                    db.execute("COMMIT")
                    return self._public(existing), False
                placeholders = ",".join("?" for _ in ACTIVE)
                if db.execute(f"SELECT 1 FROM switch_operations WHERE state IN ({placeholders})",
                              ACTIVE).fetchone():
                    raise OperationConflict("Another switch or recovery is pending")
                operation_id, at = uuid4().hex, timestamp()
                db.execute("""INSERT INTO switch_operations
                    (id, principal, idempotency_key, model, state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
                    (operation_id, principal, idempotency_key, model, at, at))
                row = db.execute("SELECT * FROM switch_operations WHERE id=?", (operation_id,))
                result = self._public(row.fetchone())
                db.execute("COMMIT")
                return result, True
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def get(self, principal, operation_id):
        if (not isinstance(principal, str) or not TOKEN.fullmatch(principal)
                or not isinstance(operation_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", operation_id)):
            raise ValueError("Invalid operation lookup")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM switch_operations WHERE id=? AND principal=?",
                             (operation_id, principal)).fetchone()
        return self._public(row) if row else None

    def unresolved(self):
        placeholders = ",".join("?" for _ in ACTIVE)
        with closing(self._connect()) as db:
            row = db.execute(f"SELECT * FROM switch_operations WHERE state IN ({placeholders})",
                             ACTIVE).fetchone()
        return self._public(row) if row else None

    def record_hold(self, operation_id, hold_id):
        if (not re.fullmatch(r"[0-9a-f]{32}", operation_id)
                or not re.fullmatch(r"switch-[0-9a-f]{32}", hold_id)):
            raise ValueError("Invalid hold correlation")
        with closing(self._connect()) as db:
            cursor = db.execute("UPDATE switch_operations SET hold_id=?, updated_at=? WHERE id=? AND hold_id IS NULL AND state='waiting_for_gpu'",
                                (hold_id, timestamp(), operation_id))
            if cursor.rowcount != 1:
                raise OperationConflict("Hold correlation cannot be replaced")

    def hold_for(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Invalid operation ID")
        with closing(self._connect()) as db:
            row = db.execute("SELECT hold_id FROM switch_operations WHERE id=?", (operation_id,)).fetchone()
        return row[0] if row else None

    def record_verified(self, operation_id, *, previous=None, current=None):
        if any(value is not None and (not isinstance(value, str) or not MODEL_NAME.fullmatch(value))
               for value in (previous, current)):
            raise ValueError("Invalid verified model")
        with closing(self._connect()) as db:
            cursor = db.execute("UPDATE switch_operations SET previous_model=COALESCE(?,previous_model), current_model=COALESCE(?,current_model), updated_at=? WHERE id=?",
                                (previous, current, timestamp(), operation_id))
            if cursor.rowcount != 1:
                raise ValueError("Unknown operation")

    def transition(self, operation_id, state, *, error_code=None):
        if (not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id)
                or not isinstance(state, str) or state not in TRANSITIONS
                or (error_code is not None and
                    (not isinstance(error_code, str) or error_code not in ERROR_CODES))):
            raise ValueError("Invalid operation state")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM switch_operations WHERE id=?", (operation_id,)).fetchone()
                if row is None:
                    raise ValueError("Unknown operation")
                if state not in TRANSITIONS[row["state"]]:
                    raise OperationConflict("Invalid operation transition")
                db.execute("UPDATE switch_operations SET state=?,error_code=?,updated_at=?, current_model=CASE WHEN ? IN ('switching', 'rolling_back', 'recovery_required') THEN NULL ELSE current_model END WHERE id=?",
                           (state, error_code, timestamp(), state, operation_id))
                row = db.execute("SELECT * FROM switch_operations WHERE id=?", (operation_id,)).fetchone()
                db.execute("COMMIT")
                return self._public(row)
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
