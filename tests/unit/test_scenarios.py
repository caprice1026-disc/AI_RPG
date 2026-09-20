"""組込みScenario定義とcatalogのUnit Test。"""

import json
from copy import deepcopy
from importlib.resources import files
from typing import Any

import pytest
from pydantic import ValidationError

from ai_rpg.scenarios import BUILTIN_SCENARIOS, ScenarioDefinition


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
    elif change == "dangling_scene":
        scenes[0]["actions"][0]["success"]["next_scene_ref"] = "missing"
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


def test_ruined_chapel_json_is_a_package_resource() -> None:
    resource = (
        files("ai_rpg.scenarios").joinpath("ruined_chapel.json").read_text(encoding="utf-8")
    )
    assert json.loads(resource)["scenario_ref"] == "ruined_chapel"


@pytest.mark.parametrize(
    "change",
    ["duplicate_scene", "dangling_scene", "invalid_skill", "effect_conflict"],
)
def test_scenario_definition_rejects_invalid_graph(change: str) -> None:
    payload = invalid_payload(change)
    with pytest.raises(ValidationError):
        ScenarioDefinition.model_validate(payload)
