"""Bounded loopback access to the family Wiki service."""
import json
import re

import httpx
from fastapi import HTTPException

WIKI_URL = "http://127.0.0.1:8020"
MAX_WIKI_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_WIKI_DOCUMENTS = 1000


def _error(status, code, message):
    return HTTPException(status, {"code": code, "message": message})


def _invalid_response():
    return _error(502, "wiki_invalid_response", "Wiki 응답 형식을 확인할 수 없습니다.")


def _valid_document(document, *, require_content=False):
    if not isinstance(document, dict) or type(document.get('id')) is not int or document['id'] < 1:
        return False
    if require_content and not isinstance(document.get('content'), str):
        return False
    return all(key not in document or isinstance(document[key], str)
               for key in ('title', 'content', 'category', 'updated_at'))


def _validate_response(method, path, result):
    if method == 'GET' and path in ('/api/search', '/api/documents'):
        if (not isinstance(result, list) or len(result) > MAX_WIKI_DOCUMENTS
                or any(not _valid_document(item, require_content=path == '/api/search') for item in result)):
            raise _invalid_response()
    elif re.fullmatch(r'/api/documents/\d+', path) and method == 'GET':
        if not _valid_document(result, require_content=True) or result['id'] != int(path.rsplit('/', 1)[1]):
            raise _invalid_response()
    elif ((path == '/api/documents' and method == 'POST')
          or (re.fullmatch(r'/api/documents/\d+', path) and method == 'PUT')):
        if not _valid_document(result):
            raise _invalid_response()
        if method == 'PUT' and result['id'] != int(path.rsplit('/', 1)[1]):
            raise _invalid_response()
    elif path.startswith('/api/documents/') and method == 'DELETE':
        if not isinstance(result, dict):
            raise _invalid_response()
    return result


async def wiki_request(method: str, path: str, **kwargs):
    method = method.upper()
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
            async with client.stream(method, WIKI_URL + path, **kwargs) as response:
                errors = {
                    404: ('wiki_not_found', 'Wiki 문서를 찾을 수 없습니다.'),
                    409: ('wiki_conflict', 'Wiki 문서가 변경되었습니다.'),
                    422: ('wiki_validation_error', 'Wiki 요청이 유효하지 않습니다.'),
                    503: ('wiki_unavailable', 'Wiki 서비스를 사용할 수 없습니다.'),
                }
                if response.status_code in errors:
                    code, message = errors[response.status_code]
                    raise _error(response.status_code, code, message)
                if not 200 <= response.status_code < 300:
                    raise _error(502, 'wiki_upstream_error', 'Wiki 요청이 실패했습니다.')
                if response.status_code == 204:
                    # A write may already have succeeded; callers must treat it as unknown.
                    raise _invalid_response()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_WIKI_RESPONSE_BYTES:
                        raise _invalid_response()
                    data.extend(chunk)
                try:
                    result = json.loads(data)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise _invalid_response() from exc
                return _validate_response(method, path, result)
    except httpx.TimeoutException as exc:
        raise _error(504, 'wiki_timeout', 'Wiki 응답 시간이 초과되었습니다.') from exc
    except httpx.HTTPError as exc:
        raise _error(503, 'wiki_unavailable', 'Wiki 서비스에 연결하지 못했습니다.') from exc


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
