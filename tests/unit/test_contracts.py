"""外部およびLLM契約のUnit Test。"""

import pytest
from pydantic import ValidationError

from ai_rpg.contracts import make_decision_types


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
