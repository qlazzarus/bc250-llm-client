import asyncio
import httpx
import pytest
from fastapi import HTTPException

from backend.app.wiki import excerpts, wiki_request


def test_excerpt_finds_relevant_passage_and_bounds_results():
    docs = [{"id": i, "title": f"문서 {i}", "updated_at": "2026-01-01", "content": "가" * 1500 + "비밀번호 안내" + "나" * 1500} for i in range(4)]
    result = excerpts(docs, "비밀번호")
    assert len(result) == 3
    assert "비밀번호 안내" in result[0]["excerpt"]
    assert len(result[0]["excerpt"]) <= 1000
    assert result[0]["updated_at"] == "2026-01-01"


def test_invalid_documents_are_ignored():
    assert excerpts([{"id": "1", "content": "내용"}, {"id": 2, "content": None}], "내용") == []


def test_wiki_request_does_not_follow_redirects_or_use_proxy(monkeypatch):
    calls = {}
    original = httpx.AsyncClient
    def handler(request):
        calls.update(method=request.method, url=str(request.url))
        return httpx.Response(200, json={"status": "ok"})
    def Client(**kwargs):
        calls.update(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert asyncio.run(wiki_request("GET", "/api/health")) == {"status": "ok"}
    assert calls["url"] == "http://127.0.0.1:8020/api/health"
    assert calls["follow_redirects"] is False
    assert calls["trust_env"] is False


def mock_wiki(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize(('status', 'code', 'expected'), [
    (404, 'wiki_not_found', 404), (409, 'wiki_conflict', 409),
    (422, 'wiki_validation_error', 422), (503, 'wiki_unavailable', 503),
    (302, 'wiki_upstream_error', 502), (500, 'wiki_upstream_error', 502),
    (204, 'wiki_invalid_response', 502),
])
def test_http_statuses_are_distinguished(monkeypatch, status, code, expected):
    mock_wiki(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wiki_request('GET', '/api/search'))
    assert (exc.value.status_code, exc.value.detail['code']) == (expected, code)


@pytest.mark.parametrize('exception,code,status', [
    (httpx.ReadTimeout('timeout'), 'wiki_timeout', 504),
    (httpx.ConnectError('offline'), 'wiki_unavailable', 503),
])
def test_transport_errors_are_distinguished(monkeypatch, exception, code, status):
    def handler(request):
        raise exception
    mock_wiki(monkeypatch, handler)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wiki_request('GET', '/api/search'))
    assert (exc.value.status_code, exc.value.detail['code']) == (status, code)


@pytest.mark.parametrize(('method', 'path', 'payload'), [
    ('GET', '/api/search', {}), ('GET', '/api/search', [None]),
    ('GET', '/api/search', [{'id': True, 'content': 'text'}]),
    ('GET', '/api/search', [{'id': 1, 'content': None}]),
    ('GET', '/api/documents', [None]),
    ('GET', '/api/documents/1', {'id': 2, 'content': 'text'}),
    ('POST', '/api/documents', {'status': 'ok'}),
    ('PUT', '/api/documents/1', {'id': 2}),
    ('DELETE', '/api/documents/1', []),
])
def test_invalid_payloads_fail_closed(monkeypatch, method, path, payload):
    mock_wiki(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wiki_request(method, path))
    assert (exc.value.status_code, exc.value.detail['code']) == (502, 'wiki_invalid_response')


def test_search_empty_and_valid_results_keep_existing_shape(monkeypatch):
    docs = [{'id': 1, 'title': '제목', 'content': '본문', 'updated_at': 'v1'}]
    responses = iter([[], docs, {'id': 1, 'title': '제목'}])
    mock_wiki(monkeypatch, lambda request: httpx.Response(200, json=next(responses)))
    assert asyncio.run(wiki_request('GET', '/api/search')) == []
    assert asyncio.run(wiki_request('GET', '/api/search')) == docs
    assert asyncio.run(wiki_request('POST', '/api/documents')) == {'id': 1, 'title': '제목'}


@pytest.mark.parametrize('body', [b'not json', b'\xff'])
def test_invalid_json_is_not_an_empty_search(monkeypatch, body):
    mock_wiki(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wiki_request('GET', '/api/search'))
    assert exc.value.detail['code'] == 'wiki_invalid_response'


def test_oversized_response_and_document_count_are_rejected(monkeypatch):
    import backend.app.wiki as wiki
    monkeypatch.setattr(wiki, 'MAX_WIKI_RESPONSE_BYTES', 20)
    mock_wiki(monkeypatch, lambda request: httpx.Response(200, content=b' ' * 21))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wiki_request('GET', '/api/search'))
    assert exc.value.detail['code'] == 'wiki_invalid_response'


def test_oversized_result_count_is_rejected(monkeypatch):
    import backend.app.wiki as wiki
    monkeypatch.setattr(wiki, 'MAX_WIKI_DOCUMENTS', 1)
    mock_wiki(monkeypatch, lambda request: httpx.Response(200, json=[{'id': 1}, {'id': 2}]))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(wiki_request('GET', '/api/documents'))
    assert exc.value.detail['code'] == 'wiki_invalid_response'
