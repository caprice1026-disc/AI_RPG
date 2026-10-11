"""Locked Pydantic AI/Google SDK contract; all HTTP is intercepted locally."""

import json
from uuid import uuid4

import httpx2
import pytest

from ai_rpg.application.ports.story_authoring import (
    AuthoringGenerationError,
    AuthoringInput,
    AuthoringUsage,
)
from ai_rpg.application.stories import compile_draft
from ai_rpg.application.story_jobs import adopt_changes, prepare_proposal
from ai_rpg.config import Settings
from ai_rpg.contracts.stories import AuthoringDraft
from ai_rpg.llm.models import build_provider_models
from ai_rpg.llm.story_authoring import PydanticAIStoryAuthoring

pytestmark = pytest.mark.contract


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["check", "fill", "outline", "concretize"])
async def test_gemini_36_authoring_schema_and_executable_candidate(monkeypatch, kind):
    requests = []
    output = {}

    def respond(request):
        requests.append(request)
        return httpx2.Response(200, json={
            "modelVersion": "gemini-3.6-flash",
            "candidates": [{"index": 0, "finishReason": "STOP", "content": {
                "role": "model", "parts": [{"text": json.dumps(output)}],
            }}],
            "usageMetadata": {
                "promptTokenCount": 100, "candidatesTokenCount": 200,
                "thoughtsTokenCount": 30, "totalTokenCount": 330,
            },
        })

    original = httpx2.AsyncClient

    class Client(original):
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 60
            super().__init__(**kwargs, transport=httpx2.MockTransport(respond))

    monkeypatch.setattr("ai_rpg.llm.models.httpx2.AsyncClient", Client)
    settings = Settings(_env_file=None, gemini_api_key="test-google", llm_timeout_seconds=60,
                        worker_lease_seconds=90)
    base = AuthoringDraft(scenario={"scenario_ref": "migration_story", "version": 1})
    usage = AuthoringUsage()
    async with build_provider_models(settings) as models:
        adapter = PydanticAIStoryAuthoring(models)
        prompt = adapter.prepare(
            AuthoringInput(kind, base, "A two-scene story", {"title": "Migration story"}),
            model_id=settings.background_model, max_bytes=128000,
        )
        if kind == "outline":
            output.update(outline={
                "title": "Migration story", "premise": "Find the exit",
                "scenes": ["Entrance", "Exit"], "characters": [],
                "endings": ["Safe"], "undecided": [],
            }, findings=[])
        elif kind == "concretize":
            candidate = json.loads(prompt)["new_scenario_example"]
            output.update(scenario_json=json.dumps(candidate), title="Migration story",
                          synopsis="Find the exit", findings=[])
        else:
            output.update(changes=[], findings=[])
        result = await adapter.generate(
            prompt, model_id=settings.background_model, max_output_tokens=8192,
            max_total_tokens=24000, usage=usage,
        )
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/models/gemini-3.6-flash:generateContent")
    body = json.loads(requests[0].content)
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["maxOutputTokens"] == 8192
    assert not body.get("tools")
    schema = config["responseJsonSchema"]
    expected = (
        "outline" if kind == "outline" else "scenario_json" if kind == "concretize" else "changes"
    )
    assert expected in schema["required"]
    assert usage.requests == 1 and usage.complete
    assert usage.actual_model == "gemini-3.6-flash"
    assert usage.input_tokens == 100 and usage.output_tokens == 230
    if kind == "concretize":
        proposal = prepare_proposal(uuid4(), uuid4(), 1, base, result.output, concretize=True)
        assert not proposal.validation.errors
        adopted = adopt_changes(base, proposal, [change.id for change in proposal.changes])
        definition, report = compile_draft(adopted, uuid4(), 1)
        assert definition is not None and not report.errors
        assert len(definition.scenes) == 2 and len(definition.endings) == 2
    else:
        assert (result.output.outline is not None) == (kind == "outline")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [
    ("timeout", "job_timeout"), ("429", "model_http_429"),
    ("invalid", "invalid_model_output"), ("length", "incomplete_model_output"),
])
async def test_gemini_36_authoring_failure_has_no_retry(monkeypatch, failure, code):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx2.ReadTimeout("private timeout", request=request)
        if failure == "429":
            return httpx2.Response(429, json={"error": {"code": 429, "message": "private"}})
        return httpx2.Response(200, json={
            "modelVersion": "gemini-3.6-flash",
            "candidates": [{"index": 0,
                            "finishReason": "MAX_TOKENS" if failure == "length" else "STOP",
                            "content": {"role": "model", "parts": [{"text": "invalid"}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10,
                              "totalTokenCount": 110},
        })

    original = httpx2.AsyncClient

    class Client(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx2.MockTransport(respond))

    monkeypatch.setattr("ai_rpg.llm.models.httpx2.AsyncClient", Client)
    settings = Settings(_env_file=None, gemini_api_key="test-google")
    async with build_provider_models(settings) as models:
        adapter = PydanticAIStoryAuthoring(models)
        with pytest.raises(AuthoringGenerationError) as error:
            await adapter.generate(
                '{"kind":"check"}', model_id=settings.background_model,
                max_output_tokens=8192, max_total_tokens=24000, usage=AuthoringUsage(),
            )
    assert error.value.code == code
    assert "private" not in str(error.value)
    assert len(requests) == 1
