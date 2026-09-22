"""Agentが型付き契約と一回の要求上限を守ることを確認する。"""

import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_rpg.application.ports.llm import ProviderOutputError, ProviderRefusalError
from ai_rpg.contracts.context import MechanicalInput, NarrativeInput
from ai_rpg.contracts.responses import MechanicalNarrationInput
from ai_rpg.llm.pydantic_ai import PydanticAILLM


def context(max_actions: int = 3) -> dict:
    fragment = dict(source="test", trust_level="trusted", access_scope="public", content="入口")
    return dict(
        player_text="周囲を調べる",
        scene_view=fragment,
        pc_view=fragment,
        recent_messages=[],
        allowed_entity_refs=[],
        output_limits=dict(max_actions=max_actions),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["intent", "narrative", "result_narration"])
async def test_agents_return_typed_output_without_tools_or_extra_requests(purpose: str) -> None:
    calls = []
    expected = {"narration": "静かな入口だ。", "choices": []}
    if purpose != "result_narration":
        expected = {"kind": "clarification_required", "question": "どこを調べますか?"}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        calls.append((messages, info))
        assert info.function_tools == []
        assert info.output_tools == []
        return ModelResponse(
            parts=[TextPart(json.dumps({"result": expected}))], finish_reason="stop"
        )

    llm = PydanticAILLM({"test": FunctionModel(respond)})
    if purpose == "intent":
        result = await llm.extract_intent(
            MechanicalInput(
                **context(),
                supported_action_types=["skill_check"],
                supported_skill_refs=["perception"],
            ),
            model_id="test",
        )
    elif purpose == "narrative":
        result = await llm.generate_narrative(NarrativeInput(**context()), model_id="test")
    else:
        result = await llm.narrate_result(
            MechanicalNarrationInput(
                player_text="周囲を調べる",
                committed_state_version=1,
                resolved_actions=[],
                public_state_after=[],
                allowed_entity_refs=[],
                output_limits={"max_actions": 3},
            ),
            model_id="test",
        )
    assert result.model_dump() == expected
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["not json", '{"result":{"kind":"attack","damage":70}}'])
async def test_invalid_output_is_not_repaired_inside_agent(raw: str) -> None:
    requests = []

    def respond(messages, info):
        requests.append(info)
        return ModelResponse(parts=[TextPart(raw)], finish_reason="stop")

    llm = PydanticAILLM({"test": FunctionModel(respond)})
    with pytest.raises(ProviderOutputError):
        await llm.generate_narrative(NarrativeInput(**context()), model_id="test")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_saved_action_limit_is_sent_and_enforced() -> None:
    requests = []
    action = {"kind": "scenario_action", "action_ref": "enter_chapel"}

    def respond(messages, info):
        requests.append(info)
        return ModelResponse(
            parts=[
                TextPart(
                    json.dumps({"result": {"kind": "action_plan", "actions": [action, action]}})
                )
            ],
            finish_reason="stop",
        )

    llm = PydanticAILLM({"test": FunctionModel(respond)})
    with pytest.raises(ProviderOutputError):
        await llm.extract_intent(
            MechanicalInput(
                **context(1), supported_action_types=["scenario_action"], supported_skill_refs=[]
            ),
            model_id="test",
        )
    assert len(requests) == 1
    assert '"maxItems": 1' in json.dumps(
        requests[0].model_request_parameters.output_object.json_schema
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,error",
    [
        ("length", ProviderOutputError),
        ("content_filter", ProviderRefusalError),
        ("error", ProviderOutputError),
    ],
)
async def test_even_valid_json_from_unfinished_or_refused_response_is_rejected(reason, error):
    def respond(messages, info):
        return ModelResponse(
            parts=[TextPart('{"result":{"kind":"narrative","narration":"入口だ。","choices":[]}}')],
            finish_reason=reason,
        )

    with pytest.raises(error):
        await PydanticAILLM({"test": FunctionModel(respond)}).generate_narrative(
            NarrativeInput(**context()), model_id="test"
        )
