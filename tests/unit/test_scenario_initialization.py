"""S1: independently authored definitions reach the existing runtime boundaries."""

import json
from dataclasses import replace
from importlib.resources import files
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ai_rpg.application.adventures import PRESETS
from ai_rpg.application.ports import CanonicalSnapshot, ResolutionWorkItem
from ai_rpg.application.ports.repositories import ScenarioRunSnapshot, ScenarioSceneSnapshot
from ai_rpg.application.scenarios import (
    ScenarioActionUnavailableError,
    ScenarioProgressor,
    ScenarioStateError,
)
from ai_rpg.application.workers import SkillCheckResolutionWorker, WorkerPhasePolicy
from ai_rpg.contracts.adventures import CreateAdventureRequest
from ai_rpg.contracts.llm_decisions import AttackIntent, OpenActionIntent, SkillCheckIntent
from ai_rpg.domain.commands import UseItemCommand
from ai_rpg.domain.models import CharacterState
from ai_rpg.engine import DiceEngine, MvpV1Ruleset, SeededRandomSource
from ai_rpg.infrastructure.postgres.adventures import PostgresAdventureStore
from ai_rpg.scenarios import BUILTIN_SCENARIOS, ScenarioCatalog, ScenarioDefinition
from ai_rpg.scenarios.initialization import initialization_for, item_effect_ref


def garden_payload() -> dict[str, Any]:
    return json.loads(files("ai_rpg.scenarios").joinpath("clockwork_garden.json").read_text())


def garden() -> ScenarioDefinition:
    return ScenarioDefinition.model_validate(garden_payload())


def run_for(definition: ScenarioDefinition, sequence: int = 1) -> ScenarioRunSnapshot:
    return ScenarioRunSnapshot(
        campaign_id=uuid4(), scenario_ref=definition.scenario_ref,
        scenario_version=definition.version, status="active", ending_ref=None,
        flags=frozenset(), definition=definition, story_version_id=uuid4(),
        scenes=tuple(ScenarioSceneSnapshot(
            id=uuid4(), sequence=scene.sequence,
            status="active" if scene.sequence == sequence else "planned",
        ) for scene in definition.scenes),
    )


@pytest.mark.parametrize(("path", "value"), [
    (("schema_version",), 3),
    (("ruleset_ref",), "future_rules"),
    (("required_capabilities",), ["teleportation"]),
    (("required_capabilities",), []),
    (("required_capabilities",), ["combat", "combat"]),
    (("initialization",), None),
    (("initialization", "characters", 0, "ref"), "hero"),
    (("initialization", "characters", 0, "ref"), "brass_moth"),
    (("initialization", "characters", 0, "max_hp"), 1001),
    (("initialization", "characters", 0, "max_hp"), True),
    (("initialization", "characters", 0, "defense"), 31),
    (("initialization", "characters", 0, "attack_bonus"), 21),
    (("initialization", "items", 0, "weapon", "damage_expression"), "99d100"),
    (("initialization", "items", 0, "weapon", "damage_bonus"), 21),
    (("initialization", "items", 0, "owner_ref"), "missing"),
    (("initialization", "items", 1, "effect_ref"), "arbitrary_script"),
    (("initialization", "items", 1, "quantity"), 100),
    (("initialization", "items", 1, "equipped"), True),
    (("initialization", "placements", 0, "entity_ref"), "ghost"),
    (("initialization", "placements", 0, "scene_ref"), "missing"),
    (("initialization", "placements", 1, "visibility"), "secret"),
    (("initialization", "placements", 1, "attackable"), False),
    (("initialization", "placements", 2, "entity_ref"), "leaf_tonic"),
    (("scenes", 1, "actions", 0, "check_ref"), "impossible"),
    (("scenes", 1, "actions", 1, "target_ref"), "ghost"),
    (("world", "protected_facts", 1, "entity_ref"), "ghost"),
    (("world", "protected_facts", 1, "entity_ref"), None),
    (("world", "protected_facts", 1, "scene_ref"), "garden"),
    (("world", "protected_facts", 1, "reveal_flag_ref"), "missing"),
    (("world", "protected_facts", 1, "acquired_flag_ref"), "missing"),
    (("world", "protected_facts", 2, "reveal_flag_ref"), "seed_seen"),
])
def test_rejects_unsupported_or_unresolvable_initialization(
    path: tuple[str | int, ...], value: Any,
) -> None:
    payload = garden_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ScenarioDefinition.model_validate(payload)


def test_pinned_definition_wins_over_same_named_catalog_entry() -> None:
    original = garden()
    other = original.model_copy(update={"title": "Changed after this run started"})
    progressor = ScenarioProgressor(ScenarioCatalog((other,)))
    run = run_for(original)
    assert progressor.definition_for(run) is original
    with pytest.raises(ScenarioStateError, match="missing"):
        progressor.definition_for(replace(run, definition=None))
    with pytest.raises(ScenarioStateError, match="match"):
        progressor.definition_for(replace(run, scenario_version=999))


def test_authored_definition_can_finish_or_withdraw_without_catalog_registration() -> None:
    definition = garden()
    progressor = ScenarioProgressor(ScenarioCatalog(()))
    run = run_for(definition)
    enter = progressor.bind_registered_action(run, "enter_greenhouse")
    moved = progressor.progress_for(run, enter, "success", None)
    assert moved is not None and moved.to_scene_id == run.scenes[1].id
    inside = replace(run, scenes=tuple(
        replace(scene, status="active" if scene.sequence == 2 else "closed")
        for scene in run.scenes
    ))
    collect = progressor.bind_registered_action(inside, "collect_seed")
    finished = progressor.progress_for(inside, collect, "success", None)
    assert finished is not None and finished.ending_ref == "harvested"
    assert finished.add_flags == ("seeds_collected",)
    leave = progressor.bind_registered_action(run, "leave_garden")
    assert progressor.progress_for(run, leave, "success", None).ending_ref == "left"


def test_protected_location_applies_to_any_item_not_only_the_goal() -> None:
    payload = garden_payload()
    payload["flags"].append({"flag_ref": "bonus_taken", "public_fact": "Took the seed."})
    payload["scenes"][0]["open_flags"].append("bonus_taken")
    payload["scenes"][1]["open_flags"].append("bonus_taken")
    payload["world"]["protected_facts"][1]["acquired_flag_ref"] = "bonus_taken"
    definition = ScenarioDefinition.model_validate(payload)
    progressor = ScenarioProgressor(ScenarioCatalog(()))
    intent = OpenActionIntent(
        kind="open_action", approach="Take the seed", check=None,
        success={"add_flags": ["bonus_taken"]}, failure=None,
    )
    with pytest.raises(ScenarioActionUnavailableError, match="authored location"):
        progressor.progress_open(run_for(definition, 1), intent, "success")
    assert progressor.progress_open(run_for(definition, 2), intent, "success").add_flags == (
        "bonus_taken",
    )


@pytest.mark.parametrize("ref", ["seed_location", "seed_pod", "botanist"])
def test_generated_facts_cannot_shadow_authored_facts_or_entities(ref: str) -> None:
    progressor = ScenarioProgressor(ScenarioCatalog(()))
    intent = OpenActionIntent(
        kind="open_action", approach="Observe", check=None, failure=None,
        success={"facts": [{"fact_ref": ref, "kind": "clue", "public_text": "A replacement"}]},
    )
    with pytest.raises(ScenarioActionUnavailableError):
        progressor.progress_open(run_for(garden()), intent, "success")


def test_protected_visibility_never_publishes_author_notes() -> None:
    progressor = ScenarioProgressor(ScenarioCatalog(()))
    definition = garden()
    assert progressor.public_protected_facts(run_for(definition)) == (
        "The gardener waits in the garden.",
    )
    run = run_for(definition, 2)
    assert progressor.public_protected_facts(run) == ()
    assert progressor.public_protected_facts(replace(run, flags=frozenset({"seed_seen"}))) == (
        "The seed pod hangs beneath the greenhouse clock.",
    )
    assert progressor.public_protected_facts(replace(
        run, flags=frozenset({"seed_seen", "seeds_collected"}),
    )) == ()


def test_chapel_text_policy_is_not_applied_to_independent_authors() -> None:
    definition = garden().model_copy(update={"scenario_ref": "ruined_chapel", "version": 3})
    intent = OpenActionIntent(
        kind="open_action", approach="Observe", check=None, failure=None,
        success={"facts": [{"fact_ref": "observation", "kind": "clue",
                            "public_text": "ゴブリンはいない。聖印は広間にある。"}]},
    )
    result = ScenarioProgressor(ScenarioCatalog(())).progress_open(
        run_for(definition), intent, "success",
    )
    assert len(result.facts) == 1


async def create_rows(definition: ScenarioDefinition) -> tuple[Any, dict[str, list[Any]]]:
    session = AsyncMock()
    session.scalar.return_value = uuid4()
    sessions = Mock()
    sessions.begin.return_value.__aenter__ = AsyncMock(return_value=session)
    sessions.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    request = CreateAdventureRequest(
        request_id=uuid4(), scenario_ref=definition.scenario_ref,
        scenario_version=definition.version, preset_ref="scout", player_name="Ada",
        ability_points={"strength": 0, "agility": 1, "insight": 1, "presence": 0},
        specialty_skill="perception",
    )
    created = await PostgresAdventureStore(sessions).create(
        uuid4(), request, definition, PRESETS[0],
    )
    rows: dict[str, list[Any]] = {}
    for call in session.execute.call_args_list:
        statement = call.args[0]
        if not getattr(statement, "is_insert", False):
            continue
        values = call.args[1] if len(call.args) > 1 else [statement.compile().params]
        rows.setdefault(statement.table.name, []).extend(values)
    return created, rows


@pytest.mark.asyncio
async def test_created_entities_resolve_through_attack_skill_and_public_snapshot() -> None:
    definition = garden()
    created, rows = await create_rows(definition)
    entities = {row["ref"]: {**row, "archived_at": None} for row in rows["entities"]}
    assert set(entities) == {
        "hero", "botanist", "brass_moth", "pruning_hook", "leaf_tonic", "seed_pod",
    }
    assert entities["brass_moth"]["kind"] == "npc"
    run = replace(run_for(definition, 2), campaign_id=created.campaign_id, scenes=tuple(
        ScenarioSceneSnapshot(row["id"], row["sequence"],
                              "active" if row["sequence"] == 2 else "closed")
        for row in rows["scenes"]
    ))
    work = ResolutionWorkItem(
        turn_id=uuid4(), campaign_id=created.campaign_id, scene_id=run.scenes[1].id,
        principal_id=uuid4(), actor_id=created.actor_id, actor_authorized=True,
        worker_epoch=1, max_actions=1, expected_state_version=0, player_text="Search",
        recent_messages=(), route="mechanical",
    )
    snapshot = CanonicalSnapshot(
        campaign_id=created.campaign_id, state_version=0,
        characters=tuple(rows["mvp_characters"]), skills=tuple(rows["mvp_skill_modifiers"]),
        equipment=tuple(rows["mvp_weapons"]), inventory=tuple(rows["mvp_inventory"]),
        skill_checks=tuple({**row, "target_id": None} for row in rows["mvp_scene_skill_checks"]),
        entities=tuple(entities.values()),
        scene_entities=tuple(row for row in rows["mvp_scene_entities"]
                             if row["scene_id"] == work.scene_id),
        scenario_run=run, abilities=tuple(rows["mvp_character_abilities"]),
    )
    worker = SkillCheckResolutionWorker(
        Mock(), Mock(), MvpV1Ruleset(DiceEngine(SeededRandomSource(1))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(ScenarioCatalog(())),
    )
    visible = {entity.ref for entity in worker._allowed_entity_refs(work, snapshot)}
    assert visible == {"hero", "brass_moth", "pruning_hook", "leaf_tonic"}
    attack = AttackIntent(kind="attack", target_ref="brass_moth", weapon_ref="pruning_hook")
    records, _, _, _ = worker._resolve_actions(work, snapshot, [attack])
    assert records[0].command.target_id == entities["brass_moth"]["id"]
    assert records[0].command.weapon_id == entities["pruning_hook"]["id"]
    assert records[0].command.damage_expression == "1d4"
    skill = SkillCheckIntent(
        kind="skill_check", skill_ref="perception", objective="Find the seed", target_ref=None,
    )
    records, _, _, _ = worker._resolve_actions(work, snapshot, [skill])
    assert records[0].command.difficulty_class == 8


@pytest.mark.asyncio
async def test_noncombat_creation_has_no_implicit_entities_or_inventory() -> None:
    payload = garden_payload()
    payload.update(world=None, initialization={}, required_capabilities=[])
    payload["scenes"] = [payload["scenes"][0]]
    payload["scenes"][0].update(open_destinations=[], open_flags=[])
    payload["scenes"][0]["actions"] = [payload["scenes"][0]["actions"][1]]
    definition = ScenarioDefinition.model_validate(payload)
    _, rows = await create_rows(definition)
    assert [row["ref"] for row in rows["entities"]] == ["hero"]
    assert "mvp_inventory" not in rows and "mvp_weapons" not in rows
    assert len(rows["mvp_characters"]) == 1


def test_renamed_consumable_resolves_known_engine_effect_without_name_dispatch() -> None:
    effect = item_effect_ref(garden(), "leaf_tonic")
    assert effect == "healing_potion"
    assert item_effect_ref(garden(), "pruning_hook") is None
    assert item_effect_ref(garden(), "healing_potion") is None
    actor = CharacterState(id=UUID(int=1), current_hp=3, max_hp=10, defense=10)
    command = UseItemCommand(
        kind="use_item", campaign_id=uuid4(), turn_id=uuid4(), action_id=uuid4(), ordinal=1,
        actor_id=actor.id, target_id=actor.id, item_id=UUID(int=2), effect_ref=effect,
    )
    result = MvpV1Ruleset(DiceEngine(SeededRandomSource(1))).resolve_use_item(
        command, actor, actor, quantity=2,
    )
    assert result.state_changes[0].hp_after > 3
    assert result.state_changes[1].quantity_after == 1


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.asyncio
async def test_chapel_keeps_exact_initial_combat_and_inventory_values(version: int) -> None:
    definition = BUILTIN_SCENARIOS.get("ruined_chapel", version)
    _, rows = await create_rows(definition)
    entities = {row["ref"]: row for row in rows["entities"]}
    assert set(entities) == {"hero", "goblin", "iron_sword", "healing_potion"}
    goblin = next(row for row in rows["mvp_characters"]
                  if row["entity_id"] == entities["goblin"]["id"])
    assert (goblin["max_hp"], goblin["current_hp"], goblin["defense"], goblin["attack_bonus"]) == (
        10, 10, 11, 1,
    )
    assert [(row["quantity"], row["equipped"]) for row in rows["mvp_inventory"]] == [
        (1, True), (2, False),
    ]
    weapon = rows["mvp_weapons"][0]
    assert (weapon["damage_expression"], weapon["damage_bonus"]) == ("1d6", 0)
    assert item_effect_ref(definition, "healing_potion") == "healing_potion"


def test_legacy_readable_payload_cannot_silently_spawn_implicit_entities() -> None:
    definition = garden().model_copy(update={"schema_version": 1, "initialization": None})
    with pytest.raises(ValueError, match="explicit initialization"):
        initialization_for(definition)


@pytest.mark.asyncio
async def test_lighthouse_keeps_equipment_without_an_unused_enemy() -> None:
    _, rows = await create_rows(BUILTIN_SCENARIOS.get("mist_lighthouse", 1))
    assert {row["ref"] for row in rows["entities"]} == {
        "hero", "iron_sword", "healing_potion",
    }
    assert len(rows["mvp_characters"]) == 1
    assert [(row["quantity"], row["equipped"]) for row in rows["mvp_inventory"]] == [
        (1, True), (2, False),
    ]


def test_protected_item_cannot_start_in_two_locations() -> None:
    payload = garden_payload()
    payload["initialization"]["placements"].append({
        "entity_ref": "seed_pod", "scene_ref": "garden", "visibility": "secret",
    })
    with pytest.raises(ValidationError, match="contradicts another placement"):
        ScenarioDefinition.model_validate(payload)
