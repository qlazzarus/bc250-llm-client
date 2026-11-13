"""Synchronous integrated Wiki operations on a caller-owned SQLite connection.

Callers must explicitly identify the integrated store, manage transactions, and
close connections. Write transactions must be started before calling a writer.
No network or model calls are performed here.
"""
from datetime import datetime, timedelta, timezone
from difflib import unified_diff
import json
import re
import sqlite3
import unicodedata
from .wiki_query import NORMALIZATION_VERSION, entity_keys, normalize_key


class WikiNotFound(LookupError):
    pass


class WikiConflict(ValueError):
    pass


class WikiDiffTooLarge(ValueError):
    pass


def _store(store_id):
    if store_id != 'integrated':
        raise ValueError('integrated store required')


def _document_id(document_id):
    if type(document_id) is not int or document_id < 1:
        raise ValueError('invalid document ID')


def _revision(revision):
    if type(revision) is not int or revision < 1:
        raise ValueError('invalid expected revision')


def _write_transaction(db):
    if not db.in_transaction:
        raise ValueError('caller must begin a write transaction')


def _fields(title, content, category):
    if (not isinstance(title, str) or not title.strip() or len(title.strip()) > 200
            or not isinstance(content, str) or not 1 <= len(content) <= 100000
            or not isinstance(category, str) or len(category) > 80):
        raise ValueError('invalid document fields')


def _now():
    return datetime.now(timezone.utc).isoformat()


def create_document(db: sqlite3.Connection, *, store_id: str, title: str, content: str, category: str = 'general') -> dict:
    _store(store_id)
    _fields(title, content, category)
    _write_transaction(db)
    timestamp = _now()
    cursor = db.execute('INSERT INTO wiki_documents(title,content,category,created_at,updated_at) VALUES (?,?,?,?,?)',
                        (title, content, category, timestamp, timestamp))
    return get_document(db, store_id=store_id, document_id=cursor.lastrowid)


def get_document(db: sqlite3.Connection, *, store_id: str, document_id: int) -> dict:
    _store(store_id)
    _document_id(document_id)
    row = db.execute('SELECT * FROM wiki_documents WHERE id=?', (document_id,)).fetchone()
    if row is None:
        raise WikiNotFound('document not found')
    return dict(row)


def list_documents(db: sqlite3.Connection, *, store_id: str, limit: int = 100) -> list[dict]:
    _store(store_id)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('invalid limit')
    return [dict(row) for row in db.execute('''SELECT d.id,d.title,d.category,d.revision,d.created_at,d.updated_at
        FROM wiki_documents d WHERE NOT EXISTS (SELECT 1 FROM memory_document_quarantine q
          WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)
        ORDER BY d.updated_at DESC,d.id DESC LIMIT ?''', (limit,)).fetchall()]


def list_aliases(db: sqlite3.Connection, *, store_id: str, document_id: int) -> list[str]:
    _visible_document(db, store_id, document_id)
    return [row['alias_original'] for row in db.execute('''SELECT alias_original FROM wiki_document_aliases
        WHERE document_id=? ORDER BY alias_key''', (document_id,)).fetchall()]


def replace_aliases(db: sqlite3.Connection, *, store_id: str, document_id: int,
                    expected_revision: int, aliases: list[str]) -> dict:
    _store(store_id)
    _document_id(document_id)
    _revision(expected_revision)
    _write_transaction(db)
    _visible_document(db, store_id, document_id)
    if not isinstance(aliases, list) or len(aliases) > 20:
        raise ValueError('invalid aliases')
    prepared = []
    seen = set()
    for alias in aliases:
        if not isinstance(alias, str) or not alias.strip() or len(alias) > 200 or '\x00' in alias:
            raise ValueError('invalid alias')
        key = normalize_key(alias)
        if not key or key in seen:
            raise ValueError('duplicate alias')
        seen.add(key)
        prepared.append((document_id, alias.strip(), key, NORMALIZATION_VERSION))
    document = get_document(db, store_id=store_id, document_id=document_id)
    document = update_document(db, store_id=store_id, document_id=document_id, expected_revision=expected_revision,
                               title=document['title'], content=document['content'], category=document['category'])
    db.execute('DELETE FROM wiki_document_aliases WHERE document_id=?', (document_id,))
    db.executemany('INSERT INTO wiki_document_aliases VALUES (?,?,?,?)', prepared)
    return {'id': document_id, 'revision': document['revision'], 'aliases': [item[1] for item in prepared]}


def update_document(db: sqlite3.Connection, *, store_id: str, document_id: int, expected_revision: int,
                    title: str, content: str, category: str = 'general') -> dict:
    _store(store_id)
    _document_id(document_id)
    _revision(expected_revision)
    _fields(title, content, category)
    _write_transaction(db)
    current = db.execute('SELECT updated_at FROM wiki_documents WHERE id=?', (document_id,)).fetchone()
    if current is None:
        raise WikiNotFound('document not found')
    try:
        previous = datetime.fromisoformat(current['updated_at'])
        if previous.tzinfo is None:
            raise ValueError('timezone required')
        timestamp = max(datetime.now(timezone.utc), previous + timedelta(microseconds=1)).isoformat()
    except (ValueError, OverflowError) as exc:
        raise WikiConflict('invalid document timestamp') from exc
    cursor = db.execute('''UPDATE wiki_documents SET title=?,content=?,category=?,revision=revision+1,updated_at=?
                           WHERE id=? AND revision=?''', (title, content, category, timestamp, document_id, expected_revision))
    if cursor.rowcount != 1:
        _raise_missing_or_conflict(db, document_id)
    return get_document(db, store_id=store_id, document_id=document_id)


def delete_document(db: sqlite3.Connection, *, store_id: str, document_id: int, expected_revision: int) -> None:
    _store(store_id)
    _document_id(document_id)
    _revision(expected_revision)
    _write_transaction(db)
    cursor = db.execute('DELETE FROM wiki_documents WHERE id=? AND revision=?', (document_id, expected_revision))
    if cursor.rowcount != 1:
        _raise_missing_or_conflict(db, document_id)


def _raise_missing_or_conflict(db, document_id):
    if db.execute('SELECT 1 FROM wiki_documents WHERE id=?', (document_id,)).fetchone() is None:
        raise WikiNotFound('document not found')
    raise WikiConflict('document revision changed')


def _visible_document(db, store_id, document_id):
    _store(store_id)
    _document_id(document_id)
    row = db.execute('''SELECT 1 FROM wiki_documents d WHERE d.id=? AND NOT EXISTS
        (SELECT 1 FROM memory_document_quarantine q WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)''',
        (document_id,)).fetchone()
    if row is None:
        raise WikiNotFound('document unavailable')


def list_revisions(db: sqlite3.Connection, *, store_id: str, document_id: int) -> list[dict]:
    _visible_document(db, store_id, document_id)
    rows = db.execute('''SELECT revision,created_at FROM wiki_document_revisions
                         WHERE document_id=? ORDER BY revision DESC LIMIT 100''', (document_id,)).fetchall()
    return [dict(row) for row in rows]


def diff_revisions(db: sqlite3.Connection, *, store_id: str, document_id: int,
                   from_revision: int, to_revision: int) -> dict:
    _visible_document(db, store_id, document_id)
    _revision(from_revision)
    _revision(to_revision)
    rows = db.execute('''SELECT revision,title,content,category FROM wiki_document_revisions
                         WHERE document_id=? AND revision IN (?,?)''',
                      (document_id, from_revision, to_revision)).fetchall()
    versions = {row['revision']: row for row in rows}
    if from_revision not in versions or to_revision not in versions:
        raise WikiNotFound('revision not found')
    old, new = versions[from_revision], versions[to_revision]
    fields = ('title', 'content', 'category')
    if sum(len(old[field].splitlines()) + len(new[field].splitlines()) for field in fields) > 4000:
        raise WikiDiffTooLarge('diff exceeds line limit')
    differences = {}
    for field in fields:
        lines = unified_diff(old[field].splitlines(keepends=True), new[field].splitlines(keepends=True),
                             fromfile=f'{field}@{from_revision}', tofile=f'{field}@{to_revision}')
        differences[field] = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                                     for line in lines)
    result = {'document_id': document_id, 'from_revision': from_revision,
              'to_revision': to_revision, 'diff': differences}
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 2 * 1024 * 1024:
        raise WikiDiffTooLarge('diff exceeds response limit')
    return result


def search_documents(db: sqlite3.Connection, *, store_id: str, query: str, limit: int = 5) -> list[dict]:
    """Bound FTS and substring candidates; managed/quarantined documents stay out."""
    _store(store_id)
    if not isinstance(query, str) or len(query) > 4000 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('invalid search request')
    query = unicodedata.normalize('NFKC', query).lower().strip()
    if not query:
        return []
    keys = entity_keys(query)
    for key in keys:
        exact = {}
        rows = db.execute('''SELECT d.* FROM wiki_document_aliases a JOIN wiki_documents d ON d.id=a.document_id
            WHERE a.normalization_version=? AND a.alias_key=? AND NOT EXISTS
            (SELECT 1 FROM memory_document_quarantine q WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)
            ORDER BY d.id LIMIT 21''', (NORMALIZATION_VERSION, key)).fetchall()
        exact.update({row['id']: dict(row) for row in rows})
        # Titles remain authoritative text; this bounded pass does not create implicit aliases.
        titles = db.execute('''SELECT d.id,d.title FROM wiki_documents d WHERE NOT EXISTS
            (SELECT 1 FROM memory_document_quarantine q WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)
            ORDER BY d.updated_at DESC,d.id DESC LIMIT 200''').fetchall()
        for row in titles:
            if normalize_key(row['title']) == key:
                exact[row['id']] = dict(db.execute('SELECT * FROM wiki_documents WHERE id=?', (row['id'],)).fetchone())
        if exact:
            ambiguous = len(exact) > 1
            return [{**exact[doc_id], **({'alias_ambiguous': True} if ambiguous else {})}
                    for doc_id in sorted(exact)[:limit]]
    words = re.findall(r'[^\W_]+', query, flags=re.UNICODE)[:12]
    numbered_bc = re.fullmatch(r'bc[ -]\d+', query) is not None
    ranked = {}
    if words:
        expression = (' AND ' if numbered_bc else ' OR ').join('"' + word.replace('"', '""') + '"' for word in words)
        rows = db.execute('''SELECT d.*, bm25(wiki_documents_fts) AS rank FROM wiki_documents_fts
            JOIN wiki_documents d ON d.id=wiki_documents_fts.rowid
            WHERE wiki_documents_fts MATCH ? AND NOT EXISTS
              (SELECT 1 FROM memory_document_quarantine q WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)
            ORDER BY rank, d.id LIMIT 30''', (expression,)).fetchall()
        for row in rows:
            item = dict(row)
            ranked[item['id']] = (100 + max(0, -item['rank']), item)
    rows = db.execute('''SELECT d.* FROM wiki_documents d WHERE NOT EXISTS
        (SELECT 1 FROM memory_document_quarantine q WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)
        ORDER BY d.updated_at DESC, d.id DESC LIMIT 200''').fetchall()
    for row in rows:
        item = dict(row)
        title = unicodedata.normalize('NFKC', item['title']).lower()
        content = unicodedata.normalize('NFKC', item['content']).lower()
        def contains(text, term):
            if re.fullmatch(r'[a-z0-9]+', term):
                return re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', text) is not None
            return term in text
        score = (100 if contains(title, query) else 0) + (40 if contains(content, query) else 0)
        for word in ([] if numbered_bc else words):
            if len(word) >= 2:
                score += (20 if contains(title, word) else 0) + (5 if contains(content, word) else 0)
        if score:
            ranked[item['id']] = (score + ranked.get(item['id'], (0, None))[0], item)
    # A narrow fallback for spelling variants, only after the original FTS and
    # substring search found nothing. It never adds or drops query terms.
    expanded = normalize_key(query)
    if not ranked and expanded != query:
        expanded_words = re.findall(r'[^\W_]+', expanded, flags=re.UNICODE)[:12]
        if expanded_words:
            expression = ' AND '.join('"' + word.replace('"', '""') + '"' for word in expanded_words)
            rows = db.execute('''SELECT d.*, bm25(wiki_documents_fts) AS rank FROM wiki_documents_fts
                JOIN wiki_documents d ON d.id=wiki_documents_fts.rowid
                WHERE wiki_documents_fts MATCH ? AND NOT EXISTS
                  (SELECT 1 FROM memory_document_quarantine q WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)
                ORDER BY rank, d.id LIMIT 30''', (expression,)).fetchall()
            for row in rows:
                item = dict(row)
                ranked[item['id']] = (100 + max(0, -item['rank']), item)
    return [item for _, item in sorted(ranked.values(), key=lambda entry: (-entry[0], entry[1]['id']))[:limit]]
