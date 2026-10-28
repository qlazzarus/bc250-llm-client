from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

from .image_prompt import text_system_instruction as image_prompt_system_instruction


class ContextBudgetExceeded(ValueError):
    pass


@dataclass(frozen=True)
class PromptPlan:
    messages: list[dict[str, str]]
    max_tokens: int
    estimated_input_tokens: int


@dataclass(frozen=True)
class SummaryCandidate:
    through_message_id: str
    messages: list[dict[str, str]]


def estimate_tokens(text: str) -> int:
    """A conservative fallback until gate-backed token counting is available."""
    return max(1, ceil(len(text.encode("utf-8")) / 2))


def response_tokens(response_length: str, settings: Any) -> int:
    requested = {
        "short": settings.response_length_short_tokens,
        "medium": settings.response_length_medium_tokens,
        "long": settings.response_length_long_tokens,
    }[response_length]
    return min(requested, settings.max_completion_tokens)


def prompt_output_tokens(conversation: dict[str, Any], settings: Any) -> int:
    if conversation["mode"] == "image_prompt":
        return settings.image_prompt_max_tokens
    return response_tokens(conversation["response_length"], settings)


def mode_instruction(conversation: dict[str, Any]) -> str:
    mode = conversation["mode"]
    # Existing image conversations may retain their old UI preferences in the
    # database for compatibility.  They are intentionally never LLM input.
    settings = {} if mode == "image_prompt" else conversation["mode_settings"]
    if mode == "quick":
        return "현재 사용자 질문에만 답하세요. 이전 대화 내용은 참고하지 마세요. 정확하고 간결하게 답하세요."
    if mode == "roleplay":
        fields = [
            ("캐릭터", settings.get("character")),
            ("세계관", settings.get("world")),
            ("말투", settings.get("voice")),
            ("현재 상황", settings.get("scenario")),
            ("규칙", settings.get("rules")),
        ]
        configured = "\n".join(f"{label}: {value}" for label, value in fields if value)
        return f"당신은 롤플레잉을 진행합니다. 설정을 일관되게 지키고 이야기 속 역할로 답하세요.\n{configured}".strip()
    return image_prompt_system_instruction(conversation.get("image_model_ids"), conversation.get("prompt_mode", "ai_optimized"), conversation.get("image_models"))


def to_turns(messages: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    turns: list[list[dict[str, str]]] = []
    for message in messages:
        if message["role"] == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def build_prompt(conversation: dict[str, Any], completed_history: list[dict[str, str]], current_user_content: str, settings: Any) -> PromptPlan:
    max_tokens = prompt_output_tokens(conversation, settings)
    input_budget = settings.llm_context_tokens - max_tokens - settings.llm_context_safety_tokens
    system_messages = [{"role": "system", "content": mode_instruction(conversation)}]
    if conversation["mode"] == "roleplay" and conversation["summary"]:
        system_messages.append({"role": "system", "content": f"이전 대화 요약:\n{conversation['summary']}"})
    current_message = {"role": "user", "content": current_user_content}
    mandatory = system_messages + [current_message]
    mandatory_tokens = sum(estimate_tokens(message["content"]) for message in mandatory)
    if mandatory_tokens > input_budget:
        raise ContextBudgetExceeded("대화 설정 또는 현재 메시지가 컨텍스트 예산을 초과했습니다. 메시지나 설정을 줄여 주세요.")

    # Do not trust a legacy database value here: image prompt generation is
    # deliberately a standalone conversion request.
    if conversation["mode"] == "image_prompt" or conversation["context_policy"] == "none":
        return PromptPlan(mandatory, max_tokens, mandatory_tokens)

    selected_turns: list[list[dict[str, str]]] = []
    used_tokens = mandatory_tokens
    for turn in reversed(to_turns(completed_history)):
        turn_tokens = sum(estimate_tokens(message["content"]) for message in turn)
        if used_tokens + turn_tokens > input_budget:
            break
        selected_turns.append(turn)
        used_tokens += turn_tokens
    selected_history = [message for turn in reversed(selected_turns) for message in turn]
    return PromptPlan(system_messages + selected_history + [current_message], max_tokens, used_tokens)


def summary_candidate(completed_history: list[dict[str, str]], settings: Any) -> SummaryCandidate | None:
    turns = to_turns(completed_history)
    if len(turns) <= settings.roleplay_summary_keep_recent_turns:
        return None
    estimated_tokens = sum(estimate_tokens(message["content"]) for message in completed_history)
    if estimated_tokens < settings.roleplay_summary_trigger_tokens:
        return None
    candidate_turns = turns[:-settings.roleplay_summary_keep_recent_turns]
    # Summary work is itself an LLM request. Process a chronological prefix that
    # fits the confirmed server context, then continue on a later trigger rather
    # than sending an arbitrarily large imported conversation to the gate.
    input_budget = settings.llm_context_tokens - settings.roleplay_summary_max_tokens - settings.llm_context_safety_tokens
    selected_turns: list[list[dict[str, str]]] = []
    used_tokens = 0
    for turn in candidate_turns:
        turn_tokens = sum(estimate_tokens(message["content"]) for message in turn)
        if used_tokens + turn_tokens > input_budget:
            break
        selected_turns.append(turn)
        used_tokens += turn_tokens
    candidate_messages = [message for turn in selected_turns for message in turn]
    if not candidate_messages:
        return None
    through_message_id = candidate_messages[-1].get("id")
    if not through_message_id:
        return None
    return SummaryCandidate(through_message_id=through_message_id, messages=candidate_messages)
