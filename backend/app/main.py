import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings, get_settings
from .db import connect, initialize
from .image_prompt import IMAGE_MODELS, ImagePromptValidationError, parse_image_prompt_config, text_response_format as image_prompt_response_format, apply_balanced_preset
from .image_intent import INTENT_INSTRUCTION, parse_intent, prompt_with_intent, response_format as image_intent_response_format
from .imagegen import (
    ImageGenerationNotFound, ImageGenerationRejected, ImageGenerationSubmissionUnknown,
    ImageGenerationUnavailable, ImageStudioAdapter,
)
from .image_jobs import (
    ImageDraftError, active_image_job_count, create_or_replay_image_job, get_draft, has_active_image_jobs, image_job_dict,
    job_snapshot, mark_image_job_cancel_requested, mark_pending_jobs_unknown, save_draft, set_image_job_state,
)
from .prompting import ContextBudgetExceeded, build_prompt, estimate_tokens, summary_candidate

logger = logging.getLogger("bc250_llm_client")


def now() -> str:
    return datetime.now(UTC).isoformat()


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="새 대화", min_length=1, max_length=120)
    mode: Literal["quick", "roleplay", "image_prompt"] = "quick"
    response_length: Literal["short", "medium", "long"] = "medium"
    mode_settings: dict[str, str] = Field(default_factory=dict)


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=120)
    response_length: Literal["short", "medium", "long"] | None = None
    mode_settings: dict[str, str] | None = None
    summary: str | None = Field(default=None, max_length=12000)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=12000)
    temperature: float = Field(default=0.7, ge=0, le=2)
    stream: bool = False
    request_id: UUID | None = None
    prompt_mode: Literal["ai_optimized", "direct_translation", "original"] = "ai_optimized"
    image_model_id: str | None = Field(default=None, min_length=1, max_length=100)


class GateControl(BaseModel):
    paused: bool | None = None


class ImageDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=8000)
    width: int
    height: int
    generation_config: dict[str, Any] | None = None
    preset_name: Literal["fast", "balanced", "quality", "custom"] | None = None
    revision: int = Field(ge=0)


class ImageJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_message_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=8000)
    width: int
    height: int
    generation_config: dict[str, Any] | None = None
    draft_revision: int = Field(ge=1)
    request_id: UUID


class ImageIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=8000)


class ImageEvaluationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: int | None = Field(ge=1, le=5, strict=True)
    note: str = Field(max_length=1000)


def fail(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(http_status, {"code": code, "message": message})


def require_key(request: Request, settings: Settings = Depends(get_settings)) -> None:
    if settings.app_auth_mode == "local":
        # A non-simple header prevents other browser origins from issuing API calls.
        # Restrict Host as well to prevent a hostile DNS name resolving to this server.
        host = request.url.hostname or ""
        allowed = {item.strip().lower() for item in settings.app_allowed_hosts.split(",") if item.strip()}
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if (host.lower() not in allowed or request.headers.get("x-bc250-client") != "1"
                or (origin is not None and origin.lower() != expected_origin.lower())
                or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none")):
            raise fail("forbidden", "허용되지 않은 요청입니다.", status.HTTP_403_FORBIDDEN)
        return
    value = request.headers.get("authorization", "")
    if value != f"Bearer {settings.app_api_key}":
        raise fail("unauthorized", "인증이 필요합니다.", status.HTTP_401_UNAUTHORIZED)


MODE_SETTING_KEYS = {
    "quick": set(),
    "roleplay": {"character", "world", "voice", "scenario", "rules"},
    "image_prompt": {"subject", "composition", "style", "lighting", "palette", "negative_prompt", "output_language"},
}
MAX_MODE_SETTINGS_CHARS = 10_000


def validate_mode_settings(mode: str, settings: dict[str, Any]) -> dict[str, str]:
    allowed = MODE_SETTING_KEYS[mode]
    unknown = set(settings) - allowed
    if unknown:
        raise fail("invalid_mode_settings", f"{mode} 모드에서 지원하지 않는 설정입니다: {', '.join(sorted(unknown))}", 422)
    if any(not isinstance(value, str) for value in settings.values()):
        raise fail("invalid_mode_settings", "모드 설정 값은 문자열이어야 합니다.", 422)
    if any(len(value.strip()) > 4_000 for value in settings.values()):
        raise fail("invalid_mode_settings", "모드 설정 항목은 4,000자를 초과할 수 없습니다.", 422)
    normalized = {key: value.strip() for key, value in settings.items() if value.strip()}
    if sum(len(value) for value in normalized.values()) > MAX_MODE_SETTINGS_CHARS:
        raise fail("invalid_mode_settings", "모드 설정 전체는 10,000자를 초과할 수 없습니다.", 422)
    return normalized


def mode_context_policy(mode: str) -> str:
    return "none" if mode in {"quick", "image_prompt"} else "recent"


def image_prompt_settings_not_supported(settings: dict[str, str]) -> None:
    if settings:
        raise fail(
            "image_prompt_settings_not_supported",
            "이미지 프롬프트 모드는 사전 설정을 지원하지 않습니다. 원하는 장면을 요청에 모두 입력하세요.",
            422,
        )


def conversation_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "mode": row["mode"],
        "context_policy": row["context_policy"],
        "mode_settings": json.loads(row["mode_settings"]),
        "response_length": row["response_length"],
        "summary": row["summary"],
        "summary_up_to_message_id": row["summary_up_to_message_id"],
        "summary_status": row["summary_status"],
        "summary_error": row["summary_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def message_dict(row):
    return {"id": row["id"], "role": row["role"], "content": row["content"], "created_at": row["created_at"]}


def generation_dict(row):
    def elapsed_ms(start: str | None, end: str | None) -> int | None:
        if not start or not end:
            return None
        return max(0, round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000))

    usage = json.loads(row["usage_json"]) if row["usage_json"] else None
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "user_message_id": row["user_message_id"],
        "assistant_message_id": row["assistant_message_id"],
        "status": row["status"],
        "error": {"code": row["error_code"], "message": row["error_message"]} if row["error_code"] else None,
        "model_id": row["model_id"],
        "requested_max_tokens": row["requested_max_tokens"],
        "estimated_input_tokens": row["estimated_input_tokens"],
        "finish_reason": row["finish_reason"],
        "usage": usage,
        "queue_duration_ms": elapsed_ms(row["queued_at"], row["started_at"]),
        "generation_duration_ms": elapsed_ms(row["started_at"], row["updated_at"]) if row["status"] in {"completed", "failed", "cancelled"} else None,
        "queued_at": row["queued_at"],
        "started_at": row["started_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def begin_generation(conversation_id: str, body: MessageCreate, settings: Settings):
    requested_id = str(body.request_id or uuid.uuid4())
    user_content = body.content.strip()
    with connect(settings.database_path) as db:
        conversation = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not conversation:
            raise fail("not_found", "대화를 찾을 수 없습니다.", 404)
        fake_allowed = body.image_model_id == "fake" and settings.imagegen_enabled and settings.imagegen_allow_fake
        if body.image_model_id is not None and (conversation["mode"] != "image_prompt" or (body.image_model_id not in IMAGE_MODELS and not fake_allowed)):
            raise fail("invalid_image_model", "지원하는 이미지 모델을 선택해 주세요.", 422)
        if conversation["mode"] == "image_prompt" and body.prompt_mode == "original":
            if body.image_model_id is None:
                raise fail("image_model_required", "원본 프롬프트를 사용하려면 이미지 모델을 선택해 주세요.", 422)
            if not user_content or len(user_content) > 8000 or "\x00" in user_content:
                raise fail("invalid_image_request", "원본 프롬프트는 1~8,000자여야 합니다.", 422)

        existing = db.execute("""SELECT generation_requests.*, messages.content AS user_content
                                 FROM generation_requests JOIN messages ON messages.id = generation_requests.user_message_id
                                 WHERE generation_requests.id=?""", (requested_id,)).fetchone()
        if existing:
            if existing["conversation_id"] != conversation_id or existing["user_content"] != user_content:
                raise fail("idempotency_conflict", "같은 요청 ID에는 같은 대화와 메시지를 사용해야 합니다.", 409)
            if conversation["mode"] == "image_prompt" and (existing["prompt_mode"] or "ai_optimized") != body.prompt_mode:
                raise fail("idempotency_conflict", "같은 요청 ID에는 같은 프롬프트 방식을 사용해야 합니다.", 409)
            if conversation["mode"] == "image_prompt" and existing["image_model_preference"] != body.image_model_id:
                raise fail("idempotency_conflict", "같은 요청 ID에는 같은 이미지 모델을 사용해야 합니다.", 409)
            if existing["status"] == "pending":
                raise fail("generation_in_progress", "이 요청은 이미 생성 중입니다.", 409)
            if existing["status"] == "completed":
                assistant = db.execute("SELECT id,role,content,created_at FROM messages WHERE id=?", (existing["assistant_message_id"],)).fetchone()
                return conversation, dict(existing), None, assistant
            try:
                db.execute("""UPDATE generation_requests SET status='pending',error_code=NULL,error_message=NULL,
                              queued_at=?,started_at=NULL,model_id=NULL,requested_max_tokens=NULL,
                              estimated_input_tokens=NULL,finish_reason=NULL,usage_json=NULL,updated_at=? WHERE id=?""", (now(), now(), requested_id))
            except sqlite3.IntegrityError:
                raise fail("conversation_generation_in_progress", "이 대화에는 이미 생성 중인 요청이 있습니다.", 409)
            generation = db.execute("SELECT * FROM generation_requests WHERE id=?", (requested_id,)).fetchone()
        else:
            user_msg = {"id": str(uuid.uuid4()), "conversation_id": conversation_id, "role": "user", "content": user_content, "created_at": now()}
            try:
                db.execute("INSERT INTO messages VALUES (:id,:conversation_id,:role,:content,:created_at)", user_msg)
                timestamp = now()
                generation = {"id": requested_id, "conversation_id": conversation_id, "user_message_id": user_msg["id"], "assistant_message_id": None, "status": "pending", "error_code": None, "error_message": None, "queued_at": timestamp, "started_at": None, "model_id": None, "requested_max_tokens": None, "estimated_input_tokens": None, "finish_reason": None, "usage_json": None, "created_at": timestamp, "updated_at": timestamp}
                db.execute("""INSERT INTO generation_requests
                              (id,conversation_id,user_message_id,assistant_message_id,status,error_code,error_message,queued_at,started_at,model_id,requested_max_tokens,estimated_input_tokens,finish_reason,usage_json,created_at,updated_at)
                              VALUES (:id,:conversation_id,:user_message_id,:assistant_message_id,:status,:error_code,:error_message,:queued_at,:started_at,:model_id,:requested_max_tokens,:estimated_input_tokens,:finish_reason,:usage_json,:created_at,:updated_at)""", generation)
                if conversation["mode"] == "image_prompt":
                    db.execute("UPDATE generation_requests SET prompt_mode=?,image_model_preference=? WHERE id=?", (body.prompt_mode, body.image_model_id, requested_id))
            except sqlite3.IntegrityError:
                raise fail("conversation_generation_in_progress", "이 대화에는 이미 생성 중인 요청이 있습니다.", 409)

        if conversation["mode"] == "image_prompt":
            return conversation, dict(generation), [], None

        history_params: list[Any] = [conversation_id, generation["user_message_id"]]
        summary_cursor = conversation["summary_up_to_message_id"]
        summary_condition = ""
        if summary_cursor:
            cursor = db.execute("SELECT id,created_at FROM messages WHERE id=? AND conversation_id=?", (summary_cursor, conversation_id)).fetchone()
            if cursor:
                summary_condition = "AND (messages.created_at > ? OR (messages.created_at = ? AND messages.id > ?))"
                history_params.extend([cursor["created_at"], cursor["created_at"], cursor["id"]])
        history_params.append(settings.max_history_messages)
        history = db.execute(f"""SELECT role,content FROM messages
                                WHERE conversation_id=? AND id != ? AND (
                                    NOT EXISTS (SELECT 1 FROM generation_requests g
                                                WHERE g.user_message_id = messages.id OR g.assistant_message_id = messages.id)
                                    OR EXISTS (SELECT 1 FROM generation_requests g
                                               WHERE g.status='completed' AND (g.user_message_id = messages.id OR g.assistant_message_id = messages.id))
                                )
                                {summary_condition}
                                ORDER BY created_at DESC, id DESC LIMIT ?""", history_params).fetchall()[::-1]
        while history and history[0]["role"] != "user":
            history.pop(0)
    return conversation, dict(generation), history, None


async def resolve_gate_model(settings: Settings) -> str:
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            response = await client.get(f"{settings.llm_gate_url}/v1/models", headers={"Authorization": f"Bearer {settings.llm_gate_api_key}"})
            response.raise_for_status()
            model_ids = [item.get("id") for item in response.json().get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str)]
    except httpx.HTTPError:
        raise fail("gate_unavailable", "모델 정보를 지금 조회할 수 없습니다.", 503)
    if not model_ids:
        raise fail("no_model_available", "LLM gate에서 사용할 수 있는 모델을 찾지 못했습니다.", 503)
    if settings.llm_model_id:
        if settings.llm_model_id not in model_ids:
            raise fail("configured_model_unavailable", "서버에 설정된 모델을 LLM gate에서 찾지 못했습니다.", 503)
        return settings.llm_model_id
    return model_ids[0]


def mark_generation_terminal(settings: Settings, generation_id: str, status_value: Literal["failed", "cancelled"], error_code: str | None = None, error_message: str | None = None, finish_reason: str | None = None) -> None:
    with connect(settings.database_path) as db:
        db.execute("""UPDATE generation_requests SET status=?,error_code=?,error_message=?,finish_reason=?,updated_at=?
                      WHERE id=? AND status='pending'""", (status_value, error_code, error_message, finish_reason, now(), generation_id))


def prepare_generation(settings: Settings, generation_id: str, model_id: str, requested_max_tokens: int, estimated_input_tokens: int) -> None:
    with connect(settings.database_path) as db:
        db.execute("""UPDATE generation_requests SET model_id=?,requested_max_tokens=?,estimated_input_tokens=?,updated_at=?
                      WHERE id=? AND status='pending'""", (model_id, requested_max_tokens, estimated_input_tokens, now(), generation_id))


def mark_generation_started(settings: Settings, generation_id: str) -> None:
    with connect(settings.database_path) as db:
        db.execute("UPDATE generation_requests SET started_at=?,updated_at=? WHERE id=? AND status='pending'", (now(), now(), generation_id))


def complete_generation(settings: Settings, generation_id: str, content: str, finish_reason: str | None, usage: dict[str, Any] | None):
    with connect(settings.database_path) as db:
        generation = db.execute("SELECT * FROM generation_requests WHERE id=?", (generation_id,)).fetchone()
        if not generation or generation["status"] != "pending":
            return None
        assistant = {"id": str(uuid.uuid4()), "conversation_id": generation["conversation_id"], "role": "assistant", "content": content, "created_at": now()}
        db.execute("INSERT INTO messages VALUES (:id,:conversation_id,:role,:content,:created_at)", assistant)
        db.execute("""UPDATE generation_requests SET status='completed',assistant_message_id=?,finish_reason=?,usage_json=?,updated_at=?
                      WHERE id=?""", (assistant["id"], finish_reason, json.dumps(usage, ensure_ascii=False) if usage else None, now(), generation_id))
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), generation["conversation_id"]))
    return assistant


def completed_history_after_summary(db: sqlite3.Connection, conversation) -> list[dict[str, str]]:
    """Return only durable, successfully completed turns after the saved summary cursor."""
    params: list[Any] = [conversation["id"]]
    cursor_condition = ""
    if conversation["summary_up_to_message_id"]:
        cursor = db.execute("SELECT id,created_at FROM messages WHERE id=? AND conversation_id=?", (conversation["summary_up_to_message_id"], conversation["id"])).fetchone()
        if cursor:
            cursor_condition = "AND (messages.created_at > ? OR (messages.created_at = ? AND messages.id > ?))"
            params.extend([cursor["created_at"], cursor["created_at"], cursor["id"]])
    rows = db.execute(f"""SELECT messages.id,messages.role,messages.content,messages.created_at
                          FROM messages
                          WHERE messages.conversation_id=? {cursor_condition} AND (
                              NOT EXISTS (SELECT 1 FROM generation_requests g
                                          WHERE g.user_message_id=messages.id OR g.assistant_message_id=messages.id)
                              OR EXISTS (SELECT 1 FROM generation_requests g WHERE g.status='completed'
                                         AND (g.user_message_id=messages.id OR g.assistant_message_id=messages.id))
                          )
                          ORDER BY messages.created_at, messages.id""", params).fetchall()
    return [dict(row) for row in rows]


def queue_roleplay_summary(settings: Settings, conversation_id: str) -> str | None:
    """Persist a summary job. The expensive gate call happens outside the request transaction."""
    with connect(settings.database_path) as db:
        conversation = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not conversation or conversation["mode"] != "roleplay":
            return None
        if db.execute("SELECT 1 FROM summary_jobs WHERE conversation_id=? AND status IN ('pending','running')", (conversation_id,)).fetchone():
            return None
        candidate = summary_candidate(completed_history_after_summary(db, conversation), settings)
        if not candidate:
            return None
        job = {
            "id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "from_message_id": conversation["summary_up_to_message_id"],
            "through_message_id": candidate.through_message_id,
            "status": "pending",
            "error_message": None,
            "created_at": now(),
            "updated_at": now(),
        }
        try:
            db.execute("""INSERT INTO summary_jobs
                          (id,conversation_id,from_message_id,through_message_id,status,error_message,created_at,updated_at)
                          VALUES (:id,:conversation_id,:from_message_id,:through_message_id,:status,:error_message,:created_at,:updated_at)""", job)
        except sqlite3.IntegrityError:
            return None
        db.execute("UPDATE conversations SET summary_status='pending',summary_error=NULL,updated_at=? WHERE id=?", (now(), conversation_id))
    return job["id"]


def summary_messages_for_job(db: sqlite3.Connection, conversation, job) -> list[dict[str, str]]:
    """Select the exact durable range originally queued, while accepting lexical UUID order ties."""
    all_messages = completed_history_after_summary(db, conversation)
    result: list[dict[str, str]] = []
    for message in all_messages:
        result.append(message)
        if message["id"] == job["through_message_id"]:
            return result
    return []


def set_summary_failure(settings: Settings, job_id: str, message: str) -> None:
    with connect(settings.database_path) as db:
        job = db.execute("SELECT * FROM summary_jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] not in {"pending", "running"}:
            return
        db.execute("UPDATE summary_jobs SET status='failed',error_message=?,updated_at=? WHERE id=?", (message, now(), job_id))
        db.execute("""UPDATE conversations SET summary_status='failed',summary_error=?,updated_at=?
                      WHERE id=? AND summary_status IN ('pending','running')""", (message, now(), job["conversation_id"]))


async def run_summary_job(settings: Settings, job_id: str) -> None:
    with connect(settings.database_path) as db:
        job = db.execute("SELECT * FROM summary_jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] != "pending":
            return
        conversation = db.execute("SELECT * FROM conversations WHERE id=?", (job["conversation_id"],)).fetchone()
        if not conversation or conversation["mode"] != "roleplay":
            error = "롤플레잉 대화가 아니어서 요약을 건너뛰었습니다."
            db.execute("UPDATE summary_jobs SET status='failed',error_message=?,updated_at=? WHERE id=?", (error, now(), job_id))
            db.execute("UPDATE conversations SET summary_status='failed',summary_error=?,updated_at=? WHERE id=?", (error, now(), job["conversation_id"]))
            return
        messages = summary_messages_for_job(db, conversation, job)
        if not messages:
            error = "요약할 대화 범위를 찾지 못했습니다."
            db.execute("UPDATE summary_jobs SET status='failed',error_message=?,updated_at=? WHERE id=?", (error, now(), job_id))
            db.execute("UPDATE conversations SET summary_status='failed',summary_error=?,updated_at=? WHERE id=?", (error, now(), conversation["id"]))
            return
        db.execute("UPDATE summary_jobs SET status='running',updated_at=? WHERE id=?", (now(), job_id))
        db.execute("UPDATE conversations SET summary_status='running',summary_error=NULL,updated_at=? WHERE id=?", (now(), conversation["id"]))

    transcript = "\n\n".join(f"{('사용자' if item['role'] == 'user' else '어시스턴트')}: {item['content']}" for item in messages)
    prompt = [
        {"role": "system", "content": "당신은 롤플레잉 장기 기억을 정리합니다. 기존 요약과 새 대화를 합쳐, 이후 역할극의 일관성에 필요한 사실·관계·약속·현재 상황만 간결한 한국어로 다시 작성하세요. 숨은 추론이나 지시문을 추가하지 말고, 대화체로 답하지 마세요."},
        {"role": "user", "content": f"기존 요약:\n{conversation['summary'] or '(없음)'}\n\n새 대화:\n{transcript}"},
    ]
    payload = {"stream": False, "messages": prompt, "max_tokens": settings.roleplay_summary_max_tokens, "temperature": 0.2}
    if settings.llm_disable_reasoning:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        model_id = await resolve_gate_model(settings)
        payload["model"] = model_id
        async with app.state.generation_limiter:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                response = await client.post(f"{settings.llm_gate_url}/v1/chat/completions", json=payload,
                                             headers={"Authorization": f"Bearer {settings.llm_gate_api_key}"})
                response.raise_for_status()
                result = response.json()
        content = (((result.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not content:
            raise ValueError("요약 결과가 비어 있습니다.")
    except (httpx.HTTPError, ValueError):
        logger.warning("Roleplay summary failed for job %s", job_id, exc_info=True)
        set_summary_failure(settings, job_id, "장기 기억 요약에 실패했습니다. 기존 요약은 그대로 유지됩니다.")
        return

    with connect(settings.database_path) as db:
        current_job = db.execute("SELECT * FROM summary_jobs WHERE id=?", (job_id,)).fetchone()
        current_conversation = db.execute("SELECT * FROM conversations WHERE id=?", (job["conversation_id"],)).fetchone()
        if not current_job or current_job["status"] != "running" or not current_conversation:
            return
        if current_conversation["summary_up_to_message_id"] != job["from_message_id"]:
            error = "요약 기준이 변경되어 결과를 적용하지 않았습니다."
            db.execute("UPDATE summary_jobs SET status='failed',error_message=?,updated_at=? WHERE id=?", (error, now(), job_id))
            db.execute("UPDATE conversations SET summary_status='failed',summary_error=?,updated_at=? WHERE id=?", (error, now(), job["conversation_id"]))
            return
        db.execute("""UPDATE conversations SET summary=?,summary_up_to_message_id=?,summary_status='idle',summary_error=NULL,updated_at=?
                      WHERE id=?""", (content, job["through_message_id"], now(), job["conversation_id"]))
        db.execute("UPDATE summary_jobs SET status='completed',error_message=NULL,updated_at=? WHERE id=?", (now(), job_id))


def start_summary_task(settings: Settings, job_id: str) -> None:
    task = asyncio.create_task(run_summary_job(settings, job_id))
    app.state.summary_tasks.add(task)
    task.add_done_callback(app.state.summary_tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_data_directory()
    initialize(settings.database_path)
    with connect(settings.database_path) as db:
        mark_pending_jobs_unknown(db, now())
    app.state.generation_limiter = asyncio.Semaphore(settings.max_concurrent_generations)
    app.state.cancelled_generations = set()
    app.state.summary_tasks = set()
    app.state.image_job_poll_times = {}
    yield


app = FastAPI(title="BC-250 LLM Client API", version="0.1.0", lifespan=lifespan)
settings = get_settings()
if settings.cors_origin_list:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type"])


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": "요청 형식이 올바르지 않습니다.", "details": exc.errors()}})


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, _exc: Exception):
    logger.exception("Unhandled request error")
    return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "서버 오류가 발생했습니다."}})


@app.middleware("http")
async def request_size_limit(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_request_bytes:
        return JSONResponse(status_code=413, content={"error": {"code": "payload_too_large", "message": "요청 크기가 제한을 초과했습니다."}})
    return await call_next(request)


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/image-generation/capabilities", dependencies=[Depends(require_key)])
async def image_generation_capabilities(settings: Settings = Depends(get_settings)):
    try:
        return await ImageStudioAdapter(settings).capabilities()
    except ImageGenerationUnavailable as exc:
        messages = {
            "imagegen_timeout": "이미지 생성 서비스 응답 시간이 초과되었습니다.",
            "imagegen_invalid_response": "이미지 생성 서비스 응답을 확인할 수 없습니다.",
        }
        raise fail("image_generation_unavailable", messages.get(str(exc), "이미지 생성 서비스를 지금 사용할 수 없습니다."), 503)


@app.post("/api/v1/image-assistant/analyze", dependencies=[Depends(require_key)])
async def analyze_image_intent(body: ImageIntentRequest, settings: Settings = Depends(get_settings)):
    source = body.content.strip()
    retry_instruction = "Return a complete valid JSON object using the required schema. Copy exact_text literally from the user request."
    if not source or "\x00" in source:
        raise fail("invalid_image_request", "이미지 요청을 입력해 주세요.", 422)
    if estimate_tokens(INTENT_INSTRUCTION) + estimate_tokens(source) + estimate_tokens(retry_instruction) + settings.image_prompt_max_tokens + settings.llm_context_safety_tokens > settings.llm_context_tokens:
        raise fail("context_budget_exceeded", "이미지 요청이 컨텍스트 예산을 초과했습니다.", 422)
    model_id = await resolve_gate_model(settings)
    messages = [{"role": "system", "content": INTENT_INSTRUCTION}, {"role": "user", "content": source}]
    payload = {"model": model_id, "messages": messages, "stream": False, "temperature": 0.2,
               "max_tokens": settings.image_prompt_max_tokens, "chat_template_kwargs": {"enable_thinking": False},
               "response_format": image_intent_response_format()}
    try:
        async with app.state.generation_limiter:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                for attempt in range(2):
                    response = await client.post(f"{settings.llm_gate_url}/v1/chat/completions", json=payload,
                                                 headers={"Authorization": f"Bearer {settings.llm_gate_api_key}"})
                    response.raise_for_status()
                    try:
                        result = response.json()
                        choice = result["choices"][0]
                        if choice.get("finish_reason") == "length":
                            raise ValueError("truncated")
                        intent = parse_intent(choice["message"]["content"], source)
                        return {"intent": intent.model_dump(), "model_id": model_id}
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        if attempt == 0:
                            payload["messages"] = messages + [{"role": "system", "content": retry_instruction}]
    except httpx.TimeoutException:
        raise fail("generation_timeout", "이미지 요청 분석 시간이 초과되었습니다.", 504)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 422):
            raise fail("image_intent_schema_unsupported", "LLM gate가 이미지 의도 분석 스키마를 지원하지 않습니다.", 502)
        raise fail("gate_unavailable", "LLM gate에 연결할 수 없습니다.", 503)
    except httpx.HTTPError:
        raise fail("gate_unavailable", "LLM gate에 연결할 수 없습니다.", 503)
    raise fail("image_intent_invalid_response", "이미지 요청 분석 결과를 검증하지 못했습니다. 다시 시도해 주세요.", 502)


@app.get("/api/v1/models", dependencies=[Depends(require_key)])
async def models(settings: Settings = Depends(get_settings)):
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            response = await client.get(f"{settings.llm_gate_url}/v1/models", headers={"Authorization": f"Bearer {settings.llm_gate_api_key}"})
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        raise fail("gate_unavailable", "모델 정보를 지금 조회할 수 없습니다.", 503)


@app.get("/api/v1/conversations", dependencies=[Depends(require_key)])
def list_conversations(settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        rows = db.execute("SELECT * FROM conversations ORDER BY updated_at DESC").fetchall()
    return {"data": [conversation_dict(row) for row in rows]}


@app.post("/api/v1/conversations", status_code=201, dependencies=[Depends(require_key)])
def create_conversation(body: ConversationCreate, settings: Settings = Depends(get_settings)):
    mode_settings = validate_mode_settings(body.mode, body.mode_settings)
    if body.mode == "image_prompt":
        image_prompt_settings_not_supported(mode_settings)
    item = {
        "id": str(uuid.uuid4()),
        "title": body.title.strip(),
        "mode": body.mode,
        "context_policy": mode_context_policy(body.mode),
        "mode_settings": json.dumps(mode_settings, ensure_ascii=False),
        "response_length": body.response_length,
        "summary": "",
        "summary_up_to_message_id": None,
        "summary_status": "idle",
        "summary_error": None,
        "created_at": now(),
        "updated_at": now(),
    }
    with connect(settings.database_path) as db:
        db.execute("""INSERT INTO conversations
                      (id,title,mode,context_policy,mode_settings,response_length,summary,summary_up_to_message_id,summary_status,summary_error,created_at,updated_at)
                      VALUES (:id,:title,:mode,:context_policy,:mode_settings,:response_length,:summary,:summary_up_to_message_id,:summary_status,:summary_error,:created_at,:updated_at)""", item)
        row = db.execute("SELECT * FROM conversations WHERE id=?", (item["id"],)).fetchone()
    return conversation_dict(row)


@app.get("/api/v1/conversations/{conversation_id}", dependencies=[Depends(require_key)])
def get_conversation(conversation_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row: raise fail("not_found", "대화를 찾을 수 없습니다.", 404)
    return conversation_dict(row)


@app.get("/api/v1/conversations/{conversation_id}/messages", dependencies=[Depends(require_key)])
def list_messages(conversation_id: str, before: str | None = None, limit: int = Query(default=50, ge=1, le=100), settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        if not db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
            raise fail("not_found", "대화를 찾을 수 없습니다.", 404)
        params: list[Any] = [conversation_id]
        cursor_condition = ""
        if before:
            cursor = db.execute("SELECT id,created_at FROM messages WHERE id=? AND conversation_id=?", (before, conversation_id)).fetchone()
            if not cursor:
                raise fail("invalid_cursor", "메시지 커서를 찾을 수 없습니다.", 404)
            cursor_condition = "AND (created_at < ? OR (created_at = ? AND id < ?))"
            params.extend([cursor["created_at"], cursor["created_at"], cursor["id"]])
        params.append(limit + 1)
        rows = db.execute(f"""SELECT id,role,content,created_at FROM messages
                              WHERE conversation_id=? {cursor_condition}
                              ORDER BY created_at DESC, id DESC LIMIT ?""", params).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    next_before = page[-1]["id"] if has_more else None
    return {"data": [message_dict(row) for row in reversed(page)], "next_before": next_before, "has_more": has_more}


@app.patch("/api/v1/conversations/{conversation_id}", dependencies=[Depends(require_key)])
def update_conversation(conversation_id: str, body: ConversationUpdate, settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        existing = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not existing: raise fail("not_found", "대화를 찾을 수 없습니다.", 404)
        title = body.title.strip() if body.title is not None else existing["title"]
        response_length = body.response_length or existing["response_length"]
        mode_settings = existing["mode_settings"]
        if body.mode_settings is not None:
            requested_mode_settings = validate_mode_settings(existing["mode"], body.mode_settings)
            if existing["mode"] == "image_prompt":
                image_prompt_settings_not_supported(requested_mode_settings)
            else:
                mode_settings = json.dumps(requested_mode_settings, ensure_ascii=False)
        summary = existing["summary"]
        summary_status = existing["summary_status"]
        summary_error = existing["summary_error"]
        if body.summary is not None:
            if existing["mode"] != "roleplay":
                raise fail("summary_not_supported", "장기 기억 요약은 롤플레잉 모드에서만 사용할 수 있습니다.", 422)
            summary = body.summary.strip()
            summary_status = "idle"
            summary_error = None
            # A background result must never overwrite a manual correction.
            db.execute("""UPDATE summary_jobs SET status='failed',error_message=?,updated_at=?
                          WHERE conversation_id=? AND status IN ('pending','running')""", ("사용자가 요약을 수정했습니다.", now(), conversation_id))
        db.execute("""UPDATE conversations SET title=?,response_length=?,mode_settings=?,summary=?,summary_status=?,summary_error=?,updated_at=?
                      WHERE id=?""", (title, response_length, mode_settings, summary, summary_status, summary_error, now(), conversation_id))
        row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    return conversation_dict(row)


@app.delete("/api/v1/conversations/{conversation_id}", status_code=204, dependencies=[Depends(require_key)])
def delete_conversation(conversation_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        if has_active_image_jobs(db, conversation_id):
            raise fail("active_image_jobs", "진행 중이거나 확인이 필요한 이미지 작업이 있어 대화를 삭제할 수 없습니다.", 409)
        if not db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,)).rowcount: raise fail("not_found", "대화를 찾을 수 없습니다.", 404)


@app.get("/api/v1/conversations/{conversation_id}/messages/{message_id}/image-draft", dependencies=[Depends(require_key)])
def get_image_draft(conversation_id: str, message_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        if not db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
            raise fail("not_found", "대화를 찾을 수 없습니다.", 404)
        draft = get_draft(db, conversation_id, message_id)
    if not draft:
        raise fail("image_draft_not_found", "저장된 이미지 편집 초안이 없습니다.", 404)
    return draft


async def image_model_catalog(settings: Settings) -> dict[str, dict[str, Any]] | None:
    if not settings.imagegen_enabled:
        return None
    try:
        capabilities = await ImageStudioAdapter(settings).capabilities()
    except ImageGenerationUnavailable:
        return None
    # Models with malformed or absent live contracts are excluded from admission.
    return {model["model_id"]: model for model in capabilities["models"] if model.get("contract_matches") is True}


@app.put("/api/v1/conversations/{conversation_id}/messages/{message_id}/image-draft", dependencies=[Depends(require_key)])
async def put_image_draft(conversation_id: str, message_id: str, body: ImageDraftUpdate, settings: Settings = Depends(get_settings)):
    models = await image_model_catalog(settings)
    with connect(settings.database_path) as db:
        try:
            draft = save_draft(db, conversation_id, message_id, body.prompt, body.width, body.height, body.revision, now(), body.generation_config, models, body.preset_name)
        except ImageDraftError as exc:
            raise fail(exc.code, exc.message, exc.http_status)
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), conversation_id))
    return draft


@app.get("/api/v1/conversations/{conversation_id}/image-jobs", dependencies=[Depends(require_key)])
def list_image_jobs(conversation_id: str, limit: int = Query(default=50, ge=1, le=100), settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        if not db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
            raise fail("not_found", "대화를 찾을 수 없습니다.", 404)
        rows = db.execute("SELECT * FROM image_jobs WHERE conversation_id=? ORDER BY created_at DESC,id DESC LIMIT ?", (conversation_id, limit)).fetchall()
    return {"data": [image_job_dict(row) for row in rows]}


def load_image_job(settings: Settings, image_job_id: str):
    with connect(settings.database_path) as db:
        row = db.execute("SELECT * FROM image_jobs WHERE id=?", (image_job_id,)).fetchone()
    if not row:
        raise fail("image_job_not_found", "이미지 작업을 찾을 수 없습니다.", 404)
    return image_job_dict(row)


async def refresh_image_job(settings: Settings, image_job: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Synchronize one accepted Studio job while keeping brief UI polling local."""
    upstream_job_id = image_job["upstream_job_id"]
    if not upstream_job_id:
        return image_job, False
    last_poll = app.state.image_job_poll_times.get(image_job["id"])
    current = time.monotonic()
    if last_poll is not None and current - last_poll < settings.image_job_poll_min_interval_seconds:
        return image_job, False
    app.state.image_job_poll_times[image_job["id"]] = current
    try:
        upstream = await ImageStudioAdapter(settings).get_job(upstream_job_id)
    except ImageGenerationNotFound:
        with connect(settings.database_path) as db:
            image_job = set_image_job_state(
                db, image_job["id"], "failed", now(), error_code="upstream_job_not_found",
                error_message="이미지 생성 서비스에서 작업을 찾을 수 없습니다.",
            )
        return image_job, True
    except ImageGenerationUnavailable as exc:
        messages = {
            "imagegen_disabled": "이미지 생성 기능이 비활성화되어 있습니다.",
            "imagegen_invalid_response": "이미지 생성 서비스 응답을 확인할 수 없습니다.",
        }
        raise fail("image_generation_unavailable", messages.get(str(exc), "이미지 작업 상태를 지금 확인할 수 없습니다."), 503)
    with connect(settings.database_path) as db:
        image_job = set_image_job_state(
            db, image_job["id"], upstream["status"], now(), upstream_job_id=upstream["upstream_job_id"],
            asset_ids=upstream["asset_ids"], metadata=upstream.get("metadata"),
        )
    return image_job, True


@app.get("/api/v1/image-jobs/{image_job_id}", dependencies=[Depends(require_key)])
async def get_image_job(image_job_id: str, settings: Settings = Depends(get_settings)):
    image_job, refreshed = await refresh_image_job(settings, load_image_job(settings, image_job_id))
    return {"data": image_job, "refreshed": refreshed}


@app.get("/api/v1/image-jobs/{image_job_id}/comparison-record", dependencies=[Depends(require_key)])
def get_image_comparison_record(image_job_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        row = db.execute("SELECT * FROM image_jobs WHERE id=?", (image_job_id,)).fetchone()
        if not row:
            raise fail("image_job_not_found", "이미지 작업을 찾을 수 없습니다.", 404)
        job = image_job_dict(row)
        source = db.execute("""SELECT user_message.content AS user_request, assistant.content AS assistant_content
                               FROM messages AS assistant
                               LEFT JOIN generation_requests AS generation ON generation.assistant_message_id=assistant.id AND generation.status='completed'
                               LEFT JOIN messages AS user_message ON user_message.id=generation.user_message_id
                               WHERE assistant.id=? AND assistant.conversation_id=?""",
                            (job["source_message_id"], job["conversation_id"])).fetchone()
    generated_prompt = None
    generated_negative = None
    if source:
        try:
            generated = json.loads(source["assistant_content"])
            if isinstance(generated, dict):
                if isinstance(generated.get("prompt"), str):
                    generated_prompt = generated["prompt"]
                if isinstance(generated.get("negative_prompt"), str):
                    generated_negative = generated["negative_prompt"]
        except (TypeError, ValueError):
            pass
    config, runtime = job["generation_config"], job["studio_metadata"]
    return {
        "schema_version": 1,
        "job_id": job["id"],
        "conversation_id": job["conversation_id"],
        "original_request": source["user_request"] if source else None,
        "prompt_mode": job["prompt_mode"],
        "generated_prompt": generated_prompt,
        "generated_negative_prompt": generated_negative,
        "submitted_prompt": job["prompt"],
        "submitted_negative_prompt": config.get("negative_prompt"),
        "model": {"model_id": config.get("model_id"), "quantization": runtime.get("quantization"), "version": None},
        "preset_name": job["preset_name"],
        "generation_settings": {"width": job["width"], "height": job["height"], "steps": job["steps"],
                                "cfg_scale": job["cfg_scale"], "requested_seed": config.get("seed", -1) if config else None,
                                "sampling_method": config.get("sampling_method"), "scheduler": config.get("scheduler")},
        "runtime": {"seed": runtime.get("seed"), "started_at": runtime.get("started_at"),
                    "completed_at": runtime.get("completed_at"), "duration_ms": runtime.get("duration_ms"), "lora": runtime.get("lora")},
        "status": job["status"],
        "simulated": config.get("model_id") == "fake",
        "asset_ids": job["asset_ids"],
        "evaluation": job["evaluation"],
        "created_at": job["created_at"],
    }


@app.put("/api/v1/image-jobs/{image_job_id}/evaluation", dependencies=[Depends(require_key)])
def update_image_evaluation(image_job_id: str, body: ImageEvaluationUpdate, settings: Settings = Depends(get_settings)):
    if "\x00" in body.note:
        raise fail("invalid_image_evaluation", "평가 메모에 허용되지 않는 문자가 있습니다.", 422)
    with connect(settings.database_path) as db:
        row = db.execute("SELECT * FROM image_jobs WHERE id=?", (image_job_id,)).fetchone()
        if not row:
            raise fail("image_job_not_found", "이미지 작업을 찾을 수 없습니다.", 404)
        if row["status"] != "completed" or not json.loads(row["asset_ids_json"]):
            raise fail("image_job_not_evaluable", "완료된 이미지 결과만 평가할 수 있습니다.", 409)
        note = body.note.strip()
        db.execute("UPDATE image_jobs SET evaluation_score=?,evaluation_note=?,evaluation_updated_at=? WHERE id=?",
                   (body.score, note, now() if body.score is not None or note else None, image_job_id))
        updated = db.execute("SELECT * FROM image_jobs WHERE id=?", (image_job_id,)).fetchone()
    return {"data": image_job_dict(updated)}


@app.post("/api/v1/image-jobs/{image_job_id}/cancel", status_code=202, dependencies=[Depends(require_key)])
async def cancel_image_job(image_job_id: str, settings: Settings = Depends(get_settings)):
    image_job = load_image_job(settings, image_job_id)
    if not image_job["upstream_job_id"]:
        raise fail("image_job_cancel_unavailable", "제출 확인 전에는 이미지 작업을 취소할 수 없습니다.", 409)
    if image_job["status"] in {"completed", "failed", "cancelled", "interrupted"}:
        raise fail("image_job_not_cancellable", "이미 종료된 이미지 작업은 취소할 수 없습니다.", 409)
    try:
        upstream = await ImageStudioAdapter(settings).cancel_job(image_job["upstream_job_id"])
    except ImageGenerationNotFound:
        with connect(settings.database_path) as db:
            set_image_job_state(
                db, image_job_id, "failed", now(), error_code="upstream_job_not_found",
                error_message="이미지 생성 서비스에서 작업을 찾을 수 없습니다.",
            )
        raise fail("upstream_job_not_found", "이미지 생성 서비스에서 작업을 찾을 수 없습니다.", 404)
    except ImageGenerationRejected:
        raise fail("image_job_cancel_rejected", "이미지 생성 서비스가 취소 요청을 수락하지 않았습니다.", 409)
    except ImageGenerationUnavailable:
        raise fail("image_generation_unavailable", "이미지 작업 취소 상태를 지금 확인할 수 없습니다.", 503)
    with connect(settings.database_path) as db:
        set_image_job_state(
            db, image_job_id, upstream["status"], now(), upstream_job_id=upstream["upstream_job_id"],
            asset_ids=upstream["asset_ids"], metadata=upstream.get("metadata"),
        )
        image_job = mark_image_job_cancel_requested(db, image_job_id, now())
    return {"data": image_job}


@app.get("/api/v1/image-jobs/{image_job_id}/assets/{asset_id}/file", dependencies=[Depends(require_key)])
async def get_image_asset_file(image_job_id: str, asset_id: str, settings: Settings = Depends(get_settings)):
    image_job = load_image_job(settings, image_job_id)
    if asset_id not in image_job["asset_ids"]:
        raise fail("image_asset_not_found", "이 이미지 작업의 결과 자산을 찾을 수 없습니다.", 404)
    try:
        media_type, content = await ImageStudioAdapter(settings).fetch_asset(asset_id, settings.max_image_asset_bytes)
    except ImageGenerationNotFound:
        raise fail("image_asset_not_found", "이미지 생성 서비스에서 결과 자산을 찾을 수 없습니다.", 404)
    except ImageGenerationUnavailable as exc:
        messages = {
            "image_asset_too_large": "결과 이미지 크기가 허용 한도를 초과했습니다.",
            "image_asset_invalid_type": "지원하지 않는 결과 이미지 형식입니다.",
            "imagegen_disabled": "이미지 생성 기능이 비활성화되어 있습니다.",
        }
        code = str(exc)
        raise fail(code if code in {"image_asset_too_large", "image_asset_invalid_type"} else "image_generation_unavailable",
                   messages.get(code, "결과 이미지를 지금 가져올 수 없습니다."), 503)
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


@app.post("/api/v1/conversations/{conversation_id}/image-jobs", status_code=202, dependencies=[Depends(require_key)])
async def create_image_job(conversation_id: str, body: ImageJobCreate, settings: Settings = Depends(get_settings)):
    request_id = str(body.request_id)
    models = await image_model_catalog(settings)
    with connect(settings.database_path) as db:
        try:
            existing = db.execute("SELECT 1 FROM image_jobs WHERE request_id=?", (request_id,)).fetchone()
            snapshot = job_snapshot(
                db, conversation_id, body.source_message_id, body.prompt, body.width, body.height, body.draft_revision, body.generation_config, models, bool(existing),
            )
            if not existing and active_image_job_count(db) >= settings.max_image_jobs:
                raise fail("image_job_limit", "이미지 작업 대기열 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.", 429)
            image_job, replayed = create_or_replay_image_job(db, request_id, snapshot, now())
        except ImageDraftError as exc:
            raise fail(exc.code, exc.message, exc.http_status)

    if replayed:
        if image_job["status"] == "submission_unknown":
            try:
                matches = await ImageStudioAdapter(settings).find_jobs_by_client_reference(f"llm-client:{image_job['id']}")
            except ImageGenerationUnavailable:
                matches = []
            if len(matches) == 1:
                with connect(settings.database_path) as db:
                    image_job = set_image_job_state(
                        db, image_job["id"], matches[0]["status"], now(), upstream_job_id=matches[0]["upstream_job_id"],
                    )
        return {"data": image_job, "replayed": True}

    payload = {
        "job_type": "image.generate",
        "source": "nsfw",
        "seed": -1,
        "priority": 0,
        "batch_count": 1,
        "preset": image_job["preset_name"] if image_job["preset_name"] in {"fast", "balanced", "quality"} else "balanced",
        "client_reference": f"llm-client:{image_job['id']}",
        **image_job["generation_config"],
    }
    try:
        upstream = await ImageStudioAdapter(settings).submit_job(payload)
    except ImageGenerationSubmissionUnknown:
        with connect(settings.database_path) as db:
            image_job = set_image_job_state(
                db, image_job["id"], "submission_unknown", now(), error_code="submission_unknown",
                error_message="제출 결과를 확인해야 합니다.",
            )
        return {"data": image_job, "replayed": False}
    except ImageGenerationRejected:
        with connect(settings.database_path) as db:
            set_image_job_state(
                db, image_job["id"], "failed", now(), error_code="image_generation_rejected",
                error_message="이미지 생성 서비스가 요청을 수락하지 않았습니다.",
            )
        raise fail("image_generation_rejected", "이미지 생성 서비스가 요청을 수락하지 않았습니다.", 422)
    except ImageGenerationUnavailable:
        with connect(settings.database_path) as db:
            set_image_job_state(
                db, image_job["id"], "failed", now(), error_code="image_generation_unavailable",
                error_message="이미지 생성 서비스를 지금 사용할 수 없습니다.",
            )
        raise fail("image_generation_unavailable", "이미지 생성 서비스를 지금 사용할 수 없습니다.", 503)

    with connect(settings.database_path) as db:
        image_job = set_image_job_state(
            db, image_job["id"], upstream["status"], now(), upstream_job_id=upstream["upstream_job_id"],
        )
    return {"data": image_job, "replayed": False}


@app.get("/api/v1/conversations/{conversation_id}/generations/{generation_id}", dependencies=[Depends(require_key)])
def get_generation(conversation_id: str, generation_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        row = db.execute("SELECT * FROM generation_requests WHERE id=? AND conversation_id=?", (generation_id, conversation_id)).fetchone()
    if not row:
        raise fail("not_found", "생성 요청을 찾을 수 없습니다.", 404)
    return generation_dict(row)


@app.delete("/api/v1/conversations/{conversation_id}/generations/{generation_id}", dependencies=[Depends(require_key)])
def cancel_generation(conversation_id: str, generation_id: str, settings: Settings = Depends(get_settings)):
    with connect(settings.database_path) as db:
        row = db.execute("SELECT * FROM generation_requests WHERE id=? AND conversation_id=?", (generation_id, conversation_id)).fetchone()
        if not row:
            raise fail("not_found", "생성 요청을 찾을 수 없습니다.", 404)
        if row["status"] == "pending":
            db.execute("UPDATE generation_requests SET status='cancelled',error_code='cancelled',error_message='사용자가 요청을 취소했습니다.',updated_at=? WHERE id=?", (now(), generation_id))
            app.state.cancelled_generations.add(generation_id)
            row = db.execute("SELECT * FROM generation_requests WHERE id=?", (generation_id,)).fetchone()
    return generation_dict(row)


def normalize_image_response(result: Any, image_model_ids: list[str] | None, image_models: dict[str, dict[str, Any]] | None,
                             structured_intent: dict[str, Any] | None, body: MessageCreate) -> tuple[str, str | None]:
    try:
        choice = result["choices"][0]
        finish_reason = choice.get("finish_reason")
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise TypeError
        content = content.strip()
    except (TypeError, KeyError, IndexError, AttributeError):
        raise ImagePromptValidationError("image_prompt_invalid_schema", "LLM 응답 형식이 올바르지 않습니다.") from None
    if finish_reason == "length":
        raise ImagePromptValidationError("image_prompt_output_truncated", "이미지 설정 JSON이 출력 길이 제한으로 완성되지 않았습니다. 다시 시도해 주세요.")
    if not content:
        raise ImagePromptValidationError("image_prompt_empty_output", "이미지 설정 JSON이 생성되지 않았습니다. 다시 시도해 주세요.")
    parsed_config = apply_balanced_preset(content, image_model_ids, image_models)
    if structured_intent and any(text not in parsed_config.prompt for text in structured_intent["exact_text"]):
        raise ImagePromptValidationError("image_prompt_exact_text_missing", "지정한 문구가 프롬프트에 포함되지 않았습니다. 다시 시도해 주세요.")
    raw = json.loads(content)
    raw_reason = raw.get("recommendation_reason", "")
    if not isinstance(raw_reason, str) or len(raw_reason) > 500 or "\x00" in raw_reason:
        raise ImagePromptValidationError("image_prompt_invalid_schema", "모델 추천 이유가 올바르지 않습니다.")
    result_config = {**parsed_config.to_dict(), "recommendation_reason": raw_reason.strip(), "prompt_mode": body.prompt_mode,
                     "model_selection": "manual" if body.image_model_id else "recommend", "preset_name": "balanced"}
    raw_negative = raw.get("negative_prompt", "")
    if isinstance(raw_negative, str) and raw_negative.strip() and "negative_prompt" not in parsed_config.options:
        result_config["negative_prompt_omitted"] = True
    if structured_intent is not None:
        result_config["structured_intent"] = structured_intent
    return json.dumps(result_config, ensure_ascii=False, separators=(",", ":")), finish_reason


@app.post("/api/v1/conversations/{conversation_id}/messages", dependencies=[Depends(require_key)])
async def send_message(conversation_id: str, body: MessageCreate, request: Request, settings: Settings = Depends(get_settings)):
    # Structured image-prompt output must arrive as one complete JSON object.
    # Reject streaming before begin_generation so it cannot leave a user message
    # or generation row behind.
    if body.stream:
        with connect(settings.database_path) as db:
            requested_conversation = db.execute("SELECT mode FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not requested_conversation:
            raise fail("not_found", "대화를 찾을 수 없습니다.", 404)
        if requested_conversation["mode"] == "image_prompt":
            raise fail("image_prompt_stream_unsupported", "이미지 프롬프트 모드는 완성된 설정 JSON만 반환하므로 스트리밍을 지원하지 않습니다.", 422)

    _conversation, generation, history, completed_assistant = begin_generation(conversation_id, body, settings)
    is_image_prompt = _conversation["mode"] == "image_prompt"
    app.state.cancelled_generations.discard(generation["id"])
    if completed_assistant:
        result = {"generation": generation_dict(generation), "assistant_message": message_dict(completed_assistant), "replayed": True}
        if body.stream:
            async def replay_stream():
                yield f"event: done\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
            return StreamingResponse(replay_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return result

    image_model_ids = None
    image_models = None
    prompt_conversation = conversation_dict(_conversation)
    if is_image_prompt:
        prompt_conversation["prompt_mode"] = body.prompt_mode
    if is_image_prompt and settings.imagegen_enabled:
        try:
            capabilities = await ImageStudioAdapter(settings).capabilities()
            runnable = [model["model_id"] for model in capabilities["models"] if model["execution_enabled"]]
            if body.image_model_id != "fake":
                runnable = [model_id for model_id in runnable if model_id != "fake"]
            if capabilities["available"] and runnable:
                image_model_ids = runnable
                image_models = {model["model_id"]: model for model in capabilities["models"] if model["model_id"] in runnable}
                prompt_conversation["image_model_ids"] = runnable
                prompt_conversation["image_models"] = image_models
        except ImageGenerationUnavailable:
            pass  # Standalone prompt writing still works while Studio is offline.
    if is_image_prompt and body.image_model_id:
        if body.image_model_id == "fake" and (image_models is None or "fake" not in image_models):
            message = "CPU 테스트 엔진을 사용할 수 없습니다. Studio 테스트 설정을 확인해 주세요."
            mark_generation_terminal(settings, generation["id"], "failed", "image_model_unavailable", message)
            raise fail("image_model_unavailable", message, 422)
        if image_model_ids is not None and body.image_model_id not in image_model_ids:
            message = "선택한 이미지 모델이 현재 실행 가능하지 않습니다. 다른 모델을 선택해 주세요."
            mark_generation_terminal(settings, generation["id"], "failed", "image_model_unavailable", message)
            raise fail("image_model_unavailable", message, 422)
        image_model_ids = [body.image_model_id]
        prompt_conversation["image_model_ids"] = image_model_ids
    if is_image_prompt and body.prompt_mode == "original":
        if generation["id"] in app.state.cancelled_generations or await request.is_disconnected():
            mark_generation_terminal(settings, generation["id"], "cancelled", "cancelled", "사용자가 요청을 취소했습니다.")
            raise fail("cancelled", "요청이 취소되었습니다.", 499)
        try:
            config = apply_balanced_preset(json.dumps({"model_id": body.image_model_id, "prompt": body.content.strip()}), image_model_ids, image_models)
        except ImagePromptValidationError as exc:
            mark_generation_terminal(settings, generation["id"], "failed", exc.code, exc.message)
            raise fail(exc.code, exc.message, 422)
        mark_generation_started(settings, generation["id"])
        content = json.dumps({**config.to_dict(), "prompt_mode": "original", "model_selection": "manual", "preset_name": "balanced"}, ensure_ascii=False, separators=(",", ":"))
        assistant = complete_generation(settings, generation["id"], content, "stop", None)
        if not assistant:
            raise fail("cancelled", "요청이 취소되었습니다.", 409)
        with connect(settings.database_path) as db:
            finished = db.execute("SELECT * FROM generation_requests WHERE id=?", (generation["id"],)).fetchone()
            user_message = db.execute("SELECT id,role,content,created_at FROM messages WHERE id=?", (generation["user_message_id"],)).fetchone()
        return {"generation": generation_dict(finished), "user_message": message_dict(user_message), "assistant_message": message_dict(assistant), "usage": None}

    try:
        model_id = await resolve_gate_model(settings)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "gate_unavailable", "message": "모델 정보를 지금 조회할 수 없습니다."}
        mark_generation_terminal(settings, generation["id"], "failed", detail["code"], detail["message"])
        raise
    structured_intent = None
    prompt_content = body.content.strip()
    if is_image_prompt and body.prompt_mode == "ai_optimized":
        try:
            if len(prompt_content) > 8000:
                raise fail("invalid_image_request", "이미지 요청은 8,000자를 초과할 수 없습니다.", 422)
            if generation["id"] in app.state.cancelled_generations or await request.is_disconnected():
                raise fail("cancelled", "요청이 취소되었습니다.", 499)
            analysis = await analyze_image_intent(ImageIntentRequest(content=prompt_content), settings)
            structured_intent = analysis["intent"]
            if generation["id"] in app.state.cancelled_generations or await request.is_disconnected():
                raise fail("cancelled", "요청이 취소되었습니다.", 499)
            prompt_content = prompt_with_intent(prompt_content, structured_intent)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"code": "image_intent_failed", "message": "이미지 요청 분석에 실패했습니다."}
            terminal = "cancelled" if detail["code"] == "cancelled" else "failed"
            mark_generation_terminal(settings, generation["id"], terminal, detail["code"], detail["message"])
            raise
        except asyncio.CancelledError:
            mark_generation_terminal(settings, generation["id"], "cancelled", "cancelled", "사용자가 요청을 취소했습니다.")
            raise
    try:
        prompt = build_prompt(prompt_conversation, [dict(row) for row in history], prompt_content, settings)
    except ContextBudgetExceeded as exc:
        mark_generation_terminal(settings, generation["id"], "failed", "context_budget_exceeded", str(exc))
        raise fail("context_budget_exceeded", str(exc), 422)
    payload = {
        "model": model_id,
        "messages": prompt.messages,
        "stream": False if is_image_prompt else body.stream,
        "temperature": 0.5 if is_image_prompt else body.temperature,
        "max_tokens": prompt.max_tokens,
    }
    prepare_generation(settings, generation["id"], model_id, prompt.max_tokens, prompt.estimated_input_tokens)
    generation.update({"model_id": model_id, "requested_max_tokens": prompt.max_tokens, "estimated_input_tokens": prompt.estimated_input_tokens})
    # Qwen3.5 can consume the full output budget on hidden reasoning. Normal chat
    # therefore defaults to its non-thinking template; this is overridable only
    # on the trusted server via LLM_DISABLE_REASONING.
    if is_image_prompt:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload["response_format"] = image_prompt_response_format(image_model_ids)
    elif settings.llm_disable_reasoning:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if body.stream:
        async def event_stream():
            def event(name: str, data: dict) -> str:
                return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

            content_parts: list[str] = []
            finish_reason: str | None = None
            usage: dict[str, Any] | None = None
            try:
                yield event("status", {"message": "LLM gate 응답을 기다리는 중입니다.", "generation": generation_dict(generation)})
                async with app.state.generation_limiter:
                    mark_generation_started(settings, generation["id"])
                    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                        async with client.stream("POST", f"{settings.llm_gate_url}/v1/chat/completions", json=payload,
                                                 headers={"Authorization": f"Bearer {settings.llm_gate_api_key}"}) as response:
                            response.raise_for_status()
                            async for raw_line in response.aiter_lines():
                                if generation["id"] in app.state.cancelled_generations or await request.is_disconnected():
                                    mark_generation_terminal(settings, generation["id"], "cancelled", "cancelled", "사용자가 요청을 취소했습니다.")
                                    app.state.cancelled_generations.discard(generation["id"])
                                    return
                                if not raw_line.startswith("data:"):
                                    continue
                                data = raw_line[5:].strip()
                                if data == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data)
                                except json.JSONDecodeError:
                                    continue
                                choice = (chunk.get("choices") or [{}])[0]
                                finish_reason = choice.get("finish_reason") or finish_reason
                                if isinstance(chunk.get("usage"), dict):
                                    usage = chunk["usage"]
                                delta = choice.get("delta") or {}
                                piece = delta.get("content") or ""
                                if piece:
                                    content_parts.append(piece)
                                    yield event("delta", {"content": piece})
                content = "".join(content_parts).strip()
                if not content:
                    error_code = "output_length_exceeded" if finish_reason == "length" else "incomplete_generation"
                    error_message = "답변 길이 제한에 도달하여 최종 답변이 생성되지 않았습니다. 출력 길이를 늘린 뒤 다시 시도해 주세요." if finish_reason == "length" else "최종 답변이 생성되지 않았습니다. 생성 토큰을 늘린 뒤 다시 시도해 주세요."
                    mark_generation_terminal(settings, generation["id"], "failed", error_code, error_message, finish_reason)
                    yield event("error", {"code": error_code, "message": error_message})
                    return
                assistant = complete_generation(settings, generation["id"], content, finish_reason, usage)
                if not assistant:
                    yield event("error", {"code": "cancelled", "message": "요청이 취소되었습니다."})
                    return
                summary_job_id = queue_roleplay_summary(settings, generation["conversation_id"])
                if summary_job_id:
                    start_summary_task(settings, summary_job_id)
                with connect(settings.database_path) as db:
                    finished = db.execute("SELECT * FROM generation_requests WHERE id=?", (generation["id"],)).fetchone()
                yield event("done", {"generation": generation_dict(finished), "assistant_message": message_dict(assistant)})
            except asyncio.CancelledError:
                mark_generation_terminal(settings, generation["id"], "cancelled", "cancelled", "사용자가 요청을 취소했습니다.")
                raise
            except httpx.TimeoutException:
                mark_generation_terminal(settings, generation["id"], "failed", "generation_timeout", "응답 생성 시간이 초과되었습니다. 다시 시도해 주세요.")
                yield event("error", {"code": "generation_timeout", "message": "응답 생성 시간이 초과되었습니다. 다시 시도해 주세요."})
            except httpx.HTTPError:
                mark_generation_terminal(settings, generation["id"], "failed", "gate_unavailable", "LLM gate에 연결할 수 없습니다. 대기열 또는 일시중지 상태일 수 있습니다.")
                yield event("error", {"code": "gate_unavailable", "message": "LLM gate에 연결할 수 없습니다. 대기열 또는 일시중지 상태일 수 있습니다."})

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    last_image_finish_reason = None
    try:
        async with app.state.generation_limiter:
            if generation["id"] in app.state.cancelled_generations or await request.is_disconnected():
                mark_generation_terminal(settings, generation["id"], "cancelled", "cancelled", "사용자가 요청을 취소했습니다.")
                raise fail("cancelled", "요청이 취소되었습니다.", 499)
            mark_generation_started(settings, generation["id"])
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                for attempt in range(2 if is_image_prompt else 1):
                    if generation["id"] in app.state.cancelled_generations or await request.is_disconnected():
                        mark_generation_terminal(settings, generation["id"], "cancelled", "cancelled", "사용자가 요청을 취소했습니다.")
                        raise fail("cancelled", "요청이 취소되었습니다.", 499)
                    response = await client.post(f"{settings.llm_gate_url}/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {settings.llm_gate_api_key}"})
                    response.raise_for_status()
                    try:
                        if is_image_prompt:
                            last_image_finish_reason = None
                        try:
                            result = response.json()
                        except ValueError:
                            if not is_image_prompt:
                                raise
                            raise ImagePromptValidationError("image_prompt_invalid_json", "LLM 응답 JSON 형식이 올바르지 않습니다.") from None
                        if is_image_prompt:
                            if isinstance(result, dict) and isinstance(result.get("choices"), list) and result["choices"] and isinstance(result["choices"][0], dict):
                                candidate_reason = result["choices"][0].get("finish_reason")
                                last_image_finish_reason = candidate_reason if isinstance(candidate_reason, str) and len(candidate_reason) <= 100 else None
                            content, finish_reason = normalize_image_response(result, image_model_ids, image_models, structured_intent, body)
                        break
                    except ImagePromptValidationError:
                        if attempt == 1:
                            raise
    except ImagePromptValidationError as exc:
        mark_generation_terminal(settings, generation["id"], "failed", exc.code, exc.message, last_image_finish_reason)
        raise fail(exc.code, exc.message, 422)
    except asyncio.CancelledError:
        mark_generation_terminal(settings, generation["id"], "cancelled", "cancelled", "사용자가 요청을 취소했습니다.")
        raise
    except HTTPException: raise
    except httpx.TimeoutException:
        mark_generation_terminal(settings, generation["id"], "failed", "generation_timeout", "응답 생성 시간이 초과되었습니다. 다시 시도해 주세요.")
        raise fail("generation_timeout", "응답 생성 시간이 초과되었습니다. 다시 시도해 주세요.", 504)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if is_image_prompt and status_code in {400, 422}:
            message = "LLM gate가 이미지 프롬프트용 구조화된 출력을 지원하지 않습니다. gate 설정을 확인해 주세요."
            mark_generation_terminal(settings, generation["id"], "failed", "image_prompt_schema_unsupported", message)
            raise fail("image_prompt_schema_unsupported", message, 502)
        if status_code in {401, 403}:
            message = "LLM gate 인증 설정을 확인해 주세요."
            mark_generation_terminal(settings, generation["id"], "failed", "gate_auth_failed", message)
            raise fail("gate_auth_failed", message, 502)
        mark_generation_terminal(settings, generation["id"], "failed", "gate_unavailable", "LLM gate에 연결할 수 없습니다. 대기열 또는 일시중지 상태일 수 있습니다.")
        raise fail("gate_unavailable", "LLM gate에 연결할 수 없습니다. 대기열 또는 일시중지 상태일 수 있습니다.", 503)
    except httpx.HTTPError:
        mark_generation_terminal(settings, generation["id"], "failed", "gate_unavailable", "LLM gate에 연결할 수 없습니다. 대기열 또는 일시중지 상태일 수 있습니다.")
        raise fail("gate_unavailable", "LLM gate에 연결할 수 없습니다. 대기열 또는 일시중지 상태일 수 있습니다.", 503)
    if not is_image_prompt:
        choice = (result.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = (message.get("content") or "").strip()
        finish_reason = choice.get("finish_reason")
    if not content:
        error_code = "output_length_exceeded" if finish_reason == "length" else "incomplete_generation"
        error_message = "답변 길이 제한에 도달하여 최종 답변이 생성되지 않았습니다. 출력 길이를 늘린 뒤 다시 시도해 주세요." if finish_reason == "length" else "최종 답변이 생성되지 않았습니다. 생성 토큰을 늘린 뒤 다시 시도해 주세요."
        mark_generation_terminal(settings, generation["id"], "failed", error_code, error_message, finish_reason)
        raise fail(error_code, error_message, 422)
    if generation["id"] in app.state.cancelled_generations or await request.is_disconnected():
        mark_generation_terminal(settings, generation["id"], "cancelled", "cancelled", "사용자가 요청을 취소했습니다.")
        raise fail("cancelled", "요청이 취소되었습니다.", 499)
    assistant = complete_generation(settings, generation["id"], content, finish_reason, result.get("usage") if isinstance(result.get("usage"), dict) else None)
    if not assistant:
        raise fail("cancelled", "요청이 취소되었습니다.", 409)
    summary_job_id = queue_roleplay_summary(settings, generation["conversation_id"])
    if summary_job_id:
        start_summary_task(settings, summary_job_id)
    with connect(settings.database_path) as db:
        finished = db.execute("SELECT * FROM generation_requests WHERE id=?", (generation["id"],)).fetchone()
        user_message = db.execute("SELECT id,role,content,created_at FROM messages WHERE id=?", (generation["user_message_id"],)).fetchone()
    return {"generation": generation_dict(finished), "user_message": message_dict(user_message), "assistant_message": message_dict(assistant), "usage": result.get("usage")}


@app.get("/api/v1/admin/gate-status", dependencies=[Depends(require_key)])
async def gate_status(settings: Settings = Depends(get_settings)):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{settings.llm_gate_url}/health", headers={"Authorization": f"Bearer {settings.llm_gate_api_key}"})
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError: raise fail("gate_unavailable", "LLM gate 상태를 조회할 수 없습니다.", 503)


@app.put("/api/v1/admin/gate-control", dependencies=[Depends(require_key)])
async def gate_control(body: GateControl, settings: Settings = Depends(get_settings)):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.put(f"{settings.llm_gate_url}/control", json=body.model_dump(exclude_none=True), headers={"Authorization": f"Bearer {settings.llm_gate_api_key}"})
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError: raise fail("gate_unavailable", "LLM gate 제어 요청에 실패했습니다.", 503)
