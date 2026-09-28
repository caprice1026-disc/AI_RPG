"""Bounded free actions must be typed before the worker may roll dice."""

from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from pydantic import ValidationError

import ai_rpg.application.workers as worker_module
from ai_rpg.application.ports import (
    CanonicalSnapshot,
    ResolutionWorkItem,
    ScenarioRunSnapshot,
    ScenarioSceneSnapshot,
)
from ai_rpg.application.ports.llm import ResolutionLLM
from ai_rpg.application.ports.repositories import ScenarioFact, ScenarioProgressUpdate
from ai_rpg.application.scenarios import ScenarioActionUnavailableError, ScenarioProgressor
from ai_rpg.application.workers import (
    ResolutionInputError,
    SkillCheckResolutionWorker,
    WorkerPhasePolicy,
)
from ai_rpg.contracts.llm_decisions import (
    ActionPlan,
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


def test_minor_failure_at_max_alert_still_allows_the_action() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    run = replace(_run(), alert_level=5)
    intent = OpenActionIntent(
        kind="open_action", approach="長椅子を動かす",
        check={"ability": "strength", "skill_ref": "athletics", "difficulty": "normal"},
        success={"facts": [{"fact_ref": "bench_aligned", "kind": "route",
                             "public_text": "長椅子が高窓への足場になった"}]},
        failure={"alert_delta": 1, "add_flags": ["alerted"]},
    )
    assert progressor.progress_open(run, intent, "success").alert_delta == 0
    assert progressor.progress_open(run, intent, "failure").alert_delta == 0
    assert progressor.progress_open(replace(run, alert_level=4), intent,
                                    "failure").alert_delta == 1
    calm = OpenActionIntent(
        kind="open_action", approach="周囲を落ち着かせる", check=None,
        success={"alert_delta": -1}, failure=None,
    )
    assert progressor.progress_open(replace(run, alert_level=0), calm,
                                    "success").alert_delta == 0
    assert progressor.progress_open(run, calm, "success").alert_delta == -1


def test_alert_change_is_available_to_the_result_narrator() -> None:
    run = replace(_run(), alert_level=2)
    worker = SkillCheckResolutionWorker(
        Mock(), Mock(spec=ResolutionLLM), MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    update = ScenarioProgressUpdate(
        from_scene_id=run.scenes[1].id, to_scene_id=None,
        add_flags=(), ending_ref=None, elapsed_actions=1, alert_delta=1,
    )
    assert "警戒が3" in worker._scenario_public_state_after(run, update).content


def test_minor_local_place_can_be_saved_without_a_major_scene_transition() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    entrance = _run(1)
    intent = OpenActionIntent(
        kind="open_action", approach="裏庭への崩れた塀に向かう", check=None,
        success={"facts": [{"fact_ref": "yard_wall", "kind": "place",
                            "public_text": "崩れた塀の陰に足を運んだ"}]}, failure=None,
    )
    update = progressor.progress_open(entrance, intent, "success")
    assert update.to_scene_id is None
    assert update.facts[0].fact_ref == "yard_wall"


def test_free_bench_search_can_propose_a_new_route_on_either_outcome() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    intent = OpenActionIntent(
        kind="open_action", approach="礼拝堂の長椅子を全てどけて隠し扉を探す",
        check={"ability": "strength", "skill_ref": "athletics", "difficulty": "normal"},
        success={"facts": [{"fact_ref": "bench_floor_seam", "kind": "route",
                            "public_text": "長椅子の下に床の継ぎ目を見つけた"}]},
        failure={"facts": [{"fact_ref": "bench_heavy", "kind": "clue",
                            "public_text": "長椅子は動かなかったが床に継ぎ目が見えた"}],
                 "alert_delta": 1},
    )
    assert progressor.progress_open(_run(2), intent, "success").facts[0].fact_ref == (
        "bench_floor_seam"
    )
    assert progressor.progress_open(_run(2), intent, "failure").facts[0].fact_ref == (
        "bench_heavy"
    )


def test_search_does_not_move_to_the_passage_without_player_travel_intent() -> None:
    run = _run(2)
    actor = UUID(int=10)
    work = ResolutionWorkItem(
        turn_id=UUID(int=11), campaign_id=run.campaign_id,
        scene_id=run.scenes[1].id, principal_id=UUID(int=12), actor_id=actor,
        actor_authorized=True, worker_epoch=1, max_actions=1,
        expected_state_version=0, player_text="礼拝堂の長椅子を全てどけて隠し扉を探す",
        recent_messages=(), route="mechanical",
    )
    snapshot = CanonicalSnapshot(
        campaign_id=run.campaign_id, state_version=0,
        characters=({"entity_id": actor, "current_hp": 10, "max_hp": 10,
                     "defense": 12, "attack_bonus": 1},),
        skills=(), equipment=(), inventory=(), skill_checks=(), entities=(),
        scenario_run=run,
    )
    worker = SkillCheckResolutionWorker(
        Mock(), Mock(spec=ResolutionLLM), MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    proposed_travel = OpenActionIntent(
        kind="open_action", approach="隠し扉から通路へ進む", check=None,
        success={"next_scene_ref": "passage"}, failure=None,
    )
    with pytest.raises(ResolutionInputError, match="移動"):
        worker._preflight_actions(work, snapshot, [proposed_travel])
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    with pytest.raises(ScenarioActionUnavailableError, match="移動"):
        progressor.validate_transition_request("長椅子を調べて移動しない", proposed_travel)
    progressor.validate_transition_request("隠し扉から通路へ進む", proposed_travel)


def test_impossible_space_trip_is_rejected_before_model_or_world_update() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    with pytest.raises(ScenarioActionUnavailableError, match="探索領域"):
        progressor.validate_player_request(_run(2), "崩れた壁から宇宙に向かう")
    intent = OpenActionIntent(
        kind="open_action", approach="崩れた壁から宇宙に向かう", check=None,
        success={"facts": [{"fact_ref": "space_path", "kind": "place",
                            "public_text": "宇宙へ移動した"}]}, failure=None,
    )
    with pytest.raises(ScenarioActionUnavailableError, match="探索領域"):
        progressor.progress_open(_run(2), intent, "success")
    progressor.validate_player_request(_run(2), "長椅子をどけ、宇宙を描いた古い壁画を探す")


@pytest.mark.asyncio
async def test_space_trip_spends_no_model_budget_or_dice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(2)
    work = ResolutionWorkItem(
        turn_id=UUID(int=11), campaign_id=run.campaign_id,
        scene_id=run.scenes[1].id, principal_id=UUID(int=12), actor_id=UUID(int=10),
        actor_authorized=True, worker_epoch=1, max_actions=1,
        expected_state_version=0, player_text="崩れた壁から宇宙に向かう",
        recent_messages=(), route="mechanical",
    )
    snapshot = CanonicalSnapshot(
        campaign_id=run.campaign_id, state_version=0,
        characters=(), skills=(), equipment=(), inventory=(), skill_checks=(),
        entities=(), scenario_run=run,
    )
    llm = Mock(spec=ResolutionLLM)
    llm.extract_intent = AsyncMock()
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr(worker_module, "_reserve_call", reserve)
    worker = SkillCheckResolutionWorker(
        Mock(), llm, MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_finalize_not_applied", finalize)

    assert await worker._resolve_mechanical(work, snapshot)
    assert "探索領域" in finalize.await_args.args[1]
    llm.extract_intent.assert_not_awaited()
    reserve.assert_not_awaited()


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


def test_protected_lore_paraphrase_and_benign_context_are_separate() -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    forbidden = (
        "最奥の間は空っぽで、誰もおらず、品物も存在しない。",
        "銀の聖印は見つからず、祭壇には何もない。",
        "広間に聖印が隠されている。",
        "ゴブリンは聖印の見張りをやめ、理由を聞かずに立ち去った。",
    )
    allowed = (
        "ゴブリンの足元に倒れた長椅子がある。",
        "祭壇の脇に古い燭台がある。",
        "聖印の紋様が描かれた木片が落ちている。",
        "見張りの周囲に足跡が増えている。",
    )
    for index, public_text in enumerate(forbidden):
        intent = OpenActionIntent(
            kind="open_action", approach="周囲を調べる", check=None,
            success={"facts": [{"fact_ref": f"forbidden_{index}", "kind": "clue",
                                "public_text": public_text}]}, failure=None,
        )
        with pytest.raises(ScenarioActionUnavailableError):
            progressor.progress_open(_run(5), intent, "success")
    for index, public_text in enumerate(allowed):
        intent = OpenActionIntent(
            kind="open_action", approach="周囲を調べる", check=None,
            success={"facts": [{"fact_ref": f"allowed_{index}", "kind": "clue",
                                "public_text": public_text}]}, failure=None,
        )
        assert progressor.progress_open(_run(5), intent, "success").facts[0].public_text == (
            public_text
        )


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


def test_combat_risk_is_confirmed_once_and_not_after_enemy_is_defeated() -> None:
    worker = SkillCheckResolutionWorker(
        Mock(), Mock(spec=ResolutionLLM), MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    enemy_id = UUID(int=30)
    attack = AttackIntent(kind="attack", target_ref="goblin", weapon_ref="iron_sword")

    def combat_snapshot(flags: frozenset[str], hp: int) -> CanonicalSnapshot:
        return CanonicalSnapshot(
            campaign_id=UUID(int=1), state_version=0, characters=(
                {"entity_id": enemy_id, "current_hp": hp},),
            skills=(), equipment=(), inventory=(), skill_checks=(),
            entities=({"id": enemy_id, "ref": "goblin", "kind": "npc",
                       "archived_at": None},), scenario_run=_run(5, flags),
        )

    assert worker._risk_for_actions([attack], combat_snapshot(frozenset(), 4)) is not None
    ongoing = combat_snapshot(frozenset({"combat_started"}), 4)
    assert worker._risk_for_actions([attack], ongoing) is None
    assert worker._risk_for_actions([attack], combat_snapshot(frozenset(), 0)) is None


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
        expected_state_version=0, player_text="長椅子を足場にして高窓から通路へ進む",
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


@pytest.mark.asyncio
async def test_invalid_yard_scene_proposal_is_repaired_as_a_local_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(1)
    actor = UUID(int=10)
    work = ResolutionWorkItem(
        turn_id=UUID(int=11), campaign_id=run.campaign_id,
        scene_id=run.scenes[0].id, principal_id=UUID(int=12), actor_id=actor,
        actor_authorized=True, worker_epoch=1, max_actions=1,
        expected_state_version=0, player_text="裏庭への崩れた壁の方に向かう",
        recent_messages=(), route="mechanical",
    )
    snapshot = CanonicalSnapshot(
        campaign_id=run.campaign_id, state_version=0,
        characters=({"entity_id": actor, "current_hp": 10, "max_hp": 10,
                     "defense": 12, "attack_bonus": 1},),
        skills=(), equipment=(), inventory=(), skill_checks=(),
        entities=({"id": actor, "ref": "hero", "label": "旅人", "kind": "pc",
                   "archived_at": None},), scenario_run=run,
        abilities=({"character_id": actor, "strength": 0, "agility": 0,
                    "insight": 0, "presence": 0, "specialty_skill": "perception"},),
    )
    invalid = OpenActionIntent(kind="open_action", approach=work.player_text, check=None,
                               success={"next_scene_ref": "yard"}, failure=None)
    valid = OpenActionIntent(kind="open_action", approach=work.player_text, check=None,
                             success={"facts": [{"fact_ref": "yard_wall", "kind": "place",
                                                 "public_text": "崩れた塀の陰に移動した"}]},
                             failure=None)
    llm = Mock(spec=ResolutionLLM)
    llm.extract_intent = AsyncMock(side_effect=[
        ActionPlan(kind="action_plan", actions=[invalid]),
        ActionPlan(kind="action_plan", actions=[valid]),
    ])
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr(worker_module, "_reserve_call", reserve)
    worker = SkillCheckResolutionWorker(
        Mock(), llm, MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    committed = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_commit_mechanical", committed)

    assert await worker._resolve_mechanical(work, snapshot)
    assert reserve.await_count == 2
    assert llm.extract_intent.await_count == 2
    assert "Destination" in llm.extract_intent.await_args_list[1].args[0].proposal_feedback
    assert committed.await_args.args[-1].facts[0].fact_ref == "yard_wall"
