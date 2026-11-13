"""Exercise the review patch against a captured server source and temporary DB."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tests/fixtures/wiki_server_w12/main.py'
PATCH = ROOT / 'docs/patches/wiki-server-w12.patch'


@pytest.fixture
def wiki(tmp_path):
    app_dir = tmp_path / 'app'
    app_dir.mkdir()
    target = app_dir / 'main.py'
    target.write_bytes(SOURCE.read_bytes())
    subprocess.run(['patch', '--batch', '-p1', '-i', str(PATCH)], cwd=tmp_path, check=True, capture_output=True)
    text = target.read_text().replace('BASE = Path("/opt/bc250-family-wiki")', f'BASE = Path({str(tmp_path)!r})')
    assert 'BASE = Path("/opt/bc250-family-wiki")' not in text
    target.write_text(text)
    spec = importlib.util.spec_from_file_location('isolated_wiki_w12', target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_document(wiki):
    return wiki.create_document(wiki.DocumentInput(title='title', content='original'))['id']


def test_same_revision_only_one_conditional_writer_succeeds(wiki):
    doc_id = make_document(wiki)
    old = wiki.get_document(doc_id)
    def change(content):
        try:
            body = wiki.ConditionalDocumentInput(title='title', content=content, expected_revision=old['revision'])
            return wiki.update_document_conditionally(doc_id, body)
        except HTTPException as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(change, ['first', 'second']))
    errors = [r for r in responses if isinstance(r, HTTPException)]
    successes = [r for r in responses if isinstance(r, dict)]
    assert len(errors) == len(successes) == 1
    assert errors[0].status_code == 409 and errors[0].detail['code'] == 'wiki_revision_conflict'
    result = wiki.get_document(doc_id)
    assert result['content'] in ('first', 'second')
    assert result['revision'] == successes[0]['revision'] != old['revision']


def test_legacy_edit_invalidates_stale_revision_and_preserves_response(wiki):
    doc_id = make_document(wiki)
    original = wiki.get_document(doc_id)
    assert wiki.update_document(doc_id, wiki.DocumentInput(title='title', content='external edit')) == {'id': doc_id, 'status': 'updated'}
    with pytest.raises(HTTPException) as exc:
        wiki.update_document_conditionally(doc_id, wiki.ConditionalDocumentInput(title='title', content='overwrite', expected_revision=original['revision']))
    assert exc.value.status_code == 409
    assert wiki.get_document(doc_id)['content'] == 'external edit'
    assert wiki.search_documents('external')[0]['id'] == doc_id
    assert wiki.delete_document(doc_id) == {'status': 'deleted'}
    with pytest.raises(HTTPException) as exc:
        wiki.update_document_conditionally(doc_id, wiki.ConditionalDocumentInput(title='x', content='x', expected_revision=original['revision']))
    assert exc.value.status_code == 404


def test_repeated_equal_content_advances_revision(wiki):
    doc_id = make_document(wiki)
    revisions = []
    for _ in range(3):
        revisions.append(wiki.get_document(doc_id)['revision'])
        wiki.update_document(doc_id, wiki.DocumentInput(title='title', content='original'))
    assert len(set(revisions)) == 3
