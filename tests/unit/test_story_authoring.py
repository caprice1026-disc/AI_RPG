"""Loose drafts, deterministic compilation and finite flag traversal."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import ai_rpg.application.stories as story_module
import ai_rpg.contracts.stories as story_contracts
from ai_rpg.api.stories import create_stories_router
from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.stories import assign_missing_refs, compile_draft, templates
from ai_rpg.contracts.stories import AuthoringDraft, StoryMetadata, TemplateDefinition
from ai_rpg.scenarios import ScenarioCatalog, ScenarioDefinition


def small_draft() -> AuthoringDraft:
    return AuthoringDraft(
        scenario={
            "schema_version": 2,
            "scenario_ref": "small_story",
            "version": 1,
            "title": "A short story",
            "objective": "Find the key and leave",
            "ruleset_ref": "mvp_v1",
            "required_capabilities": [],
            "initialization": {},
            "scenes": [
                {
                    "scene_ref": "entrance",
                    "sequence": 1,
                    "title": "Entrance",
                    "description": "An open door.",
                    "actions": [
                        {
                            "kind": "scenario_action",
                            "action_ref": "leave",
                            "label": "Leave",
                            "public_fact": "You left.",
                            "success": {"ending_ref": "escaped"},
                        },
                    ],
                }
            ],
            "flags": [{"flag_ref": "key", "public_fact": "You found a key."}],
            "endings": [{"ending_ref": "escaped", "title": "Escaped", "summary": "Safe."}],
        },
        metadata=StoryMetadata(title="A short story"),
    )


def test_incomplete_saves_are_distinct_from_executable_definitions() -> None:
    draft = AuthoringDraft(scenario={"title": "", "scenes": [{"actions": []}]})
    definition, report = compile_draft(draft, uuid4(), 1)
    assert definition is None
    assert report.errors
    assert all(error.code == "invalid_definition" for error in report.errors)
    assert report.coverage["registered_actions"] == "not_run"
    assert all(error.field_path.startswith("/scenario") for error in report.errors)


def test_registered_graph_with_unobtainable_required_flag_is_blocked() -> None:
    data = small_draft().model_dump(mode="json")
    data["scenario"]["scenes"][0]["actions"][0]["required_flags"] = ["key"]
    _, report = compile_draft(AuthoringDraft.model_validate(data), uuid4(), 1)
    assert {error.code for error in report.errors} == {"unreachable_ending"}
    assert report.coverage["reachable_endings"] == []


def test_finite_search_honors_disabled_flags_and_detects_a_reachable_softlock() -> None:
    data = small_draft().model_dump(mode="json")
    actions = data["scenario"]["scenes"][0]["actions"]
    actions[0]["disabled_flags"] = ["key"]
    actions.append(
        {
            "kind": "scenario_action",
            "action_ref": "take_key",
            "label": "Take key",
            "public_fact": "Got it",
            "disabled_flags": ["key"],
            "success": {"add_flags": ["key"]},
        }
    )
    _, report = compile_draft(AuthoringDraft.model_validate(data), uuid4(), 1)
    assert "unreachable_ending" in {error.code for error in report.errors}
    assert report.coverage["reachable_endings"] == ["escaped"]


def test_finite_search_reaches_ending_after_obtaining_flag() -> None:
    data = small_draft().model_dump(mode="json")
    actions = data["scenario"]["scenes"][0]["actions"]
    actions[0]["required_flags"] = ["key"]
    actions.append(
        {
            "kind": "scenario_action",
            "action_ref": "take_key",
            "label": "Take key",
            "public_fact": "Got it",
            "disabled_flags": ["key"],
            "success": {"add_flags": ["key"]},
        }
    )
    _, report = compile_draft(AuthoringDraft.model_validate(data), uuid4(), 1)
    assert report.errors == []
    assert report.coverage["states_explored"] == 2
    assert report.coverage["reachable_endings"] == ["escaped"]


def test_exploration_budget_is_a_warning_and_never_a_proof_of_reachability() -> None:
    data = small_draft().model_dump(mode="json")
    data["scenario"]["scenes"][0]["actions"][0]["success"] = {"add_flags": ["key"]}
    _, report = compile_draft(AuthoringDraft.model_validate(data), uuid4(), 1, state_limit=1)
    assert report.errors == []
    assert report.coverage["registered_actions"] == "partial"
    assert "exploration_limit" in {warning.code for warning in report.warnings}


def test_template_registry_copies_and_explicit_initialization_compile() -> None:
    registered = templates().templates
    assert len(registered) >= 2
    for template in registered:
        definition, report = compile_draft(template.initial_draft, uuid4(), 1)
        assert not report.errors, report.errors
        assert definition is not None and definition.schema_version == 2
        expected_object = "銀の聖印" if template.template_id == "ruined_chapel" else "航海日誌"
        assert any(item.label == expected_object for item in definition.initialization.items)
        assert "freeform_unverified" in {warning.code for warning in report.warnings}
    registered[0].initial_draft.scenario["title"] = "Changed"
    assert templates().templates[0].initial_draft.scenario["title"] != "Changed"


def test_template_alias_uses_source_object_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        story_module.TEMPLATE_REGISTRY, "third_template",
        story_module.TEMPLATE_REGISTRY["mist_lighthouse"],
    )
    registered = {template.template_id: template for template in templates().templates}
    alias = registered["third_template"]
    assert alias.initial_draft == registered["mist_lighthouse"].initial_draft
    definition, report = compile_draft(alias.initial_draft, uuid4(), 1)
    assert report.errors == []
    assert definition is not None
    assert any(item.label == "航海日誌" for item in definition.initialization.items)


def test_native_schema2_template_preserves_objects_without_legacy_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ScenarioDefinition.model_validate_json(
        files("ai_rpg.scenarios").joinpath("clockwork_garden.json").read_text(encoding="utf-8"),
    )
    assert source.schema_version == 2
    assert source.scenario_ref not in story_module.TEMPLATE_OBJECT_LABELS
    monkeypatch.setattr(story_module, "BUILTIN_SCENARIOS", ScenarioCatalog([
        *(story_module.BUILTIN_SCENARIOS.get(*key) for key in story_module.BUILTIN_KEYS),
        source,
    ]))
    monkeypatch.setitem(story_module.TEMPLATE_REGISTRY, "native_garden", (
        story_module.TemplateRegistryEntry(
            source_ref=source.scenario_ref, source_version=source.version, version=1,
            description="Native schema-2 objects need no legacy label mapping.",
        )
    ))

    template = next(t for t in templates().templates if t.template_id == "native_garden")
    definition, report = compile_draft(template.initial_draft, uuid4(), 1)
    assert report.errors == []
    assert definition is not None
    assert definition.initialization == source.initialization
    assert definition.world == source.world
    assert definition.scenes == source.scenes
    assert {item.ref: item.label for item in definition.initialization.items} == {
        "pruning_hook": "Pruning hook", "leaf_tonic": "Leaf tonic", "seed_pod": "Last seed pod",
    }


def test_old_template_payload_defaults_to_empty_guidance() -> None:
    template = TemplateDefinition.model_validate({
        "template_id": "old_template", "version": 1, "title": "Old template",
        "description": "Existing payload", "required_capabilities": [],
        "sections": ["world"], "initial_draft": {},
    })
    assert template.questions == []
    assert template.recommended_structure == []


def test_template_questions_are_typed_optional_display_guidance() -> None:
    template = TemplateDefinition.model_validate({
        "template_id": "guided", "version": 2, "title": "Guided template",
        "description": "Plain text only", "required_capabilities": [],
        "sections": ["world"], "initial_draft": {},
        "questions": [{"prompt": "What is the goal?", "target_section": "world"}],
        "recommended_structure": ["Introduce the goal", "Offer a way to leave"],
    })
    assert template.questions[0].model_dump() == {
        "prompt": "What is the goal?", "hint": "", "target_section": "world",
        "field_path": None,
    }
    assert template.initial_draft == AuthoringDraft()
    assert template.recommended_structure == ["Introduce the goal", "Offer a way to leave"]


@pytest.mark.parametrize("change", [
    {"prompt": ""}, {"hint": 123}, {"field_path": "scenario/objective"},
    {"target_section": "missing"}, {"html": "<script>alert(1)</script>"},
])
def test_template_guidance_rejects_invalid_questions(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TemplateDefinition.model_validate({
            "template_id": "guided", "version": 1, "title": "Guide",
            "description": "Guide", "required_capabilities": [],
            "sections": ["world"], "initial_draft": {},
            "questions": [{"prompt": "What is the goal?", "target_section": "world", **change}],
        })


def test_template_api_exposes_independent_registry_guidance_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = next(t for t in templates().templates if t.template_id == "mist_lighthouse")
    question = story_contracts.TemplateQuestion(
        prompt="What should the visitor recover?", hint="Choose one object.",
        target_section="world", field_path="/scenario/objective",
    )
    monkeypatch.setitem(story_module.TEMPLATE_REGISTRY, "third_template", replace(
        story_module.TEMPLATE_REGISTRY["mist_lighthouse"], version=7,
        title="An independent template", description="A different authoring guide",
        sections=("world", "scenes"), questions=(question,),
        recommended_structure=("Arrival", "Investigation", "Return"),
    ))

    async def authenticated() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(uuid4(), "test", "test", datetime.now(UTC), frozenset())

    app = FastAPI()
    app.include_router(create_stories_router(
        principal_provider=authenticated, service=story_module.StoryService(MagicMock()),
    ))
    with TestClient(app) as client:
        response = client.get("/stories/templates")
    assert response.status_code == 200
    payloads = {t["template_id"]: t for t in response.json()["templates"]}
    alias = payloads["third_template"]
    assert alias["version"] == 7
    assert alias["title"] == "An independent template"
    assert alias["description"] == "A different authoring guide"
    assert alias["sections"] == ["world", "scenes"]
    assert alias["questions"] == [{
        "prompt": "What should the visitor recover?", "hint": "Choose one object.",
        "target_section": "world", "field_path": "/scenario/objective",
    }]
    assert alias["recommended_structure"] == ["Arrival", "Investigation", "Return"]
    assert alias["initial_draft"] == original.initial_draft.model_dump(mode="json")
    assert alias["initial_draft"]["scenario"]["version"] == 1
    assert payloads["mist_lighthouse"] == original.model_dump(mode="json")

    returned = next(t for t in templates().templates if t.template_id == "third_template")
    returned.initial_draft.scenario["title"] = "Edited draft"
    returned.questions.clear()
    returned.recommended_structure.append("Unwanted change")
    fresh = next(t for t in templates().templates if t.template_id == "third_template")
    assert fresh.model_dump(mode="json") == alias


def test_builtin_templates_offer_questions_and_recommended_structure() -> None:
    for template in templates().templates:
        assert template.questions
        assert template.recommended_structure
        assert all(question.target_section in template.sections for question in template.questions)


def test_metadata_only_changes_reuse_compiled_hash_but_not_validation_report() -> None:
    draft = small_draft()
    changed = draft.model_copy(update={"metadata": StoryMetadata(title="New public title")})
    _, old = compile_draft(draft, uuid4(), 1)
    _, new = compile_draft(changed, uuid4(), 2)
    assert old.content_hash == new.content_hash
    assert old.draft_hash != new.draft_hash
    scenario = deepcopy(draft.scenario)
    scenario["title"] = "Different in-game title"
    _, changed_content = compile_draft(draft.model_copy(update={"scenario": scenario}), uuid4(), 3)
    assert old.content_hash != changed_content.content_hash


def test_unknown_capability_and_dangling_reference_are_errors() -> None:
    data = small_draft().model_dump(mode="json")
    data["scenario"]["required_capabilities"] = ["run_javascript"]
    _, report = compile_draft(AuthoringDraft.model_validate(data), uuid4(), 1)
    assert report.errors
    data["scenario"]["required_capabilities"] = []
    data["scenario"]["scenes"][0]["actions"][0]["success"] = {"next_scene_ref": "missing"}
    _, report = compile_draft(AuthoringDraft.model_validate(data), uuid4(), 1)
    assert report.errors


def test_new_refs_are_server_generated_and_stable_after_save() -> None:
    draft = AuthoringDraft(scenario={
        "scenes": [{"title": "Room", "actions": [{}]}],
        "world": {"protected_facts": [{"statement": "An old key exists."}]},
    })
    assigned = assign_missing_refs(draft)
    assert assigned.scenario["world"]["protected_facts"][0]["fact_ref"].startswith("ref_")
    assert assign_missing_refs(assigned) == assigned
    assert draft.scenario != assigned.scenario


def test_drafts_reject_nonfinite_or_oversize_content() -> None:
    with pytest.raises(ValidationError):
        AuthoringDraft(scenario={"value": float("nan")})
    with pytest.raises(ValidationError):
        AuthoringDraft(scenario={"value": "x" * 512_001})
