"""v0.2の信頼境界を固定するContract Test。"""

from itertools import product
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_rpg.contracts.context import EntityRef, OutputLimits
from ai_rpg.contracts.llm_decisions import AttackIntent, make_decision_types
from ai_rpg.contracts.player_turn import PlayerTurnInput
from ai_rpg.contracts.responses import TurnRecovery, TurnResponse


@pytest.mark.contract
@pytest.mark.parametrize("field", ["damage", "hp_after", "state_mutation"])
def test_action_intent_rejects_engine_owned_fields(field: str) -> None:
    payload = {"kind": "attack", "target_ref": "goblin", "weapon_ref": None, field: 1}
    with pytest.raises(ValidationError):
        AttackIntent.model_validate(payload)


@pytest.mark.contract
def test_contracts_reject_unknown_fields_and_mixed_input_union() -> None:
    common = {"request_id": str(uuid4()), "expected_state_version": 0, "actor_id": str(uuid4())}
    with pytest.raises(ValidationError):
        PlayerTurnInput.model_validate(
            {**common, "content": {"kind": "text", "text": "進む", "choice_id": uuid4()}}
        )
    with pytest.raises(ValidationError):
        EntityRef.model_validate(
            {"ref": "goblin", "label": "ゴブリン", "entity_kind": "npc", "id": uuid4()}
        )


@pytest.mark.contract
@pytest.mark.parametrize("value", [True, "3"])
def test_strict_integer_rejects_bool_and_numeric_string(value: object) -> None:
    with pytest.raises(ValidationError):
        OutputLimits.model_validate({"max_actions": value, "max_choices": 5})


@pytest.mark.contract
def test_action_limit_is_embedded_in_schema_and_validation() -> None:
    _, mechanical = make_decision_types(2)
    schema = mechanical.json_schema()
    plan_schema = schema["$defs"]["ActionPlanLimit2"]
    assert plan_schema["properties"]["actions"]["maxItems"] == 2
    action = {"kind": "attack", "target_ref": "goblin", "weapon_ref": None}
    with pytest.raises(ValidationError):
        mechanical.validate_python({"kind": "action_plan", "actions": [action] * 3})


@pytest.mark.contract
@pytest.mark.parametrize("max_actions", [True, 0, -1, "3"])
def test_decision_factory_rejects_invalid_limits(max_actions: object) -> None:
    with pytest.raises(ValueError):
        make_decision_types(max_actions)  # type: ignore[arg-type]


@pytest.mark.contract
@pytest.mark.parametrize("fallback,reason", product([False, True], [None, "UNKNOWN"]))
def test_turn_recovery_all_consistency_combinations(fallback: bool, reason: str | None) -> None:
    expected = fallback == (reason is not None)
    if expected:
        TurnRecovery.model_validate({"fallback": fallback, "reason": reason})
    else:
        with pytest.raises(ValidationError):
            TurnRecovery.model_validate({"fallback": fallback, "reason": reason})


@pytest.mark.contract
def test_turn_response_all_status_combinations() -> None:
    """status validatorが取り得る状態値の直積を漏れなく検査する。"""

    routes = [None, "narrative", "mechanical"]
    resolutions = ["pending", "resolving", "committed", "not_applied", "failed"]
    narrations = ["pending", "generating", "completed", "fallback"]
    for (
        route,
        resolution,
        narration_status,
        has_version,
        has_text,
        has_choices,
        has_results,
        recovery_fallback,
    ) in product(
        routes,
        resolutions,
        narrations,
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
    ):
        done = narration_status in ("completed", "fallback")
        recovery = {
            "fallback": recovery_fallback,
            "reason": "UNKNOWN" if recovery_fallback else None,
        }
        expected = (
            (resolution == "committed") == has_version
            and not (resolution == "committed" and route is None)
            and not (has_results and (route != "mechanical" or resolution != "committed"))
            and done == has_text
            and not (has_choices and not done)
            and (narration_status == "fallback") == recovery_fallback
        )
        payload = {
            "turn_id": uuid4(),
            "route": route,
            "resolution_status": resolution,
            "narration_status": narration_status,
            "committed_state_version": 1 if has_version else None,
            "narration": "結果" if has_text else None,
            "choices": [{"id": uuid4(), "label": "進む"}] if has_choices else [],
            "action_results": (
                [
                    {
                        "action_id": uuid4(),
                        "ordinal": 1,
                        "result": {
                            "kind": "not_applicable",
                            "reason": "target_unavailable",
                        },
                    }
                ]
                if has_results
                else []
            ),
            "recovery": recovery,
        }
        if expected:
            TurnResponse.model_validate(payload)
        else:
            with pytest.raises(ValidationError):
                TurnResponse.model_validate(payload)


@pytest.mark.contract
def test_discriminated_union_rejects_unknown_kind() -> None:
    adapter = TypeAdapter(PlayerTurnInput)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "request_id": uuid4(),
                "expected_state_version": 0,
                "actor_id": uuid4(),
                "content": {"kind": "attack", "text": "進む"},
            }
        )
