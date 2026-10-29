import asyncio
import httpx

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
    class Client:
        def __init__(self, **kwargs): calls.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, url, **kwargs):
            calls.update(method=method, url=url)
            return httpx.Response(200, json={"status": "ok"}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert asyncio.run(wiki_request("GET", "/api/health")) == {"status": "ok"}
    assert calls["url"] == "http://127.0.0.1:8020/api/health"
    assert calls["follow_redirects"] is False
    assert calls["trust_env"] is False
