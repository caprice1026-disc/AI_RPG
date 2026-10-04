"""Pinned item effects and protected facts at the actual worker boundaries."""

import json
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_rpg.application.ports import CanonicalSnapshot, ResolutionWorkItem
from ai_rpg.application.ports.repositories import ScenarioSceneSnapshot
from ai_rpg.application.scenarios import ScenarioProgressor
from ai_rpg.application.workers import (
    ResolutionInputError,
    SkillCheckResolutionWorker,
    WorkerPhasePolicy,
)
from ai_rpg.contracts.llm_decisions import UseItemIntent
from ai_rpg.contracts.responses import InventoryItem
from ai_rpg.domain.results import HealingApplied, ItemConsumed
from ai_rpg.engine import DiceEngine, MvpV1Ruleset
from ai_rpg.scenarios import BUILTIN_SCENARIOS, ScenarioCatalog, ScenarioDefinition

from .test_combat import Rolls
from .test_scenario_initialization import create_rows, garden, garden_payload, run_for


async def worker_case(definition: ScenarioDefinition | None = None, *, sequence: int = 2):
    definition = definition or garden()
    created, rows = await create_rows(definition)
    run = replace(run_for(definition, sequence), campaign_id=created.campaign_id, scenes=tuple(
        ScenarioSceneSnapshot(row["id"], row["sequence"],
                              "active" if row["sequence"] == sequence else "planned")
        for row in rows["scenes"]
    ))
    work = ResolutionWorkItem(
        turn_id=uuid4(), campaign_id=created.campaign_id, scene_id=run.scenes[sequence - 1].id,
        principal_id=uuid4(), actor_id=created.actor_id, actor_authorized=True,
        worker_epoch=1, max_actions=1, expected_state_version=0,
        player_text="Drink the leaf tonic", recent_messages=(), route="mechanical",
    )
    snapshot = CanonicalSnapshot(
        campaign_id=created.campaign_id, state_version=0,
        characters=tuple({**row, "current_hp": 3} if row["entity_id"] == created.actor_id
                         else row for row in rows["mvp_characters"]),
        skills=tuple(rows["mvp_skill_modifiers"]), equipment=tuple(rows["mvp_weapons"]),
        inventory=tuple(rows["mvp_inventory"]),
        skill_checks=tuple({**row, "target_id": None} for row in rows["mvp_scene_skill_checks"]),
        entities=tuple({**row, "archived_at": None} for row in rows["entities"]),
        scene_entities=tuple(row for row in rows["mvp_scene_entities"]
                             if row["scene_id"] == work.scene_id),
        scenario_run=run, abilities=tuple(rows["mvp_character_abilities"]),
    )
    return work, snapshot


def worker(rolls=(), catalog=None) -> SkillCheckResolutionWorker:
    return SkillCheckResolutionWorker(
        Mock(), Mock(), MvpV1Ruleset(DiceEngine(Rolls(rolls))),
        WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=ScenarioProgressor(catalog or ScenarioCatalog(())),
    )


@pytest.mark.asyncio
async def test_custom_item_binds_and_consumes_pinned_effect_once() -> None:
    work, snapshot = await worker_case()
    payload = garden_payload()
    payload["initialization"]["items"][1]["effect_ref"] = None
    changed_catalog = ScenarioCatalog((ScenarioDefinition.model_validate(payload),))
    resolver = worker([4], changed_catalog)
    intent = UseItemIntent(kind="use_item", item_ref="leaf_tonic", target_ref=None)
    plan = resolver._preflight_actions(work, snapshot, [intent])
    tonic_id = next(row["id"] for row in snapshot.entities if row["ref"] == "leaf_tonic")
    assert plan.scenario_bindings == [None]
    assert plan.item_effects == {tonic_id: "healing_potion"}
    records, _, _, progress = resolver._resolve_actions(work, snapshot, [intent], preflight=plan)
    command, result = records[0].command, records[0].result
    assert (command.item_id, command.effect_ref) == (tonic_id, "healing_potion")
    assert result.dice[0].expression == "1d6+2"
    healing, consumed = result.state_changes
    assert isinstance(healing, HealingApplied) and healing.hp_after == 9
    assert isinstance(consumed, ItemConsumed) and consumed.quantity_after == 1
    assert progress.elapsed_actions == 1 and progress.add_flags == ()


@pytest.mark.parametrize("failure", [
    "unowned", "empty", "full_hp", "other_target", "weapon", "unknown", "name_spoof",
    "pinned_effect_removed",
])
@pytest.mark.asyncio
async def test_invalid_item_use_fails_before_any_dice(failure: str) -> None:
    payload = garden_payload()
    item_ref, target_ref = "leaf_tonic", None
    if failure == "name_spoof":
        payload["initialization"]["items"][1].update(ref="healing_potion", effect_ref=None)
        item_ref = "healing_potion"
    if failure == "pinned_effect_removed":
        payload["initialization"]["items"][1]["effect_ref"] = None
    work, snapshot = await worker_case(ScenarioDefinition.model_validate(payload))
    tonic_id = next(row["id"] for row in snapshot.entities if row["ref"] == item_ref)
    if failure == "unowned":
        snapshot = replace(snapshot, inventory=tuple(
            {**row, "owner_id": uuid4()} if row["item_id"] == tonic_id else row
            for row in snapshot.inventory
        ))
    if failure == "empty":
        snapshot = replace(snapshot, inventory=tuple(
            {**row, "quantity": 0} if row["item_id"] == tonic_id else row
            for row in snapshot.inventory
        ))
    if failure == "full_hp":
        snapshot = replace(snapshot, characters=tuple(
            {**row, "current_hp": row["max_hp"]} for row in snapshot.characters
        ))
    if failure == "other_target":
        target_ref = "brass_moth"
    if failure == "weapon":
        item_ref = "pruning_hook"
    if failure == "unknown":
        item_ref = "magic_bottle"
    # A non-empty latest catalog must not supply an effect missing from the pinned version.
    resolver = worker([], ScenarioCatalog((garden(),)))
    with pytest.raises(ResolutionInputError):
        resolver._resolve_actions(work, snapshot, [
            UseItemIntent(kind="use_item", item_ref=item_ref, target_ref=target_ref),
        ])


@pytest.mark.asyncio
async def test_development_seed_without_scenario_keeps_original_potion_behavior() -> None:
    work, snapshot = await worker_case()
    snapshot = replace(snapshot, scenario_run=None, entities=tuple(
        {**row, "ref": "healing_potion"} if row["ref"] == "leaf_tonic" else row
        for row in snapshot.entities
    ))
    records, _, _, progress = worker([1])._resolve_actions(work, snapshot, [
        UseItemIntent(kind="use_item", item_ref="healing_potion", target_ref="hero"),
    ])
    assert records[0].result.state_changes[0].hp_after == 6
    assert progress is None


@pytest.mark.parametrize("quantity", [0, 1])
@pytest.mark.asyncio
async def test_context_offers_item_use_only_for_owned_known_effects(quantity: int) -> None:
    work, snapshot = await worker_case()
    tonic_id = next(row["id"] for row in snapshot.entities if row["ref"] == "leaf_tonic")
    snapshot = replace(snapshot, inventory=tuple(
        {**row, "quantity": quantity} if row["item_id"] == tonic_id else row
        for row in snapshot.inventory
    ))
    context = worker()._mechanical_input(work, snapshot)
    assert ("use_item" in context.supported_action_types) is bool(quantity)
    items = {item["item_ref"]: item for item in json.loads(context.pc_view.content)["inventory"]}
    assert items["leaf_tonic"]["effect_ref"] == "healing_potion"
    assert items["pruning_hook"]["effect_ref"] is None


@pytest.mark.parametrize(("sequence", "flags", "expected"), [
    (1, frozenset(), ["The gardener waits in the garden."]),
    (2, frozenset(), []),
    (2, frozenset({"seed_seen"}), ["The seed pod hangs beneath the greenhouse clock."]),
    (2, frozenset({"seed_seen", "seeds_collected"}), []),
])
@pytest.mark.asyncio
async def test_worker_public_context_respects_location_reveal_and_author_only(
    sequence: int, flags: frozenset[str], expected: list[str],
) -> None:
    work, snapshot = await worker_case(sequence=sequence)
    snapshot = replace(snapshot, scenario_run=replace(snapshot.scenario_run, flags=flags))
    resolver = worker()
    for context in (
        resolver._mechanical_input(work, snapshot), resolver._narrative_input(work, snapshot),
    ):
        scene = json.loads(context.scene_view.content)
        assert scene["world"]["protected_facts"] == expected
        assert "Author-only planning note" not in context.model_dump_json()
        if not flags:
            assert "hangs beneath the greenhouse clock" not in context.model_dump_json()


@pytest.mark.parametrize(("ref", "version"), [("ruined_chapel", 3), ("mist_lighthouse", 1)])
def test_legacy_public_protected_fact_projection_is_unchanged(ref: str, version: int) -> None:
    definition = BUILTIN_SCENARIOS.get(ref, version)
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    for scene in definition.scenes:
        run = run_for(definition, scene.sequence)
        assert progressor.public_protected_facts(run) == tuple(
            fact.statement for fact in definition.world.protected_facts
            if fact.scene_ref is None or fact.scene_ref == scene.scene_ref
        )


def test_inventory_dto_exposes_only_supported_optional_effects() -> None:
    fields = {
        "item_id": uuid4(), "item_ref": "leaf_tonic", "name": "Leaf tonic",
        "quantity": 1, "equipped": False,
    }
    assert InventoryItem.model_validate(fields).effect_ref is None
    item = InventoryItem.model_validate({**fields, "effect_ref": "healing_potion"})
    assert item.model_dump(mode="json")["effect_ref"] == "healing_potion"
    with pytest.raises(ValidationError):
        InventoryItem.model_validate({**fields, "effect_ref": "custom_script"})
