import pytest

import os
import json
import sqlite3
import tempfile

os.environ.update({"APP_API_KEY": "client-key", "LLM_GATE_API_KEY": "gate-key"})

from fastapi.testclient import TestClient
from backend.app import main
from backend.app.db import connect, initialize


class FakeModelsResponse:
    def raise_for_status(self): pass
    def json(self): return {"data": [{"id": "gate-selected-model"}]}


@pytest.fixture
def mock_image_analysis(monkeypatch):
    async def analyze(_body, _settings):
        return {"intent": {"intent": "image_generation", "subject": None, "environment": None, "style": None,
                           "lighting": None, "composition": None, "required_elements": [], "forbidden_elements": [],
                           "exact_text": [], "preferred_model": None, "aspect_ratio": None}, "model_id": "gate-selected-model"}
    monkeypatch.setattr(main, "analyze_image_intent", analyze)


def make_client(monkeypatch):
    directory = tempfile.TemporaryDirectory()
    monkeypatch.setattr(main.settings, "database_path", f"{directory.name}/test.db")
    monkeypatch.setattr(main.get_settings(), "database_path", f"{directory.name}/test.db")
    return TestClient(main.app), directory


def conversation_messages(client, conversation_id, headers):
    return client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers).json()["data"]


def add_completed_image_source(database_path, conversation_id, suffix="source", content=None):
    timestamp = "2026-01-01T00:00:00+00:00"
    user_id = f"{suffix}-user"
    assistant_id = f"{suffix}-assistant"
    content = content or json.dumps({
        "model_id": "z-image-nsfw-v2", "prompt": "original prompt", "width": 768,
        "height": 768, "steps": 6, "cfg_scale": 1.0,
    })
    with connect(database_path) as db:
        db.execute("INSERT INTO messages VALUES (?, ?, 'user', 'original request', ?)", (user_id, conversation_id, timestamp))
        db.execute("INSERT INTO messages VALUES (?, ?, 'assistant', ?, ?)", (assistant_id, conversation_id, content, timestamp))
        db.execute("""INSERT INTO generation_requests
                      (id,conversation_id,user_message_id,assistant_message_id,status,queued_at,created_at,updated_at)
                      VALUES (?, ?, ?, ?, 'completed', ?, ?, ?)""",
                   (f"{suffix}-generation", conversation_id, user_id, assistant_id, timestamp, timestamp, timestamp))
    return assistant_id, content


def test_conversation_lifecycle(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            created = client.post("/api/v1/conversations", headers=headers, json={"title": "테스트"})
            assert created.status_code == 201
            identifier = created.json()["id"]
            assert client.get("/api/v1/conversations", headers=headers).json()["data"][0]["id"] == identifier
            assert client.patch(f"/api/v1/conversations/{identifier}", headers=headers, json={"title": "변경"}).json()["title"] == "변경"
            assert client.delete(f"/api/v1/conversations/{identifier}", headers=headers).status_code == 204
    finally:
        directory.cleanup()


def test_message_pages_are_cursor_based_and_conversation_metadata_is_separate(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={}).json()["id"]
            with connect(main.get_settings().database_path) as db:
                for index in range(5):
                    db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", (
                        f"page-{index}", cid, "user" if index % 2 == 0 else "assistant", f"메시지 {index}", f"2026-01-01T00:00:0{index}+00:00",
                    ))

            metadata = client.get(f"/api/v1/conversations/{cid}", headers=headers).json()
            assert "messages" not in metadata
            latest = client.get(f"/api/v1/conversations/{cid}/messages?limit=2", headers=headers).json()
            assert [message["content"] for message in latest["data"]] == ["메시지 3", "메시지 4"]
            assert latest["has_more"] is True
            assert latest["next_before"] == "page-3"

            previous = client.get(f"/api/v1/conversations/{cid}/messages?limit=2&before={latest['next_before']}", headers=headers).json()
            assert [message["content"] for message in previous["data"]] == ["메시지 1", "메시지 2"]
            assert previous["has_more"] is True
            assert previous["next_before"] == "page-1"
    finally:
        directory.cleanup()


def test_requires_bearer_key(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            assert client.get("/api/v1/conversations").status_code == 401
            assert client.get("/api/v1/conversations", headers={"Authorization": "Bearer wrong-key"}).status_code == 401
    finally:
        directory.cleanup()


def test_image_generation_capabilities_are_disabled_without_server_opt_in(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            response = client.get("/api/v1/image-generation/capabilities", headers={"Authorization": "Bearer client-key"})
            assert response.status_code == 200
            result = response.json()
            assert (result["enabled"], result["available"], result["reason"]) == (False, False, "disabled")
            assert len(result["models"]) == 7
            assert all(not model["execution_enabled"] for model in result["models"])

    finally:
        directory.cleanup()


def test_streaming_saves_only_final_content_and_uses_non_thinking_payload(monkeypatch):
    client, directory = make_client(monkeypatch)
    captured = {}

    class FakeStreamResponse:
        def raise_for_status(self): pass

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"reasoning_content":"숨겨진 사고"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"최종 "}}]}'
            yield 'data: {"choices":[{"delta":{"content":"답변"}}]}'
            yield "data: [DONE]"

        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

        async def get(self, *_args, **_kwargs): return FakeModelsResponse()

        def stream(self, _method, _url, **kwargs):
            captured.update(kwargs["json"])
            return FakeStreamResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={}).json()["id"]
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "질문", "stream": True})

            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert 'event: delta\ndata: {"content": "최종 "}' in response.text
            assert 'event: done' in response.text
            assert "숨겨진 사고" not in response.text
            assert captured["stream"] is True
            assert captured["chat_template_kwargs"] == {"enable_thinking": False}

            assert [message["content"] for message in conversation_messages(client, cid, headers)] == ["질문", "최종 답변"]
    finally:
        directory.cleanup()


def test_streaming_reasoning_only_response_is_not_saved(monkeypatch):
    client, directory = make_client(monkeypatch)

    class FakeStreamResponse:
        def raise_for_status(self): pass

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"reasoning_content":"긴 사고"}}]}'
            yield "data: [DONE]"

        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        def stream(self, *_args, **_kwargs): return FakeStreamResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={}).json()["id"]
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "질문", "stream": True})

            assert response.status_code == 200
            assert 'event: error' in response.text
            assert "긴 사고" not in response.text

            assert [message["content"] for message in conversation_messages(client, cid, headers)] == ["질문"]
    finally:
        directory.cleanup()


def test_mode_create_update_and_validation(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            created = client.post("/api/v1/conversations", headers=headers, json={
                "title": "모험",
                "mode": "roleplay",
                "response_length": "long",
                "mode_settings": {"character": "탐험가", "world": "화성"},
            })
            assert created.status_code == 201
            conversation = created.json()
            assert conversation["mode"] == "roleplay"
            assert conversation["context_policy"] == "recent"
            assert conversation["mode_settings"] == {"character": "탐험가", "world": "화성"}
            assert conversation["response_length"] == "long"

            updated = client.patch(f"/api/v1/conversations/{conversation['id']}", headers=headers, json={
                "response_length": "short",
                "mode_settings": {"character": "새 탐험가", "rules": "한국어로 말한다"},
            })
            assert updated.status_code == 200
            assert updated.json()["mode"] == "roleplay"
            assert updated.json()["response_length"] == "short"
            assert updated.json()["mode_settings"]["character"] == "새 탐험가"

            summary = client.patch(f"/api/v1/conversations/{conversation['id']}", headers=headers, json={"summary": "선장은 화성 기지에 남아 있다."})
            assert summary.status_code == 200
            assert summary.json()["summary"] == "선장은 화성 기지에 남아 있다."

            invalid_settings = client.post("/api/v1/conversations", headers=headers, json={"mode": "quick", "mode_settings": {"world": "허용 안 됨"}})
            assert invalid_settings.status_code == 422
            assert invalid_settings.json()["error"]["code"] == "invalid_mode_settings"

            mode_change = client.patch(f"/api/v1/conversations/{conversation['id']}", headers=headers, json={"mode": "quick"})
            assert mode_change.status_code == 422
            assert mode_change.json()["error"]["code"] == "validation_error"

            quick = client.post("/api/v1/conversations", headers=headers, json={"mode": "quick"}).json()
            unsupported_summary = client.patch(f"/api/v1/conversations/{quick['id']}", headers=headers, json={"summary": "허용 안 됨"})
            assert unsupported_summary.status_code == 422
            assert unsupported_summary.json()["error"]["code"] == "summary_not_supported"

            image_with_settings = client.post("/api/v1/conversations", headers=headers, json={
                "mode": "image_prompt", "mode_settings": {"style": "cinematic"},
            })
            assert image_with_settings.status_code == 422
            assert image_with_settings.json()["error"]["code"] == "image_prompt_settings_not_supported"

            image = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"})
            assert image.status_code == 201
            assert image.json()["context_policy"] == "none"
            assert image.json()["mode_settings"] == {}
            image_id = image.json()["id"]
            with connect(main.get_settings().database_path) as db:
                db.execute("UPDATE conversations SET mode_settings=? WHERE id=?", (json.dumps({"style": "legacy"}), image_id))

            renamed = client.patch(f"/api/v1/conversations/{image_id}", headers=headers, json={"title": "이미지 요청"})
            assert renamed.status_code == 200
            assert renamed.json()["mode_settings"] == {"style": "legacy"}
            empty_settings = client.patch(f"/api/v1/conversations/{image_id}", headers=headers, json={"mode_settings": {}})
            assert empty_settings.status_code == 200
            assert empty_settings.json()["mode_settings"] == {"style": "legacy"}
            rejected_settings = client.patch(f"/api/v1/conversations/{image_id}", headers=headers, json={"mode_settings": {"style": "new"}})
            assert rejected_settings.status_code == 422
            assert rejected_settings.json()["error"]["code"] == "image_prompt_settings_not_supported"
    finally:
        directory.cleanup()


def test_legacy_database_migrates_without_losing_conversation_data(tmp_path):
    database_path = str(tmp_path / "legacy.db")
    connection = sqlite3.connect(database_path)
    connection.executescript("""
        CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
    """)
    connection.execute("INSERT INTO conversations VALUES ('legacy-id', '기존 대화', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    connection.execute("INSERT INTO messages VALUES ('legacy-message', 'legacy-id', 'user', '보존되어야 함', '2026-01-01T00:00:00+00:00')")
    connection.commit()
    connection.close()

    initialize(database_path)
    with connect(database_path) as before_stateless_migration:
        before_stateless_migration.execute("""INSERT INTO conversations
            (id,title,created_at,updated_at,mode,context_policy,mode_settings,response_length,summary,summary_status)
            VALUES ('old-image', '기존 이미지', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                    'image_prompt', 'recent', '{\"style\": \"legacy\"}', 'medium', 'old summary', 'idle')""")
        before_stateless_migration.execute("""INSERT INTO conversations
            (id,title,created_at,updated_at,mode,context_policy,mode_settings,response_length,summary,summary_status)
            VALUES ('old-roleplay', '기존 역할극', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                    'roleplay', 'recent', '{\"world\": \"Mars\"}', 'medium', '', 'idle')""")
    initialize(database_path)

    with connect(database_path) as migrated:
        conversation = migrated.execute("SELECT * FROM conversations WHERE id='legacy-id'").fetchone()
        message = migrated.execute("SELECT * FROM messages WHERE id='legacy-message'").fetchone()
        old_image = migrated.execute("SELECT * FROM conversations WHERE id='old-image'").fetchone()
        old_roleplay = migrated.execute("SELECT * FROM conversations WHERE id='old-roleplay'").fetchone()
        versions = migrated.execute("SELECT version FROM schema_migrations").fetchall()

    assert conversation["title"] == "기존 대화"
    assert conversation["mode"] == "quick"
    assert conversation["context_policy"] == "recent"
    assert conversation["mode_settings"] == "{}"
    assert conversation["response_length"] == "medium"
    assert message["content"] == "보존되어야 함"
    assert conversation["summary_status"] == "idle"
    assert old_image["context_policy"] == "none"
    assert old_image["mode_settings"] == '{"style": "legacy"}'
    assert old_image["summary"] == "old summary"
    assert old_roleplay["context_policy"] == "recent"
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]


def test_image_draft_revision_validation_and_image_job_deletion_guard(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            image_conversation = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            other_conversation = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            assistant_id, original_content = add_completed_image_source(main.get_settings().database_path, image_conversation)

            created = client.put(
                f"/api/v1/conversations/{image_conversation}/messages/{assistant_id}/image-draft",
                headers=headers,
                json={"prompt": "edited prompt", "width": 512, "height": 512, "revision": 0},
            )
            assert created.status_code == 200
            assert created.json()["revision"] == 1
            assert created.json()["prompt"] == "edited prompt"
            with connect(main.get_settings().database_path) as db:
                source = db.execute("SELECT content FROM messages WHERE id=?", (assistant_id,)).fetchone()
            assert source["content"] == original_content

            stale = client.put(
                f"/api/v1/conversations/{image_conversation}/messages/{assistant_id}/image-draft",
                headers=headers,
                json={"prompt": "stale", "width": 512, "height": 512, "revision": 0},
            )
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "draft_revision_conflict"

            updated = client.put(
                f"/api/v1/conversations/{image_conversation}/messages/{assistant_id}/image-draft",
                headers=headers,
                json={"prompt": "edited again", "width": 768, "height": 768, "revision": 1},
            )
            assert updated.status_code == 200
            assert updated.json()["revision"] == 2
            fetched = client.get(f"/api/v1/conversations/{image_conversation}/messages/{assistant_id}/image-draft", headers=headers)
            assert fetched.json()["prompt"] == "edited again"

            wrong_room = client.put(
                f"/api/v1/conversations/{other_conversation}/messages/{assistant_id}/image-draft",
                headers=headers,
                json={"prompt": "wrong room", "width": 512, "height": 512, "revision": 0},
            )
            assert wrong_room.status_code == 404

            rectangle = client.put(
                f"/api/v1/conversations/{image_conversation}/messages/{assistant_id}/image-draft",
                headers=headers,
                json={"prompt": "rectangle", "width": 512, "height": 768, "revision": 2},
            )
            assert rectangle.status_code == 422
            assert rectangle.json()["error"]["code"] == "unsupported_image_resolution"
            assert client.put(
                f"/api/v1/conversations/{image_conversation}/messages/{assistant_id}/image-draft",
                headers=headers,
                json={"prompt": "   ", "width": 512, "height": 512, "revision": 2},
            ).status_code == 422
            assert client.put(
                f"/api/v1/conversations/{image_conversation}/messages/{assistant_id}/image-draft",
                headers=headers,
                json={"prompt": "x" * 8001, "width": 512, "height": 512, "revision": 2},
            ).status_code == 422

            with connect(main.get_settings().database_path) as db:
                timestamp = "2026-01-01T00:00:01+00:00"
                db.execute("""INSERT INTO image_jobs
                              (id,request_id,conversation_id,source_message_id,draft_revision,prompt,width,height,steps,cfg_scale,payload_fingerprint,client_reference,status,created_at,updated_at)
                              VALUES ('active-job','active-request',?,?,?,?,?,?,?,?,?,?,'submission_unknown',?,?)""",
                           (image_conversation, assistant_id, 2, "edited again", 768, 768, 6, 1.0, "fingerprint", "llm-client:active-job", timestamp, timestamp))
            blocked = client.delete(f"/api/v1/conversations/{image_conversation}", headers=headers)
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "active_image_jobs"
            with connect(main.get_settings().database_path) as db:
                db.execute("UPDATE image_jobs SET status='completed' WHERE id='active-job'")
            listed = client.get(f"/api/v1/conversations/{image_conversation}/image-jobs", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["data"][0]["status"] == "completed"
            assert client.delete(f"/api/v1/conversations/{image_conversation}", headers=headers).status_code == 204
    finally:
        directory.cleanup()


def test_image_draft_rejects_user_and_invalid_assistant_sources(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            conversation_id = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            bad_assistant, _ = add_completed_image_source(main.get_settings().database_path, conversation_id, "bad", "not json")
            invalid = client.put(
                f"/api/v1/conversations/{conversation_id}/messages/{bad_assistant}/image-draft",
                headers=headers,
                json={"prompt": "draft", "width": 512, "height": 512, "revision": 0},
            )
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "invalid_image_source"
            with connect(main.get_settings().database_path) as db:
                db.execute("INSERT INTO messages VALUES ('plain-user', ?, 'user', 'text', '2026-01-01T00:00:00+00:00')", (conversation_id,))
            user_source = client.put(
                f"/api/v1/conversations/{conversation_id}/messages/plain-user/image-draft",
                headers=headers,
                json={"prompt": "draft", "width": 512, "height": 512, "revision": 0},
            )
            assert user_source.status_code == 422
            assert user_source.json()["error"]["code"] == "invalid_image_source"
    finally:
        directory.cleanup()


def test_image_job_submission_replay_and_snapshot_conflict(monkeypatch):
    client, directory = make_client(monkeypatch)
    captured = []

    class FakeStudio:
        async def submit_job(self, payload):
            captured.append(payload)
            return {"upstream_job_id": "studio-job-1", "status": "queued"}

        async def find_jobs_by_client_reference(self, _reference):
            return []

    monkeypatch.setattr(main, "ImageStudioAdapter", lambda _settings: FakeStudio())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            conversation_id = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            assistant_id, _ = add_completed_image_source(main.get_settings().database_path, conversation_id, "submit")
            assert client.put(
                f"/api/v1/conversations/{conversation_id}/messages/{assistant_id}/image-draft", headers=headers,
                json={"prompt": "edited for Studio", "width": 768, "height": 768, "revision": 0},
            ).status_code == 200
            request_id = "11111111-1111-4111-8111-111111111111"
            body = {
                "source_message_id": assistant_id, "prompt": "edited for Studio", "width": 768, "height": 768,
                "draft_revision": 1, "request_id": request_id,
            }
            created = client.post(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers, json=body)
            assert created.status_code == 202
            assert created.json()["replayed"] is False
            assert created.json()["data"]["status"] == "queued"
            assert created.json()["data"]["upstream_job_id"] == "studio-job-1"
            assert captured == [{
                "job_type": "image.generate", "source": "nsfw", "model_id": "z-image-nsfw-v2",
                "prompt": "edited for Studio", "width": 768, "height": 768, "steps": 6,
                "cfg_scale": 1.0, "seed": -1, "priority": 0, "batch_count": 1,
                "preset": "balanced", "client_reference": f"llm-client:{request_id}",
            }]
            # Simulate a job saved before preset labels existed.
            from backend.app.image_jobs import job_snapshot, snapshot_fingerprint
            with connect(main.get_settings().database_path) as db:
                snapshot = job_snapshot(db, conversation_id, assistant_id, body['prompt'], 768, 768, 1)
                old_snapshot = {key: value for key, value in snapshot.items() if key != 'preset_name'}
                db.execute('UPDATE image_jobs SET payload_fingerprint=? WHERE id=?', (snapshot_fingerprint(old_snapshot), request_id))
            replayed = client.post(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers, json=body)
            assert replayed.status_code == 202
            assert replayed.json()["replayed"] is True
            assert len(captured) == 1

            assert client.put(
                f"/api/v1/conversations/{conversation_id}/messages/{assistant_id}/image-draft", headers=headers,
                json={"prompt": "different snapshot", "width": 512, "height": 512, "revision": 1},
            ).status_code == 200
            conflict = client.post(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers, json={
                **body, "prompt": "different snapshot", "width": 512, "height": 512, "draft_revision": 2,
            })
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "image_job_idempotency_conflict"
    finally:
        directory.cleanup()


def test_image_job_submission_unknown_is_not_resent_and_can_be_matched(monkeypatch):
    client, directory = make_client(monkeypatch)
    submitted = []
    matches = []

    class FakeStudio:
        async def submit_job(self, payload):
            submitted.append(payload)
            raise main.ImageGenerationSubmissionUnknown("submission_unknown")

        async def find_jobs_by_client_reference(self, _reference):
            return matches

    monkeypatch.setattr(main, "ImageStudioAdapter", lambda _settings: FakeStudio())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            conversation_id = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            assistant_id, _ = add_completed_image_source(main.get_settings().database_path, conversation_id, "unknown")
            client.put(
                f"/api/v1/conversations/{conversation_id}/messages/{assistant_id}/image-draft", headers=headers,
                json={"prompt": "unknown submission", "width": 512, "height": 512, "revision": 0},
            )
            body = {
                "source_message_id": assistant_id, "prompt": "unknown submission", "width": 512, "height": 512,
                "draft_revision": 1, "request_id": "22222222-2222-4222-8222-222222222222",
            }
            unknown = client.post(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers, json=body)
            assert unknown.status_code == 202
            assert unknown.json()["data"]["status"] == "submission_unknown"
            assert len(submitted) == 1

            replayed = client.post(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers, json=body)
            assert replayed.json()["data"]["status"] == "submission_unknown"
            assert len(submitted) == 1

            matches.append({"upstream_job_id": "matched-studio-job", "status": "queued"})
            recovered = client.post(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers, json=body)
            assert recovered.json()["data"]["status"] == "queued"
            assert recovered.json()["data"]["upstream_job_id"] == "matched-studio-job"
            assert len(submitted) == 1
    finally:
        directory.cleanup()


def test_image_job_rejection_marks_local_job_failed(monkeypatch):
    client, directory = make_client(monkeypatch)

    class FakeStudio:
        async def submit_job(self, _payload):
            raise main.ImageGenerationRejected("imagegen_rejected")

    monkeypatch.setattr(main, "ImageStudioAdapter", lambda _settings: FakeStudio())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            conversation_id = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            assistant_id, _ = add_completed_image_source(main.get_settings().database_path, conversation_id, "rejected")
            client.put(
                f"/api/v1/conversations/{conversation_id}/messages/{assistant_id}/image-draft", headers=headers,
                json={"prompt": "rejected", "width": 512, "height": 512, "revision": 0},
            )
            rejected = client.post(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers, json={
                "source_message_id": assistant_id, "prompt": "rejected", "width": 512, "height": 512,
                "draft_revision": 1, "request_id": "33333333-3333-4333-8333-333333333333",
            })
            assert rejected.status_code == 422
            jobs = client.get(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers).json()["data"]
            assert jobs[0]["status"] == "failed"
            assert jobs[0]["error"]["code"] == "image_generation_rejected"
    finally:
        directory.cleanup()


def test_startup_marks_incomplete_image_submission_unknown(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            conversation_id = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            assistant_id, _ = add_completed_image_source(main.get_settings().database_path, conversation_id, "restart")
            timestamp = "2026-01-01T00:00:00+00:00"
            with connect(main.get_settings().database_path) as db:
                db.execute("""INSERT INTO image_jobs
                              (id,request_id,conversation_id,source_message_id,draft_revision,prompt,width,height,steps,cfg_scale,payload_fingerprint,client_reference,status,created_at,updated_at)
                              VALUES ('restart-job','restart-request',?,?,?,?,?,?,?,?,?,?,'submission_pending',?,?)""",
                           (conversation_id, assistant_id, 1, "pending submission", 512, 512, 6, 1.0,
                            "restart-fingerprint", "llm-client:restart-job", timestamp, timestamp))

        with TestClient(main.app) as restarted:
            jobs = restarted.get(f"/api/v1/conversations/{conversation_id}/image-jobs", headers={"Authorization": "Bearer client-key"}).json()["data"]
            assert jobs[0]["status"] == "submission_unknown"
            assert jobs[0]["error"]["code"] == "submission_unknown"
    finally:
        directory.cleanup()


def test_image_job_status_cancel_and_owned_asset_proxy(monkeypatch):
    client, directory = make_client(monkeypatch)
    calls = {"get": [], "cancel": [], "asset": []}

    class FakeStudio:
        async def get_job(self, upstream_job_id):
            calls["get"].append(upstream_job_id)
            if upstream_job_id == "missing-upstream":
                raise main.ImageGenerationNotFound("image_job_not_found")
            if upstream_job_id == "unavailable-upstream":
                raise main.ImageGenerationUnavailable("imagegen_unavailable")
            return {"upstream_job_id": upstream_job_id, "status": "completed", "asset_ids": ["asset-1"], "metadata": {"seed": 4242, "duration_ms": 2500, "lora": {"enabled": True, "strength": 0.7}}}

        async def cancel_job(self, upstream_job_id):
            calls["cancel"].append(upstream_job_id)
            return {"upstream_job_id": upstream_job_id, "status": "running", "asset_ids": []}

        async def fetch_asset(self, asset_id, max_bytes):
            calls["asset"].append((asset_id, max_bytes))
            return "image/png", b"png-data"

    monkeypatch.setattr(main, "ImageStudioAdapter", lambda _settings: FakeStudio())
    monkeypatch.setattr(main.get_settings(), "image_job_poll_min_interval_seconds", 0)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            conversation_id = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            assistant_id, _ = add_completed_image_source(main.get_settings().database_path, conversation_id, "status")
            timestamp = "2026-01-01T00:00:00+00:00"
            with connect(main.get_settings().database_path) as db:
                for job_id, upstream, job_status in (
                    ("sync-job", "studio-sync", "queued"),
                    ("cancel-job", "studio-cancel", "running"),
                    ("unknown-job", None, "submission_unknown"),
                    ("missing-job", "missing-upstream", "queued"),
                    ("unavailable-job", "unavailable-upstream", "queued"),
                ):
                    db.execute("""INSERT INTO image_jobs
                                  (id,request_id,conversation_id,source_message_id,draft_revision,prompt,width,height,steps,cfg_scale,payload_fingerprint,client_reference,upstream_job_id,status,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (job_id, f"request-{job_id}", conversation_id, assistant_id, 1, "prompt", 512, 512, 6,
                                1.0, f"fingerprint-{job_id}", f"llm-client:{job_id}", upstream, job_status, timestamp, timestamp))

            refreshed = client.get("/api/v1/image-jobs/sync-job", headers=headers)
            assert refreshed.status_code == 200
            assert refreshed.json()["refreshed"] is True
            assert refreshed.json()["data"]["status"] == "completed"
            assert refreshed.json()["data"]["asset_ids"] == ["asset-1"]
            assert refreshed.json()["data"]["studio_metadata"]["seed"] == 4242
            records = client.get(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers).json()['data']
            assert next(item for item in records if item['id'] == 'sync-job')['studio_metadata']['duration_ms'] == 2500

            denied_asset = client.get("/api/v1/image-jobs/sync-job/assets/not-owned/file", headers=headers)
            assert denied_asset.status_code == 404
            assert calls["asset"] == []
            image = client.get("/api/v1/image-jobs/sync-job/assets/asset-1/file", headers=headers)
            assert image.status_code == 200
            assert image.content == b"png-data"
            assert image.headers["content-type"] == "image/png"
            assert image.headers["x-content-type-options"] == "nosniff"
            assert calls["asset"] == [("asset-1", main.get_settings().max_image_asset_bytes)]

            cancelled = client.post("/api/v1/image-jobs/cancel-job/cancel", headers=headers)
            assert cancelled.status_code == 202
            assert cancelled.json()["data"]["status"] == "running"
            assert cancelled.json()["data"]["cancel_requested"] is True
            assert calls["cancel"] == ["studio-cancel"]
            unknown_cancel = client.post("/api/v1/image-jobs/unknown-job/cancel", headers=headers)
            assert unknown_cancel.status_code == 409
            assert unknown_cancel.json()["error"]["code"] == "image_job_cancel_unavailable"

            missing = client.get("/api/v1/image-jobs/missing-job", headers=headers)
            assert missing.status_code == 200
            assert missing.json()["data"]["status"] == "failed"
            assert missing.json()["data"]["error"]["code"] == "upstream_job_not_found"
            unavailable = client.get("/api/v1/image-jobs/unavailable-job", headers=headers)
            assert unavailable.status_code == 503
            assert unavailable.json()["error"]["code"] == "image_generation_unavailable"
            assert main.load_image_job(main.get_settings(), "unavailable-job")["status"] == "queued"
    finally:
        directory.cleanup()


def test_image_job_evaluation_validates_and_persists(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            mid, _ = add_completed_image_source(main.get_settings().database_path, cid, 'evaluate')
            timestamp = '2026-01-01T00:00:00+00:00'
            with connect(main.get_settings().database_path) as db:
                for job_id, status, assets in [('reviewable', 'completed', '["asset"]'), ('unfinished', 'running', '[]'), ('empty', 'completed', '[]')]:
                    db.execute('''INSERT INTO image_jobs (id,request_id,conversation_id,source_message_id,draft_revision,prompt,width,height,steps,cfg_scale,payload_fingerprint,client_reference,status,asset_ids_json,created_at,updated_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                               (job_id, 'request-' + job_id, cid, mid, 1, 'test', 512, 512, 6, 1.0, 'fp-' + job_id,
                                'ref-' + job_id, status, assets, timestamp, timestamp))
            path = '/api/v1/image-jobs/reviewable/evaluation'
            assert client.put(path, json={'score': 4, 'note': 'test'}).status_code == 401
            assert client.put('/api/v1/image-jobs/missing/evaluation', headers=headers, json={'score': 4, 'note': ''}).status_code == 404
            for job_id in ('unfinished', 'empty'):
                assert client.put('/api/v1/image-jobs/' + job_id + '/evaluation', headers=headers, json={'score': 4, 'note': ''}).status_code == 409
            for value in (0, 6, True):
                assert client.put(path, headers=headers, json={'score': value, 'note': ''}).status_code == 422
            assert client.put(path, headers=headers, json={'score': 4, 'note': 'x' * 1001}).status_code == 422
            saved = client.put(path, headers=headers, json={'score': 4, 'note': '  조명이 좋음  '})
            assert saved.status_code == 200
            assert saved.json()['data']['evaluation']['score'] == 4
            assert saved.json()['data']['evaluation']['note'] == '조명이 좋음'
            with connect(main.get_settings().database_path) as db:
                db.execute('UPDATE image_jobs SET generation_config_json=?,studio_metadata_json=?,preset_name=? WHERE id=?', (
                    json.dumps({'model_id': 'z-image-nsfw-v2', 'seed': 42, 'negative_prompt': 'cars'}),
                    json.dumps({'seed': 42, 'duration_ms': 1250, 'quantization': 'Q8_0', 'lora': {'enabled': False}}),
                    'balanced', 'reviewable'))
            export_path = '/api/v1/image-jobs/reviewable/comparison-record'
            assert client.get(export_path).status_code == 401
            assert client.get('/api/v1/image-jobs/missing/comparison-record', headers=headers).status_code == 404
            record = client.get(export_path, headers=headers).json()
            assert record['original_request'] == 'original request'
            assert record['generated_prompt'] == 'original prompt' and record['submitted_prompt'] == 'test'
            assert record['submitted_negative_prompt'] == 'cars'
            assert record['model'] == {'model_id': 'z-image-nsfw-v2', 'quantization': 'Q8_0', 'version': None}
            assert record['preset_name'] == 'balanced' and record['runtime']['seed'] == 42
            assert record['runtime']['duration_ms'] == 1250 and record['evaluation']['score'] == 4
            assert record['status'] == 'completed' and record['asset_ids'] == ['asset']
            initialize(main.get_settings().database_path)
            records = client.get(f'/api/v1/conversations/{cid}/image-jobs', headers=headers).json()['data']
            assert next(job for job in records if job['id'] == 'reviewable')['evaluation']['score'] == 4
            cleared = client.put(path, headers=headers, json={'score': None, 'note': ''})
            assert cleared.json()['data']['evaluation'] == {'score': None, 'note': '', 'updated_at': None}
            with connect(main.get_settings().database_path) as db:
                db.execute("UPDATE messages SET content='legacy text' WHERE id=?", (mid,))
                db.execute("UPDATE image_jobs SET status='failed',generation_config_json='{}',studio_metadata_json='{}',preset_name=NULL WHERE id='reviewable'")
            legacy = client.get(export_path, headers=headers).json()
            assert legacy['status'] == 'failed' and legacy['generated_prompt'] is None
            assert legacy['model']['version'] is None and legacy['runtime']['seed'] is None
    finally:
        directory.cleanup()


def test_fake_studio_edit_submit_complete_and_asset_flow(monkeypatch):
    """End-to-end contract test: no real Studio, gate, or GPU is contacted."""
    client, directory = make_client(monkeypatch)
    submitted = []

    class FakeStudio:
        async def submit_job(self, payload):
            submitted.append(payload)
            return {"upstream_job_id": "fake-upstream-job", "status": "queued"}

        async def get_job(self, upstream_job_id):
            assert upstream_job_id == "fake-upstream-job"
            return {"upstream_job_id": upstream_job_id, "status": "completed", "asset_ids": ["fake-asset"]}

        async def fetch_asset(self, asset_id, max_bytes):
            assert asset_id == "fake-asset"
            assert max_bytes == main.get_settings().max_image_asset_bytes
            return "image/png", b"fake-png"

    monkeypatch.setattr(main, "ImageStudioAdapter", lambda _settings: FakeStudio())
    monkeypatch.setattr(main.get_settings(), "image_job_poll_min_interval_seconds", 0)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            conversation_id = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            assistant_id, original = add_completed_image_source(main.get_settings().database_path, conversation_id, "full-flow")
            draft = client.put(
                f"/api/v1/conversations/{conversation_id}/messages/{assistant_id}/image-draft", headers=headers,
                json={"prompt": "edited prompt for fake Studio", "width": 512, "height": 512, "revision": 0},
            ).json()
            created = client.post(f"/api/v1/conversations/{conversation_id}/image-jobs", headers=headers, json={
                "source_message_id": assistant_id, "prompt": draft["prompt"], "width": draft["width"], "height": draft["height"],
                "draft_revision": draft["revision"], "request_id": "99999999-9999-4999-8999-999999999999",
            })
            assert created.status_code == 202
            job_id = created.json()["data"]["id"]
            assert submitted[0]["prompt"] == draft["prompt"]
            assert submitted[0]["client_reference"] == f"llm-client:{job_id}"

            completed = client.get(f"/api/v1/image-jobs/{job_id}", headers=headers)
            assert completed.status_code == 200
            assert completed.json()["data"]["status"] == "completed"
            assert completed.json()["data"]["asset_ids"] == ["fake-asset"]
            asset = client.get(f"/api/v1/image-jobs/{job_id}/assets/fake-asset/file", headers=headers)
            assert asset.status_code == 200
            assert asset.headers["content-type"] == "image/png"
            assert asset.content == b"fake-png"
            with connect(main.get_settings().database_path) as db:
                assert db.execute("SELECT content FROM messages WHERE id=?", (assistant_id,)).fetchone()["content"] == original
    finally:
        directory.cleanup()


def test_roleplay_summary_job_only_covers_completed_history(monkeypatch):
    client, directory = make_client(monkeypatch)
    monkeypatch.setattr(main.get_settings(), "roleplay_summary_trigger_tokens", 1)
    monkeypatch.setattr(main.get_settings(), "roleplay_summary_keep_recent_turns", 1)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={"mode": "roleplay"}).json()["id"]
            with connect(main.get_settings().database_path) as db:
                for number in range(2):
                    user_id, assistant_id = f"summary-u-{number}", f"summary-a-{number}"
                    timestamp = f"2026-01-01T00:00:0{number * 2}+00:00"
                    db.execute("INSERT INTO messages VALUES (?, ?, 'user', ?, ?)", (user_id, cid, f"질문 {number}", timestamp))
                    db.execute("INSERT INTO messages VALUES (?, ?, 'assistant', ?, ?)", (assistant_id, cid, f"답변 {number}", f"2026-01-01T00:00:0{number * 2 + 1}+00:00"))
                    db.execute("""INSERT INTO generation_requests
                                  (id,conversation_id,user_message_id,assistant_message_id,status,queued_at,created_at,updated_at)
                                  VALUES (?, ?, ?, ?, 'completed', ?, ?, ?)""",
                               (f"summary-g-{number}", cid, user_id, assistant_id, timestamp, timestamp, timestamp))

            job_id = main.queue_roleplay_summary(main.get_settings(), cid)
            assert job_id is not None
            with connect(main.get_settings().database_path) as db:
                job = db.execute("SELECT * FROM summary_jobs WHERE id=?", (job_id,)).fetchone()
                conversation = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            assert job["through_message_id"] == "summary-a-0"
            assert conversation["summary_status"] == "pending"
    finally:
        directory.cleanup()


def test_failed_request_retries_without_duplicate_user_message(monkeypatch):
    client, directory = make_client(monkeypatch)
    calls = {"count": 0}

    class FakeResponse:
        def __init__(self, content): self.content = content
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": self.content}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **_kwargs):
            calls["count"] += 1
            return FakeResponse("" if calls["count"] == 1 else "재시도 답변")

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={}).json()["id"]
            request_id = "5e7da217-1e17-4c6f-8042-468cbb88decb"
            payload = {"content": "재시도 질문", "request_id": request_id}

            failed = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json=payload)
            assert failed.status_code == 422
            status = client.get(f"/api/v1/conversations/{cid}/generations/{request_id}", headers=headers)
            assert status.json()["status"] == "failed"

            succeeded = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json=payload)
            assert succeeded.status_code == 200
            assert succeeded.json()["generation"]["status"] == "completed"
            assert [message["content"] for message in conversation_messages(client, cid, headers)] == ["재시도 질문", "재시도 답변"]
            assert calls["count"] == 2

            replayed = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json=payload)
            assert replayed.status_code == 200
            assert replayed.json()["replayed"] is True
            assert calls["count"] == 2
    finally:
        directory.cleanup()


def test_pending_generation_blocks_new_request_and_can_be_cancelled(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={}).json()["id"]
            generation_id = "a0a573fd-c927-4c5a-a80a-adbdf6bfe6f0"
            with connect(main.get_settings().database_path) as db:
                db.execute("INSERT INTO messages VALUES ('pending-user', ?, 'user', '진행 중 질문', '2026-01-01T00:00:00+00:00')", (cid,))
                db.execute("""INSERT INTO generation_requests
                              (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at)
                              VALUES (?, ?, 'pending-user', 'pending', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')""", (generation_id, cid))

            blocked = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "새 질문"})
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "conversation_generation_in_progress"

            cancelled = client.delete(f"/api/v1/conversations/{cid}/generations/{generation_id}", headers=headers)
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["error"]["code"] == "cancelled"
    finally:
        directory.cleanup()


def test_hides_reasoning_and_saves_final_content(monkeypatch):
    client, directory = make_client(monkeypatch)
    captured = {}
    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "최종 답변", "reasoning_content": "비공개 사고"}}]}
    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *args, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={}).json()["id"]
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "질문"})
            assert response.status_code == 200
            assert response.json()["assistant_message"]["content"] == "최종 답변"
            assert captured["chat_template_kwargs"] == {"enable_thinking": False}
            assert "비공개 사고" not in str(conversation_messages(client, cid, headers))
    finally:
        directory.cleanup()


def test_server_resolves_gate_model_instead_of_client_model(monkeypatch):
    client, directory = make_client(monkeypatch)
    captured = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "서버 모델 답변"}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={}).json()["id"]
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "질문"})

            assert response.status_code == 200
            assert captured["model"] == "gate-selected-model"
            assert [message["role"] for message in captured["messages"]] == ["system", "user"]
            assert captured["messages"][-1]["content"] == "질문"

            rejected = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "질문", "model": "untrusted-client-model"})
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "validation_error"
    finally:
        directory.cleanup()


def test_context_history_never_starts_with_an_orphaned_assistant(monkeypatch):
    client, directory = make_client(monkeypatch)
    captured = []

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "응답"}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            captured.append(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    monkeypatch.setattr(main.get_settings(), "max_history_messages", 1)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={"mode": "roleplay"}).json()["id"]
            assert client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "첫 질문"}).status_code == 200
            assert client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "둘째 질문"}).status_code == 200

            assert [message["content"] for message in captured[-1]["messages"]] == [
                "당신은 롤플레잉을 진행합니다. 설정을 일관되게 지키고 이야기 속 역할로 답하세요.",
                "둘째 질문",
            ]
    finally:
        directory.cleanup()


def test_generation_reports_length_finish_reason_usage_and_selected_output_limit(monkeypatch):
    client, directory = make_client(monkeypatch)
    captured = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {
                "choices": [{"finish_reason": "length", "message": {"content": "잘린 답변"}}],
                "usage": {"prompt_tokens": 21, "completion_tokens": 512, "total_tokens": 533},
            }

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={"response_length": "short"}).json()["id"]
            request_id = "4e40843e-d0d5-4b5b-9709-194975b0bcbd"
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "길게 답해", "request_id": request_id})

            assert response.status_code == 200
            assert captured["max_tokens"] == 512
            generation = response.json()["generation"]
            assert generation["status"] == "completed"
            assert generation["model_id"] == "gate-selected-model"
            assert generation["requested_max_tokens"] == 512
            assert generation["finish_reason"] == "length"
            assert generation["usage"]["completion_tokens"] == 512
            assert generation["queue_duration_ms"] is not None
            assert generation["generation_duration_ms"] is not None
    finally:
        directory.cleanup()


def test_empty_length_limited_response_has_specific_error_and_failed_generation(monkeypatch):
    client, directory = make_client(monkeypatch)

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **_kwargs): return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={}).json()["id"]
            request_id = "fbb4115d-d2b8-466f-80f9-5e09c3d0cc61"
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "답변", "request_id": request_id})

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "output_length_exceeded"
            generation = client.get(f"/api/v1/conversations/{cid}/generations/{request_id}", headers=headers).json()
            assert generation["status"] == "failed"
            assert generation["finish_reason"] == "length"
            assert generation["error"]["code"] == "output_length_exceeded"
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
def test_image_prompt_uses_non_streaming_strict_json_payload_and_saves_normalized_result(monkeypatch):
    client, directory = make_client(monkeypatch)
    captured = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                    "model_id": "z-image-nsfw-v2",
                    "prompt": "  adult Korean woman in a black silk dress, cinematic hotel lounge at night  ",
                    "width": 768,
                    "height": 768,
                    "steps": 6,
                    "cfg_scale": 1,
                    "recommendation_reason": "자연스러운 장면 묘사에 적합합니다.",
                }), "reasoning_content": "do not expose"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 40, "total_tokens": 52},
            }

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "검은 드레스 호텔 사진"})

            assert response.status_code == 200
            assert captured["model"] == "gate-selected-model"
            assert captured["stream"] is False
            assert captured["temperature"] == 0.5
            assert captured["max_tokens"] == 512
            assert captured["chat_template_kwargs"] == {"enable_thinking": False}
            assert captured["response_format"]["json_schema"]["name"] == "image_prompt_text"
            assert set(captured["response_format"]["json_schema"]["schema"]["properties"]) == {"model_id", "prompt", "negative_prompt", "recommendation_reason"}
            assert captured["response_format"]["json_schema"]["strict"] is True
            assistant = response.json()["assistant_message"]
            assistant_data = json.loads(assistant["content"])
            assert assistant_data.pop("structured_intent")["intent"] == "image_generation"
            assert assistant_data == {
                "model_id": "z-image-nsfw-v2",
                "prompt": "adult Korean woman in a black silk dress, cinematic hotel lounge at night",
                "width": 768,
                "height": 768,
                "steps": 6,
                "cfg_scale": 1.0,
                "recommendation_reason": "자연스러운 장면 묘사에 적합합니다.",
                "prompt_mode": "ai_optimized",
                "model_selection": "recommend",
                "preset_name": "balanced",
            }
            assert "do not expose" not in assistant["content"]
    finally:
        directory.cleanup()


def test_direct_translation_mode_survives_retry_and_is_recorded_in_job(monkeypatch):
    client, directory = make_client(monkeypatch)
    payloads = []
    submitted = []

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                'model_id': 'z-image-nsfw-v2', 'prompt': 'A red umbrella in Seoul', 'recommendation_reason': '장면 묘사에 적합합니다.'
            })}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            payloads.append(kwargs['json'])
            return FakeResponse()

    class FakeStudio:
        async def submit_job(self, payload):
            submitted.append(payload)
            return {'upstream_job_id': 'translation-job', 'status': 'queued'}

    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **_kwargs: FakeHTTP())
    monkeypatch.setattr(main, 'ImageStudioAdapter', lambda _settings: FakeStudio())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            request_id = '44444444-4444-4444-8444-444444444444'
            body = {'content': '서울의 빨간 우산', 'prompt_mode': 'direct_translation', 'request_id': request_id}
            response = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json=body)
            assert response.status_code == 200, response.text
            result = json.loads(response.json()['assistant_message']['content'])
            assert result['prompt_mode'] == 'direct_translation'
            assert 'do not embellish' in payloads[0]['messages'][0]['content']
            conflict = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json={**body, 'prompt_mode': 'ai_optimized'})
            assert conflict.status_code == 409
            mid = response.json()['assistant_message']['id']
            path = f'/api/v1/conversations/{cid}/messages/{mid}/image-draft'
            draft = client.put(path, headers=headers, json={'prompt': result['prompt'], 'width': result['width'], 'height': result['height'], 'revision': 0})
            assert draft.status_code == 200, draft.text
            job = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json={
                'source_message_id': mid, 'prompt': result['prompt'], 'width': result['width'], 'height': result['height'],
                'draft_revision': 1, 'request_id': '55555555-5555-4555-8555-555555555555'})
            assert job.status_code == 202, job.text
            assert job.json()['data']['prompt_mode'] == 'direct_translation'
            assert 'prompt_mode' not in submitted[0]
    finally:
        directory.cleanup()


def test_original_prompt_needs_model_and_skips_llm_but_can_submit(monkeypatch):
    client, directory = make_client(monkeypatch)
    submitted = []

    async def no_llm(_settings):
        raise AssertionError('original prompt must not contact the LLM gate')

    class FakeStudio:
        async def submit_job(self, payload):
            submitted.append(payload)
            return {'upstream_job_id': 'original-job', 'status': 'queued'}

    monkeypatch.setattr(main, 'resolve_gate_model', no_llm)
    monkeypatch.setattr(main, 'ImageStudioAdapter', lambda _settings: FakeStudio())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            body = {'content': '  한글 원문, Text: OPEN  ', 'prompt_mode': 'original',
                    'request_id': '99999999-9999-4999-8999-999999999999'}
            missing = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json=body)
            assert missing.status_code == 422 and missing.json()['error']['code'] == 'image_model_required'
            body['image_model_id'] = 'klein-4b-q4'
            response = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json=body)
            assert response.status_code == 200, response.text
            result = json.loads(response.json()['assistant_message']['content'])
            assert result['prompt'] == '한글 원문, Text: OPEN'
            assert result['prompt_mode'] == 'original'
            assert result['steps'] == 4
            replay = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json=body)
            assert replay.status_code == 200 and replay.json()['replayed'] is True
            mid = response.json()['assistant_message']['id']
            draft = client.put(f'/api/v1/conversations/{cid}/messages/{mid}/image-draft', headers=headers,
                               json={'prompt': result['prompt'], 'width': result['width'], 'height': result['height'], 'revision': 0})
            assert draft.status_code == 200, draft.text
            job = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json={
                'source_message_id': mid, 'prompt': result['prompt'], 'width': result['width'], 'height': result['height'],
                'draft_revision': 1, 'request_id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'})
            assert job.status_code == 202, job.text
            assert job.json()['data']['prompt_mode'] == 'original'
            assert submitted[0]['prompt'] == '한글 원문, Text: OPEN'
    finally:
        directory.cleanup()


def test_cpu_test_engine_requires_opt_in_and_uses_existing_job_flow(monkeypatch):
    from copy import deepcopy
    from backend.app.image_prompt import IMAGE_MODELS
    client, directory = make_client(monkeypatch)
    settings = main.get_settings()
    monkeypatch.setattr(settings, 'imagegen_enabled', True)
    monkeypatch.setattr(settings, 'imagegen_allow_fake', True)
    fake = deepcopy(IMAGE_MODELS['realistic-vision-v6'])
    fake.update(model_id='fake', family='fake', execution_enabled=True, contract_matches=True, simulated=True,
                supported_resolutions=[[64, 64]], presets={name: {'width': 64, 'height': 64, 'steps': 2, 'cfg_scale': 1.0} for name in ('fast', 'balanced', 'quality')})
    calls = []

    class FakeStudio:
        async def capabilities(self):
            return {'available': True, 'simulated': True, 'models': [fake]}
        async def submit_job(self, payload):
            calls.append(payload)
            return {'upstream_job_id': 'fake-upstream', 'status': 'queued'}
        async def get_job(self, upstream_job_id):
            return {'upstream_job_id': upstream_job_id, 'status': 'completed', 'asset_ids': ['fake-asset'], 'metadata': {'seed': 42}}
        async def fetch_asset(self, asset_id, _max_bytes):
            assert asset_id == 'fake-asset'
            return 'image/png', b'cpu-test-png'

    async def no_llm(_settings): raise AssertionError('fake test should not call LLM')
    monkeypatch.setattr(main, 'resolve_gate_model', no_llm)
    monkeypatch.setattr(main, 'ImageStudioAdapter', lambda _settings: FakeStudio())
    monkeypatch.setattr(settings, 'image_job_poll_min_interval_seconds', 0)
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            path = f'/api/v1/conversations/{cid}/messages'
            result = client.post(path, headers=headers, json={'content': 'test', 'prompt_mode': 'original', 'image_model_id': 'fake'})
            assert result.status_code == 200, result.text
            config = json.loads(result.json()['assistant_message']['content'])
            assert (config['model_id'], config['width'], config['height'], config['steps']) == ('fake', 64, 64, 2)
            other_mid, _ = add_completed_image_source(settings.database_path, cid, 'not-fake')
            other_draft = client.put(f'/api/v1/conversations/{cid}/messages/{other_mid}/image-draft', headers=headers,
                                     json={'prompt': 'test', 'width': 64, 'height': 64, 'revision': 0,
                                           'generation_config': {'model_id': 'fake', 'prompt': 'test', 'width': 64, 'height': 64, 'steps': 2, 'cfg_scale': 1.0}})
            assert other_draft.status_code == 422 and other_draft.json()['error']['code'] == 'invalid_image_model'
            mid = result.json()['assistant_message']['id']
            draft = client.put(f'/api/v1/conversations/{cid}/messages/{mid}/image-draft', headers=headers,
                               json={'prompt': 'test', 'width': 64, 'height': 64, 'revision': 0, 'preset_name': 'balanced'})
            assert draft.status_code == 200, draft.text
            body = {'source_message_id': mid, 'prompt': 'test', 'width': 64, 'height': 64, 'draft_revision': 1,
                    'request_id': 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'}
            created = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json=body)
            assert created.status_code == 202, created.text
            assert calls[0]['model_id'] == 'fake' and calls[0]['steps'] == 2
            assert client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json=body).json()['replayed'] is True
            assert len(calls) == 1
            job_id = created.json()['data']['id']
            assert client.get(f'/api/v1/image-jobs/{job_id}', headers=headers).json()['data']['status'] == 'completed'
            assert client.get(f'/api/v1/image-jobs/{job_id}/assets/fake-asset/file', headers=headers).content == b'cpu-test-png'
            assert client.get(f'/api/v1/image-jobs/{job_id}/comparison-record', headers=headers).json()['simulated'] is True
            monkeypatch.setattr(settings, 'imagegen_allow_fake', False)
            assert client.post(path, headers=headers, json={'content': 'test', 'prompt_mode': 'original', 'image_model_id': 'fake'}).status_code == 422
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
def test_manual_model_selection_constrains_llm_and_idempotency(monkeypatch):
    client, directory = make_client(monkeypatch)
    payloads = []

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                'model_id': 'klein-4b-q4', 'prompt': 'A red umbrella', 'recommendation_reason': '간결한 장면 묘사'
            })}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            payloads.append(kwargs['json'])
            return FakeResponse()

    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **_kwargs: FakeHTTP())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            body = {'content': '빨간 우산', 'request_id': '66666666-6666-4666-8666-666666666666', 'image_model_id': 'klein-4b-q4'}
            response = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json=body)
            assert response.status_code == 200, response.text
            assert json.loads(response.json()['assistant_message']['content'])['model_id'] == 'klein-4b-q4'
            schema = payloads[0]['response_format']['json_schema']['schema']
            assert schema['properties']['model_id']['enum'] == ['klein-4b-q4']
            assert 'realistic-vision-v6' not in payloads[0]['messages'][0]['content']
            assert client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json={**body, 'image_model_id': 'klein-4b-q8'}).status_code == 409
            assert client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json={**body, 'image_model_id': 'fake'}).status_code == 422
    finally:
        directory.cleanup()


def test_manual_unavailable_model_stops_before_llm_request(monkeypatch):
    client, directory = make_client(monkeypatch)
    monkeypatch.setattr(main.get_settings(), 'imagegen_enabled', True)

    class FakeStudio:
        async def capabilities(self):
            return {'available': True, 'models': [{'model_id': 'klein-4b-q8', 'execution_enabled': True}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **_kwargs):
            raise AssertionError('LLM should not be called for an unavailable model')

    monkeypatch.setattr(main, 'ImageStudioAdapter', lambda _settings: FakeStudio())
    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **_kwargs: FakeHTTP())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            result = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json={
                'content': '빨간 우산', 'image_model_id': 'klein-4b-q4'})
            assert result.status_code == 422
            assert result.json()['error']['code'] == 'image_model_unavailable'
    finally:
        directory.cleanup()


@pytest.mark.parametrize('valid_second', [True, False])
def test_image_intent_api_retries_invalid_response_once(monkeypatch, valid_second):
    client, directory = make_client(monkeypatch)
    calls = []
    valid = {'intent': 'image_generation', 'subject': 'two adults', 'environment': 'Seoul', 'style': None,
             'lighting': None, 'composition': None, 'required_elements': ['red umbrella'],
             'forbidden_elements': ['cars'], 'exact_text': ['OPEN'], 'preferred_model': None, 'aspect_ratio': None}

    class FakeResponse:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'finish_reason': 'stop', 'message': {'content': self.text}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            calls.append(kwargs['json']['messages'].copy())
            return FakeResponse(json.dumps(valid) if len(calls) == 2 and valid_second else '{bad')

    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **_kwargs: FakeHTTP())
    try:
        with client:
            response = client.post('/api/v1/image-assistant/analyze', headers={'Authorization': 'Bearer client-key'},
                                   json={'content': '서울의 성인 두 명, 빨간 우산, 자동차 제외, 간판에 OPEN'})
            assert len(calls) == 2
            assert len(calls[1]) == len(calls[0]) + 1
            if valid_second:
                assert response.status_code == 200
                assert response.json()['intent']['exact_text'] == ['OPEN']
            else:
                assert response.status_code == 502
                assert response.json()['error']['code'] == 'image_intent_invalid_response'
    finally:
        directory.cleanup()


@pytest.mark.parametrize('prompt', ['A red umbrella beside an OPEN sign', 'A red umbrella'])
def test_optimized_prompt_uses_intent_and_preserves_exact_text(monkeypatch, prompt):
    client, directory = make_client(monkeypatch)
    calls = []
    intent = {'intent': 'image_generation', 'subject': 'red umbrella', 'environment': None, 'style': None,
              'lighting': None, 'composition': None, 'required_elements': ['red umbrella'],
              'forbidden_elements': [], 'exact_text': ['OPEN'], 'preferred_model': None, 'aspect_ratio': None}

    class FakeResponse:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.value)}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            payload = kwargs['json']
            calls.append(payload)
            if payload['response_format']['json_schema']['name'] == 'structured_image_intent':
                return FakeResponse(intent)
            return FakeResponse({'model_id': 'z-image-nsfw-v2', 'prompt': prompt, 'recommendation_reason': '장면 묘사'})

    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **_kwargs: FakeHTTP())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            response = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json={'content': '빨간 우산과 OPEN 간판'})
            assert len(calls) == (2 if 'OPEN' in prompt else 3)
            assert '"exact_text":["OPEN"]' in calls[1]['messages'][-1]['content']
            if 'OPEN' not in prompt:
                assert response.status_code == 422
                assert response.json()['error']['code'] == 'image_prompt_exact_text_missing'
            else:
                assert response.status_code == 200, response.text
                result = json.loads(response.json()['assistant_message']['content'])
                assert result['structured_intent'] == intent
                mid = response.json()['assistant_message']['id']
                draft = client.put(f'/api/v1/conversations/{cid}/messages/{mid}/image-draft', headers=headers,
                                   json={'prompt': prompt, 'width': 768, 'height': 768, 'revision': 0})
                assert draft.status_code == 200, draft.text
    finally:
        directory.cleanup()


def test_intent_failure_marks_generation_failed_without_prompt_request(monkeypatch):
    client, directory = make_client(monkeypatch)
    calls = []

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'invalid'}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            calls.append(kwargs['json']['response_format']['json_schema']['name'])
            return FakeResponse()

    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **_kwargs: FakeHTTP())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            request_id = '77777777-7777-4777-8777-777777777777'
            response = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json={'content': '빨간 우산', 'request_id': request_id})
            assert response.status_code == 502
            assert calls == ['structured_image_intent', 'structured_image_intent']
            status = client.get(f'/api/v1/conversations/{cid}/generations/{request_id}', headers=headers).json()
            assert status['status'] == 'failed' and status['error']['code'] == 'image_intent_invalid_response'
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
@pytest.mark.parametrize(('model_id', 'accepted'), [('realistic-vision-v6', True), ('pornmaster-klein-9b', False)])
def test_generated_negative_prompt_is_filtered_and_reaches_queue(monkeypatch, model_id, accepted):
    client, directory = make_client(monkeypatch)
    submitted = []

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            'model_id': model_id, 'prompt': 'A street without cars', 'negative_prompt': 'cars', 'recommendation_reason': '장면 묘사'
        })}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **_kwargs): return FakeResponse()

    class FakeStudio:
        async def submit_job(self, payload):
            submitted.append(payload)
            return {'upstream_job_id': 'negative-job', 'status': 'queued'}

    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **_kwargs: FakeHTTP())
    monkeypatch.setattr(main, 'ImageStudioAdapter', lambda _settings: FakeStudio())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            response = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers,
                                   json={'content': '자동차 없는 거리', 'image_model_id': model_id})
            assert response.status_code == 200, response.text
            result = json.loads(response.json()['assistant_message']['content'])
            assert ('negative_prompt' in result) is accepted
            assert result.get('negative_prompt_omitted', False) is (not accepted)
            mid = response.json()['assistant_message']['id']
            path = f'/api/v1/conversations/{cid}/messages/{mid}/image-draft'
            draft = client.put(path, headers=headers, json={'prompt': result['prompt'], 'width': result['width'], 'height': result['height'], 'revision': 0})
            assert draft.status_code == 200, draft.text
            job = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json={
                'source_message_id': mid, 'prompt': result['prompt'], 'width': result['width'], 'height': result['height'],
                'draft_revision': 1, 'request_id': '88888888-8888-4888-8888-888888888888'})
            assert job.status_code == 202, job.text
            assert ('negative_prompt' in submitted[0]) is accepted
            assert 'negative_prompt_omitted' not in submitted[0]
    finally:
        directory.cleanup()


def test_image_prompt_rejects_streaming_before_creating_generation(monkeypatch):
    client, directory = make_client(monkeypatch)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "프롬프트", "stream": True})

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "image_prompt_stream_unsupported"
            assert conversation_messages(client, cid, headers) == []
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
def test_image_prompt_invalid_or_length_limited_output_fails_then_retries_without_duplicate_user_message(monkeypatch):
    client, directory = make_client(monkeypatch)
    calls = {"count": 0}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            calls["count"] += 1
            if calls["count"] <= 2:
                return {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}
            if calls["count"] <= 4:
                return {"choices": [{"finish_reason": "length", "message": {"content": json.dumps({
                    "model_id": "z-image-nsfw-v2", "prompt": "valid but truncated", "width": 768, "height": 768, "steps": 6, "cfg_scale": 1.0,
                })}}]}
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "model_id": "z-image-nsfw-v2", "prompt": "recovered prompt", "width": 512, "height": 512, "steps": 6, "cfg_scale": 1.0,
            })}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **_kwargs): return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            request_id = "00121402-74b5-4a01-9c8a-7ebaa1feabc1"
            payload = {"content": "이미지 프롬프트", "request_id": request_id}

            invalid = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json=payload)
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "image_prompt_invalid_json"
            assert client.get(f"/api/v1/conversations/{cid}/generations/{request_id}", headers=headers).json()["status"] == "failed"

            truncated = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json=payload)
            assert truncated.status_code == 422
            assert truncated.json()["error"]["code"] == "image_prompt_output_truncated"
            assert client.get(f"/api/v1/conversations/{cid}/generations/{request_id}", headers=headers).json()["finish_reason"] == "length"

            succeeded = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json=payload)
            assert succeeded.status_code == 200
            assert succeeded.json()["generation"]["status"] == "completed"
            messages = conversation_messages(client, cid, headers)
            assert [message["role"] for message in messages] == ["user", "assistant"]
            assert json.loads(messages[-1]["content"])["prompt"] == "recovered prompt"
            assert calls["count"] == 5
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
@pytest.mark.parametrize('first', ['invalid', 'missing_choice', 'empty', 'truncated'])
def test_image_prompt_recovers_on_second_response_without_duplicate_messages(monkeypatch, first):
    client, directory = make_client(monkeypatch)
    calls = []

    class FakeResponse:
        def __init__(self, attempt): self.attempt = attempt
        def raise_for_status(self): pass
        def json(self):
            if self.attempt == 1:
                if first == 'missing_choice': return {'choices': []}
                content = 'not json' if first == 'invalid' else ''
                return {'choices': [{'finish_reason': 'length' if first == 'truncated' else 'stop', 'message': {'content': content}}]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                'model_id': 'z-image-nsfw-v2', 'prompt': 'A red umbrella', 'recommendation_reason': 'scene'
            })}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            calls.append(kwargs['json'])
            return FakeResponse(len(calls))

    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **_kwargs: FakeHTTP())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            response = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json={'content': '빨간 우산'})
            assert response.status_code == 200, response.text
            assert len(calls) == 2 and calls[0]['messages'] == calls[1]['messages']
            assert json.loads(response.json()['assistant_message']['content'])['prompt'] == 'A red umbrella'
            assert [message['role'] for message in conversation_messages(client, cid, headers)] == ['user', 'assistant']
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
def test_image_prompt_retry_stops_after_cancellation(monkeypatch):
    client, directory = make_client(monkeypatch)
    calls = []
    request_id = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            main.app.state.cancelled_generations.add(request_id)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'invalid'}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **_kwargs):
            calls.append(True)
            return FakeResponse()

    monkeypatch.setattr(main.httpx, 'AsyncClient', lambda **_kwargs: FakeHTTP())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            response = client.post(f'/api/v1/conversations/{cid}/messages', headers=headers, json={'content': '빨간 우산', 'request_id': request_id})
            assert response.status_code == 499 and len(calls) == 1
            assert client.get(f'/api/v1/conversations/{cid}/generations/{request_id}', headers=headers).json()['status'] == 'cancelled'
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
def test_image_prompt_structured_output_rejection_has_safe_gate_error(monkeypatch):
    client, directory = make_client(monkeypatch)

    class FakeResponse:
        def raise_for_status(self):
            raise main.httpx.HTTPStatusError(
                "untrusted upstream error body",
                request=main.httpx.Request("POST", "http://gate/v1/chat/completions"),
                response=main.httpx.Response(400),
            )

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **_kwargs): return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "프롬프트"})

            assert response.status_code == 502
            assert response.json()["error"]["code"] == "image_prompt_schema_unsupported"
            assert "untrusted upstream" not in response.text
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
def test_image_prompt_gate_auth_error_is_not_reported_as_schema_failure(monkeypatch):
    client, directory = make_client(monkeypatch)

    class FakeResponse:
        def raise_for_status(self):
            raise main.httpx.HTTPStatusError(
                "untrusted upstream error body",
                request=main.httpx.Request("POST", "http://gate/v1/chat/completions"),
                response=main.httpx.Response(401),
            )

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **_kwargs): return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "프롬프트"})

            assert response.status_code == 502
            assert response.json()["error"]["code"] == "gate_auth_failed"
            assert "untrusted upstream" not in response.text
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
def test_image_prompt_requests_are_stateless(monkeypatch):
    client, directory = make_client(monkeypatch)
    captured = []

    def result(prompt, width=768, height=768):
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "model_id": "z-image-nsfw-v2", "prompt": prompt, "width": width, "height": height, "steps": 6, "cfg_scale": 1.0,
        })}}]}

    class FakeResponse:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return self.value

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            captured.append(kwargs["json"])
            request = kwargs["json"]["messages"][-1]["content"]
            if request.startswith("Original user request:\nB의 장면\n"): return FakeResponse(result("other conversation prompt"))
            if request.startswith("Original user request:\n조명을 더 어둡게\n"): return FakeResponse(result("revised darker prompt", 512, 512))
            return FakeResponse(result("first prompt"))

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            a_id = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            b_id = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]

            assert client.post(f"/api/v1/conversations/{a_id}/messages", headers=headers, json={"content": "A의 첫 장면"}).status_code == 200
            assert client.post(f"/api/v1/conversations/{b_id}/messages", headers=headers, json={"content": "B의 장면"}).status_code == 200
            revised = client.post(f"/api/v1/conversations/{a_id}/messages", headers=headers, json={"content": "조명을 더 어둡게"})

            assert revised.status_code == 200
            messages = captured[-1]["messages"]
            contents = [message["content"] for message in messages]
            assert contents[-1].count("조명을 더 어둡게") == 1
            assert "A의 첫 장면" not in contents
            assert "B의 장면" not in contents
            assert len(contents) == 2
            assert json.loads(revised.json()["assistant_message"]["content"])["prompt"] == "revised darker prompt"
    finally:
        directory.cleanup()


@pytest.mark.usefixtures('mock_image_analysis')
def test_image_prompt_history_excludes_failed_and_cancelled_requests_and_never_queues_summary(monkeypatch):
    client, directory = make_client(monkeypatch)
    captured = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "model_id": "z-image-nsfw-v2", "prompt": "completed prompt", "width": 768, "height": 768, "steps": 6, "cfg_scale": 1.0,
            })}}]}

    class FakeHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *_args, **_kwargs): return FakeModelsResponse()
        async def post(self, *_args, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    monkeypatch.setattr(main.get_settings(), "roleplay_summary_trigger_tokens", 1)
    try:
        with client:
            headers = {"Authorization": "Bearer client-key"}
            cid = client.post("/api/v1/conversations", headers=headers, json={"mode": "image_prompt"}).json()["id"]
            with connect(main.get_settings().database_path) as db:
                for state in ("failed", "cancelled"):
                    message_id = state + "-image-user"
                    generation_id = state + "-image-generation"
                    db.execute("INSERT INTO messages VALUES (?, ?, 'user', ?, ?)", (message_id, cid, state + " should not enter context", "2026-01-01T00:00:00+00:00"))
                    db.execute("""INSERT INTO generation_requests
                                  (id,conversation_id,user_message_id,status,queued_at,created_at,updated_at)
                                  VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')""",
                               (generation_id, cid, message_id, state))

            response = client.post(f"/api/v1/conversations/{cid}/messages", headers=headers, json={"content": "현재 요청"})

            assert response.status_code == 200
            contents = [message["content"] for message in captured["messages"]]
            assert "failed should not enter context" not in contents
            assert "cancelled should not enter context" not in contents
            assert contents[-1].startswith("Original user request:\n현재 요청\n")
            with connect(main.get_settings().database_path) as db:
                assert db.execute("SELECT COUNT(*) AS count FROM summary_jobs WHERE conversation_id=?", (cid,)).fetchone()["count"] == 0
    finally:
        directory.cleanup()


@pytest.mark.parametrize('model_id', ['moss-image-nsfw', 'pornmaster-klein-9b', 'realistic-vision-v6', 'z-image-turbo', 'klein-4b-q4', 'klein-4b-q8', 'z-image-nsfw-v2'])
def test_model_options_persist_and_reach_studio(monkeypatch, model_id):
    from backend.app.image_prompt import IMAGE_MODELS
    client, directory = make_client(monkeypatch)
    captured = []

    class FakeStudio:
        async def submit_job(self, payload):
            captured.append(payload)
            return {'upstream_job_id': 'model-job', 'status': 'queued'}

    monkeypatch.setattr(main, 'ImageStudioAdapter', lambda _settings: FakeStudio())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            mid, _ = add_completed_image_source(main.get_settings().database_path, cid, 'multi')
            preset = IMAGE_MODELS[model_id]['presets']['balanced']
            config = {key: preset[key] for key in ('width', 'height', 'steps', 'cfg_scale')}
            config.update(model_id=model_id, prompt='mountain landscape', seed=4242, sampling_method='euler')
            if model_id == 'moss-image-nsfw':
                config['scheduler'] = 'discrete'
            if model_id == 'pornmaster-klein-9b':
                config.update(cfg_scale=2, negative_prompt='blur')
            draft = {key: config[key] for key in ('prompt', 'width', 'height')}
            path = f'/api/v1/conversations/{cid}/messages/{mid}/image-draft'
            saved = client.put(path, headers=headers, json={**draft, 'revision': 0, 'generation_config': config})
            assert saved.status_code == 200, saved.text
            assert client.get(path, headers=headers).json()['generation_config'] == config
            body = {**draft, 'source_message_id': mid, 'draft_revision': 1, 'request_id': '11111111-1111-4111-8111-111111111111', 'generation_config': config}
            tampered = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json={**body, 'generation_config': {**config, 'seed': 42}})
            assert tampered.status_code == 409
            result = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json=body)
            assert result.status_code == 202, result.text
            assert all(captured[0][key] == value for key, value in config.items())
            assert result.json()['data']['generation_config'] == config
            replay = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json=body)
            assert replay.status_code == 202 and replay.json()['replayed'] is True
            assert len(captured) == 1
    finally:
        directory.cleanup()


def test_live_contract_is_used_for_new_draft_and_replay_survives_change(monkeypatch):
    from copy import deepcopy
    from backend.app.image_prompt import IMAGE_MODELS, apply_balanced_preset

    client, directory = make_client(monkeypatch)
    model = deepcopy(IMAGE_MODELS['z-image-nsfw-v2'])
    model['presets']['balanced'].update(width=512, height=512, steps=7)
    model['supported_resolutions'] = [[512, 512]]
    catalog = {model['model_id']: model}
    result = apply_balanced_preset(json.dumps({'model_id': model['model_id'], 'prompt': 'scene'}), models=catalog)
    assert (result.width, result.height, result.steps) == (512, 512, 7)

    async def live_catalog(_settings):
        return catalog

    class FakeStudio:
        async def submit_job(self, payload):
            assert payload['steps'] == 7
            return {'upstream_job_id': 'live-job', 'status': 'queued'}

    monkeypatch.setattr(main, 'image_model_catalog', live_catalog)
    monkeypatch.setattr(main, 'ImageStudioAdapter', lambda _settings: FakeStudio())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            mid, _ = add_completed_image_source(main.get_settings().database_path, cid, 'live')
            config = result.to_dict()
            path = f'/api/v1/conversations/{cid}/messages/{mid}/image-draft'
            draft = client.put(path, headers=headers, json={'prompt': 'scene', 'width': 512, 'height': 512, 'revision': 0, 'generation_config': config})
            assert draft.status_code == 200, draft.text
            body = {'source_message_id': mid, 'prompt': 'scene', 'width': 512, 'height': 512, 'draft_revision': 1,
                    'request_id': '22222222-2222-4222-8222-222222222222', 'generation_config': config}
            created = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json=body)
            assert created.status_code == 202, created.text
            model['supported_resolutions'] = [[768, 768]]
            replay = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json=body)
            assert replay.status_code == 202 and replay.json()['replayed'] is True
            body['request_id'] = '33333333-3333-4333-8333-333333333333'
            assert client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json=body).status_code == 422
    finally:
        directory.cleanup()


def test_preset_label_matches_saved_values_and_reaches_job(monkeypatch):
    client, directory = make_client(monkeypatch)
    submitted = []

    class FakeStudio:
        async def submit_job(self, payload):
            submitted.append(payload)
            return {'upstream_job_id': 'preset-job', 'status': 'queued'}

    monkeypatch.setattr(main, 'ImageStudioAdapter', lambda _settings: FakeStudio())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            mid, _ = add_completed_image_source(main.get_settings().database_path, cid, 'preset')
            path = f'/api/v1/conversations/{cid}/messages/{mid}/image-draft'
            config = {'model_id': 'z-image-nsfw-v2', 'prompt': 'street', 'width': 768, 'height': 768, 'steps': 4, 'cfg_scale': 1.0}
            base = {'prompt': 'street', 'width': 768, 'height': 768, 'revision': 0, 'generation_config': config}
            mismatch = client.put(path, headers=headers, json={**base, 'preset_name': 'balanced'})
            assert mismatch.status_code == 422 and mismatch.json()['error']['code'] == 'image_preset_mismatch'
            saved = client.put(path, headers=headers, json={**base, 'preset_name': 'fast'})
            assert saved.status_code == 200 and saved.json()['preset_name'] == 'fast'
            body = {'source_message_id': mid, 'prompt': 'street', 'width': 768, 'height': 768, 'draft_revision': 1,
                    'generation_config': config, 'request_id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'}
            created = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json=body)
            assert created.status_code == 202 and created.json()['data']['preset_name'] == 'fast'
            assert submitted[0]['preset'] == 'fast'
            assert client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json=body).json()['replayed'] is True
            updated = client.put(path, headers=headers, json={**base, 'revision': 1, 'preset_name': 'custom'})
            assert updated.status_code == 200 and updated.json()['preset_name'] == 'custom'
    finally:
        directory.cleanup()


@pytest.mark.parametrize('enabled', [False, True])
def test_moss_lora_selection_is_saved_and_sent_only_with_capability(monkeypatch, enabled):
    from copy import deepcopy
    from backend.app.image_prompt import IMAGE_MODELS
    client, directory = make_client(monkeypatch)
    model = deepcopy(IMAGE_MODELS['moss-image-nsfw'])
    model['lora_toggle_supported'] = True
    catalog = {model['model_id']: model}
    captured = []

    async def live_catalog(_settings): return catalog

    class FakeStudio:
        async def submit_job(self, payload):
            captured.append(payload)
            return {'upstream_job_id': 'lora-job', 'status': 'queued'}

    monkeypatch.setattr(main, 'image_model_catalog', live_catalog)
    monkeypatch.setattr(main, 'ImageStudioAdapter', lambda _settings: FakeStudio())
    try:
        with client:
            headers = {'Authorization': 'Bearer client-key'}
            cid = client.post('/api/v1/conversations', headers=headers, json={'mode': 'image_prompt'}).json()['id']
            mid, _ = add_completed_image_source(main.get_settings().database_path, cid, 'lora')
            config = {'model_id': model['model_id'], 'prompt': 'landscape', 'width': 512, 'height': 512,
                      'steps': 20, 'cfg_scale': 3.0, 'lora_enabled': enabled}
            path = f'/api/v1/conversations/{cid}/messages/{mid}/image-draft'
            body = {'prompt': 'landscape', 'width': 512, 'height': 512, 'revision': 0, 'generation_config': config, 'preset_name': 'balanced'}
            saved = client.put(path, headers=headers, json=body)
            assert saved.status_code == 200, saved.text
            job = client.post(f'/api/v1/conversations/{cid}/image-jobs', headers=headers, json={
                'source_message_id': mid, 'prompt': 'landscape', 'width': 512, 'height': 512,
                'draft_revision': 1, 'generation_config': config, 'request_id': 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'})
            assert job.status_code == 202, job.text
            assert captured[0]['lora_enabled'] is enabled
            assert job.json()['data']['generation_config']['lora_enabled'] is enabled
            model['lora_toggle_supported'] = False
            retry = client.put(path, headers=headers, json={**body, 'revision': 1})
            assert retry.status_code == 422
    finally:
        directory.cleanup()


def test_local_mode_requires_browser_marker_and_allowed_host(monkeypatch):
    client, directory = make_client(monkeypatch)
    monkeypatch.setattr(main.settings, "app_auth_mode", "local")
    monkeypatch.setattr(main.settings, "app_allowed_hosts", "testserver")
    try:
        with client:
            path = "/api/v1/conversations"
            headers = {"X-BC250-Client": "1"}
            assert client.get(path, headers=headers).status_code == 200
            assert client.get(path).status_code == 403
            assert client.get(path, headers={**headers, "host": "evil.example"}).status_code == 403
            assert client.post(path, headers={**headers, "origin": "https://evil.example"}, json={}).status_code == 403
            assert client.get(path, headers={**headers, "sec-fetch-site": "cross-site"}).status_code == 403
            assert client.get(path, headers={**headers, "origin": "http://testserver"}).status_code == 200
    finally:
        directory.cleanup()
