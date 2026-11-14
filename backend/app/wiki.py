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
    """Choose one bounded passage, favoring less frequent question terms.

    The search remains lexical: missing terms and paraphrases can still miss the
    relevant passage. Repeated matches are capped to keep the work bounded.
    """
    terms = list(dict.fromkeys(w.casefold() for w in re.findall(r"[^\W_]+", query) if len(w) >= 2))[:12]
    result = []
    for doc in documents[:3]:
        if not isinstance(doc, dict) or not isinstance(doc.get("content"), str) or type(doc.get("id")) is not int:
            continue
        content = doc["content"]
        # Preserve original character offsets even when Unicode case folding expands a character.
        lowered = content.casefold()
        offsets = None
        if len(lowered) != len(content):
            offsets = [index for index, character in enumerate(content) for _ in character.casefold()]
        title = str(doc.get("title", "")).casefold()
        hits = []
        for order, term in enumerate(terms):
            positions = []
            at = lowered.find(term)
            while at >= 0 and len(positions) < 32:
                positions.append(offsets[at] if offsets is not None else at)
                at = lowered.find(term, at + len(term))
            matched_term = term
            # Only try a Korean particle when the original term was absent.
            # Require a standalone stem of at least two Hangul syllables in the
            # document; never broaden document retrieval or stored text.
            if not positions and re.fullmatch(r'[가-힣]{2,}[은는이가을를]', term):
                stem = term[:-1]
                boundary = re.compile(r'(?<![가-힣a-z0-9])' + re.escape(stem) + r'(?![가-힣a-z0-9])')
                for match in boundary.finditer(lowered):
                    at = match.start()
                    positions.append(offsets[at] if offsets is not None else at)
                    if len(positions) == 32:
                        break
                if positions:
                    matched_term = stem
            if positions:
                hits.append((matched_term, positions, order))
        start = 0
        if hits:
            # Prefer a non-title term that occurs less often; use later query terms
            # only as a deterministic tie breaker. Keep 300 characters of context.
            _, positions, _ = min(hits, key=lambda hit: (len(hit[1]), hit[0] in title, -hit[2]))
            start = max(0, positions[0] - 300)
        result.append({"id": doc["id"], "title": str(doc.get("title", ""))[:200], "updated_at": str(doc.get("updated_at", ""))[:80], "excerpt": content[start:start + 1000]})
    return result
