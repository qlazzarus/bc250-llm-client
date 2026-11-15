"""Persistent Wiki address registry. Callers own the surrounding transaction."""
import json
import sqlite3
import unicodedata


def _key(parts):
    return json.dumps([unicodedata.normalize('NFC', part).casefold() for part in parts], ensure_ascii=False, separators=(',', ':'))


def _validate(parts):
    if not isinstance(parts, list) or not 2 <= len(parts) <= 17:
        raise ValueError('invalid wiki path')
    if any(not isinstance(part, str) or not part.strip() or part in ('.', '..') or '\x00' in part or len(part) > 200 for part in parts):
        raise ValueError('invalid wiki path segment')


def _category(db, segments):
    parent = 0
    for segment in segments:
        name_key = unicodedata.normalize('NFC', segment).casefold()
        row = db.execute('SELECT id FROM wiki_categories WHERE parent_key=? AND name_key=?', (parent, name_key)).fetchone()
        if row is None:
            cur = db.execute('INSERT INTO wiki_categories(parent_key,name,name_key) VALUES (?,?,?)', (parent, segment, name_key))
            parent = cur.lastrowid
        else:
            parent = row['id']
    return parent


def register(db, document_id, segments, *, collision_suffix=False):
    _validate(segments)
    original = list(segments)
    suffix = 0
    while True:
        key = _key(segments)
        existing = db.execute('SELECT document_id FROM wiki_document_paths WHERE path_key=?', (key,)).fetchone()
        if existing is None or existing['document_id'] == document_id:
            break
        if not collision_suffix:
            raise ValueError('wiki path already reserved')
        suffix += 1
        ending = f' ({document_id}' + (f'-{suffix}' if suffix > 1 else '') + ')'
        segments = original[:-1] + [original[-1][:200 - len(ending)] + ending]
    category_id = _category(db, segments[:-1])
    db.execute('UPDATE wiki_document_paths SET is_current=0 WHERE document_id=? AND is_current=1', (document_id,))
    db.execute('''INSERT INTO wiki_document_paths(path_key,segments,document_id,is_current) VALUES (?,?,?,1)
                  ON CONFLICT(path_key) DO UPDATE SET segments=excluded.segments,is_current=1''',
               (key, json.dumps(segments, ensure_ascii=False), document_id))
    db.execute('''INSERT INTO wiki_document_categories(document_id,category_id) VALUES (?,?)
                  ON CONFLICT(document_id) DO UPDATE SET category_id=excluded.category_id''', (document_id, category_id))
    return segments


def current(db, document_id):
    row = db.execute('SELECT segments FROM wiki_document_paths WHERE document_id=? AND is_current=1', (document_id,)).fetchone()
    return json.loads(row['segments']) if row else None


def resolve(db, segments):
    _validate(segments)
    row = db.execute('''SELECT p.document_id FROM wiki_document_paths p JOIN wiki_documents d ON d.id=p.document_id
        WHERE p.path_key=? AND NOT EXISTS (SELECT 1 FROM memory_document_quarantine q
        WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)''', (_key(segments),)).fetchone()
    if row is None:
        return None
    return {'id': row['document_id'], 'path_segments': current(db, row['document_id'])}


def migrate(db):
    db.execute('''CREATE TABLE wiki_categories(id INTEGER PRIMARY KEY, parent_key INTEGER NOT NULL,
        name TEXT NOT NULL, name_key TEXT NOT NULL, UNIQUE(parent_key,name_key))''')
    db.execute('''CREATE TABLE wiki_document_categories(document_id INTEGER PRIMARY KEY REFERENCES wiki_documents(id) ON DELETE CASCADE,
        category_id INTEGER NOT NULL REFERENCES wiki_categories(id))''')
    db.execute('''CREATE TABLE wiki_document_paths(path_key TEXT PRIMARY KEY, segments TEXT NOT NULL,
        document_id INTEGER REFERENCES wiki_documents(id) ON DELETE SET NULL, is_current INTEGER NOT NULL CHECK(is_current IN (0,1)))''')
    db.execute('CREATE UNIQUE INDEX wiki_one_current_path ON wiki_document_paths(document_id) WHERE is_current=1 AND document_id IS NOT NULL')
    for row in db.execute('SELECT id,title,category FROM wiki_documents ORDER BY id').fetchall():
        category = row['category'] or '미분류'
        title = row['title']
        # Dot-only URL segments are normalized by browsers; assign a safe deterministic address.
        if category in ('.', '..') or not category.strip() or '\x00' in category: category = f'분류-{row["id"]}'
        if title in ('.', '..') or not title.strip() or '\x00' in title: title = f'문서-{row["id"]}'
        register(db, row['id'], [category, title], collision_suffix=True)
