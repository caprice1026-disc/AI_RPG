"""Bounded free actions must be typed before the worker may roll dice."""

from unittest.mock import Mock
from uuid import UUID

import pytest
from pydantic import ValidationError

from ai_rpg.application.ports import (
    CanonicalSnapshot,
    ResolutionWorkItem,
    ScenarioRunSnapshot,
    ScenarioSceneSnapshot,
)
from ai_rpg.application.ports.llm import ResolutionLLM
from ai_rpg.application.ports.repositories import ScenarioFact
from ai_rpg.application.scenarios import ScenarioActionUnavailableError, ScenarioProgressor
from ai_rpg.application.workers import SkillCheckResolutionWorker, WorkerPhasePolicy
from ai_rpg.contracts.llm_decisions import (
    AttackIntent,
    OpenActionIntent,
    ScenarioActionIntent,
    UseItemIntent,
    make_decision_types,
)
from ai_rpg.domain.commands import OpenActionCommand
from ai_rpg.engine import DiceEngine, MvpV1Ruleset, SeededRandomSource
from ai_rpg.engine.ruleset import MvpV2Ruleset
from ai_rpg.scenarios import BUILTIN_SCENARIOS


def test_open_action_requires_both_outcomes_before_a_check() -> None:
    _, mechanical = make_decision_types(1)
    proposal = {
        "kind": "open_action", "approach": "長椅子を足場にして高窓へ向かう",
        "check": {"ability": "agility", "skill_ref": "acrobatics", "difficulty": "normal"},
        "success": {"next_scene_ref": "passage", "add_flags": ["shortcut_found"]},
        "failure": {"alert_delta": 1, "add_flags": ["alerted"]},
    }
    parsed = mechanical.validate_python({"kind": "action_plan", "actions": [proposal]})
    assert isinstance(parsed.actions[0], OpenActionIntent)
    assert parsed.actions[0].failure.alert_delta == 1
    with pytest.raises(ValidationError):
        mechanical.validate_python({"kind": "action_plan", "actions": [{
            **proposal, "failure": None,
        }]})


def test_open_action_rejects_unbounded_numeric_mutations() -> None:
    _, mechanical = make_decision_types(1)
    proposal = {
        "kind": "open_action", "approach": "長椅子を動かす", "check": None,
        "success": {"facts": [{"fact_ref": "high_window_step", "kind": "route",
                              "public_text": "高窓への足場ができた"}]},
        "failure": None,
    }
    assert mechanical.validate_python({"kind": "action_plan", "actions": [proposal]})
    with pytest.raises(ValidationError):
        mechanical.validate_python({"kind": "action_plan", "actions": [{
            **proposal, "success": {"hp_delta": 99},
        }]})


def test_v2_ability_and_specialty_modifier_is_deterministic() -> None:
    rules = MvpV2Ruleset(DiceEngine(SeededRandomSource(1)))
    assert rules.open_modifier(
        ability="agility", skill_ref="acrobatics",
        scores={"strength": 0, "agility": 3, "insight": 2, "presence": 0},
        specialty="acrobatics",
    ) == 5
    assert rules.open_modifier(
        ability="agility", skill_ref="stealth",
        scores={"strength": 0, "agility": 3, "insight": 2, "presence": 0},
        specialty="acrobatics",
    ) == 3


def _run(scene_sequence: int = 2, flags: frozenset[str] = frozenset()) -> ScenarioRunSnapshot:
    return ScenarioRunSnapshot(
        campaign_id=UUID(int=1), scenario_ref="ruined_chapel", scenario_version=3,
        status="active", ending_ref=None,
        scenes=tuple(ScenarioSceneSnapshot(UUID(int=n + 1), n, "active" if n == scene_sequence
                                   else "closed" if n < scene_sequence else "planned")
                     for n in range(1, 7)), flags=flags,
    )


def test_open_success_can_take_a_shortcut_and_failure_can_raise_alert() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    intent = OpenActionIntent(
        kind="open_action", approach="長椅子を足場に高窓へ",
        check={"ability": "agility", "skill_ref": "acrobatics", "difficulty": "normal"},
        success={"next_scene_ref": "passage", "add_flags": ["shortcut_found"]},
        failure={"add_flags": ["alerted"], "alert_delta": 1},
    )
    success = progressor.progress_open(_run(), intent, "success")
    failure = progressor.progress_open(_run(), intent, "failure")
    assert success.to_scene_id == UUID(int=5)
    assert success.add_flags == ("shortcut_found",)
    assert success.elapsed_actions == 1
    assert failure.to_scene_id is None
    assert failure.alert_delta == 1


def test_open_cannot_rewrite_goal_or_finish_without_required_flag() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    base = {"kind": "open_action", "approach": "抜け道を探す", "check": None,
            "failure": None}
    with pytest.raises(ScenarioActionUnavailableError):
        progressor.progress_open(_run(), OpenActionIntent(**base, success={
            "add_flags": ["relic_recovered"]}), "success")
    with pytest.raises(ScenarioActionUnavailableError):
        progressor.progress_open(_run(), OpenActionIntent(**base, success={
            "ending_ref": "recovered"}), "success")
    with pytest.raises(ScenarioActionUnavailableError):
        progressor.progress_open(_run(), OpenActionIntent(**base, success={
            "ending_ref": "defeated"}), "success")
    with pytest.raises(ScenarioActionUnavailableError):
        progressor.progress_open(
            _run(5, frozenset({"relic_recovered"})),
            OpenActionIntent(**base, success={"ending_ref": "costly_success"}),
            "success",
        )


def test_generated_fact_ref_cannot_be_reused_for_a_different_fact() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    snapshot = _run()
    snapshot = ScenarioRunSnapshot(
        campaign_id=snapshot.campaign_id, scenario_ref=snapshot.scenario_ref,
        scenario_version=snapshot.scenario_version, status=snapshot.status,
        ending_ref=None, scenes=snapshot.scenes, flags=snapshot.flags,
        facts=(ScenarioFact("high_window_step", "route", "高窓への足場", snapshot.scenes[1].id),),
    )
    intent = OpenActionIntent(kind="open_action", approach="高窓を調べる", check=None,
                              success={"facts": [{"fact_ref": "high_window_step",
                                                  "kind": "clue", "public_text": "別の手掛かり"}]},
                              failure=None)
    with pytest.raises(ScenarioActionUnavailableError):
        progressor.progress_open(snapshot, intent, "success")


def test_generated_fact_cannot_shadow_or_relocate_protected_lore() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    for fact_ref, public_text in (
        ("relic_location", "広間に別の扉がある"),
        ("untrue_relic", "聖印は広間の長椅子の下にある"),
        ("empty_altar", "祭壇には何も置かれていない"),
        ("empty_inner_room", "奥の部屋は空になっている"),
    ):
        proposal = OpenActionIntent(
            kind="open_action", approach="広間を調べる", check=None,
            success={"facts": [{"fact_ref": fact_ref, "kind": "clue",
                                "public_text": public_text}]}, failure=None,
        )
        with pytest.raises(ScenarioActionUnavailableError):
            progressor.progress_open(_run(), proposal, "success")


def test_v3_allows_early_goal_and_alternative_ending_at_core_location() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    for ending_ref, flag_ref in (
        ("recovered", "relic_recovered"),
        ("alternative_resolution", "alternative_resolved"),
    ):
        intent = OpenActionIntent(
            kind="open_action", approach="祭壇で別の方法を試す", check=None,
            success={"ending_ref": ending_ref, "add_flags": [flag_ref]}, failure=None,
        )
        update = progressor.progress_open(_run(5), intent, "success")
        assert update.ending_ref == ending_ref
        assert update.add_flags == (flag_ref,)
        assert update.elapsed_actions == 1


def test_generated_person_must_exist_in_the_current_landmark() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    snapshot = _run(2)
    snapshot = ScenarioRunSnapshot(
        campaign_id=snapshot.campaign_id, scenario_ref=snapshot.scenario_ref,
        scenario_version=snapshot.scenario_version, status=snapshot.status,
        ending_ref=None, scenes=snapshot.scenes, flags=snapshot.flags,
        facts=(ScenarioFact("keeper", "person", "奥の番人", snapshot.scenes[4].id),),
    )
    intent = OpenActionIntent(kind="open_action", approach="番人に話す", check=None,
                              target_fact_ref="keeper", success={}, failure=None)
    with pytest.raises(ScenarioActionUnavailableError):
        progressor.progress_open(snapshot, intent, "success")


def test_generated_place_has_stable_ref_in_public_context_and_can_be_targeted() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    snapshot = _run(2)
    fact = ScenarioFact("window_step", "place", "高窓の足場", snapshot.scenes[1].id)
    snapshot = ScenarioRunSnapshot(
        campaign_id=snapshot.campaign_id, scenario_ref=snapshot.scenario_ref,
        scenario_version=snapshot.scenario_version, status=snapshot.status,
        ending_ref=None, scenes=snapshot.scenes, flags=snapshot.flags, facts=(fact,),
    )
    context = progressor.public_context_for(snapshot)
    assert [(item.fact_ref, item.scene_ref) for item in context.generated_facts] == [
        ("window_step", "hall")
    ]
    intent = OpenActionIntent(kind="open_action", approach="足場を登る", check=None,
                              target_fact_ref="window_step", success={}, failure=None)
    assert progressor.progress_open(snapshot, intent, "success").elapsed_actions == 1
    absent = intent.model_copy(update={"target_fact_ref": "unknown"})
    with pytest.raises(ScenarioActionUnavailableError):
        progressor.progress_open(snapshot, absent, "success")


def test_v3_rejects_multiple_actions_before_any_roll() -> None:
    worker = SkillCheckResolutionWorker(
        Mock(), Mock(spec=ResolutionLLM), MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    snapshot = CanonicalSnapshot(
        campaign_id=UUID(int=1), state_version=0, characters=(), skills=(), equipment=(),
        inventory=(), skill_checks=(), entities=(), scenario_run=_run(),
    )
    intents = [
        UseItemIntent(kind="use_item", item_ref="healing_potion", target_ref=None),
        UseItemIntent(kind="use_item", item_ref="healing_potion", target_ref=None),
    ]
    with pytest.raises(ValueError, match="one action"):
        worker._scenario_bindings(snapshot, intents)


def test_v3_registered_ending_and_combat_attack_need_risk_preview() -> None:
    worker = SkillCheckResolutionWorker(
        Mock(), Mock(spec=ResolutionLLM), MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    entrance = CanonicalSnapshot(
        campaign_id=UUID(int=1), state_version=0, characters=(), skills=(), equipment=(),
        inventory=(), skill_checks=(), entities=(), scenario_run=_run(1),
    )
    retreat = ScenarioActionIntent(kind="scenario_action", action_ref="leave_entrance")
    assert worker._risk_for_actions([retreat], entrance) is not None
    sanctum = CanonicalSnapshot(
        campaign_id=UUID(int=1), state_version=0, characters=(), skills=(), equipment=(),
        inventory=(), skill_checks=(), entities=(), scenario_run=_run(5),
    )
    attack = AttackIntent(kind="attack", target_ref="goblin", weapon_ref="iron_sword")
    assert "HP" in worker._risk_for_actions([attack], sanctum)[1]


def test_v3_combat_exit_does_not_warn_of_an_impossible_counterattack() -> None:
    worker = SkillCheckResolutionWorker(
        Mock(), Mock(spec=ResolutionLLM), MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    sanctum = CanonicalSnapshot(
        campaign_id=UUID(int=1), state_version=0, characters=(), skills=(), equipment=(),
        inventory=(), skill_checks=(), entities=(),
        scenario_run=_run(5, frozenset({"combat_started"})),
    )
    retreat = ScenarioActionIntent(kind="scenario_action", action_ref="back_to_passage")
    assert worker._risk_for_actions([retreat], sanctum) is None


def test_v3_minor_setback_does_not_need_risk_confirmation_even_if_model_labels_it_major() -> None:
    worker = SkillCheckResolutionWorker(
        Mock(), Mock(spec=ResolutionLLM), MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    hall = CanonicalSnapshot(
        campaign_id=UUID(int=1), state_version=0, characters=(), skills=(), equipment=(),
        inventory=(), skill_checks=(), entities=(), scenario_run=_run(2),
    )
    intent = OpenActionIntent(
        kind="open_action", approach="長椅子を動かす",
        check={"ability": "strength", "skill_ref": None, "difficulty": "normal"},
        success={"next_scene_ref": "passage"},
        failure={"add_flags": ["alerted"], "alert_delta": 1},
        major_risk="物音で見張りが警戒するかもしれません。",
    )
    assert worker._risk_for_actions([intent], hall) is None
    costly = intent.model_copy(update={
        "failure": intent.failure.model_copy(update={"add_flags": ["costly"]}),
    })
    assert worker._risk_for_actions([costly], hall) is not None


def test_worker_resolves_bench_shortcut_with_saved_ability() -> None:
    run = _run()
    worker = SkillCheckResolutionWorker(
        Mock(), Mock(spec=ResolutionLLM), MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    actor = UUID(int=10)
    work = ResolutionWorkItem(
        turn_id=UUID(int=11), campaign_id=run.campaign_id,
        scene_id=run.scenes[1].id, principal_id=UUID(int=12), actor_id=actor,
        actor_authorized=True, worker_epoch=1, max_actions=3,
        expected_state_version=0, player_text="長椅子を足場にする",
        recent_messages=(), route="mechanical",
    )
    snapshot = CanonicalSnapshot(
        campaign_id=run.campaign_id, state_version=0,
        characters=({"entity_id": actor, "current_hp": 10, "max_hp": 10,
                     "defense": 12, "attack_bonus": 2},),
        skills=(), equipment=(), inventory=(), skill_checks=(),
        entities=({"id": actor, "ref": "hero", "label": "ミナ", "kind": "pc",
                   "archived_at": None},),
        scene_entities=(), scenario_run=run,
        abilities=({"character_id": actor, "strength": 0, "agility": 3,
                    "insight": 2, "presence": 0, "specialty_skill": "acrobatics"},),
    )
    intent = OpenActionIntent(
        kind="open_action", approach="長椅子を足場に高窓へ",
        check={"ability": "agility", "skill_ref": "acrobatics", "difficulty": "normal"},
        success={"next_scene_ref": "passage", "add_flags": ["shortcut_found"]},
        failure={"add_flags": ["alerted"], "alert_delta": 1},
    )
    records, resolved, _state, update = worker._resolve_actions(work, snapshot, [intent])
    assert isinstance(records[0].command, OpenActionCommand)
    assert records[0].command.modifier == 5
    assert resolved[0].result.outcome == "success"
    assert update is not None and update.to_scene_id == run.scenes[3].id
