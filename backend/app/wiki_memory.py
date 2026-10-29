"""Validation for autonomous Wiki memory candidates."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["none", "create", "append"]
    source_text: str = Field(max_length=800)
    title: str = Field(max_length=120)
    document_id: int | None

    @model_validator(mode="after")
    def validate_action(self):
        if self.action != "none" and (not self.source_text.strip() or "\x00" in self.source_text):
            raise ValueError("source text is required")
        if self.action == "create" and (not self.title.strip() or self.document_id is not None):
            raise ValueError("invalid create")
        if self.action == "append" and (self.document_id is None or self.document_id < 1):
            raise ValueError("invalid append")
        return self


def parse_decision(raw: str, user_message: str, documents: list[dict]) -> MemoryDecision:
    decision = MemoryDecision.model_validate_json(raw)
    if decision.action != "none" and decision.source_text not in user_message:
        raise ValueError("memory must be an exact quotation from the user")
    if decision.action == "append" and decision.document_id not in {d.get("id") for d in documents}:
        raise ValueError("document was not in search results")
    return decision
