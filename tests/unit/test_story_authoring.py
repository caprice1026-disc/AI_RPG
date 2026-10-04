"""Loose drafts, deterministic compilation and finite flag traversal."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_rpg.application.stories import assign_missing_refs, compile_draft, templates
from ai_rpg.contracts.stories import AuthoringDraft, StoryMetadata


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
