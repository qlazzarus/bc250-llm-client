"""Transactional primitives for the integrated Wiki memory writer."""
import hashlib
import json
import re
import uuid
from dataclasses import replace

from .db import connect
from .memory_dedup import map_shared_fact
from .memory_repository import _fingerprint, _now
from .wiki_repository import create_document


class MemoryWriteConflict(ValueError):
    """The source, existing slot, or operation changed; do not retry a write."""


def _map_source(text):
    """An explicit correction must start with an unambiguous correction marker."""
    match = re.match(r'^\s*(?:정정|수정)\s*[:：]\s*', text)
    if not match:
        return map_shared_fact(text), False
    candidate = map_shared_fact(text[match.end():])
    return (replace(candidate, start=candidate.start + match.end(), end=candidate.end + match.end()) if candidate else None), True


def _request_hash(generation_id, candidate, source_message_id, source_text):
    encoded = json.dumps({'version': 1, 'generation_id': generation_id,
                          'source_message_id': source_message_id,
                          'subject': candidate.subject, 'predicate': candidate.predicate,
                          'value': candidate.value, 'start': candidate.start, 'end': candidate.end,
                          'source_sha256': hashlib.sha256(source_text.encode('utf-8')).hexdigest()},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def create_initial_memory(database_path: str, generation_id: str) -> dict:
    """Create a new shared slot or replay its committed result.

    Different existing values and open operations are rejected; confirmation and
    update are intentionally outside this primitive. Only integrated jobs whose
    original user message matches an approved template can enter this path.
    """
    db = connect(database_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        job = db.execute('''SELECT j.*,g.user_message_id,g.status AS generation_status,m.content,m.role,c.mode
            FROM wiki_memory_jobs j JOIN generation_requests g ON g.id=j.generation_id
            JOIN messages m ON m.id=g.user_message_id AND m.conversation_id=j.conversation_id
            JOIN conversations c ON c.id=j.conversation_id
            WHERE j.generation_id=? AND g.conversation_id=j.conversation_id''', (generation_id,)).fetchone()
        if (not job or job['store_id'] != 'integrated' or job['role'] != 'user'
                or job['mode'] != 'quick' or job['generation_status'] != 'completed'):
            raise MemoryWriteConflict('integrated user source unavailable')
        candidate, correction = _map_source(job['content'])
        if candidate is None or job['content'][candidate.start:candidate.end] != candidate.value:
            raise MemoryWriteConflict('source is not a supported shared fact')
        request_hash = _request_hash(generation_id, candidate, job['user_message_id'], job['content'])
        existing = db.execute('SELECT * FROM memory_operations WHERE id=?', (generation_id,)).fetchone()
        if existing:
            if existing['store_id'] != 'integrated' or existing['request_hash'] != request_hash or existing['state'] != 'applied':
                raise MemoryWriteConflict('operation cannot be replayed')
            record = db.execute('SELECT * FROM memory_records WHERE id=? AND store_id=?', (existing['record_id'], 'integrated')).fetchone()
            if not record or record['state'] not in ('active', 'superseded') or not record['wiki_document_id']:
                raise MemoryWriteConflict('committed result is no longer available')
            evidence = db.execute('''SELECT d.revision,q.reason FROM wiki_documents d
                JOIN memory_document_quarantine q ON q.store_id='integrated' AND q.wiki_document_id=d.id
                WHERE d.id=?''', (record['wiki_document_id'],)).fetchone()
            if not evidence or evidence['reason'] != 'owned' or str(evidence['revision']) != record['wiki_revision']:
                raise MemoryWriteConflict('committed document evidence changed')
            result = {'status': 'completed', 'document_id': record['wiki_document_id'], 'revision': record['wiki_revision'], 'replayed': True}
            db.commit()
            return result
        if job['status'] != 'pending':
            raise MemoryWriteConflict('job is not pending')
        if correction:
            raise MemoryWriteConflict('correction target missing')
        if db.execute("""SELECT 1 FROM memory_records WHERE store_id='integrated' AND scope_key='shared'
                AND subject_key=? AND predicate_key=? AND state='active'""", (candidate.subject, candidate.predicate)).fetchone():
            raise MemoryWriteConflict('active slot requires duplicate or conflict handling')
        if db.execute("""SELECT 1 FROM memory_operations WHERE store_id='integrated' AND scope_key='shared'
                AND subject_key=? AND predicate_key=? AND state IN ('claimed','prepared','sending','unknown','confirmed')""",
                (candidate.subject, candidate.predicate)).fetchone():
            raise MemoryWriteConflict('slot has an unresolved operation')
        timestamp = _now()
        record_id = str(uuid.uuid4())
        normalized, fingerprint = _fingerprint('shared', candidate.subject, candidate.predicate, candidate.kind, candidate.value)
        db.execute('''INSERT INTO memory_records (id,scope_key,subject_key,predicate_key,kind,value,normalized_value,
            fingerprint,source_conversation_id,source_message_id,source_start,source_end,state,created_at,updated_at,store_id)
            VALUES (?,'shared',?,?,?,?,?,?,?,?,?,?,'candidate',?,?,'integrated')''',
            (record_id, candidate.subject, candidate.predicate, candidate.kind, candidate.value, normalized, fingerprint,
             job['conversation_id'], job['user_message_id'], candidate.start, candidate.end, timestamp, timestamp))
        db.execute('''INSERT INTO memory_operations (id,record_id,scope_key,subject_key,predicate_key,operation_type,
            request_hash,state,created_at,updated_at,store_id) VALUES (?,?,'shared',?,?,'create',?,'claimed',?,?,'integrated')''',
            (generation_id, record_id, candidate.subject, candidate.predicate, request_hash, timestamp, timestamp))
        document = create_document(db, store_id='integrated', title=f'{candidate.subject} · {candidate.predicate}',
                                   content=job['content'], category='memory')
        db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'owned',?,?)", (document['id'], timestamp, timestamp))
        db.execute("UPDATE memory_records SET state='active',wiki_document_id=?,wiki_revision=?,updated_at=? WHERE id=? AND state='candidate'",
                   (document['id'], str(document['revision']), timestamp, record_id))
        db.execute("UPDATE memory_operations SET state='applied',wiki_document_id=?,updated_at=? WHERE id=? AND state='claimed'",
                   (document['id'], timestamp, generation_id))
        updated = db.execute("""UPDATE wiki_memory_jobs SET status='completed',action='create',document_id=?,title=?,
            source_text=?,source_message_id=?,memory_kind='fact',reason_code='stored',needs_attention=0,updated_at=?
            WHERE generation_id=? AND store_id='integrated' AND status='pending'""",
            (document['id'], document['title'], candidate.value, job['user_message_id'], timestamp, generation_id))
        if updated.rowcount != 1:
            raise MemoryWriteConflict('job changed')
        db.commit()
        return {'status': 'completed', 'document_id': document['id'], 'revision': str(document['revision']), 'replayed': False}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _confirmation_token(operation, candidate, active):
    fields = ['memory-confirmation-v1', operation['id'], operation['request_hash'], operation['expected_revision'],
              candidate['id'], candidate['fingerprint'], candidate['source_message_id'],
              active['id'], active['wiki_revision'], active['fingerprint'], active['source_message_id']]
    if any(not isinstance(value, str) or not value for value in fields):
        raise MemoryWriteConflict('confirmation binding is incomplete')
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _verified_active(db, active):
    if not active or active['state'] != 'active' or active['store_id'] != 'integrated':
        raise MemoryWriteConflict('active memory changed')
    source = db.execute("SELECT content FROM messages WHERE id=? AND conversation_id=? AND role='user'",
                        (active['source_message_id'], active['source_conversation_id'])).fetchone()
    normalized, fingerprint = _fingerprint(active['scope_key'], active['subject_key'], active['predicate_key'], active['kind'], active['value'])
    if (not source or source['content'][active['source_start']:active['source_end']] != active['value']
            or normalized != active['normalized_value'] or fingerprint != active['fingerprint']):
        raise MemoryWriteConflict('active memory source changed')
    document = db.execute('''SELECT d.revision,d.content,q.reason FROM wiki_documents d
        JOIN memory_document_quarantine q ON q.store_id='integrated' AND q.wiki_document_id=d.id
        WHERE d.id=?''', (active['wiki_document_id'],)).fetchone()
    if (not document or document['reason'] != 'owned' or str(document['revision']) != active['wiki_revision']
            or document['content'] != source['content']):
        raise MemoryWriteConflict('active memory document changed')


def prepare_conflict(database_path: str, generation_id: str) -> dict:
    """Record a duplicate or a conflict requiring confirmation, without writing Wiki."""
    db = connect(database_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        job = db.execute('''SELECT j.*,g.user_message_id,g.status AS generation_status,m.content,m.role,c.mode
            FROM wiki_memory_jobs j JOIN generation_requests g ON g.id=j.generation_id
            JOIN messages m ON m.id=g.user_message_id AND m.conversation_id=j.conversation_id
            JOIN conversations c ON c.id=j.conversation_id
            WHERE j.generation_id=? AND g.conversation_id=j.conversation_id''', (generation_id,)).fetchone()
        if (not job or job['store_id'] != 'integrated' or job['status'] != 'pending'
                or job['generation_status'] != 'completed' or job['role'] != 'user' or job['mode'] != 'quick'):
            raise MemoryWriteConflict('integrated user source unavailable')
        candidate, _ = _map_source(job['content'])
        if not candidate or job['content'][candidate.start:candidate.end] != candidate.value:
            raise MemoryWriteConflict('source is not a supported shared fact')
        active = db.execute("""SELECT * FROM memory_records WHERE store_id='integrated' AND scope_key='shared'
            AND subject_key=? AND predicate_key=? AND state='active'""", (candidate.subject, candidate.predicate)).fetchone()
        _verified_active(db, active)
        normalized, fingerprint = _fingerprint('shared', candidate.subject, candidate.predicate, candidate.kind, candidate.value)
        timestamp = _now()
        if fingerprint == active['fingerprint']:
            if normalized != active['normalized_value']:
                raise MemoryWriteConflict('fingerprint collision')
            db.execute("""UPDATE wiki_memory_jobs SET status='skipped',reason_code='duplicate',needs_attention=0,
                document_id=?,source_message_id=?,memory_kind='fact',updated_at=? WHERE generation_id=? AND status='pending'""",
                (active['wiki_document_id'], job['user_message_id'], timestamp, generation_id))
            db.commit()
            return {'status': 'skipped', 'reason': 'duplicate'}
        if normalized == active['normalized_value']:
            raise MemoryWriteConflict('fingerprint mismatch')
        record_id = str(uuid.uuid4())
        db.execute('''INSERT INTO memory_records (id,scope_key,subject_key,predicate_key,kind,value,normalized_value,
            fingerprint,source_conversation_id,source_message_id,source_start,source_end,state,supersedes_id,created_at,updated_at,store_id)
            VALUES (?,'shared',?,?,?,?,?,?,?,?,?,?,'candidate',?,?,?,'integrated')''',
            (record_id, candidate.subject, candidate.predicate, candidate.kind, candidate.value, normalized, fingerprint,
             job['conversation_id'], job['user_message_id'], candidate.start, candidate.end, active['id'], timestamp, timestamp))
        db.execute('''INSERT INTO memory_operations (id,record_id,scope_key,subject_key,predicate_key,operation_type,
            request_hash,expected_revision,wiki_document_id,state,created_at,updated_at,store_id)
            VALUES (?,?,'shared',?,?,'supersede',?,?,?,'prepared',?,?,'integrated')''',
            (generation_id, record_id, candidate.subject, candidate.predicate,
             _request_hash(generation_id, candidate, job['user_message_id'], job['content']), active['wiki_revision'], active['wiki_document_id'], timestamp, timestamp))
        updated = db.execute("""UPDATE wiki_memory_jobs SET status='awaiting_confirmation',action='replace',document_id=?,
            title=?,source_text=?,source_message_id=?,previous_content=?,proposed_content=?,memory_kind='fact',
            reason_code='conflict',needs_attention=1,updated_at=? WHERE generation_id=? AND status='pending' AND store_id='integrated'""",
            (active['wiki_document_id'], f'{candidate.subject} · {candidate.predicate}', candidate.value, job['user_message_id'],
             active['value'], candidate.value, timestamp, generation_id))
        if updated.rowcount != 1:
            raise MemoryWriteConflict('job changed')
        operation = db.execute('SELECT * FROM memory_operations WHERE id=?', (generation_id,)).fetchone()
        record = db.execute('SELECT * FROM memory_records WHERE id=?', (record_id,)).fetchone()
        token = _confirmation_token(operation, record, active)
        db.commit()
        return {'status': 'awaiting_confirmation', 'confirmation_token': token}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def resolve_conflict(database_path: str, conversation_id: str, generation_id: str, token: str, *, approve: bool) -> dict:
    """Validate and consume a confirmation while committing a new immutable version."""
    from datetime import datetime, timezone, timedelta
    import hmac
    db = connect(database_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        job = db.execute("SELECT * FROM wiki_memory_jobs WHERE generation_id=? AND conversation_id=? AND store_id='integrated'",
                         (generation_id, conversation_id)).fetchone()
        operation = db.execute("SELECT * FROM memory_operations WHERE id=? AND store_id='integrated'", (generation_id,)).fetchone()
        if not job or not operation or operation['operation_type'] != 'supersede':
            raise MemoryWriteConflict('confirmation unavailable')
        candidate = db.execute('SELECT * FROM memory_records WHERE id=?', (operation['record_id'],)).fetchone()
        active = db.execute('SELECT * FROM memory_records WHERE id=?', (candidate['supersedes_id'],)).fetchone() if candidate else None
        if (not candidate or not active or candidate['store_id'] != 'integrated'
                or candidate['source_conversation_id'] != conversation_id or not isinstance(token, str)
                or not hmac.compare_digest(_confirmation_token(operation, candidate, active), token)):
            raise MemoryWriteConflict('confirmation binding changed')
        if operation['state'] in ('applied', 'cancelled'):
            expected = 'applied' if approve else 'cancelled'
            if operation['state'] != expected:
                raise MemoryWriteConflict('confirmation already resolved differently')
            db.commit()
            return {'status': job['status'], 'document_id': job['document_id'], 'replayed': True}
        if operation['state'] != 'prepared' or job['status'] != 'awaiting_confirmation' or candidate['state'] != 'candidate':
            raise MemoryWriteConflict('confirmation is not pending')
        if not approve:
            timestamp = _now()
            db.execute("UPDATE memory_records SET state='quarantined',updated_at=? WHERE id=?", (timestamp, candidate['id']))
            db.execute("UPDATE memory_operations SET state='cancelled',updated_at=? WHERE id=?", (timestamp, generation_id))
            db.execute("UPDATE wiki_memory_jobs SET status='skipped',reason_code='conflict_kept',needs_attention=0,updated_at=? WHERE generation_id=?", (timestamp, generation_id))
            db.commit()
            return {'status': 'skipped', 'document_id': active['wiki_document_id'], 'replayed': False}
        try:
            created = datetime.fromisoformat(operation['created_at'])
            if created.tzinfo is None or datetime.now(timezone.utc) - created > timedelta(hours=24):
                raise MemoryWriteConflict('confirmation expired')
        except (ValueError, TypeError) as exc:
            raise MemoryWriteConflict('invalid confirmation time') from exc
        source = db.execute("SELECT content FROM messages WHERE id=? AND conversation_id=? AND role='user'",
                            (candidate['source_message_id'], conversation_id)).fetchone()
        mapped, _ = _map_source(source['content']) if source else (None, False)
        if (not mapped or (mapped.subject, mapped.predicate, mapped.value, mapped.start, mapped.end)
                != (candidate['subject_key'], candidate['predicate_key'], candidate['value'], candidate['source_start'], candidate['source_end'])
                or _request_hash(generation_id, mapped, candidate['source_message_id'], source['content']) != operation['request_hash']):
            raise MemoryWriteConflict('candidate source changed')
        _verified_active(db, active)
        if (active['id'] != candidate['supersedes_id'] or active['wiki_revision'] != operation['expected_revision']
                or active['wiki_document_id'] != operation['wiki_document_id']):
            raise MemoryWriteConflict('target changed')
        timestamp = _now()
        document = create_document(db, store_id='integrated', title=job['title'], content=source['content'], category='memory')
        db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'owned',?,?)", (document['id'], timestamp, timestamp))
        changed = db.execute("UPDATE memory_records SET state='superseded',updated_at=? WHERE id=? AND state='active'", (timestamp, active['id']))
        if changed.rowcount != 1:
            raise MemoryWriteConflict('target changed')
        db.execute("UPDATE memory_records SET state='active',wiki_document_id=?,wiki_revision=?,updated_at=? WHERE id=? AND state='candidate'",
                   (document['id'], str(document['revision']), timestamp, candidate['id']))
        db.execute("UPDATE memory_operations SET state='applied',wiki_document_id=?,updated_at=? WHERE id=? AND state='prepared'",
                   (document['id'], timestamp, generation_id))
        db.execute("UPDATE wiki_memory_jobs SET status='completed',reason_code='stored',needs_attention=0,document_id=?,updated_at=? WHERE generation_id=?",
                   (document['id'], timestamp, generation_id))
        db.commit()
        return {'status': 'completed', 'document_id': document['id'], 'replayed': False}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def confirmation_token(database_path: str, conversation_id: str, generation_id: str) -> str | None:
    db = connect(database_path)
    try:
        job = db.execute("SELECT 1 FROM wiki_memory_jobs WHERE generation_id=? AND conversation_id=? AND store_id='integrated' AND status='awaiting_confirmation'", (generation_id, conversation_id)).fetchone()
        operation = db.execute("SELECT * FROM memory_operations WHERE id=? AND store_id='integrated' AND state='prepared'", (generation_id,)).fetchone()
        if not job or not operation:
            return None
        candidate = db.execute('SELECT * FROM memory_records WHERE id=?', (operation['record_id'],)).fetchone()
        active = db.execute('SELECT * FROM memory_records WHERE id=?', (candidate['supersedes_id'],)).fetchone() if candidate else None
        if not candidate or not active or candidate['source_conversation_id'] != conversation_id:
            return None
        return _confirmation_token(operation, candidate, active)
    except MemoryWriteConflict:
        return None
    finally:
        db.close()


def process_integrated_job(database_path: str, generation_id: str) -> None:
    """Process one local job; DB writes remain idempotent across restarts."""
    db = connect(database_path)
    try:
        row = db.execute('''SELECT j.status,j.store_id,j.explicit,m.content FROM wiki_memory_jobs j
            JOIN generation_requests g ON g.id=j.generation_id AND g.conversation_id=j.conversation_id
            JOIN messages m ON m.id=g.user_message_id AND m.conversation_id=j.conversation_id AND m.role='user'
            WHERE j.generation_id=? AND g.status='completed' ''', (generation_id,)).fetchone()
        if not row or row['store_id'] != 'integrated' or row['status'] not in ('pending', 'awaiting_confirmation'):
            return
        candidate, correction = _map_source(row['content'])
        status, explicit = row['status'], row['explicit']
    finally:
        db.close()
    if status == 'awaiting_confirmation':
        if correction and candidate:
            db = connect(database_path)
            try:
                job = db.execute("SELECT conversation_id FROM wiki_memory_jobs WHERE generation_id=? AND store_id='integrated'", (generation_id,)).fetchone()
                token = confirmation_token(database_path, job['conversation_id'], generation_id) if job else None
            finally:
                db.close()
            if token:
                resolve_conflict(database_path, job['conversation_id'], generation_id, token, approve=True)
        return
    if not candidate:
        with connect(database_path) as db:
            if len(row['content']) > 800:
                db.execute("""UPDATE wiki_memory_jobs SET status='skipped',reason_code='document_input',needs_attention=?,
                    error_message=?,updated_at=? WHERE generation_id=? AND store_id='integrated' AND status='pending'""",
                    (int(bool(explicit)), '긴 자료는 자동 기억으로 저장할 수 없습니다. 별도로 공유 Wiki 문서에 저장해 주세요.' if explicit else None, _now(), generation_id))
                return
            db.execute("""UPDATE wiki_memory_jobs SET status=?,reason_code='unsupported_format',needs_attention=?,
                error_message=?,updated_at=? WHERE generation_id=? AND store_id='integrated' AND status='pending'""",
                ('failed' if explicit else 'skipped', int(bool(explicit)),
                 '현재 자동 기억 형식으로 처리할 수 없습니다. 공유 Wiki에서 원문을 검토해 주세요.' if explicit else None, _now(), generation_id))
        return
    try:
        create_initial_memory(database_path, generation_id)
        return
    except MemoryWriteConflict as exc:
        if str(exc) not in ('active slot requires duplicate or conflict handling', 'correction target missing'):
            raise
    result = prepare_conflict(database_path, generation_id)
    if correction and result['status'] == 'awaiting_confirmation':
        db = connect(database_path)
        try:
            job = db.execute("SELECT conversation_id FROM wiki_memory_jobs WHERE generation_id=? AND store_id='integrated'", (generation_id,)).fetchone()
        finally:
            db.close()
        if not job:
            raise MemoryWriteConflict('source no longer available')
        resolve_conflict(database_path, job['conversation_id'], generation_id, result['confirmation_token'], approve=True)


def search_active_memories(database_path: str, query: str) -> list[dict]:
    """Return verified shared slots only for explicitly named devices and properties."""
    from .wiki_query import normalize_key
    text = normalize_key(query)
    if not re.search(r'(?<![a-z0-9])bc250(?![a-z0-9])', text):
        return []
    client = bool(re.search(r'(?:클라이언트|client)', text))
    subject = 'bc250_client' if client else 'bc250_server'
    patterns = {'operating_system': r'운영\s*체제|(?<![a-z])os(?![a-z])',
                'backup_path': r'백업\s*(?:경로|위치)', 'backup_frequency': r'백업\s*주기',
                'service_port': r'(?:서비스\s*)?포트'}
    predicates = [key for key, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE)]
    if not predicates:
        return []
    db = connect(database_path)
    try:
        db.execute('BEGIN')
        rows = db.execute("""SELECT * FROM memory_records WHERE store_id='integrated' AND scope_key='shared'
            AND subject_key=? AND state='active' ORDER BY predicate_key LIMIT 4""", (subject,)).fetchall()
        result = []
        for row in rows:
            if row['predicate_key'] not in predicates:
                continue
            try:
                _verified_active(db, row)
            except (MemoryWriteConflict, TypeError, ValueError):
                continue
            result.append({'subject': subject, 'predicate': row['predicate_key'], 'value': row['value'],
                           'document_id': row['wiki_document_id'], 'revision': row['wiki_revision']})
        return result
    finally:
        db.close()
