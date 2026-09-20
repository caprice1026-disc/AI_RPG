"""Scenario定義から進行更新と公開Contextを作る純粋Application test。"""

from dataclasses import replace
from uuid import UUID

import pytest

from ai_rpg.application.ports import ScenarioRunSnapshot, ScenarioSceneSnapshot
from ai_rpg.application.scenarios import (
    ScenarioProgressor,
    ScenarioPublicContext,
    ScenarioStateError,
)
from ai_rpg.scenarios import (
    BUILTIN_SCENARIOS,
    AttackScenarioAction,
    DirectScenarioAction,
    SkillScenarioAction,
)

CAMPAIGN_ID = UUID(int=1)
ENTRANCE_ID = UUID(int=2)
HALL_ID = UUID(int=3)
SANCTUM_ID = UUID(int=4)
SCENE_IDS = (ENTRANCE_ID, HALL_ID, SANCTUM_ID)

progressor = ScenarioProgressor(BUILTIN_SCENARIOS)


def scenario_snapshot(
    active_sequence: int,
    *,
    flags: set[str] | None = None,
) -> ScenarioRunSnapshot:
    return ScenarioRunSnapshot(
        campaign_id=CAMPAIGN_ID,
        scenario_ref="ruined_chapel",
        scenario_version=1,
        status="active",
        ending_ref=None,
        scenes=tuple(
            ScenarioSceneSnapshot(
                id=scene_id,
                sequence=sequence,
                status=(
                    "closed"
                    if sequence < active_sequence
                    else "active"
                    if sequence == active_sequence
                    else "planned"
                ),
            )
            for sequence, scene_id in enumerate(SCENE_IDS, start=1)
        ),
        flags=frozenset(flags or ()),
    )


def entrance_snapshot() -> ScenarioRunSnapshot:
    return scenario_snapshot(1)


def hall_snapshot(*, flags: set[str] | None = None) -> ScenarioRunSnapshot:
    return scenario_snapshot(2, flags=flags)


def sanctum_snapshot(*, flags: set[str] | None = None) -> ScenarioRunSnapshot:
    return scenario_snapshot(3, flags=flags)


def direct_binding(snapshot: ScenarioRunSnapshot, action_ref: str) -> DirectScenarioAction:
    binding = progressor.bind_scenario_action(snapshot, action_ref)
    assert isinstance(binding, DirectScenarioAction)
    return binding


def skill_binding(snapshot: ScenarioRunSnapshot, skill_ref: str) -> SkillScenarioAction:
    binding = progressor.bind_skill_check(snapshot, skill_ref)
    assert isinstance(binding, SkillScenarioAction)
    return binding


def attack_binding(snapshot: ScenarioRunSnapshot, target_ref: str) -> AttackScenarioAction:
    binding = progressor.bind_attack(snapshot, target_ref)
    assert isinstance(binding, AttackScenarioAction)
    return binding


def test_enter_advances_to_hall() -> None:
    snapshot = entrance_snapshot()

    progress = progressor.progress_for(
        snapshot,
        binding=direct_binding(snapshot, "enter_chapel"),
        outcome="neutral",
        target_hp_after=None,
    )

    assert progress is not None
    assert progress.from_scene_id == ENTRANCE_ID
    assert progress.to_scene_id == HALL_ID
    assert progress.add_flags == ()
    assert progress.ending_ref is None


def test_search_failure_adds_alerted_and_advances() -> None:
    snapshot = hall_snapshot()

    progress = progressor.progress_for(
        snapshot,
        binding=skill_binding(snapshot, "perception"),
        outcome="failure",
        target_hp_after=None,
    )

    assert progress is not None
    assert progress.add_flags == ("alerted",)
    assert progress.to_scene_id == SANCTUM_ID
    assert progress.ending_ref is None


def test_search_success_adds_clue_and_advances() -> None:
    snapshot = hall_snapshot()

    progress = progressor.progress_for(
        snapshot,
        binding=skill_binding(snapshot, "perception"),
        outcome="success",
        target_hp_after=None,
    )

    assert progress is not None
    assert progress.add_flags == ("clue_found",)
    assert progress.to_scene_id == SANCTUM_ID
    assert progress.ending_ref is None


def test_alerted_persuasion_success_is_costly_success() -> None:
    snapshot = sanctum_snapshot(flags={"alerted"})

    progress = progressor.progress_for(
        snapshot,
        binding=skill_binding(snapshot, "persuasion"),
        outcome="success",
        target_hp_after=None,
    )

    assert progress is not None
    assert progress.ending_ref == "costly_success"


def test_stealth_failure_is_costly_success() -> None:
    snapshot = sanctum_snapshot()

    progress = progressor.progress_for(
        snapshot,
        binding=skill_binding(snapshot, "stealth"),
        outcome="failure",
        target_hp_after=None,
    )

    assert progress is not None
    assert progress.to_scene_id is None
    assert progress.ending_ref == "costly_success"


def test_attack_does_not_progress_while_target_has_hp() -> None:
    snapshot = sanctum_snapshot()

    progress = progressor.progress_for(
        snapshot,
        binding=attack_binding(snapshot, "goblin"),
        outcome="success",
        target_hp_after=1,
    )

    assert progress is None


def test_attack_progresses_when_target_reaches_zero_hp() -> None:
    snapshot = sanctum_snapshot()

    progress = progressor.progress_for(
        snapshot,
        binding=attack_binding(snapshot, "goblin"),
        outcome="success",
        target_hp_after=0,
    )

    assert progress is not None
    assert progress.from_scene_id == SANCTUM_ID
    assert progress.to_scene_id is None
    assert progress.ending_ref == "recovered"


def test_retreat_uses_registered_ending() -> None:
    snapshot = sanctum_snapshot()

    progress = progressor.progress_for(
        snapshot,
        binding=direct_binding(snapshot, "retreat"),
        outcome="neutral",
        target_hp_after=None,
    )

    assert progress is not None
    assert progress.ending_ref == "retreated"


def test_binding_rejects_unregistered_or_other_scene_actions() -> None:
    snapshot = entrance_snapshot()

    assert progressor.bind_scenario_action(snapshot, "retreat") is None
    assert progressor.bind_scenario_action(snapshot, "not_registered") is None
    assert progressor.bind_skill_check(snapshot, "perception") is None
    assert progressor.bind_attack(snapshot, "goblin") is None


def test_public_context_maps_flags_without_exposing_hidden_conditions() -> None:
    context = progressor.public_context_for(sanctum_snapshot(flags={"alerted"}))

    assert context == ScenarioPublicContext(
        scene_title="奥の部屋",
        scene_description="銀の聖印を守るゴブリンが待ち構えている。",
        objective="廃礼拝堂の奥から銀の聖印を回収する",
        discovered_facts=("礼拝堂の守衛に侵入を警戒されている。",),
        available_actions=(
            ("negotiate_guard", "守衛と交渉する"),
            ("sneak_to_relic", "聖印へ忍び寄る"),
            ("defeat_guard", "守衛を倒す"),
            ("retreat", "撤退する"),
        ),
    )
    assert "alerted" not in repr(context)
    assert "costly_success" not in repr(context)


def test_definition_for_rejects_unregistered_version() -> None:
    snapshot = replace(entrance_snapshot(), scenario_version=2)

    with pytest.raises(ScenarioStateError, match="Scenario定義"):
        progressor.definition_for(snapshot)


def test_public_context_rejects_missing_active_scene() -> None:
    snapshot = entrance_snapshot()
    scenes = tuple(replace(scene, status="planned") for scene in snapshot.scenes)

    with pytest.raises(ScenarioStateError, match="active Scene"):
        progressor.public_context_for(replace(snapshot, scenes=scenes))


def test_public_context_rejects_runtime_definition_sequence_mismatch() -> None:
    snapshot = entrance_snapshot()
    scenes = (*snapshot.scenes[:-1], replace(snapshot.scenes[-1], sequence=4))

    with pytest.raises(ScenarioStateError, match="sequence"):
        progressor.public_context_for(replace(snapshot, scenes=scenes))
