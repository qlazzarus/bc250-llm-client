"""Loopback-only access to the family Wiki service."""
import httpx
from fastapi import HTTPException

WIKI_URL = "http://127.0.0.1:8020"


async def wiki_request(method: str, path: str, **kwargs):
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, WIKI_URL + path, **kwargs)
            if response.status_code == 404:
                raise HTTPException(404, {"code": "wiki_not_found", "message": "Wiki 문서를 찾을 수 없습니다."})
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, {"code": "wiki_unavailable", "message": "Wiki 서비스에 연결하지 못했습니다."}) from exc


def excerpts(documents, query):
    """Bound the prompt and prefer passages containing terms from the question."""
    import re
    terms = [w.casefold() for w in re.findall(r"[^\W_]+", query) if len(w) >= 2]
    result = []
    for doc in documents[:3]:
        if not isinstance(doc, dict) or not isinstance(doc.get("content"), str) or not isinstance(doc.get("id"), int):
            continue
        content = doc["content"]
        lowered = content.casefold()
        positions = [lowered.find(term) for term in terms]
        positions = [p for p in positions if p >= 0]
        start = max(0, min(positions) - 300) if positions else 0
        result.append({"id": doc["id"], "title": str(doc.get("title", ""))[:200], "updated_at": str(doc.get("updated_at", ""))[:80], "excerpt": content[start:start + 1000]})
    return result
