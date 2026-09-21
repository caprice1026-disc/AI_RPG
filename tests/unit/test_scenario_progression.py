"""Scenario定義から進行更新と公開Contextを作る純粋Application test。"""

from dataclasses import replace
from uuid import UUID

import pytest

from ai_rpg.application.ports import ScenarioRunSnapshot, ScenarioSceneSnapshot
from ai_rpg.application.scenarios import (
    ScenarioActionUnavailableError,
    ScenarioProgressor,
    ScenarioPublicContext,
    ScenarioStateError,
)
from ai_rpg.scenarios import (
    BUILTIN_SCENARIOS,
    AttackScenarioAction,
    DirectScenarioAction,
    ScenarioCatalog,
    ScenarioDefinition,
    SkillScenarioAction,
)

CAMPAIGN_ID = UUID(int=1)
ENTRANCE_ID = UUID(int=2)
HALL_ID = UUID(int=3)
SANCTUM_ID = UUID(int=4)
SCENE_IDS = (ENTRANCE_ID, HALL_ID, SANCTUM_ID)

progressor = ScenarioProgressor(BUILTIN_SCENARIOS)


def progressor_with_conditions(
    conditions: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
) -> ScenarioProgressor:
    payload = BUILTIN_SCENARIOS.get("ruined_chapel", 1).model_dump(mode="json")
    for scene in payload["scenes"]:
        for action in scene["actions"]:
            if action["action_ref"] in conditions:
                required, disabled = conditions[action["action_ref"]]
                action["required_flags"] = list(required)
                action["disabled_flags"] = list(disabled)
    definition = ScenarioDefinition.model_validate(payload)
    return ScenarioProgressor(ScenarioCatalog((definition,)))


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


@pytest.mark.parametrize(
    ("action_kind", "action_ref", "flags", "outcome", "expected_ending"),
    [
        ("skill", "persuasion", (), "success", "recovered"),
        ("skill", "persuasion", ("alerted",), "success", "costly_success"),
        ("skill", "persuasion", (), "failure", "costly_success"),
        ("skill", "persuasion", ("alerted",), "failure", "costly_success"),
        ("skill", "stealth", (), "success", "recovered"),
        ("skill", "stealth", ("alerted",), "success", "costly_success"),
        ("skill", "stealth", (), "failure", "costly_success"),
        ("skill", "stealth", ("alerted",), "failure", "costly_success"),
        ("attack", "goblin", (), "success", "recovered"),
        ("attack", "goblin", ("alerted",), "success", "costly_success"),
    ],
    ids=[
        "persuasion-success-unalerted",
        "persuasion-success-alerted",
        "persuasion-failure-unalerted",
        "persuasion-failure-alerted",
        "stealth-success-unalerted",
        "stealth-success-alerted",
        "stealth-failure-unalerted",
        "stealth-failure-alerted",
        "combat-complete-unalerted",
        "combat-complete-alerted",
    ],
)
def test_sanctum_progression_matrix(
    action_kind: str,
    action_ref: str,
    flags: tuple[str, ...],
    outcome: str,
    expected_ending: str,
) -> None:
    snapshot = sanctum_snapshot(flags=set(flags))
    binding = (
        skill_binding(snapshot, action_ref)
        if action_kind == "skill"
        else attack_binding(snapshot, action_ref)
    )

    progress = progressor.progress_for(
        snapshot,
        binding=binding,
        outcome=outcome,
        target_hp_after=0 if action_kind == "attack" else None,
    )

    assert progress is not None
    assert progress.to_scene_id is None
    assert progress.ending_ref == expected_ending


def test_attack_does_not_progress_while_target_has_hp() -> None:
    snapshot = sanctum_snapshot()

    progress = progressor.progress_for(
        snapshot,
        binding=attack_binding(snapshot, "goblin"),
        outcome="success",
        target_hp_after=1,
    )

    assert progress is None


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


@pytest.mark.parametrize(
    ("action_kind", "action_ref", "expected_type"),
    [
        ("direct", "retreat", DirectScenarioAction),
        ("skill", "persuasion", SkillScenarioAction),
        ("attack", "goblin", AttackScenarioAction),
    ],
)
def test_binding_enforces_required_and_disabled_flags(
    action_kind: str,
    action_ref: str,
    expected_type: type[object],
) -> None:
    conditional = progressor_with_conditions(
        {
            {
                "direct": "retreat",
                "skill": "negotiate_guard",
                "attack": "defeat_guard",
            }[action_kind]: (("clue_found",), ("alerted",))
        }
    )

    def bind(snapshot: ScenarioRunSnapshot) -> object | None:
        if action_kind == "direct":
            return conditional.bind_scenario_action(snapshot, action_ref)
        if action_kind == "skill":
            return conditional.bind_skill_check(snapshot, action_ref)
        return conditional.bind_attack(snapshot, action_ref)

    with pytest.raises(ScenarioActionUnavailableError):
        bind(sanctum_snapshot())
    assert isinstance(bind(sanctum_snapshot(flags={"clue_found"})), expected_type)
    with pytest.raises(ScenarioActionUnavailableError):
        bind(sanctum_snapshot(flags={"clue_found", "alerted"}))


def test_public_context_filters_unavailable_actions_without_exposing_conditions() -> None:
    conditional = progressor_with_conditions(
        {
            "negotiate_guard": (("clue_found",), ("alerted",)),
            "sneak_to_relic": ((), ("clue_found",)),
            "defeat_guard": (("clue_found",), ("alerted",)),
            "retreat": (("alerted",), ()),
        }
    )

    context = conditional.public_context_for(
        sanctum_snapshot(flags={"clue_found"})
    )

    assert context.available_actions == (
        ("negotiate_guard", "守衛と交渉する"),
        ("defeat_guard", "守衛を倒す"),
    )
    assert "required_flags" not in repr(context)
    assert "disabled_flags" not in repr(context)
    assert "clue_found" not in repr(context)


def test_progress_rejects_structurally_equal_non_identical_binding() -> None:
    snapshot = hall_snapshot()
    registered = skill_binding(snapshot, "perception")
    copied = registered.model_copy(deep=True)
    assert copied == registered
    assert copied is not registered

    with pytest.raises(ScenarioStateError, match="Action binding"):
        progressor.progress_for(
            snapshot,
            binding=copied,
            outcome="success",
            target_hp_after=None,
        )


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
