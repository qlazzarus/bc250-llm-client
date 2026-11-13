from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.app.db import connect, initialize
from backend.app.wiki_repository import (WikiConflict, WikiNotFound, create_document, delete_document,
                                          get_document, list_documents, search_documents, update_document)


@pytest.fixture
def path(tmp_path):
    path = str(tmp_path / 'client.db')
    initialize(path)
    return path


def add(path, title='서버', content='Fedora backup'):
    db = connect(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            return create_document(db, store_id='integrated', title=title, content=content)
    finally:
        db.close()


def test_crud_requires_store_transaction_and_revision(path):
    db = connect(path)
    try:
        with pytest.raises(ValueError, match='transaction'):
            create_document(db, store_id='integrated', title='x', content='x')
        with pytest.raises(ValueError, match='integrated store'):
            get_document(db, store_id='legacy_external', document_id=1)
    finally:
        db.close()
    doc = add(path)
    db = connect(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            result = update_document(db, store_id='integrated', document_id=doc['id'], expected_revision=1,
                                     title='서버', content='Ubuntu')
            assert result['revision'] == 2
        with db:
            db.execute('BEGIN IMMEDIATE')
            with pytest.raises(WikiConflict):
                delete_document(db, store_id='integrated', document_id=doc['id'], expected_revision=1)
            with pytest.raises(WikiNotFound):
                update_document(db, store_id='integrated', document_id=999, expected_revision=1, title='x', content='x')
            delete_document(db, store_id='integrated', document_id=doc['id'], expected_revision=2)
        with pytest.raises(WikiNotFound):
            get_document(db, store_id='integrated', document_id=doc['id'])
    finally:
        db.close()


def test_two_writers_same_revision_only_one_succeeds(path):
    doc = add(path)
    def update(content):
        db = connect(path)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                return update_document(db, store_id='integrated', document_id=doc['id'], expected_revision=1,
                                       title='서버', content=content)['revision']
        except WikiConflict:
            return 'conflict'
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(update, ['one', 'two']))) == ['2', 'conflict']


def test_legacy_id_cannot_be_used_against_integrated_document(path):
    doc = add(path)
    db = connect(path)
    try:
        db.execute("INSERT INTO memory_document_quarantine VALUES ('legacy_external',?,'owned','t','t')", (doc['id'],))
        db.commit()
        for function, args in [(get_document, {}), (update_document, {'expected_revision': 1, 'title': 'x', 'content': 'x'}),
                               (delete_document, {'expected_revision': 1})]:
            with pytest.raises(ValueError, match='integrated store'):
                function(db, store_id='legacy_external', document_id=doc['id'], **args)
        assert search_documents(db, store_id='integrated', query='Fedora')[0]['id'] == doc['id']
        db.execute("INSERT INTO memory_document_quarantine VALUES ('integrated',?,'owned','t','t')", (doc['id'],))
        assert search_documents(db, store_id='integrated', query='Fedora') == []
    finally:
        db.close()


def test_search_is_bounded_and_does_not_hide_fts_errors(path):
    add(path, 'BC250 서버', 'Fedora 43')
    db = connect(path)
    try:
        assert list_documents(db, store_id='integrated')[0]['title'] == 'BC250 서버'
        assert search_documents(db, store_id='integrated', query='') == []
        assert search_documents(db, store_id='integrated', query='"Fedora" OR NOT*')[0]['title'] == 'BC250 서버'
        with pytest.raises(ValueError):
            search_documents(db, store_id='integrated', query='a' * 4001)
        with pytest.raises(ValueError):
            list_documents(db, store_id='integrated', limit=501)
        db.execute('DROP TABLE wiki_documents_fts')
        import sqlite3
        with pytest.raises(sqlite3.OperationalError):
            search_documents(db, store_id='integrated', query='Fedora')
    finally:
        db.close()


def test_writer_rolls_back_with_caller_transaction(path):
    db = connect(path)
    try:
        with pytest.raises(RuntimeError):
            with db:
                db.execute('BEGIN IMMEDIATE')
                create_document(db, store_id='integrated', title='draft', content='rollbacktest')
                raise RuntimeError('fail')
        assert list_documents(db, store_id='integrated') == []
        assert search_documents(db, store_id='integrated', query='rollbacktest') == []
    finally:
        db.close()
