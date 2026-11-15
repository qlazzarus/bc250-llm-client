import sqlite3
import pytest
from backend.app.db import initialize, connect
from backend.app import wiki_paths
from backend.app.wiki_repository import create_document, update_document, delete_document


def test_paths_stable_on_rename_and_reserved_after_deletion(tmp_path):
    path = str(tmp_path / 'db')
    initialize(path)
    db = connect(path)
    with db:
        db.execute('BEGIN IMMEDIATE')
        doc = create_document(db, store_id='integrated', title='기기 #1', content='x', category='general/iot', category_path=['general', 'iot'])
    assert doc['path_segments'] == ['general', 'iot', '기기 #1']
    with db:
        db.execute('BEGIN IMMEDIATE')
        updated = update_document(db, store_id='integrated', document_id=doc['id'], expected_revision=1, title='새 이름', content='x', category='general/iot')
    assert wiki_paths.resolve(db, ['general', 'iot', '기기 #1']) == {'id': doc['id'], 'path_segments': ['general', 'iot', '새 이름']}
    with db:
        db.execute('BEGIN IMMEDIATE')
        delete_document(db, store_id='integrated', document_id=doc['id'], expected_revision=updated['revision'])
    assert wiki_paths.resolve(db, ['general', 'iot', '기기 #1']) is None
    with db:
        db.execute('BEGIN IMMEDIATE')
        new = create_document(db, store_id='integrated', title='기기 #1', content='x', category='general/iot', category_path=['general', 'iot'])
    assert new['path_segments'] != ['general', 'iot', '기기 #1']
    db.close()


def test_case_and_unicode_collision_do_not_take_existing_path(tmp_path):
    path = str(tmp_path / 'db'); initialize(path); db = connect(path)
    with db:
        db.execute('BEGIN IMMEDIATE')
        first = create_document(db, store_id='integrated', title='서버', content='x')
        second = create_document(db, store_id='integrated', title='서버', content='x')
    assert first['path_segments'] != second['path_segments']
    assert wiki_paths.resolve(db, ['GENERAL', '서버'])['id'] == first['id']
    db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'owned','t','t')", (first['id'],))
    assert wiki_paths.resolve(db, ['general', '서버']) is None
    db.close()


def test_migration_preserves_legacy_slashes_and_duplicate_documents(tmp_path):
    path = str(tmp_path / 'db'); initialize(path); db = connect(path)
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DROP TABLE wiki_document_categories')
        db.execute('DROP TABLE wiki_document_paths')
        db.execute('DROP TABLE wiki_categories')
        db.execute('DELETE FROM schema_migrations WHERE version=23')
        db.execute("INSERT INTO wiki_documents(title,content,category,created_at,updated_at) VALUES ('A','x','general/iot','t','t')")
        db.execute("INSERT INTO wiki_documents(title,content,category,created_at,updated_at) VALUES ('A','y','general/iot','t','t')")
    db.close(); initialize(path); db = connect(path)
    first, second = db.execute('SELECT id FROM wiki_documents ORDER BY id').fetchall()
    assert wiki_paths.current(db, first['id']) == ['general/iot', 'A']
    assert wiki_paths.current(db, second['id']) == ['general/iot', f'A ({second["id"]})']
    assert db.execute('SELECT COUNT(*) FROM wiki_documents').fetchone()[0] == 2
    db.close()

@pytest.mark.asyncio
async def test_path_api_respects_auth_and_quarantine(tmp_path):
    import httpx
    from types import SimpleNamespace
    from backend.app import main
    from backend.app.config import get_settings
    path = str(tmp_path / 'db'); initialize(path); db = connect(path)
    with db:
        db.execute('BEGIN IMMEDIATE')
        doc = create_document(db, store_id='integrated', title='AMD BC-250 AI 서버', content='x')
    settings = SimpleNamespace(database_path=path, wiki_store='integrated', app_auth_mode='key', app_api_key='test', max_request_bytes=65536)
    main.app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            payload = {'segments': ['general', 'AMD BC-250 AI 서버']}
            assert (await client.post('/api/v1/wiki/resolve-path', json=payload)).status_code == 401
            client.headers['Authorization'] = 'Bearer test'
            assert (await client.post('/api/v1/wiki/resolve-path', json=payload)).json() == {'id': doc['id'], 'path_segments': payload['segments']}
            with db:
                db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'owned','t','t')", (doc['id'],))
            assert (await client.post('/api/v1/wiki/resolve-path', json=payload)).status_code == 404
    finally:
        main.app.dependency_overrides.pop(get_settings, None)
        db.close()
