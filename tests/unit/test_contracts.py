"""外部およびLLM契約のUnit Test。"""

import pytest
from pydantic import ValidationError

from ai_rpg.contracts import ScenarioActionIntent, make_decision_types
from ai_rpg.contracts.context import ContextFragment, MechanicalInput, OutputLimits


def test_action_limit_is_part_of_structured_output_schema() -> None:
    """実効Action上限を超えるLLM出力を拒否する。"""

    _, mechanical = make_decision_types(1)
    action = {"kind": "attack", "target_ref": "goblin", "weapon_ref": None}
    with pytest.raises(ValidationError):
        mechanical.validate_python({"kind": "action_plan", "actions": [action, action]})


def test_contract_rejects_unknown_fields() -> None:
    """構造化出力の未定義フィールドを権限として受理しない。"""

    _, mechanical = make_decision_types(1)
    with pytest.raises(ValidationError):
        mechanical.validate_python(
            {
                "kind": "action_plan",
                "actions": [
                    {
                        "kind": "attack",
                        "target_ref": "goblin",
                        "weapon_ref": None,
                        "damage": 999,
                    }
                ],
            }
        )


def test_scenario_action_is_a_typed_intent() -> None:
    _, mechanical = make_decision_types(3)

    decision = mechanical.validate_python(
        {
            "kind": "action_plan",
            "actions": [{"kind": "scenario_action", "action_ref": "enter_chapel"}],
        }
    )

    assert isinstance(decision.actions[0], ScenarioActionIntent)
    assert decision.actions[0].action_ref == "enter_chapel"


def test_mechanical_input_supports_scenario_actions() -> None:
    fragment = ContextFragment(
        source="scenario",
        trust_level="trusted",
        access_scope="public",
        content="廃礼拝堂の入口",
    )

    mechanical_input = MechanicalInput(
        player_text="礼拝堂に入る",
        scene_view=fragment,
        pc_view=fragment,
        recent_messages=[],
        allowed_entity_refs=[],
        output_limits=OutputLimits(),
        supported_action_types=["scenario_action"],
        supported_skill_refs=[],
    )

    assert mechanical_input.supported_action_types == ["scenario_action"]
