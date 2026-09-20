"""組込みScenario定義とcatalogのUnit Test。"""

import json
from copy import deepcopy
from importlib.resources import files
from typing import Any

import pytest
from pydantic import ValidationError

from ai_rpg.scenarios import BUILTIN_SCENARIOS, ScenarioCatalog, ScenarioDefinition


def valid_payload() -> dict[str, Any]:
    return {
        "scenario_ref": "test_scenario",
        "version": 1,
        "title": "Test Scenario",
        "objective": "Reach an ending",
        "scenes": [
            {
                "scene_ref": "entrance",
                "sequence": 1,
                "title": "Entrance",
                "description": "The entrance.",
                "actions": [
                    {
                        "action_ref": "enter",
                        "label": "Enter",
                        "public_fact": "Entered.",
                        "kind": "scenario_action",
                        "success": {
                            "next_scene_ref": "hall",
                            "ending_ref": None,
                            "add_flags": [],
                        },
                    }
                ],
            },
            {
                "scene_ref": "hall",
                "sequence": 2,
                "title": "Hall",
                "description": "The hall.",
                "actions": [
                    {
                        "action_ref": "search",
                        "label": "Search",
                        "kind": "skill_check",
                        "check_ref": "normal",
                        "skill_ref": "perception",
                        "success": {
                            "next_scene_ref": None,
                            "ending_ref": "complete",
                            "add_flags": ["found"],
                        },
                        "failure": {
                            "next_scene_ref": None,
                            "ending_ref": "retreated",
                            "add_flags": [],
                        },
                    }
                ],
            },
        ],
        "flags": [{"flag_ref": "found", "public_fact": "Found a clue."}],
        "endings": [
            {"ending_ref": "complete", "title": "Complete", "summary": "Done."},
            {"ending_ref": "retreated", "title": "Retreated", "summary": "Left."},
        ],
    }


def invalid_payload(change: str) -> dict[str, Any]:
    payload = deepcopy(valid_payload())
    scenes = payload["scenes"]

    if change == "duplicate_scene":
        scenes.append(deepcopy(scenes[0]))
    elif change == "duplicate_action":
        scenes[1]["actions"].append(deepcopy(scenes[0]["actions"][0]))
    elif change == "duplicate_flag":
        payload["flags"].append(deepcopy(payload["flags"][0]))
    elif change == "duplicate_ending":
        payload["endings"].append(deepcopy(payload["endings"][0]))
    elif change == "duplicate_sequence":
        scenes[1]["sequence"] = 1
    elif change == "missing_sequence_one":
        scenes[0]["sequence"] = 3
    elif change == "dangling_scene":
        scenes[0]["actions"][0]["success"]["next_scene_ref"] = "missing"
    elif change == "dangling_ending":
        scenes[1]["actions"][0]["success"]["ending_ref"] = "missing"
    elif change == "dangling_flag":
        scenes[1]["actions"][0]["success"]["add_flags"] = ["missing"]
    elif change == "invalid_skill":
        scenes[1]["actions"][0]["skill_ref"] = "alchemy"
    elif change == "effect_conflict":
        scenes[0]["actions"][0]["success"]["ending_ref"] = "complete"
    else:
        raise AssertionError(f"Unknown change: {change}")

    return payload


def test_builtin_ruined_chapel_is_typed_and_closed() -> None:
    scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 1)
    assert scenario.title == "廃礼拝堂の聖印"
    assert [scene.scene_ref for scene in scenario.scenes] == [
        "entrance",
        "hall",
        "sanctum",
    ]
    assert {ending.ending_ref for ending in scenario.endings} == {
        "recovered",
        "costly_success",
        "retreated",
    }


def test_builtin_alerted_outcomes_are_definition_driven() -> None:
    scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 1)
    sanctum_actions = scenario.scenes[2].actions

    negotiation_override = sanctum_actions[0].success.overrides[0]
    assert negotiation_override.requires_flags == ("alerted",)
    assert negotiation_override.ending_ref == "costly_success"

    stealth_override = sanctum_actions[1].success.overrides[0]
    assert stealth_override.requires_flags == ("alerted",)
    assert stealth_override.ending_ref == "costly_success"

    defeated_override = sanctum_actions[2].defeated.overrides[0]
    assert defeated_override.requires_flags == ("alerted",)
    assert defeated_override.ending_ref == "costly_success"


def test_scenario_definition_accepts_conditional_ending_override() -> None:
    payload = valid_payload()
    payload["scenes"][1]["actions"][0]["success"]["overrides"] = [
        {"requires_flags": ["found"], "ending_ref": "retreated"}
    ]

    scenario = ScenarioDefinition.model_validate(payload)

    override = scenario.scenes[1].actions[0].success.overrides[0]
    assert override.requires_flags == ("found",)
    assert override.ending_ref == "retreated"


def test_builtin_scenario_nested_collections_are_immutable_tuples() -> None:
    scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 1)

    assert isinstance(scenario.scenes, tuple)
    assert isinstance(scenario.flags, tuple)
    assert isinstance(scenario.endings, tuple)
    assert isinstance(scenario.scenes[0].actions, tuple)
    assert isinstance(scenario.scenes[1].actions[0].success.add_flags, tuple)
    assert isinstance(scenario.scenes[2].actions[0].success.overrides, tuple)
    assert isinstance(
        scenario.scenes[2].actions[0].success.overrides[0].requires_flags,
        tuple,
    )


@pytest.mark.parametrize(
    "change",
    ["empty_override_flags", "override_on_transition", "override_flag", "override_ending"],
)
def test_scenario_definition_rejects_invalid_ending_override(change: str) -> None:
    payload = valid_payload()
    ending_effect = payload["scenes"][1]["actions"][0]["success"]
    override = {"requires_flags": ["found"], "ending_ref": "retreated"}

    if change == "empty_override_flags":
        override["requires_flags"] = []
        ending_effect["overrides"] = [override]
    elif change == "override_on_transition":
        payload["scenes"][0]["actions"][0]["success"]["overrides"] = [override]
    elif change == "override_flag":
        override["requires_flags"] = ["missing"]
        ending_effect["overrides"] = [override]
    elif change == "override_ending":
        override["ending_ref"] = "missing"
        ending_effect["overrides"] = [override]
    else:
        raise AssertionError(f"Unknown change: {change}")

    with pytest.raises(ValidationError):
        ScenarioDefinition.model_validate(payload)


def test_ruined_chapel_json_is_a_package_resource() -> None:
    resource = (
        files("ai_rpg.scenarios").joinpath("ruined_chapel.json").read_text(encoding="utf-8")
    )
    assert json.loads(resource)["scenario_ref"] == "ruined_chapel"


def test_scenario_catalog_rejects_duplicate_scenario_key() -> None:
    scenario = ScenarioDefinition.model_validate(valid_payload())

    with pytest.raises(ValueError):
        ScenarioCatalog((scenario, scenario))


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_scene",
        "duplicate_action",
        "duplicate_flag",
        "duplicate_ending",
        "duplicate_sequence",
        "missing_sequence_one",
        "dangling_scene",
        "dangling_ending",
        "dangling_flag",
        "invalid_skill",
        "effect_conflict",
    ],
)
def test_scenario_definition_rejects_invalid_graph(change: str) -> None:
    payload = invalid_payload(change)
    with pytest.raises(ValidationError):
        ScenarioDefinition.model_validate(payload)
