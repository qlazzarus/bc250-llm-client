"""Validation for autonomous Wiki memory candidates."""
from typing import Literal
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator

MEMORY_POLICY_VERSION = 1


class MemoryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["none", "create", "append", "replace"]
    kind: Literal["fact", "preference", "plan", "question", "uncertain"]
    reason: Literal["durable", "question", "ambiguous", "not_durable", "sensitive", "insufficient_context"]
    source_text: str = Field(max_length=800)
    source_message_id: str | None
    title: str = Field(max_length=120)
    document_id: int | None
    old_text: str | None = Field(max_length=800)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "none":
            if self.kind not in {"question", "uncertain"}:
                raise ValueError("none requires question or uncertain kind")
            if self.source_message_id is not None:
                raise ValueError("none must not select a source message")
            return self
        if self.kind not in {"fact", "preference", "plan"} or self.reason != "durable":
            raise ValueError("write requires a durable memory kind")
        if not self.source_message_id or not self.source_text.strip() or "\x00" in self.source_text:
            raise ValueError("source message and text are required")
        if self.action == "create" and (not self.title.strip() or self.document_id is not None):
            raise ValueError("invalid create")
        if self.action in {"append", "replace"} and (self.document_id is None or self.document_id < 1):
            raise ValueError("invalid append")
        if self.action == "replace" and (not self.old_text or not self.old_text.strip()):
            raise ValueError("original text is required")
        return self


def parse_decision(raw: str, user_message: str, documents: list[dict], *, user_message_id: str,
                   previous_user_messages: list[dict[str, str]] | None = None) -> MemoryDecision:
    decision = MemoryDecision.model_validate_json(raw)
    sources = {user_message_id: user_message}
    sources.update({message["id"]: message["content"] for message in previous_user_messages or []})
    if decision.action != "none":
        selected = sources.get(decision.source_message_id or "")
        if selected is None or decision.source_text not in selected:
            raise ValueError("memory must be an exact quotation from the selected user message")
    if decision.action in {"append", "replace"} and decision.document_id not in {d.get("id") for d in documents}:
        raise ValueError("document was not in search results")
    return decision


def candidate_text_is_safe(decision: MemoryDecision) -> bool:
    """Reject a model-labelled fact when its quoted text is visibly a request."""
    if decision.action == "none":
        return True
    source = decision.source_text.strip()
    if "?" in source or "？" in source:
        return False
    if re.search(r"(?:추천|알려|설명|비교|찾아)\s*(?:해\s*)?(?:줘|주세요|주실래요?)|(?:무엇|뭐|누구|언제|어디|왜|어떻게|어떤|어느|몇|얼마).*(?:야|예|인가|인가요|나요|까요)[.!]?$", source, re.IGNORECASE):
        return False
    if decision.kind == "plan" and not re.search(r"(?:\d{1,4}\s*(?:년|월|일|시|분)|오늘|내일|모레|다음\s*(?:주|달|달|년)|이번\s*(?:주|달|년))", source):
        return False
    return True


def explicit_memory_request(text: str) -> bool:
    """Recognize common direct requests; ambiguous language still uses model judgment."""
    return bool(re.search(r"(?:기억해\s*(?:줘|두|라)?|기억하(?:고|여|도록)|기억(?:에|으로)?\s*(?:저장|남겨|기록)|(?:위키|wiki|기억)에?\s*(?:저장|기록|추가|수정)|(?:저장|기록)해\s*(?:줘|두))", text, re.IGNORECASE))


def automatic_memory_skip_reason(text: str) -> Literal["memory_opt_out", "obvious_question", "greeting_or_simple_request"] | None:
    """Reject clear non-memory candidates before Wiki search or model inference.

    This is deliberately narrow: messages with a first-person or current-state
    cue remain candidates for the structured extraction step, even when they
    also ask a question or recommendation.
    """
    normalized = " ".join(text.split())
    if not normalized:
        return "greeting_or_simple_request"
    if re.search(r"(?:기억|저장|기록)\s*하지\s*(?:마|말(?:아)?)(?:\s*줘)?|\b(?:do not|don't)\s+(?:remember|save|store)\b", normalized, re.IGNORECASE):
        return "memory_opt_out"
    if re.search(r"(?:기억해\s*줘|저장해\s*줘|기록해\s*줘)\s*라는\s*(?:말|표현).*(?:뭐|무엇|뜻)", normalized, re.IGNORECASE):
        return "obvious_question"
    if explicit_memory_request(normalized):
        return None

    has_fact_cue = bool(re.search(r"(?:나는|내가|제가|저는|지금\s*쓰는|사용\s*(?:중|하고)|좋아(?:해|한다)|계획(?:이야|입니다))", normalized, re.IGNORECASE))
    question_or_request = bool(
        re.search(r"[?？]", normalized)
        or re.search(r"(?:무엇|뭐|누구|언제|어디|왜|어떻게|어떤|어느|몇|얼마).*(?:야|예|인가|인가요|나요|까요)[.!]?$", normalized, re.IGNORECASE)
        or re.fullmatch(r"(?:추천|설명|비교|알려|찾아)\s*(?:해\s*)?(?:줘|주세요|주실래요?)[.!]?", normalized, re.IGNORECASE)
    )
    if question_or_request and not has_fact_cue:
        return "obvious_question"
    if re.fullmatch(r"(?:안녕(?:하세요)?|반가워|hi|hello)[.!]?", normalized, re.IGNORECASE):
        return "greeting_or_simple_request"
    return None


def replace_once(content: str, old_text: str, new_text: str) -> str:
    if not old_text or content.count(old_text) != 1:
        raise ValueError("replacement target must occur exactly once")
    return content.replace(old_text, new_text, 1)


def search_query(question: str, history: list[dict]) -> str:
    """Include recent user context for short follow-up questions."""
    if len(question.strip()) > 40:
        return question[:4000]
    previous = next((str(row.get("content", ""))[:400] for row in reversed(history) if row.get("role") == "user"), "")
    return (question + " " + previous).strip()[:4000]
