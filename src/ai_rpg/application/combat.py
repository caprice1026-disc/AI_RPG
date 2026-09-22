"""One rule-based enemy reaction; no LLM or persistence side effects."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID

from ai_rpg.application.ports.repositories import (
    ActionRecord,
    CanonicalSnapshot,
    ResolutionWorkItem,
    ScenarioProgressUpdate,
)
from ai_rpg.application.scenarios import ScenarioProgressor
from ai_rpg.domain.commands import AttackCommand
from ai_rpg.domain.events import EnemyReaction, RNGMetadata
from ai_rpg.domain.models import CharacterState
from ai_rpg.domain.results import AppliedResult, DamageApplied, HealingApplied
from ai_rpg.engine import MvpV1Ruleset


@dataclass(frozen=True, slots=True)
class CombatResolution:
    reactions: tuple[EnemyReaction, ...]
    scenario_update: ScenarioProgressUpdate | None


def resolve_enemy_reaction(
    work: ResolutionWorkItem,
    snapshot: CanonicalSnapshot,
    actions: tuple[ActionRecord, ...],
    scenario_update: ScenarioProgressUpdate | None,
    *,
    ruleset: MvpV1Ruleset,
    progressor: ScenarioProgressor | None,
    reaction_id_factory: Callable[[], UUID],
    rng_source: Literal["secure", "seeded_test", "recorded_replay"],
    rng_implementation_version: str,
) -> CombatResolution:
    unchanged = CombatResolution((), scenario_update)
    run = snapshot.scenario_run
    if run is None or progressor is None:
        return unchanged
    combat = progressor.scene_for(run).combat
    if combat is None or not any(isinstance(a.result, AppliedResult) for a in actions):
        return unchanged
    if scenario_update is not None and (
        scenario_update.to_scene_id is not None or scenario_update.ending_ref is not None
    ):
        return unchanged
    enemy = next(
        (
            e
            for e in snapshot.entities
            if e["ref"] == combat.enemy_ref and e["kind"] == "npc" and e["archived_at"] is None
        ),
        None,
    )
    if enemy is None or not any(
        row["entity_id"] == enemy["id"] and row["is_public"] and row["is_attack_reachable"]
        for row in snapshot.scene_entities
    ):
        raise ValueError("Configured enemy is not in the active public scene")
    enemy_id = UUID(str(enemy["id"]))
    attacked = any(
        isinstance(a.command, AttackCommand)
        and a.command.target_id == enemy_id
        and isinstance(a.result, AppliedResult)
        for a in actions
    )
    if combat.started_flag not in run.flags and not attacked:
        return unchanged
    characters = {
        UUID(str(row["entity_id"])): CharacterState(
            id=UUID(str(row["entity_id"])),
            current_hp=int(str(row["current_hp"])),
            max_hp=int(str(row["max_hp"])),
            defense=int(str(row["defense"])),
            attack_bonus=int(str(row["attack_bonus"])),
        )
        for row in snapshot.characters
    }
    for action in actions:
        if isinstance(action.result, AppliedResult):
            for change in action.result.state_changes:
                if isinstance(change, DamageApplied | HealingApplied):
                    previous = characters[change.target_id]
                    if previous.current_hp != change.hp_before:
                        raise ValueError("Player HP changes are not continuous")
                    characters[change.target_id] = replace(previous, current_hp=change.hp_after)
    attacker, target = characters[enemy_id], characters[work.actor_id]
    if attacker.current_hp == 0 or target.current_hp == 0:
        return unchanged
    command = AttackCommand(
        kind="attack",
        action_id=reaction_id_factory(),
        campaign_id=work.campaign_id,
        turn_id=work.turn_id,
        actor_id=enemy_id,
        ordinal=1,
        target_id=work.actor_id,
        weapon_id=None,
        attack_bonus=attacker.attack_bonus,
        damage_expression=combat.damage_expression,
        damage_bonus=combat.damage_bonus,
    )
    result = ruleset.resolve_attack(command, attacker, target)
    draw_index = sum(len(action.rng) for action in actions)
    reaction = EnemyReaction(
        command=command,
        result=result,
        rng=tuple(
            RNGMetadata(
                source=rng_source,
                implementation_version=rng_implementation_version,
                draw_index=draw_index + index,
            )
            for index in range(len(result.dice))
        ),
    )
    new_flags = list(scenario_update.add_flags if scenario_update is not None else ())
    if combat.started_flag not in run.flags and combat.started_flag not in new_flags:
        new_flags.append(combat.started_flag)
    defeated = any(isinstance(c, DamageApplied) and c.hp_after == 0 for c in result.state_changes)
    if new_flags or defeated:
        scenario_update = ScenarioProgressUpdate(
            from_scene_id=work.scene_id,
            to_scene_id=None,
            add_flags=tuple(new_flags),
            ending_ref=combat.defeat_ending_ref if defeated else None,
        )
    return CombatResolution((reaction,), scenario_update)
