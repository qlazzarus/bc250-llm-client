"""Validation for autonomous Wiki memory candidates."""
from typing import Literal
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["none", "create", "append", "replace"]
    source_text: str = Field(max_length=800)
    title: str = Field(max_length=120)
    document_id: int | None
    old_text: str | None = Field(max_length=800)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action != "none" and (not self.source_text.strip() or "\x00" in self.source_text):
            raise ValueError("source text is required")
        if self.action == "create" and (not self.title.strip() or self.document_id is not None):
            raise ValueError("invalid create")
        if self.action in {"append", "replace"} and (self.document_id is None or self.document_id < 1):
            raise ValueError("invalid append")
        if self.action == "replace" and (not self.old_text or not self.old_text.strip()):
            raise ValueError("original text is required")
        return self


def parse_decision(raw: str, user_message: str, documents: list[dict], previous_user_messages: list[str] | None = None) -> MemoryDecision:
    decision = MemoryDecision.model_validate_json(raw)
    if decision.action != "none" and not any(decision.source_text in message for message in [user_message, *(previous_user_messages or [])]):
        raise ValueError("memory must be an exact quotation from the user")
    if decision.action in {"append", "replace"} and decision.document_id not in {d.get("id") for d in documents}:
        raise ValueError("document was not in search results")
    return decision


def explicit_memory_request(text: str) -> bool:
    """Recognize common direct requests; ambiguous language still uses model judgment."""
    return bool(re.search(r"(?:기억해\s*(?:줘|두|라)?|기억하(?:고|여|도록)|기억(?:에|으로)?\s*(?:저장|남겨|기록)|(?:위키|wiki|기억)에?\s*(?:저장|기록|추가|수정)|(?:저장|기록)해\s*(?:줘|두))", text, re.IGNORECASE))


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
