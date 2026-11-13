import json

from backend.app.wiki_evidence import evidence_snapshot


def test_provided_depends_on_actual_payload_and_citations_are_checked():
    found = [{'id': 1, 'title': '문서', 'revision': 2}, {'id': 2, 'title': '미전달'}]
    sources = [{'id': 1, 'title': '문서', 'excerpt': '발췌', 'updated_at': 't'}]
    payload = {'messages': [{'role': 'user', 'content': '참고자료: ' + json.dumps(sources, ensure_ascii=False)}]}
    evidence = evidence_snapshot(found, sources, [], payload, '답 [wiki:1] [wiki:99]', 'integrated')
    assert [item['id'] for item in evidence['retrieved']] == [1, 2]
    assert evidence['provided'][0]['excerpt'] == '발췌'
    assert evidence['provided'][0]['revision'] == 2
    assert evidence['cited'] == [1] and evidence['invalid_citations'] == [99]
    assert evidence['evidence_status'] == 'citation_only'
    payload['messages'][0]['content'] = '참고자료 없이 질문'
    evidence = evidence_snapshot(found, sources, [], payload, '[wiki:1]', 'integrated')
    assert evidence['provided'] == [] and evidence['invalid_citations'] == [1]


def test_shared_memory_value_is_not_persisted_as_metadata():
    memories = [{'document_id': 1, 'revision': '1', 'subject': 's', 'predicate': 'p', 'value': 'private'}]
    payload = {'messages': [{'role': 'user', 'content': json.dumps(memories, ensure_ascii=False)}]}
    evidence = evidence_snapshot([], [], memories, payload, '[wiki:1] [wiki:2]', 'integrated')
    assert 'private' not in json.dumps(evidence)
    assert evidence['memory_provided'][0]['document_id'] == 1
    assert evidence['memory_cited'] == [1] and evidence['invalid_citations'] == [2]


def test_evidence_migration_is_repeatable_and_deleted_with_message(tmp_path):
    from backend.app.db import connect, initialize
    path = str(tmp_path / 'client.db')
    initialize(path)
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES ('c','t','t','t')")
        db.execute("INSERT INTO messages VALUES ('m','c','assistant','답','t')")
        db.execute("INSERT INTO wiki_message_evidence VALUES ('m','{}')")
        assert db.execute('SELECT count(*) FROM schema_migrations WHERE version=21').fetchone()[0] == 1
        db.execute("DELETE FROM messages WHERE id='m'")
        assert db.execute('SELECT count(*) FROM wiki_message_evidence').fetchone()[0] == 0
