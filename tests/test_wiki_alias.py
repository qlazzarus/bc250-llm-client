import pytest

from backend.app.db import connect, initialize
from backend.app.wiki_query import normalize_key, rag_search_query
from backend.app.wiki_repository import (WikiConflict, WikiNotFound, create_document,
    delete_document, list_aliases, replace_aliases, search_documents)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'wiki.db')
    initialize(path)
    conn = connect(path)
    yield conn
    conn.close()


def add(db, title, aliases=()):
    with db:
        db.execute('BEGIN IMMEDIATE')
        doc = create_document(db, store_id='integrated', title=title, content='Fedora 서버 백업 경로')
        if aliases:
            replace_aliases(db, store_id='integrated', document_id=doc['id'], expected_revision=1, aliases=list(aliases))
    return doc['id']


def search(db, q):
    return search_documents(db, store_id='integrated', query=q)


def test_explicit_alias_and_title_variants(db):
    doc_id = add(db, 'bc250 ai 서버', ['bc250'])
    add(db, '다른 문서')
    for q in ['bc250', 'bc-250', 'BC 250', 'bc250 이 뭐야?', 'bc250 ai 서버', 'bc250 ai 서버가 뭐야?']:
        assert search(db, q)[0]['id'] == doc_id, q
    for q in ['bc2500', 'xbc250', 'bc25', 'bc 2500']:
        assert all(row['id'] != doc_id for row in search(db, q)), q
    assert normalize_key('ＢＣ－２５０') == 'bc250'


def test_collision_and_namespace_quarantine(db):
    first = add(db, '서버 하나', ['bc250'])
    second = add(db, '서버 둘', ['bc-250'])
    rows = search(db, 'BC 250')
    assert {row['id'] for row in rows} == {first, second}
    assert all(row['alias_ambiguous'] for row in rows)
    db.execute("INSERT INTO memory_document_quarantine VALUES ('legacy_external',?,'owned','t','t')", (first,))
    assert {row['id'] for row in search(db, 'bc250')} == {first, second}
    db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'owned','t','t')", (first,))
    assert [row['id'] for row in search(db, 'bc250')] == [second]
    with pytest.raises(WikiNotFound):
        list_aliases(db, store_id='integrated', document_id=first)


def test_alias_write_is_revision_checked_and_deleted_with_document(db):
    doc_id = add(db, '서버', ['bc250'])
    with pytest.raises(WikiConflict):
        with db:
            db.execute('BEGIN IMMEDIATE')
            replace_aliases(db, store_id='integrated', document_id=doc_id, expected_revision=1, aliases=['다른 별칭'])
    assert list_aliases(db, store_id='integrated', document_id=doc_id) == ['bc250']
    with pytest.raises(ValueError):
        with db:
            db.execute('BEGIN IMMEDIATE')
            replace_aliases(db, store_id='integrated', document_id=doc_id, expected_revision=2, aliases=['bc250', 'BC-250'])
    assert list_aliases(db, store_id='integrated', document_id=doc_id) == ['bc250']
    with db:
        db.execute('BEGIN IMMEDIATE')
        delete_document(db, store_id='integrated', document_id=doc_id, expected_revision=2)
    assert db.execute('SELECT COUNT(*) FROM wiki_document_aliases').fetchone()[0] == 0


def test_queries_keep_independent_question_and_only_use_user_for_followup():
    history = [{'role': 'user', 'content': 'BC250 백업'}, {'role': 'assistant', 'content': '틀린 답'}]
    assert rag_search_query('bc250 이 뭐야?', history) == 'bc250 이 뭐야?'
    assert rag_search_query('그 경로는?', history) == '그 경로는? BC250 백업'


def test_alias_search_includes_old_documents_beyond_title_scan_limit(db):
    old_id = add(db, 'BC-250 장비', ['bc250'])
    for index in range(201):
        add(db, f'문서 {index}')
    assert search(db, 'bc250')[0]['id'] == old_id
    with db:
        db.execute('BEGIN IMMEDIATE')
        replace_aliases(db, store_id='integrated', document_id=old_id, expected_revision=2, aliases=[])
        db.execute("UPDATE wiki_documents SET updated_at='2000-01-01T00:00:00+00:00',revision=revision+1 WHERE id=?", (old_id,))
    assert all(item['id'] != old_id for item in search(db, 'bc250'))


def test_title_change_leaves_explicit_alias_and_fts_in_sync(db):
    from backend.app.wiki_repository import update_document
    doc_id = add(db, '예전 제목', ['서버별칭'])
    with db:
        db.execute('BEGIN IMMEDIATE')
        update_document(db, store_id='integrated', document_id=doc_id, expected_revision=2,
                        title='새 제목', content='Ubuntu 전용')
    assert search(db, '서버별칭')[0]['id'] == doc_id
    assert search(db, '새 제목')[0]['id'] == doc_id
    assert search(db, 'Ubuntu')[0]['id'] == doc_id
    assert not search(db, '예전')


def test_alias_input_special_characters_and_limits(db):
    doc_id = add(db, '서버', ['bc250'])
    for query in ['" OR NOT * : ()', 'x' * 4000]:
        assert isinstance(search(db, query), list)
    for aliases in [[''] , ['a'] * 21, ['a\x00b'], ['x' * 201]]:
        with pytest.raises(ValueError):
            with db:
                db.execute('BEGIN IMMEDIATE')
                replace_aliases(db, store_id='integrated', document_id=doc_id, expected_revision=2, aliases=aliases)
    assert list_aliases(db, store_id='integrated', document_id=doc_id) == ['bc250']


def test_similar_separated_number_does_not_match_other_bc_device(db):
    doc_id = add(db, 'BC-250 하드웨어')
    for query in ('bc 2500', 'bc-2500', 'bc 25'):
        assert all(row['id'] != doc_id for row in search(db, query))


def test_existing_fts_queries_remain_relevant(db):
    # Compare with the pre-I06 FTS + substring ranking on the same fixture.
    import re
    import time
    import unicodedata
    from statistics import median
    documents = [('Fedora 운영체제', 'Fedora Linux 설치 안내'),
                 ('백업 위치', 'archive backup 저장 경로'),
                 ('네트워크', 'network router 설정'),
                 ('사용자 안내', '가족 wiki 소개')]
    with db:
        db.execute('BEGIN IMMEDIATE')
        ids = [create_document(db, store_id='integrated', title=title, content=content)['id']
               for title, content in documents]
    def previous_search(query):
        query = unicodedata.normalize('NFKC', query).lower().strip()
        words = re.findall(r'[^\W_]+', query, flags=re.UNICODE)[:12]
        expression = ' OR '.join('"' + word.replace('"', '""') + '"' for word in words)
        ranked = {}
        rows = db.execute('''SELECT d.*,bm25(wiki_documents_fts) AS rank FROM wiki_documents_fts
            JOIN wiki_documents d ON d.id=wiki_documents_fts.rowid
            WHERE wiki_documents_fts MATCH ? AND NOT EXISTS
            (SELECT 1 FROM memory_document_quarantine q WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)
            ORDER BY rank,d.id LIMIT 30''', (expression,)).fetchall()
        for row in rows:
            ranked[row['id']] = (100 + max(0, -row['rank']), row['id'])
        rows = db.execute('''SELECT d.* FROM wiki_documents d WHERE NOT EXISTS
            (SELECT 1 FROM memory_document_quarantine q WHERE q.store_id='integrated' AND q.wiki_document_id=d.id)
            ORDER BY d.updated_at DESC,d.id DESC LIMIT 200''').fetchall()
        for row in rows:
            title = unicodedata.normalize('NFKC', row['title']).lower()
            content = unicodedata.normalize('NFKC', row['content']).lower()
            score = (100 if query in title else 0) + (40 if query in content else 0)
            for word in words:
                if len(word) >= 2:
                    score += (20 if word in title else 0) + (5 if word in content else 0)
            if score:
                ranked[row['id']] = (score + ranked.get(row['id'], (0, None))[0], row['id'])
        return [doc_id for _, doc_id in sorted(ranked.values(), key=lambda entry: (-entry[0], entry[1]))[:3]]

    timings = {'previous': [], 'current': []}
    for query, expected in zip(('Fedora', 'backup', 'network', '가족'), ids):
        previous = previous_search(query)
        current = [row['id'] for row in search(db, query)[:3]]
        assert expected in previous and expected in current
        assert set(current) == set(previous)
        for _ in range(25):
            started = time.perf_counter()
            previous_search(query)
            timings['previous'].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            search(db, query)
            timings['current'].append((time.perf_counter() - started) * 1000)
    old_p95 = sorted(timings['previous'])[int(len(timings['previous']) * .95) - 1]
    new_p95 = sorted(timings['current'])[int(len(timings['current']) * .95) - 1]
    print(f"fixture ms: previous p50={median(timings['previous']):.3f} p95={old_p95:.3f}; "
          f"current p50={median(timings['current']):.3f} p95={new_p95:.3f}")
