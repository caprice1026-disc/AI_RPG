"""用途別Fakeの出力検証と固定短編の決定的応答。"""

import json

import pytest
from pydantic import ValidationError

from ai_rpg.contracts.context import NarrativeInput
from ai_rpg.llm import (
    DevelopmentFakeLLM,
    ScriptedFakeLLM,
)


@pytest.mark.asyncio
async def test_fake_port_rejects_invalid_typed_output_without_retry() -> None:
    fragment = dict(source="test", trust_level="trusted", access_scope="public", content="入口")
    context = NarrativeInput(
        player_text="話す",
        scene_view=fragment,
        pc_view=fragment,
        recent_messages=[],
        allowed_entity_refs=[],
        output_limits={},
    )
    transport = ScriptedFakeLLM([{"kind": "narrative", "narration": "", "choices": []}])
    with pytest.raises(ValidationError):
        await transport.generate_narrative(context, model_id="fake")
    assert transport.request_count == 1


@pytest.mark.asyncio
async def test_fake_transport_can_script_timeout() -> None:
    transport = ScriptedFakeLLM([TimeoutError("scripted timeout")])

    with pytest.raises(TimeoutError, match="scripted timeout"):
        await transport.request("fake", "intent", "test", "{}", {"type": "object"})

    assert transport.request_count == 1


@pytest.mark.asyncio
async def test_fake_transport_can_script_transient_failure() -> None:
    transport = ScriptedFakeLLM([ConnectionError("provider unavailable")])

    with pytest.raises(ConnectionError, match="provider unavailable"):
        await transport.request("fake", "intent", "test", "{}", {"type": "object"})

    assert transport.request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("player_text", "action_ref", "expected"),
    [
        (
            "礼拝堂に入る",
            "enter_chapel",
            {"kind": "scenario_action", "action_ref": "enter_chapel"},
        ),
        (
            "広間を調べる",
            "search_hall",
            {
                "kind": "skill_check",
                "skill_ref": "perception",
                "objective": "広間を調べる",
                "target_ref": None,
            },
        ),
        (
            "守衛と交渉する",
            "negotiate_guard",
            {
                "kind": "skill_check",
                "skill_ref": "persuasion",
                "objective": "守衛と交渉する",
                "target_ref": None,
            },
        ),
        (
            "聖印へ忍び寄る",
            "sneak_to_relic",
            {
                "kind": "skill_check",
                "skill_ref": "stealth",
                "objective": "聖印へ忍び寄る",
                "target_ref": None,
            },
        ),
        (
            "ゴブリンを攻撃する",
            "defeat_guard",
            {
                "kind": "attack",
                "target_ref": "goblin",
                "weapon_ref": "iron_sword",
            },
        ),
        (
            "撤退する",
            "retreat",
            {"kind": "scenario_action", "action_ref": "retreat"},
        ),
    ],
)
async def test_development_fake_uses_only_available_scenario_actions(
    player_text: str,
    action_ref: str,
    expected: dict[str, object],
) -> None:
    transport = DevelopmentFakeLLM()
    input_data = json.dumps(
        {
            "player_text": player_text,
            "scene_view": {
                "content": json.dumps(
                    {"available_actions": [{"action_ref": action_ref, "label": "利用可能"}]},
                    ensure_ascii=False,
                )
            },
        },
        ensure_ascii=False,
    )

    decision = await transport.request("fake", "intent", "test", input_data, {})

    assert decision == {"kind": "action_plan", "actions": [expected]}


@pytest.mark.asyncio
async def test_development_fake_does_not_invent_unavailable_scenario_action() -> None:
    transport = DevelopmentFakeLLM()
    input_data = json.dumps(
        {
            "player_text": "撤退する",
            "scene_view": {
                "content": json.dumps(
                    {
                        "available_actions": [
                            {"action_ref": "enter_chapel", "label": "礼拝堂に入る"}
                        ]
                    },
                    ensure_ascii=False,
                )
            },
        },
        ensure_ascii=False,
    )

    decision = await transport.request("fake", "intent", "test", input_data, {})

    assert decision == {
        "kind": "clarification_required",
        "question": "現在の場面で可能な行動を指定してください。",
    }
