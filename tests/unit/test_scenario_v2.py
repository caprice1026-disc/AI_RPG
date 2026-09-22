"""Validate the bounded adventure definition, without DB or provider calls."""

from importlib.resources import files

import pytest

from ai_rpg.scenarios import (
    BUILTIN_SCENARIOS,
    AttackScenarioAction,
    DirectScenarioAction,
    ScenarioCombatDefinition,
    ScenarioDefinition,
    SkillScenarioAction,
)


def test_catalog_keeps_both_ruined_chapel_versions() -> None:
    for version, filename in [(1, "ruined_chapel.json"), (2, "ruined_chapel_v2.json")]:
        scenario = BUILTIN_SCENARIOS.get("ruined_chapel", version)
        resource = files("ai_rpg.scenarios").joinpath(filename).read_text(encoding="utf-8")
        assert scenario == ScenarioDefinition.model_validate_json(resource)
        assert scenario.version == version


def test_v2_has_six_ordered_scenes_and_unambiguous_engine_bindings() -> None:
    scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 2)
    assert [(scene.scene_ref, scene.sequence) for scene in scenario.scenes] == [
        ("entrance", 1),
        ("hall", 2),
        ("archive", 3),
        ("passage", 4),
        ("sanctum", 5),
        ("return", 6),
    ]
    for scene in scenario.scenes:
        skills = [action for action in scene.actions if isinstance(action, SkillScenarioAction)]
        assert len({action.skill_ref for action in skills}) == len(skills)
        for action in skills:
            assert action.skill_ref in {"perception", "persuasion", "stealth"}
            assert action.check_ref in {"easy", "normal", "hard"}
        attacks = [action for action in scene.actions if isinstance(action, AttackScenarioAction)]
        assert len({action.target_ref for action in attacks}) == len(attacks)
        assert all(action.target_ref == "goblin" for action in attacks)


@pytest.mark.parametrize(("scene_ref", "next_ref"), [("hall", "archive"), ("archive", "passage")])
def test_mandatory_checks_explicitly_fail_forward(scene_ref: str, next_ref: str) -> None:
    scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 2)
    scene = next(scene for scene in scenario.scenes if scene.scene_ref == scene_ref)
    checks = [action for action in scene.actions if isinstance(action, SkillScenarioAction)]
    assert len(checks) == 1
    check = checks[0]
    for effect in (check.success, check.failure):
        assert effect.next_scene_ref == next_ref
        assert effect.ending_ref is None
        assert effect.add_flags
    assert "costly" in check.failure.add_flags


def test_sanctum_routes_reach_return_and_combat_disables_only_peaceful_options() -> None:
    scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 2)
    scene = next(scene for scene in scenario.scenes if scene.scene_ref == "sanctum")
    assert isinstance(scene.combat, ScenarioCombatDefinition)
    assert scene.combat.enemy_ref == "goblin"
    assert scene.combat.started_flag == "combat_started"
    assert scene.combat.defeat_ending_ref == "defeated"
    assert scene.combat.damage_expression == "1d4"
    assert scene.combat.damage_bonus == 0
    assert scene.npc_notes
    assert all(other.combat is None for other in scenario.scenes if other is not scene)

    checks = [action for action in scene.actions if isinstance(action, SkillScenarioAction)]
    assert {action.skill_ref for action in checks} == {"persuasion", "stealth"}
    for check in checks:
        assert not check.required_flags
        assert check.disabled_flags == ("combat_started",)
        for effect in (check.success, check.failure):
            assert effect.next_scene_ref == "return"
            assert effect.ending_ref is None
        assert "costly" in check.failure.add_flags
        assert "costly" not in check.success.add_flags

    attack = next(action for action in scene.actions if isinstance(action, AttackScenarioAction))
    assert attack.required_flags == attack.disabled_flags == ()
    assert attack.defeated.next_scene_ref == "return"
    assert attack.defeated.ending_ref is None
    retreat = next(action for action in scene.actions if isinstance(action, DirectScenarioAction))
    assert retreat.required_flags == retreat.disabled_flags == ()
    assert retreat.success.ending_ref == "retreated"


def test_quest_preparation_and_relic_recovery_are_separate_direct_steps() -> None:
    scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 2)
    actions = {action.action_ref: action for scene in scenario.scenes for action in scene.actions}
    for first_ref, next_ref, flag in [
        ("accept_quest", "enter_chapel", "quest_accepted"),
        ("prepare_return_route", "approach_sanctum", "route_prepared"),
        ("recover_relic", "return_relic", "relic_recovered"),
    ]:
        first, following = actions[first_ref], actions[next_ref]
        assert isinstance(first, DirectScenarioAction)
        assert isinstance(following, DirectScenarioAction)
        assert first.success.next_scene_ref is None
        assert first.success.ending_ref is None
        assert flag in first.success.add_flags
        assert flag in first.disabled_flags
        assert flag in following.required_flags
    finish = actions["return_relic"]
    assert isinstance(finish, DirectScenarioAction)
    assert finish.success.ending_ref == "recovered"
    assert [
        (override.requires_flags, override.ending_ref) for override in finish.success.overrides
    ] == [
        (("costly",), "costly_success"),
    ]


def test_every_reachable_definition_state_can_terminate() -> None:
    scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 2)
    scenes = {scene.scene_ref: scene for scene in scenario.scenes}
    # Explore definition effects and the declared combat-start branch. HP resolution
    # and counterattack persistence belong to the controller's Engine/DB tests.
    state = ("entrance", frozenset[str]())
    pending = [state]
    edges: dict[tuple[str, frozenset[str]], set[tuple[str, frozenset[str]]]] = {}
    terminating: set[tuple[str, frozenset[str]]] = set()
    endings: set[str] = set()
    while pending:
        state = pending.pop()
        if state in edges:
            continue
        scene_ref, flags = state
        scene = scenes[scene_ref]
        available = [
            action
            for action in scene.actions
            if set(action.required_flags) <= flags and flags.isdisjoint(action.disabled_flags)
        ]
        assert available, f"No action available at {state}"
        edges[state] = set()
        for action in available:
            if isinstance(action, SkillScenarioAction):
                effects = (action.success, action.failure)
            elif isinstance(action, DirectScenarioAction):
                effects = (action.success,)
            else:
                effects = (action.defeated,)
                assert scene.combat is not None
                endings.add(scene.combat.defeat_ending_ref)
                terminating.add(state)
                if scene.combat.started_flag not in flags:
                    edges[state].add((scene_ref, flags | {scene.combat.started_flag}))
            for effect in effects:
                ending = next(
                    (
                        override.ending_ref
                        for override in effect.overrides
                        if set(override.requires_flags) <= flags
                    ),
                    effect.ending_ref,
                )
                if ending is not None:
                    endings.add(ending)
                    terminating.add(state)
                else:
                    destination = (
                        effect.next_scene_ref or scene_ref,
                        flags | set(effect.add_flags),
                    )
                    assert destination != state, f"Non-progressing definition effect at {state}"
                    edges[state].add(destination)
        pending.extend(edges[state] - edges.keys())

    assert {scene_ref for scene_ref, _flags in edges} == set(scenes)
    assert endings == {"recovered", "costly_success", "retreated", "defeated"}
    assert endings == {ending.ending_ref for ending in scenario.endings}
    while remaining := {
        state
        for state, destinations in edges.items()
        if state not in terminating and destinations & terminating
    }:
        terminating.update(remaining)
    assert terminating == edges.keys()
