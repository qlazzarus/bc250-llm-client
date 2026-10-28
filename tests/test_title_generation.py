import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("APP_API_KEY", "client-key")
os.environ.setdefault("LLM_GATE_API_KEY", "gate-key")

from backend.app import main
from backend.app.db import connect, initialize


def test_title_generation_and_manual_rename_are_safe(tmp_path, monkeypatch):
    database = str(tmp_path / "test.db")
    initialize(database)
    with connect(database) as db:
        db.execute("INSERT INTO conversations (id,title,created_at,updated_at,title_pending) VALUES ('c','새 대화','t','t',1)")
    settings = SimpleNamespace(database_path=database, llm_gate_url="http://127.0.0.1:8081", llm_gate_api_key="key", llm_timeout_seconds=5)
    main.app.state.generation_limiter = asyncio.Semaphore(1)
    async def model(_settings):
        return "model"
    monkeypatch.setattr(main, "resolve_gate_model", model)

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": '"화성 여행 계획"'}}]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    asyncio.run(main.generate_conversation_title(settings, "c", "화성 여행을 계획해줘"))
    with connect(database) as db:
        row = db.execute("SELECT title,title_pending FROM conversations WHERE id='c'").fetchone()
        assert (row["title"], row["title_pending"]) == ("화성 여행 계획", 0)
        db.execute("UPDATE conversations SET title='직접 지정', title_pending=0 WHERE id='c'")
    asyncio.run(main.generate_conversation_title(settings, "c", "다른 메시지"))
    with connect(database) as db:
        assert db.execute("SELECT title FROM conversations WHERE id='c'").fetchone()["title"] == "직접 지정"
