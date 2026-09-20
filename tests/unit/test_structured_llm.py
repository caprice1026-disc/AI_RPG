"""永続予約を通る構造化出力adapterとFake transport。"""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_rpg.llm import (
    CallBudgetExceeded,
    DevelopmentFakeTransport,
    ScriptedFakeTransport,
    StructuredOutputAdapter,
    StructuredRequest,
)


@pytest.mark.asyncio
async def test_adapter_reserves_before_transport_and_validates_output() -> None:
    order: list[str] = []

    async def reserve() -> bool:
        order.append("reserve")
        return True

    transport = ScriptedFakeTransport([{"value": 7}])
    adapter = StructuredOutputAdapter(transport, reserve)

    result = await adapter.generate(
        StructuredRequest(
            model_id="fake",
            purpose="intent",
            system_instruction="test",
            input_data="{}",
            output_adapter=TypeAdapter(dict[str, int]),
        )
    )

    assert result == {"value": 7}
    assert order == ["reserve"]
    assert transport.request_count == 1
    assert transport.calls[0].purpose == "intent"
    assert transport.calls[0].output_schema["type"] == "object"


@pytest.mark.asyncio
async def test_adapter_does_not_call_transport_when_db_reservation_fails() -> None:
    reserve = AsyncMock(return_value=False)
    transport = ScriptedFakeTransport([{"value": 7}])

    with pytest.raises(CallBudgetExceeded):
        await StructuredOutputAdapter(transport, reserve).generate(
            StructuredRequest(
                model_id="fake",
                purpose="intent",
                system_instruction="test",
                input_data="{}",
                output_adapter=TypeAdapter(dict[str, int]),
            )
        )

    assert transport.request_count == 0


@pytest.mark.asyncio
async def test_invalid_fake_output_still_consumes_one_reserved_call() -> None:
    reserve = AsyncMock(return_value=True)
    transport = ScriptedFakeTransport([{"value": "invalid"}])

    with pytest.raises(ValidationError):
        await StructuredOutputAdapter(transport, reserve).generate(
            StructuredRequest(
                model_id="fake",
                purpose="intent",
                system_instruction="test",
                input_data="{}",
                output_adapter=TypeAdapter(dict[str, int]),
            )
        )

    reserve.assert_awaited_once()
    assert transport.request_count == 1


@pytest.mark.asyncio
async def test_fake_transport_can_script_timeout() -> None:
    transport = ScriptedFakeTransport([TimeoutError("scripted timeout")])

    with pytest.raises(TimeoutError, match="scripted timeout"):
        await transport.request("fake", "intent", "test", "{}", {"type": "object"})

    assert transport.request_count == 1


@pytest.mark.asyncio
async def test_fake_transport_can_script_transient_failure() -> None:
    transport = ScriptedFakeTransport([ConnectionError("provider unavailable")])

    with pytest.raises(ConnectionError, match="provider unavailable"):
        await transport.request(
            "fake", "intent", "test", "{}", {"type": "object"}
        )

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
    transport = DevelopmentFakeTransport()
    input_data = json.dumps(
        {
            "player_text": player_text,
            "scene_view": {
                "content": json.dumps(
                    {
                        "available_actions": [
                            {"action_ref": action_ref, "label": "利用可能"}
                        ]
                    },
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
    transport = DevelopmentFakeTransport()
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
