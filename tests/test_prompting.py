from types import SimpleNamespace

import pytest

from backend.app.prompting import ContextBudgetExceeded, build_prompt, summary_candidate


def settings(**overrides):
    defaults = {
        "response_length_short_tokens": 16,
        "response_length_medium_tokens": 32,
        "response_length_long_tokens": 64,
        "max_completion_tokens": 64,
        "image_prompt_max_tokens": 48,
        "llm_context_tokens": 256,
        "llm_context_safety_tokens": 16,
        "roleplay_summary_trigger_tokens": 20,
        "roleplay_summary_keep_recent_turns": 1,
        "roleplay_summary_max_tokens": 64,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def conversation(mode, context_policy, mode_settings=None, response_length="medium", summary=""):
    return {
        "mode": mode,
        "context_policy": context_policy,
        "mode_settings": mode_settings or {},
        "response_length": response_length,
        "summary": summary,
    }


def test_quick_mode_does_not_send_history():
    plan = build_prompt(
        conversation("quick", "none"),
        [{"role": "user", "content": "이전 질문"}, {"role": "assistant", "content": "이전 답변"}],
        "현재 질문",
        settings(),
    )

    assert [message["content"] for message in plan.messages] == [
        "현재 사용자 질문에만 답하세요. 이전 대화 내용은 참고하지 마세요. 정확하고 간결하게 답하세요.",
        "현재 질문",
    ]
    assert plan.max_tokens == 32


def test_roleplay_keeps_whole_recent_turns_and_mode_settings():
    history = [
        {"role": "user", "content": "첫 질문"},
        {"role": "assistant", "content": "첫 답변"},
        {"role": "user", "content": "둘째 질문"},
        {"role": "assistant", "content": "둘째 답변"},
    ]
    plan = build_prompt(
        conversation("roleplay", "recent", {"character": "선장", "world": "화성", "voice": "담담함"}),
        history,
        "현재 질문",
        settings(),
    )

    assert "캐릭터: 선장" in plan.messages[0]["content"]
    assert "세계관: 화성" in plan.messages[0]["content"]
    assert [message["content"] for message in plan.messages[1:]] == ["첫 질문", "첫 답변", "둘째 질문", "둘째 답변", "현재 질문"]


def test_context_budget_rejects_required_prompt_that_cannot_fit():
    with pytest.raises(ContextBudgetExceeded):
        build_prompt(
            conversation("roleplay", "recent", {"world": "가" * 200}),
            [],
            "현재 질문",
            settings(llm_context_tokens=80, response_length_medium_tokens=32, llm_context_safety_tokens=16),
        )


def test_summary_candidate_keeps_recent_complete_turns_outside_summary_range():
    history = [
        {"id": "u1", "role": "user", "content": "첫 장면 " * 4},
        {"id": "a1", "role": "assistant", "content": "첫 응답 " * 4},
        {"id": "u2", "role": "user", "content": "둘째 장면 " * 4},
        {"id": "a2", "role": "assistant", "content": "둘째 응답 " * 4},
    ]

    candidate = summary_candidate(history, settings())

    assert candidate is not None
    assert candidate.through_message_id == "a1"
    assert [message["id"] for message in candidate.messages] == ["u1", "a1"]


def test_image_prompt_is_stateless_and_ignores_legacy_preferences_and_summary():
    history = [
        {"role": "user", "content": "호텔 라운지에서 촬영해줘"},
        {"role": "assistant", "content": "legacy natural-language image prompt"},
        {"role": "user", "content": "조명을 더 어둡게 바꿔줘"},
        {"role": "assistant", "content": '{"model_id":"z-image-nsfw-v2"}'},
    ]
    plan = build_prompt(
        conversation("image_prompt", "recent", {
            "subject": "성인 여성",
            "composition": "portrait",
            "style": "cinematic photo",
            "lighting": "night lounge",
            "palette": "black and gold",
            "negative_prompt": "blurry, watermark",
            "output_language": "Korean",
        }, response_length="long", summary="This roleplay summary must not leak."),
        history,
        "검은 실크 드레스로 변경해줘",
        settings(llm_context_tokens=8192),
    )

    assert plan.max_tokens == 48
    assert "z-image-nsfw-v2" in plan.messages[0]["content"]
    assert "English prompt" in plan.messages[0]["content"]
    assert "Do not produce generation settings" in plan.messages[0]["content"]
    assert "Elements to avoid: blurry, watermark" not in plan.messages[0]["content"]
    assert "output_language" not in plan.messages[0]["content"]
    assert "Previous assistant messages" not in plan.messages[0]["content"]
    assert "Configured preferences" not in plan.messages[0]["content"]
    assert "Avoid adding unnecessary unspecified details" in plan.messages[0]["content"]
    assert "This roleplay summary must not leak." not in [message["content"] for message in plan.messages]
    assert [message["content"] for message in plan.messages[1:]] == ["검은 실크 드레스로 변경해줘"]


def test_image_prompt_context_budget_uses_its_own_reserved_output_tokens():
    with pytest.raises(ContextBudgetExceeded):
        build_prompt(
            conversation("image_prompt", "recent", {"subject": "가" * 80}),
            [],
            "현재 요청",
            settings(llm_context_tokens=100, image_prompt_max_tokens=48, llm_context_safety_tokens=16),
        )
