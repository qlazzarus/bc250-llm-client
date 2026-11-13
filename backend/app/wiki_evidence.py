"""Bounded provenance metadata for explicitly requested Wiki evidence."""
import json
import re


def evidence_snapshot(found, sources, memories, payload, answer, store_id):
    retrieved = []
    for item in found[:3]:
        if isinstance(item, dict) and type(item.get('id')) is int:
            retrieved.append({'id': item['id'], 'title': str(item.get('title', ''))[:200],
                              'revision': item.get('revision'), 'updated_at': str(item.get('updated_at', ''))[:80],
                              'store_id': store_id})
    current = next((message.get('content', '') for message in reversed(payload.get('messages', []))
                    if message.get('role') == 'user'), '')
    references = json.dumps(sources, ensure_ascii=False)
    by_id = {item['id']: item for item in retrieved}
    provided = [{**item, 'store_id': store_id, 'revision': by_id.get(item['id'], {}).get('revision')}
                for item in sources] if sources and references in current else []
    provided_ids = {item['id'] for item in provided}
    cited_ids = list(dict.fromkeys(int(value) for value in re.findall(r'\[wiki:([1-9][0-9]{0,9})\]', answer)))[:20]
    memory_reference = json.dumps(memories, ensure_ascii=False)
    provided_memory = memories if memories and memory_reference in current else []
    memory_ids = {item['document_id'] for item in provided_memory}
    return {'version': 1, 'retrieved': retrieved, 'provided': provided,
            'memory_provided': [{'document_id': item['document_id'], 'revision': item['revision'],
                                 'subject': item['subject'], 'predicate': item['predicate']} for item in provided_memory],
            'cited': [value for value in cited_ids if value in provided_ids],
            'memory_cited': [value for value in cited_ids if value in memory_ids],
            'invalid_citations': [value for value in cited_ids if value not in provided_ids and value not in memory_ids],
            'evidence_status': 'citation_only'}
