"""Local transactions for shared memory; no Wiki or model I/O occurs here."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import unicodedata

from .db import connect

# Versioned and deliberately narrow; user or model text is never a key.
SHARED_SLOTS = {'bc250_server': frozenset({'operating_system', 'backup_path', 'backup_frequency'}),
                'bc250_client': frozenset({'service_port'})}


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _transaction(path):
    db = connect(path)
    try:
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _validate_slot(scope, subject, predicate):
    if scope != 'shared' or predicate not in SHARED_SLOTS.get(subject, ()):
        raise ValueError('unsupported or ambiguous memory slot')


def _fingerprint(scope, subject, predicate, kind, value):
    normalized = ' '.join(unicodedata.normalize('NFC', value).split())
    digest = hashlib.sha256()
    for field in ('memory-fingerprint-v1', scope, subject, predicate, kind, normalized):
        data = field.encode('utf-8')
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    return normalized, digest.hexdigest()


def get_active_memory(database_path: str, scope_key: str, subject_key: str, predicate_key: str, *, store_id='legacy_external') -> dict | None:
    """Return only an active slot; callers must still verify Wiki revision before use."""
    db = connect(database_path)
    try:
        row = db.execute("""SELECT * FROM memory_records
            WHERE store_id=? AND scope_key=? AND subject_key=? AND predicate_key=? AND state='active'""",
            (store_id, scope_key, subject_key, predicate_key)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def claim_memory(database_path, *, record_id, operation_id, subject_key, predicate_key, kind,
                 conversation_id, message_id, start, end, value, request_hash,
                 supersedes_id=None, explicit_correction=False, store_id='legacy_external'):
    """Atomically claim a shared slot after validating its user-message source."""
    _validate_slot('shared', subject_key, predicate_key)
    if store_id not in ('legacy_external', 'integrated'):
        raise ValueError('unsupported memory store')
    if (kind != 'fact' or not record_id or not operation_id or not request_hash or not isinstance(value, str)
            or not value.strip() or type(start) is not int or type(end) is not int or not 0 <= start < end):
        raise ValueError('invalid memory candidate')
    normalized, fingerprint = _fingerprint('shared', subject_key, predicate_key, kind, value)
    timestamp = _now()
    with _transaction(database_path) as db:
        source = db.execute("SELECT content FROM messages WHERE id=? AND conversation_id=? AND role='user'",
                            (message_id, conversation_id)).fetchone()
        if not source or end > len(source['content']) or source['content'][start:end] != value:
            raise ValueError('source span does not match user message')
        active = db.execute("""SELECT id FROM memory_records WHERE store_id=? AND scope_key='shared' AND subject_key=?
            AND predicate_key=? AND state='active'""", (store_id, subject_key, predicate_key)).fetchone()
        if active and (not explicit_correction or supersedes_id != active['id']):
            raise ValueError('active slot requires explicit correction')
        if not active and (supersedes_id is not None or explicit_correction):
            raise ValueError('correction target is not active')
        db.execute("""INSERT INTO memory_records (id,scope_key,subject_key,predicate_key,kind,value,
            normalized_value,fingerprint,source_conversation_id,source_message_id,source_start,source_end,
            state,supersedes_id,created_at,updated_at,store_id) VALUES (?,'shared',?,?,?,?,?,?,?,?,?,?,'candidate',?,?,?,?)""",
            (record_id, subject_key, predicate_key, kind, value, normalized, fingerprint, conversation_id,
             message_id, start, end, supersedes_id, timestamp, timestamp, store_id))
        db.execute("""INSERT INTO memory_operations (id,record_id,scope_key,subject_key,predicate_key,
            operation_type,request_hash,state,created_at,updated_at,store_id) VALUES (?,?,'shared',?,?,?,?,'claimed',?,?,?)""",
            (operation_id, record_id, subject_key, predicate_key,
             'supersede' if supersedes_id else 'create', request_hash, timestamp, timestamp, store_id))


def transition_operation(database_path, operation_id, expected, target, *, document_id=None, revision=None):
    allowed = {('claimed', 'prepared'), ('prepared', 'sending'), ('claimed', 'cancelled'),
               ('prepared', 'cancelled'), ('sending', 'unknown'), ('sending', 'confirmed'),
               ('unknown', 'confirmed'), ('claimed', 'rejected'), ('prepared', 'rejected')}
    if (expected, target) not in allowed:
        raise ValueError('invalid operation transition')
    if target == 'confirmed' and (type(document_id) is not int or document_id < 1
                                  or not isinstance(revision, str) or not revision.strip()):
        raise ValueError('confirmed result requires document and revision')
    if target != 'confirmed' and (document_id is not None or revision is not None):
        raise ValueError('document and revision require confirmed result')
    timestamp = _now()
    with _transaction(database_path) as db:
        operation = db.execute('SELECT * FROM memory_operations WHERE id=? AND state=?', (operation_id, expected)).fetchone()
        if not operation:
            raise ValueError('operation state changed')
        if target == 'confirmed':
            record = db.execute('SELECT state FROM memory_records WHERE id=?', (operation['record_id'],)).fetchone()
            if not record or record['state'] != 'candidate':
                raise ValueError('source no longer available')
            if db.execute('SELECT 1 FROM memory_document_quarantine WHERE store_id=? AND wiki_document_id=?', (operation['store_id'], document_id)).fetchone():
                raise ValueError('document is already managed or quarantined')
            db.execute('UPDATE memory_records SET wiki_document_id=?,wiki_revision=?,updated_at=? WHERE id=?',
                       (document_id, revision, timestamp, operation['record_id']))
            db.execute("INSERT INTO memory_document_quarantine (store_id,wiki_document_id,reason,created_at,updated_at) VALUES (?,?,'owned',?,?)", (operation['store_id'], document_id, timestamp, timestamp))
        db.execute('UPDATE memory_operations SET state=?,wiki_document_id=COALESCE(?,wiki_document_id),updated_at=? WHERE id=?',
                   (target, document_id, timestamp, operation_id))


def apply_confirmed(database_path, operation_id):
    """Activate a confirmed candidate atomically after caller rechecks Wiki revision."""
    timestamp = _now()
    with _transaction(database_path) as db:
        row = db.execute("""SELECT r.* FROM memory_operations o JOIN memory_records r ON r.id=o.record_id
            WHERE o.id=? AND o.store_id=r.store_id AND o.state='confirmed' AND r.state='candidate'""", (operation_id,)).fetchone()
        if not row or not row['wiki_document_id'] or not row['wiki_revision']:
            raise ValueError('operation is not confirmed')
        if row['supersedes_id']:
            updated = db.execute("""UPDATE memory_records SET state='superseded',updated_at=? WHERE id=?
                AND state='active' AND store_id=? AND scope_key=? AND subject_key=? AND predicate_key=?""",
                (timestamp, row['supersedes_id'], row['store_id'], row['scope_key'], row['subject_key'], row['predicate_key']))
            if updated.rowcount != 1:
                raise ValueError('correction target changed')
        db.execute("UPDATE memory_records SET state='active',updated_at=? WHERE id=?", (timestamp, row['id']))
        db.execute("UPDATE memory_operations SET state='applied',updated_at=? WHERE id=?", (timestamp, operation_id))


def redact_conversation_memory(db, conversation_id, timestamp):
    """Redact in the caller's deletion transaction; do not alter Wiki documents."""
    rows = db.execute('SELECT id,store_id,wiki_document_id FROM memory_records WHERE source_conversation_id=?',
                      (conversation_id,)).fetchall()
    _redact_records(db, rows, timestamp)


def redact_message_memory(db, conversation_id, message_id, timestamp):
    """Redact one message's records inside the caller's message deletion transaction.

    No message deletion endpoint currently uses this helper. The caller must delete
    the matching message in the same transaction and roll back both on failure.
    """
    rows = db.execute('''SELECT id,store_id,wiki_document_id FROM memory_records
                         WHERE source_conversation_id=? AND source_message_id=?''',
                      (conversation_id, message_id)).fetchall()
    _redact_records(db, rows, timestamp)


def _redact_records(db, rows, timestamp):
    for row in rows:
        if row['wiki_document_id'] is not None:
            db.execute("""INSERT INTO memory_document_quarantine(store_id,wiki_document_id,reason,created_at,updated_at)
                VALUES (?,?,'source_deleted',?,?) ON CONFLICT(store_id,wiki_document_id)
                DO UPDATE SET reason='source_deleted',updated_at=excluded.updated_at""",
                (row['store_id'], row['wiki_document_id'], timestamp, timestamp))
        db.execute("""UPDATE memory_operations SET state='cancelled',updated_at=? WHERE record_id=?
            AND state IN ('claimed','prepared')""", (timestamp, row['id']))
        db.execute("UPDATE memory_operations SET state='unknown',updated_at=? WHERE record_id=? AND state='sending'",
                   (timestamp, row['id']))
        db.execute("""UPDATE memory_records SET state='redacted',value=NULL,normalized_value=NULL,fingerprint=NULL,
            source_start=NULL,source_end=NULL,updated_at=? WHERE id=?""", (timestamp, row['id']))
