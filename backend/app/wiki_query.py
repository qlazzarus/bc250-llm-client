"""Narrow Wiki lookup normalization; never changes stored text."""
import re
import unicodedata

NORMALIZATION_VERSION = 1
_BC250 = re.compile(r'(?<![a-z0-9])bc[ -]?250(?![a-z0-9])')
_DEFINITION_END = re.compile(r'\s*(?:뭐(?:야|예요|에요|입니까)|무엇(?:인가요|입니까|이야|인가)|알려\s*줘)\s*[?.!？]*\s*$')
_FOLLOWUP = re.compile(r'^\s*(?:그|이|저)\s*(?:서버|것|거|경로|설정|문서)(?:은|는|이|가|을|를)?\s*(?:뭐|어디|어떻게|알려|있|야|예|입|\?)')


def normalize_key(text: str) -> str:
    value = ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
    return _BC250.sub('bc250', value)


def entity_keys(question: str) -> list[str]:
    value = normalize_key(question)
    base = _DEFINITION_END.sub('', value).strip().rstrip('?？.!').strip()
    keys = [base] if base else []
    # Remove a Korean particle only if it follows an explicit BC250 group or
    # a space-separated title and a registered key confirms the alternative.
    stripped = re.sub(r'(?<=\d)(?:은|는|이|가)$', '', base)
    stripped = re.sub(r'\s+(?:은|는|이|가)$', '', stripped).strip()
    if _DEFINITION_END.search(value):
        stripped = re.sub(r'(?<=[가-힣])(?:은|는|이|가)$', '', stripped)
    if stripped and stripped != base:
        keys.append(stripped)
    return keys


def rag_search_query(question: str, history: list[dict]) -> str:
    """Keep independent questions independent; use a previous user only for clear follow-ups."""
    text = question.strip()
    if len(text) <= 40 and _FOLLOWUP.search(text):
        previous = next((str(row.get('content', ''))[:400] for row in reversed(history) if row.get('role') == 'user'), '')
        return (text + ' ' + previous).strip()[:4000]
    return question[:4000]
