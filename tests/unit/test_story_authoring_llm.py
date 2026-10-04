"""One physical request and sanitized provider result classification."""

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic_ai.exceptions import ContentFilterError, ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from ai_rpg.application.ports.story_authoring import (
    AuthoringGenerationError,
    AuthoringInput,
    AuthoringUsage,
)
from ai_rpg.application.stories import templates
from ai_rpg.application.story_jobs import adopt_changes, prepare_proposal
from ai_rpg.contracts.stories import AuthoringDraft, StoryMetadata
from ai_rpg.llm.story_authoring import (
    _INSTRUCTIONS,
    DevelopmentFakeStoryAuthoring,
    PydanticAIStoryAuthoring,
    authoring_prompt,
)


@pytest.mark.parametrize(
    "mode,code",
    [
        ("refused", "model_refused"),
        ("filtered", "model_refused"),
        ("truncated", "incomplete_model_output"),
        ("truncated_json", "incomplete_model_output"),
        ("invalid", "invalid_model_output"),
        ("http", "model_http_429"),
        ("budget", "token_budget_exceeded"),
    ],
)
def test_single_request_errors_are_sanitized_with_partial_usage(mode: str, code: str) -> None:
    calls = []

    async def generate(messages: Any, info: Any) -> ModelResponse:
        calls.append(1)
        assert not info.function_tools
        assert info.model_settings["openai_store"] is False
        if mode == "refused":
            raise ContentFilterError("secret provider response")
        if mode == "http":
            raise ModelHTTPError(429, "test", {"secret": "API_KEY"})
        return ModelResponse(
            parts=[
                TextPart(
                    "invalid"
                    if mode in {"invalid", "truncated_json"}
                    else '{"changes":[],"findings":[]}'
                )
            ],
            finish_reason="content_filter"
            if mode == "filtered"
            else "length"
            if mode in {"truncated", "truncated_json"}
            else "stop",
            usage=RequestUsage(input_tokens=100, output_tokens=1000 if mode == "budget" else 10),
        )

    async def run() -> None:
        adapter = PydanticAIStoryAuthoring({"test": FunctionModel(generate)})
        usage = AuthoringUsage()
        prompt = adapter.prepare(
            AuthoringInput("check", AuthoringDraft(), "", None), model_id="test", max_bytes=128000
        )
        with pytest.raises(AuthoringGenerationError) as error:
            await adapter.generate(
                prompt, model_id="test", max_output_tokens=100, max_total_tokens=2000, usage=usage
            )
        assert error.value.code == code
        assert "secret" not in str(error.value)
        assert len(calls) == 1
        if mode not in {"refused", "http"}:
            assert usage.complete and usage.input_tokens == 100

    asyncio.run(run())


@pytest.mark.parametrize("fixed_path", [None, "/metadata/title", "/metadata", "/scenario/title"])
def test_blank_concretization_approved_title_beats_stale_instructions_but_not_fixed_draft(
    fixed_path: str | None,
) -> None:
    old_title = "雨上がりの郵便小屋"
    approved_title = "雨上がりの郵便小屋・実機確認1004"
    fixed_title = "作者が固定したタイトル"
    base = AuthoringDraft(
        scenario={"scenario_ref": "my_new_story", "version": 1, "title": fixed_title},
        metadata=StoryMetadata(title=fixed_title),
        field_policies={fixed_path: "fixed"} if fixed_path else {},
    )
    before = base.model_dump_json()
    calls = []

    async def provider(messages: Any, info: Any) -> ModelResponse:
        context = json.loads(
            next(
                part.content
                for message in reversed(messages)
                for part in message.parts
                if isinstance(part, UserPromptPart)
            )
        )
        assert old_title in context["instructions"]
        assert context["approved_outline"]["title"] == approved_title
        calls.append(1)
        candidate = context["new_scenario_example"]
        candidate["title"] = old_title
        # Model repeats the stale instruction even if prompted otherwise. The
        # adapter must preserve the explicit author decision, not trust this value.
        return ModelResponse(
            parts=[
                TextPart(
                    json.dumps(
                        {
                            "scenario_json": json.dumps(candidate),
                            "title": old_title,
                            "synopsis": "A new story",
                            "findings": [],
                        }
                    )
                )
            ],
            finish_reason="stop",
        )

    async def run() -> None:
        adapter = PydanticAIStoryAuthoring({"test": FunctionModel(provider)})
        prompt = adapter.prepare(
            AuthoringInput(
                "concretize",
                base,
                f"タイトルは必ず『{old_title}』にしてください。",
                {"title": approved_title},
            ),
            model_id="test",
            max_bytes=128000,
        )
        result = await adapter.generate(
            prompt,
            model_id="test",
            max_output_tokens=8192,
            max_total_tokens=24000,
            usage=AuthoringUsage(),
        )
        proposal = prepare_proposal(uuid4(), uuid4(), 1, base, result.output, concretize=True)
        assert not proposal.validation.errors
        adopted = adopt_changes(base, proposal, [change.id for change in proposal.changes])
        assert adopted.scenario["title"] == (
            fixed_title if fixed_path == "/scenario/title" else approved_title
        )
        assert adopted.metadata.title == (
            fixed_title if fixed_path in {"/metadata/title", "/metadata"} else approved_title
        )
        if fixed_path:
            assert any(f.code == "fixed_field_change" for f in proposal.findings)
        assert len(calls) == 1
        assert base.model_dump_json() == before

    asyncio.run(run())


def test_existing_scenario_concretization_does_not_automatically_retitle_it() -> None:
    base = templates().templates[1].initial_draft
    before = base.model_dump_json()

    async def provider(messages: Any, info: Any) -> ModelResponse:
        return ModelResponse(parts=[TextPart('{"changes":[],"findings":[]}')], finish_reason="stop")

    async def run() -> None:
        adapter = PydanticAIStoryAuthoring({"test": FunctionModel(provider)})
        prompt = adapter.prepare(
            AuthoringInput(
                "concretize",
                base,
                "Keep existing mechanics",
                {"title": "A different outline title"},
            ),
            model_id="test",
            max_bytes=128000,
        )
        result = await adapter.generate(
            prompt,
            model_id="test",
            max_output_tokens=8192,
            max_total_tokens=24000,
            usage=AuthoringUsage(),
        )
        proposal = prepare_proposal(uuid4(), uuid4(), 1, base, result.output, concretize=True)
        assert not proposal.changes
        assert base.model_dump_json() == before

    asyncio.run(run())


def test_domain_bounds_still_validate_after_provider_schema_simplification() -> None:
    payload = {
        "changes": [
            {
                "field_path": "/" + "x" * 1001,
                "operation": "set",
                "value_json": "null",
                "reason": "test",
            }
        ],
        "findings": [],
    }

    async def generate(messages: Any, info: Any) -> ModelResponse:
        return ModelResponse(parts=[TextPart(json.dumps(payload))], finish_reason="stop")

    async def run() -> None:
        usage = AuthoringUsage()
        adapter = PydanticAIStoryAuthoring({"test": FunctionModel(generate)})
        with pytest.raises(AuthoringGenerationError, match="invalid_model_output"):
            await adapter.generate(
                "{}", model_id="test", max_output_tokens=8192, max_total_tokens=24000, usage=usage
            )
        assert usage.requests == 1 and usage.complete

    asyncio.run(run())


def test_development_fake_supports_all_job_kinds_without_a_provider() -> None:
    async def run() -> None:
        adapter = DevelopmentFakeStoryAuthoring()
        for kind in ("check", "fill", "outline", "concretize"):
            usage = AuthoringUsage()
            prompt = adapter.prepare(
                AuthoringInput(kind, AuthoringDraft(), "", None), model_id="fake", max_bytes=128000
            )
            result = await adapter.generate(
                prompt, model_id="fake", max_output_tokens=8192, max_total_tokens=24000, usage=usage
            )
            assert (result.output.outline is not None) == (kind == "outline")
            assert bool(result.output.changes) == (kind in {"fill", "concretize"})
            assert usage.requests == 0 and usage.actual_model == "development-fake"

    asyncio.run(run())


def test_prompt_separates_registered_graph_from_unverified_freeform_flag_routes() -> None:
    draft = templates().templates[1].initial_draft
    prompt = json.loads(authoring_prompt(AuthoringInput("check", draft, "", None)))
    scope = prompt["validation_scope"]
    assert prompt["supported_skill_refs"] == ["perception", "persuasion", "stealth"]
    assert scope["freeform_enabled"] is True
    assert scope["missing_registered_flag_producer"] == "not_proof_of_impossibility"
    assert scope["unverified_freeform_findings"] == "warning_not_error"
    assert "A missing\nregistered flag producer alone NEVER proves" in _INSTRUCTIONS
    assert "code=unverified_freeform_route" in _INSTRUCTIONS
    blank = json.loads(authoring_prompt(AuthoringInput("check", AuthoringDraft(), "", None)))
    assert blank["validation_scope"]["freeform_enabled"] is False


@pytest.mark.parametrize("empty_output", [False, True])
def test_blank_concretization_requires_complete_candidate_without_silent_repairs(
    empty_output: bool,
) -> None:
    base = AuthoringDraft(
        scenario={"scenario_ref": "author_owned", "version": 1, "objective": "Keep my goal"},
        field_policies={"/scenario/objective": "fixed"},
    )
    before = base.model_dump_json()
    calls = []

    async def generate(messages: Any, info: Any) -> ModelResponse:
        calls.append(1)
        assert (
            "scenario_json" in info.model_request_parameters.output_object.json_schema["required"]
        )
        return ModelResponse(
            parts=[
                TextPart(
                    json.dumps(
                        {
                            "scenario_json": json.dumps(candidate),
                            "title": "Author's new story",
                            "synopsis": "A new journey, not a built-in template.",
                            "findings": [],
                        }
                    )
                )
            ],
            finish_reason="stop",
        )

    adapter = PydanticAIStoryAuthoring({"test": FunctionModel(generate)})
    prompt = adapter.prepare(
        AuthoringInput("concretize", base, "Two scenes without combat", {"title": "New journey"}),
        model_id="test",
        max_bytes=128000,
    )
    candidate = json.loads(prompt)["new_scenario_example"]
    assert candidate["scenario_ref"] == "author_owned"
    assert len(candidate["scenes"]) == 2
    if empty_output:
        candidate = {}
    else:
        # Even the new complete-candidate provider shape cannot override author policy/identity.
        candidate.update(scenario_ref="model_injection", objective="Ignore the fixed goal")

    async def run() -> None:
        result = await adapter.generate(
            prompt,
            model_id="test",
            max_output_tokens=8192,
            max_total_tokens=24000,
            usage=AuthoringUsage(),
        )
        proposal = prepare_proposal(
            uuid4(),
            uuid4(),
            1,
            base,
            result.output,
            concretize=True,
        )
        assert bool(proposal.validation.errors) == empty_output
        adopted = adopt_changes(base, proposal, [change.id for change in proposal.changes])
        assert adopted.scenario["scenario_ref"] == "author_owned"
        assert adopted.scenario["objective"] == "Keep my goal"
        if empty_output:
            assert "scenes" not in adopted.scenario
        else:
            assert len(adopted.scenario["scenes"]) == 2
            assert len(adopted.scenario["endings"]) == 2
        assert base.model_dump_json() == before
        assert len(calls) == 1

    asyncio.run(run())
