"""実SDKから送信されるSchemaと、一回の物理HTTP要求の契約。"""

import json

import httpx2
import pytest

from ai_rpg.application.ports.llm import (
    ProviderHTTPError,
    ProviderOutputError,
    ProviderRefusalError,
)
from ai_rpg.config import Settings
from ai_rpg.contracts.context import MechanicalInput, NarrativeInput
from ai_rpg.contracts.responses import MechanicalNarrationInput
from ai_rpg.llm.models import build_language_models

pytestmark = pytest.mark.contract
MODELS = ["google:gemini-3.5-flash", "openai-responses:gpt-5-mini"]


def _context():
    fragment = dict(source="test", trust_level="trusted", access_scope="public", content="入口")
    return dict(
        player_text="礼拝堂に入る",
        scene_view=fragment,
        pc_view=fragment,
        recent_messages=[],
        allowed_entity_refs=[],
        output_limits=dict(max_actions=2),
    )


def _response(model, raw, failure=None):
    if model.startswith("google:"):
        if failure == "refusal":
            return {"promptFeedback": {"blockReason": "SAFETY"}}
        return {
            "modelVersion": "gemini-3.5-flash",
            "candidates": [
                {
                    "index": 0,
                    "content": {"role": "model", "parts": [{"text": raw}]},
                    "finishReason": "MAX_TOKENS" if failure == "length" else "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 10,
                "totalTokenCount": 20,
            },
        }
    content = (
        {"type": "refusal", "refusal": "private provider refusal"}
        if failure == "refusal"
        else {"type": "output_text", "text": raw, "annotations": []}
    )
    return dict(
        id="resp_test",
        object="response",
        created_at=1,
        model="gpt-5-mini",
        status="incomplete" if failure == "length" else "completed",
        incomplete_details={"reason": "max_output_tokens"} if failure == "length" else None,
        error=None,
        output=[
            dict(
                type="message",
                id="msg_test",
                status="completed",
                role="assistant",
                content=[content],
            )
        ],
        usage=dict(
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
            input_tokens_details=dict(cached_tokens=0),
            output_tokens_details=dict(reasoning_tokens=0),
        ),
    )


def _mock_http(monkeypatch, handler):
    original = httpx2.AsyncClient

    class Client(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx2.MockTransport(handler))

    monkeypatch.setattr("ai_rpg.llm.models.httpx2.AsyncClient", Client)


async def _call(llm, model, purpose):
    if purpose == "intent":
        return await llm.extract_intent(
            MechanicalInput(
                **_context(), supported_action_types=["scenario_action"], supported_skill_refs=[]
            ),
            model_id=model,
        )
    if purpose == "narrative":
        return await llm.generate_narrative(NarrativeInput(**_context()), model_id=model)
    return await llm.narrate_result(
        MechanicalNarrationInput(
            player_text="入る",
            committed_state_version=1,
            resolved_actions=[],
            public_state_after=[],
            allowed_entity_refs=[],
            output_limits={},
        ),
        model_id=model,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("purpose", ["intent", "narrative", "result_narration"])
async def test_runtime_agents_send_typed_object_schema_once(monkeypatch, model, purpose):
    expected = {"narration": "入口だ。", "choices": []}
    if purpose == "intent":
        expected = {
            "kind": "action_plan",
            "actions": [{"kind": "scenario_action", "action_ref": "enter_chapel"}],
        }
    elif purpose == "narrative":
        expected = {"kind": "narrative", **expected}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json=_response(model, json.dumps({"result": expected})))

    _mock_http(monkeypatch, respond)
    async with build_language_models(
        Settings(
            _env_file=None,
            llm_model=model,
            gemini_api_key="test-google",
            openai_api_key="test-openai",
        ),
        fake=False,
    ) as llm:
        output = await _call(llm, model, purpose)
    assert output.model_dump() == expected
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    if model.startswith("google:"):
        assert requests[0].url.host == "generativelanguage.googleapis.com"
        assert requests[0].headers["x-goog-api-key"] == "test-google"
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        schema = body["generationConfig"]["responseJsonSchema"]
        assert not body.get("tools")
    else:
        assert str(requests[0].url) == "https://api.openai.com/v1/responses"
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert "previous_response_id" not in body
        assert not body.get("tools")
        schema = body["text"]["format"]["schema"]
        assert '"oneOf"' not in json.dumps(schema)
        assert '"discriminator"' not in json.dumps(schema)
    assert schema["type"] == "object"
    assert "result" in schema["properties"]
    if purpose != "result_narration":
        assert '"maxItems": 2' in json.dumps(schema)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize(
    "failure,error",
    [
        ("invalid_json", ProviderOutputError),
        ("invalid_http_json", ProviderOutputError),
        ("missing_http_fields", ProviderOutputError),
        ("invalid_schema", ProviderOutputError),
        ("empty", ProviderOutputError),
        ("length", ProviderOutputError),
        ("refusal", ProviderRefusalError),
        ("429", ProviderHTTPError),
        ("503", ProviderHTTPError),
        ("timeout", TimeoutError),
        ("connection", ConnectionError),
    ],
)
async def test_failures_never_trigger_hidden_repair_or_sdk_retry(
    monkeypatch, model, failure, error
):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "invalid_http_json":
            return httpx2.Response(200, text="private malformed provider response")
        if failure == "missing_http_fields":
            return httpx2.Response(200, json={})
        if failure == "timeout":
            raise httpx2.ReadTimeout("private timeout body", request=request)
        if failure == "connection":
            raise httpx2.ConnectError("private connection body", request=request)
        if failure in ("429", "503"):
            return httpx2.Response(
                int(failure),
                json={"error": {"message": "private error body", "code": int(failure)}},
            )
        raw = '{"result":{"kind":"narrative","narration":"入口だ。","choices":[]}}'
        raw = {
            "invalid_json": "private invalid json",
            "invalid_schema": '{"result":{"damage":70}}',
            "empty": "",
        }.get(failure, raw)
        return httpx2.Response(200, json=_response(model, raw, failure))

    _mock_http(monkeypatch, respond)
    async with build_language_models(
        Settings(
            _env_file=None,
            llm_model=model,
            gemini_api_key="test-google",
            openai_api_key="test-openai",
        ),
        fake=False,
    ) as llm:
        with pytest.raises(error) as caught:
            await _call(llm, model, "narrative")
    assert "private" not in str(caught.value)
    assert len(requests) == 1
