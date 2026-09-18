"""OpenAI Responses API adapterのHTTP contract。"""

import json

import httpx
import pytest
from pydantic import TypeAdapter

from ai_rpg.contracts import make_decision_types
from ai_rpg.contracts.responses import MechanicalNarrationDraft
from ai_rpg.llm import (
    OpenAIResponsesTransport,
    ProviderHTTPError,
    ProviderOutputError,
    ProviderRefusalError,
    StructuredOutputAdapter,
    StructuredRequest,
)

pytestmark = pytest.mark.contract


def _schema_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for nested in value.values() for key in _schema_keys(nested)
        }
    if isinstance(value, list):
        return {key for nested in value for key in _schema_keys(nested)}
    return set()


@pytest.mark.asyncio
async def test_openai_transport_sends_one_strict_responses_request() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"result":{"value":7}}',
                            }
                        ],
                    }
                ],
            },
        )

    transport = OpenAIResponsesTransport(
        api_key="test-secret",
        timeout_seconds=17,
        base_url="https://openai.invalid/v1",
        http_transport=httpx.MockTransport(respond),
    )

    result = await transport.request(
        "gpt-test",
        "intent",
        "Return an intent.",
        '{"player_text":"look"}',
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )

    assert result == {"value": 7}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://openai.invalid/v1/responses"
    assert request.headers["Authorization"] == "Bearer test-secret"
    assert request.extensions["timeout"] == {
        "connect": 17.0,
        "read": 17.0,
        "write": 17.0,
        "pool": 17.0,
    }
    assert json.loads(request.content) == {
        "model": "gpt-test",
        "instructions": "Return an intent.",
        "input": '{"player_text":"look"}',
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "ai_rpg_intent",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "result": {
                            "type": "object",
                            "properties": {"value": {"type": "integer"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        }
                    },
                    "required": ["result"],
                    "additionalProperties": False,
                },
            }
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purpose", "output_adapter", "provider_output"),
    [
        pytest.param(
            "intent",
            make_decision_types(3)[0],
            {
                "kind": "resolution_required",
                "actions": [
                    {"kind": "attack", "target_ref": "goblin", "weapon_ref": None}
                ],
            },
            id="narrative-decision",
        ),
        pytest.param(
            "intent",
            make_decision_types(3)[1],
            {
                "kind": "action_plan",
                "actions": [
                    {
                        "kind": "use_item",
                        "item_ref": "healing_potion",
                        "target_ref": None,
                    }
                ],
            },
            id="mechanical-decision",
        ),
        pytest.param(
            "result_narration",
            TypeAdapter(MechanicalNarrationDraft),
            {"narration": "3ダメージを与えた。", "choices": []},
            id="mechanical-narration",
        ),
    ],
)
async def test_openai_transport_converts_real_worker_schema_to_supported_object(
    purpose: str,
    output_adapter: TypeAdapter[object],
    provider_output: dict[str, object],
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"result": provider_output}),
                            }
                        ],
                    }
                ],
            },
        )

    async def reserve() -> bool:
        return True

    transport = OpenAIResponsesTransport(
        "test-secret", 10, http_transport=httpx.MockTransport(respond)
    )
    result = await StructuredOutputAdapter(transport, reserve).generate(
        StructuredRequest(
            model_id="gpt-test",
            purpose=purpose,  # type: ignore[arg-type]
            system_instruction="Return the requested object.",
            input_data="{}",
            output_adapter=output_adapter,
        )
    )

    assert result.model_dump(mode="json") == provider_output
    sent_schema = json.loads(requests[0].content)["text"]["format"]["schema"]
    assert sent_schema["type"] == "object"
    assert sent_schema["required"] == ["result"]
    assert sent_schema["additionalProperties"] is False
    if purpose == "intent":
        assert "$defs" in sent_schema
        assert "$defs" not in sent_schema["properties"]["result"]
    keys = _schema_keys(sent_schema)
    assert "oneOf" not in keys
    assert "discriminator" not in keys
    assert "anyOf" in keys or purpose == "result_narration"


@pytest.mark.asyncio
async def test_openai_transport_classifies_refusal() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "cannot comply"}],
                    }
                ],
            },
        )

    transport = OpenAIResponsesTransport(
        "test-secret", 10, http_transport=httpx.MockTransport(respond)
    )

    with pytest.raises(ProviderRefusalError):
        await transport.request("gpt-test", "intent", "test", "{}", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"status": "completed", "output": []}),
        httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "not-json"}],
                    }
                ],
            },
        ),
        httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"value":7}'}
                        ],
                    }
                ],
            },
        ),
        httpx.Response(200, json={"status": "incomplete", "output": []}),
    ],
)
async def test_openai_transport_rejects_missing_or_invalid_structured_output(
    response: httpx.Response,
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    transport = OpenAIResponsesTransport(
        "test-secret", 10, http_transport=httpx.MockTransport(respond)
    )

    with pytest.raises(ProviderOutputError):
        await transport.request("gpt-test", "intent", "test", "{}", {})
    assert calls == 1


@pytest.mark.asyncio
async def test_openai_transport_does_not_retry_http_failure() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    transport = OpenAIResponsesTransport(
        "test-secret", 10, http_transport=httpx.MockTransport(respond)
    )

    with pytest.raises(ProviderHTTPError, match="503"):
        await transport.request("gpt-test", "intent", "test", "{}", {})
    assert calls == 1


@pytest.mark.asyncio
async def test_openai_transport_maps_network_failure_without_retry() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline", request=request)

    transport = OpenAIResponsesTransport(
        "test-secret", 10, http_transport=httpx.MockTransport(respond)
    )

    with pytest.raises(ConnectionError):
        await transport.request("gpt-test", "intent", "test", "{}", {})
    assert calls == 1
