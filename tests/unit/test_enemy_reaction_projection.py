"""Enemy reactions stay separate from player actions and share their atomic projection."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_rpg.application.ports.repositories import (
    ActionRecord,
    CommitBundle,
    InvalidCommitBundleError,
    ScenarioProgressUpdate,
)
from ai_rpg.application.resolution import (
    CharacterHpMutation,
    InventoryQuantityMutation,
    TurnCommitContext,
    project_resolution,
)
from ai_rpg.contracts.context import OutputLimits
from ai_rpg.contracts.responses import AdventureState, MechanicalNarrationInput, TurnResponse
from ai_rpg.domain.commands import AttackCommand, ScenarioActionCommand, UseItemCommand
from ai_rpg.domain.events import DomainEventV1, EnemyReaction, RNGMetadata
from ai_rpg.domain.models import CharacterState
from ai_rpg.domain.results import (
    AppliedResult,
    DamageApplied,
    NotApplicableResult,
    ResolvedAction,
    ResolvedEnemyReaction,
)
from ai_rpg.engine import DiceEngine, MvpV1Ruleset


class SequenceRandom:
    def __init__(self, values: list[int]) -> None:
        self.values = iter(values)

    def randint(self, lower: int, upper: int) -> int:
        value = next(self.values)
        assert lower <= value <= upper
        return value


def _public(reaction: EnemyReaction) -> ResolvedEnemyReaction:
    return ResolvedEnemyReaction(
        reaction_id=reaction.command.action_id,
        actor_id=reaction.command.actor_id,
        target_id=reaction.command.target_id,
        result=reaction.result,
    )


def _with_reaction(bundle: CommitBundle, reaction: EnemyReaction) -> CommitBundle:
    return replace(
        bundle,
        enemy_reactions=(reaction,),
        narration_input=bundle.narration_input.model_copy(
            update={"enemy_reactions": [_public(reaction)]}
        ),
    )


def _bundle(
    *,
    heal: bool = True,
    attack_roll: int = 12,
    damage_bonus: int = 0,
    damage_expression: str = "1d6",
    damage_rolls: tuple[int, ...] = (3,),
    version: int = 5,
) -> tuple[CommitBundle, TurnCommitContext]:
    campaign_id, scene_id, turn_id = uuid4(), uuid4(), uuid4()
    player = CharacterState(uuid4(), current_hp=5, max_hp=10, defense=12)
    enemy = CharacterState(uuid4(), current_hp=8, max_hp=8, defense=12, attack_bonus=2)
    rules = MvpV1Ruleset(DiceEngine(SequenceRandom([2, attack_roll, *damage_rolls])))
    if heal:
        command = UseItemCommand(
            action_id=uuid4(), campaign_id=campaign_id, turn_id=turn_id,
            actor_id=player.id, ordinal=1, kind="use_item", item_id=uuid4(),
            target_id=player.id, effect_ref="healing_potion",
        )
        result = rules.resolve_use_item(command, player, player, quantity=2)
        player = replace(player, current_hp=9)
        action = ActionRecord(
            command=command, result=result,
            rng=(RNGMetadata(source="seeded_test", implementation_version="mvp_v1", draw_index=0),),
        )
    else:
        rules = MvpV1Ruleset(DiceEngine(SequenceRandom([attack_roll, *damage_rolls])))
        action = ActionRecord(
            command=ScenarioActionCommand(
                action_id=uuid4(), campaign_id=campaign_id, turn_id=turn_id,
                actor_id=player.id, ordinal=1, kind="scenario_action", action_ref="wait",
            ),
            result=AppliedResult(
                kind="applied", outcome="neutral", facts=["Waited"], dice=[], state_changes=[],
            ),
        )
    attack = AttackCommand(
        action_id=uuid4(), campaign_id=campaign_id, turn_id=turn_id,
        actor_id=enemy.id, ordinal=1, kind="attack", target_id=player.id,
        weapon_id=None, attack_bonus=2, damage_expression=damage_expression,
        damage_bonus=damage_bonus,
    )
    reaction_result = rules.resolve_attack(attack, enemy, player)
    reaction = EnemyReaction(
        command=attack,
        result=reaction_result,
        rng=tuple(
            RNGMetadata(
                source="seeded_test", implementation_version="mvp_v1",
                draw_index=len(action.rng) + index,
            )
            for index in range(len(reaction_result.dice))
        ),
    )
    bundle = CommitBundle(
        campaign_id=campaign_id, scene_id=scene_id, turn_id=turn_id,
        worker_epoch=1, base_state_version=4, actions=(action,),
        narration_input=MechanicalNarrationInput(
            player_text="Act", committed_state_version=version,
            resolved_actions=[ResolvedAction(
                action_id=action.command.action_id, ordinal=1, result=action.result,
            )],
            enemy_reactions=[_public(reaction)], public_state_after=[], allowed_entity_refs=[],
            output_limits=OutputLimits(max_actions=1),
        ),
        enemy_reactions=(reaction,),
    )
    return bundle, TurnCommitContext(scene_id, player.id, max_actions=1, enemy_id=enemy.id)


def test_old_narration_rows_default_to_no_enemy_reactions() -> None:
    source = MechanicalNarrationInput.model_validate(
        {
            "player_text": "Wait",
            "committed_state_version": 0,
            "resolved_actions": [],
            "public_state_after": [],
            "allowed_entity_refs": [],
            "output_limits": OutputLimits(),
        }
    )
    assert source.enemy_reactions == []


def test_old_turn_rows_default_to_no_enemy_reactions() -> None:
    response = TurnResponse.model_validate(
        {
            "turn_id": uuid4(),
            "route": None,
            "resolution_status": "pending",
            "narration_status": "pending",
            "committed_state_version": None,
            "narration": None,
            "choices": [],
            "action_results": [],
            "recovery": {"fallback": False, "reason": None},
        }
    )
    assert response.enemy_reactions == []


def test_old_commit_context_defaults_to_no_configured_enemy() -> None:
    context = TurnCommitContext(scene_id=uuid4(), actor_id=uuid4(), max_actions=1)
    assert context.enemy_id is None


def test_adventure_state_accepts_old_rows_and_optional_public_combat() -> None:
    saved = {
        "scenario_ref": "ruined_chapel", "title": "Chapel", "objective": "Explore",
        "status": "active", "current_scene": None, "discovered_facts": [],
        "available_actions": [], "ending": None,
    }
    assert AdventureState.model_validate(saved).combat is None
    combat = {
        "enemy_ref": "guardian", "enemy_name": "Guardian", "current_hp": 3,
        "max_hp": 8, "active": True,
    }
    state = AdventureState.model_validate(saved | {"combat": combat})
    assert state.combat is not None
    assert state.combat.model_dump() == combat


def test_old_bundle_defaults_to_no_enemy_reactions() -> None:
    bundle, turn = _bundle()
    old_bundle = CommitBundle(
        campaign_id=bundle.campaign_id, scene_id=bundle.scene_id, turn_id=bundle.turn_id,
        worker_epoch=1, base_state_version=4, actions=bundle.actions,
        narration_input=bundle.narration_input.model_copy(update={"enemy_reactions": []}),
    )
    projection = project_resolution(
        old_bundle, replace(turn, enemy_id=None), first_event_sequence=1,
    )
    assert old_bundle.enemy_reactions == ()
    assert [event.type for event in projection.events] == [
        "DiceRolled", "HealingApplied", "ItemConsumed", "ActionResolved",
    ]


def test_healing_then_reaction_merges_hp_and_keeps_player_action_limit() -> None:
    bundle, turn = _bundle()
    projection = project_resolution(bundle, turn, first_event_sequence=8)

    assert projection.actions == bundle.actions
    assert len(projection.actions) == turn.max_actions == 1
    assert projection.committed_state_version == 5
    item_command = bundle.actions[0].command
    assert isinstance(item_command, UseItemCommand)
    assert projection.canonical_mutations == (
        CharacterHpMutation(turn.actor_id, hp_before=5, hp_after=6, max_hp=10),
        InventoryQuantityMutation(turn.actor_id, item_command.item_id, 2, 1),
    )
    assert projection.narration_input.enemy_reactions == [_public(bundle.enemy_reactions[0])]


def test_reaction_event_follows_player_events_before_scenario_progress() -> None:
    bundle, turn = _bundle()
    bundle = replace(bundle, scenario_update=ScenarioProgressUpdate(
        turn.scene_id, None, ("defeated",), "defeat",
    ))
    projection = project_resolution(bundle, turn, first_event_sequence=8)

    assert [event.type for event in projection.events] == [
        "DiceRolled", "HealingApplied", "ItemConsumed", "ActionResolved",
        "EnemyReactionResolved", "ScenarioProgressed",
    ]
    assert [event.sequence for event in projection.events] == [8, 9, 10, 11, 12, 13]
    assert len({event.id for event in projection.events}) == 6
    for event in projection.events:
        assert (event.campaign_id, event.scene_id, event.turn_id, event.state_version) == (
            bundle.campaign_id, bundle.scene_id, bundle.turn_id, 5,
        )
    reaction_event = projection.events[-2]
    assert reaction_event.action_id is None
    assert reaction_event.payload == bundle.enemy_reactions[0]
    adapter = TypeAdapter(DomainEventV1)
    assert adapter.validate_json(reaction_event.model_dump_json()) == reaction_event
    with pytest.raises(ValidationError):
        adapter.validate_python(reaction_event.model_dump() | {"action_id": uuid4()})


def test_reaction_alone_can_increment_state_version() -> None:
    bundle, turn = _bundle(heal=False)
    projection = project_resolution(bundle, turn, first_event_sequence=1)
    assert projection.committed_state_version == 5
    assert projection.canonical_mutations == (CharacterHpMutation(turn.actor_id, 5, 2, None),)


@pytest.mark.parametrize("progress", [False, True])
def test_missed_reaction_preserves_hp_and_only_scenario_progress_changes_version(
    progress: bool,
) -> None:
    bundle, turn = _bundle(heal=False, attack_roll=1, version=5 if progress else 4)
    if progress:
        bundle = replace(bundle, scenario_update=ScenarioProgressUpdate(
            turn.scene_id, None, ("retreated",), "retreat",
        ))
    projection = project_resolution(bundle, turn, first_event_sequence=1)
    assert projection.committed_state_version == (5 if progress else 4)
    assert projection.canonical_mutations == ()
    assert [event.type for event in projection.events] == [
        "ActionResolved", "EnemyReactionResolved", *(["ScenarioProgressed"] if progress else []),
    ]


def test_healing_canceled_by_reaction_has_no_net_hp_mutation() -> None:
    bundle, turn = _bundle(damage_bonus=1)
    projection = project_resolution(bundle, turn, first_event_sequence=1)
    assert len(projection.canonical_mutations) == 1
    assert isinstance(projection.canonical_mutations[0], InventoryQuantityMutation)


def test_normalized_multi_dice_and_negative_bonus_can_produce_zero_damage() -> None:
    bundle, turn = _bundle(
        heal=False, damage_expression="2D6 + 1", damage_rolls=(2, 3), damage_bonus=-8, version=4,
    )
    projection = project_resolution(bundle, turn, first_event_sequence=1)
    assert projection.committed_state_version == 4
    assert projection.canonical_mutations == ()


@pytest.mark.parametrize("field", ["campaign_id", "turn_id", "actor_id", "target_id", "ordinal"])
def test_reaction_rejects_wrong_parents_actor_target_or_ordinal(field: str) -> None:
    bundle, turn = _bundle()
    reaction = bundle.enemy_reactions[0]
    reaction = reaction.model_copy(update={
        "command": reaction.command.model_copy(update={field: 2 if field == "ordinal" else uuid4()})
    })
    with pytest.raises(InvalidCommitBundleError):
        project_resolution(_with_reaction(bundle, reaction), turn, first_event_sequence=1)


@pytest.mark.parametrize("configured_enemy", ["missing", "other_npc", "player"])
def test_reaction_requires_the_configured_nonplayer_enemy(configured_enemy: str) -> None:
    bundle, turn = _bundle()
    if configured_enemy == "player":
        reaction = bundle.enemy_reactions[0]
        bundle = _with_reaction(bundle, reaction.model_copy(update={
            "command": reaction.command.model_copy(update={"actor_id": turn.actor_id}),
        }))
    enemy_id = {"missing": None, "other_npc": uuid4(), "player": turn.actor_id}[configured_enemy]
    with pytest.raises(InvalidCommitBundleError):
        project_resolution(bundle, replace(turn, enemy_id=enemy_id), first_event_sequence=1)


def test_reaction_id_cannot_reuse_a_player_action_id() -> None:
    bundle, turn = _bundle()
    reaction = bundle.enemy_reactions[0]
    reaction = reaction.model_copy(update={"command": reaction.command.model_copy(
        update={"action_id": bundle.actions[0].command.action_id},
    )})
    with pytest.raises(InvalidCommitBundleError):
        project_resolution(_with_reaction(bundle, reaction), turn, first_event_sequence=1)


def test_reaction_event_id_cannot_reuse_a_player_event_id() -> None:
    bundle, turn = _bundle(heal=False)
    with pytest.raises(InvalidCommitBundleError, match="Event ID"):
        project_resolution(
            bundle, turn, first_event_sequence=1, event_id_factory=lambda: UUID(int=1),
        )


def test_at_most_one_enemy_reaction_is_allowed() -> None:
    bundle, turn = _bundle()
    reaction = bundle.enemy_reactions[0]
    second = reaction.model_copy(update={"command": reaction.command.model_copy(
        update={"action_id": uuid4()},
    )})
    bundle = replace(
        bundle, enemy_reactions=(reaction, second),
        narration_input=bundle.narration_input.model_copy(
            update={"enemy_reactions": [_public(reaction), _public(second)]},
        ),
    )
    with pytest.raises(InvalidCommitBundleError):
        project_resolution(bundle, turn, first_event_sequence=1)


def test_reaction_requires_at_least_one_applied_player_action() -> None:
    bundle, turn = _bundle(heal=False)
    action = replace(bundle.actions[0], result=NotApplicableResult(
        kind="not_applicable", reason="rule_precondition",
    ))
    bundle = replace(bundle, actions=(action,), narration_input=bundle.narration_input.model_copy(
        update={"resolved_actions": [ResolvedAction(
            action_id=action.command.action_id, ordinal=1, result=action.result,
        )]},
    ))
    with pytest.raises(InvalidCommitBundleError, match="applied player action"):
        project_resolution(bundle, turn, first_event_sequence=1)


@pytest.mark.parametrize("problem", ["missing", "gap", "restart", "extra"])
def test_reaction_rng_must_follow_player_dice_contiguously(problem: str) -> None:
    bundle, turn = _bundle()
    reaction = bundle.enemy_reactions[0]
    rng = reaction.rng
    if problem == "missing":
        rng = rng[:1]
    elif problem == "extra":
        rng += (rng[-1].model_copy(update={"draw_index": 3}),)
    else:
        indexes = (1, 3) if problem == "gap" else (0, 1)
        rng = tuple(item.model_copy(update={"draw_index": index})
                    for item, index in zip(rng, indexes, strict=True))
    reaction = reaction.model_copy(update={"rng": rng})
    with pytest.raises(InvalidCommitBundleError):
        project_resolution(_with_reaction(bundle, reaction), turn, first_event_sequence=1)


@pytest.mark.parametrize("problem", [
    "neutral", "failure_with_damage", "success_without_damage", "duplicate_damage",
    "wrong_target", "healing", "item_consumption", "wrong_amount", "wrong_hp", "broken_hp_chain",
    "missing_attack_die", "extra_die", "attack_expression", "attack_modifier", "attack_total",
    "attack_roll_range", "damage_expression", "damage_modifier", "damage_total",
    "damage_roll_count", "damage_roll_range",
])
def test_reaction_rejects_inconsistent_result_and_dice(problem: str) -> None:
    bundle, turn = _bundle()
    reaction = bundle.enemy_reactions[0]
    result = reaction.result
    damage = result.state_changes[0]
    assert isinstance(damage, DamageApplied)
    updates: dict[str, object] = {}
    if problem in {"neutral", "failure_with_damage"}:
        updates["outcome"] = "neutral" if problem == "neutral" else "failure"
    elif problem == "success_without_damage":
        updates["state_changes"] = []
    elif problem == "duplicate_damage":
        updates["state_changes"] = [damage, damage]
    elif problem == "wrong_target":
        updates["state_changes"] = [damage.model_copy(update={"target_id": uuid4()})]
    elif problem in {"healing", "item_consumption"}:
        applied = bundle.actions[0].result
        assert isinstance(applied, AppliedResult)
        updates["state_changes"] = [applied.state_changes[0 if problem == "healing" else 1]]
    elif problem == "wrong_amount":
        updates["state_changes"] = [damage.model_copy(update={"amount": 2, "hp_after": 7})]
    elif problem == "wrong_hp":
        updates["state_changes"] = [damage.model_copy(update={"hp_after": 5})]
    elif problem == "broken_hp_chain":
        updates["state_changes"] = [damage.model_copy(update={"hp_before": 8, "hp_after": 5})]
    else:
        dice = list(result.dice)
        if problem == "missing_attack_die":
            dice = dice[1:]
        elif problem == "extra_die":
            dice.append(dice[-1])
        else:
            index = 0 if problem.startswith("attack_") else 1
            field = problem.split("_", 1)[1]
            update: dict[str, object] = {
                "expression": "1d8", "modifier": 7, "total": 99,
                "roll_count": [1, 2], "roll_range": [21 if index == 0 else 7],
            }
            dice[index] = dice[index].model_copy(update={
                "rolls" if field.startswith("roll_") else field: update[field],
            })
        updates["dice"] = dice
    reaction = reaction.model_copy(update={"result": result.model_copy(update=updates)})
    if "dice" in updates:
        reaction = reaction.model_copy(update={"rng": tuple(
            RNGMetadata(source="seeded_test", implementation_version="mvp_v1", draw_index=i + 1)
            for i in range(len(reaction.result.dice))
        )})
    with pytest.raises(InvalidCommitBundleError):
        project_resolution(_with_reaction(bundle, reaction), turn, first_event_sequence=1)


@pytest.mark.parametrize("field", ["missing", "reaction_id", "actor_id", "target_id", "result"])
def test_reaction_narration_must_exactly_match_public_projection(field: str) -> None:
    bundle, turn = _bundle()
    public = bundle.narration_input.enemy_reactions[0]
    if field == "missing":
        reactions = []
    else:
        value = (
            public.result.model_copy(update={"facts": ["invented"]})
            if field == "result" else uuid4()
        )
        reactions = [public.model_copy(update={field: value})]
    bundle = replace(bundle, narration_input=bundle.narration_input.model_copy(
        update={"enemy_reactions": reactions},
    ))
    with pytest.raises(InvalidCommitBundleError, match="描写入力"):
        project_resolution(bundle, turn, first_event_sequence=1)


@pytest.mark.parametrize("route,status,version", [
    ("narrative", "committed", 5), (None, "pending", None),
    ("mechanical", "resolving", None), ("mechanical", "not_applied", None),
    ("mechanical", "failed", None),
])
def test_response_requires_committed_mechanical_turn_for_reactions(
    route: str | None, status: str, version: int | None,
) -> None:
    bundle, _ = _bundle()
    with pytest.raises(ValidationError):
        TurnResponse.model_validate({
            "turn_id": bundle.turn_id, "route": route, "resolution_status": status,
            "narration_status": "pending", "committed_state_version": version,
            "narration": None, "choices": [], "action_results": [],
            "enemy_reactions": bundle.narration_input.enemy_reactions,
            "recovery": {"fallback": False, "reason": None},
        })


def test_response_reaction_exposes_result_without_private_command_or_rng() -> None:
    bundle, _ = _bundle()
    response = TurnResponse.model_validate({
        "turn_id": bundle.turn_id, "route": "mechanical", "resolution_status": "committed",
        "narration_status": "pending", "committed_state_version": 5,
        "narration": None, "choices": [], "action_results": bundle.narration_input.resolved_actions,
        "enemy_reactions": bundle.narration_input.enemy_reactions,
        "recovery": {"fallback": False, "reason": None},
    })
    public = response.model_dump(mode="json")["enemy_reactions"][0]
    assert set(public) == {"reaction_id", "actor_id", "target_id", "result"}
    assert public["reaction_id"] == str(bundle.enemy_reactions[0].command.action_id)
    assert TurnResponse.model_validate_json(response.model_dump_json()) == response


def test_reaction_damage_can_reduce_player_to_zero_hp() -> None:
    bundle, turn = _bundle(heal=False, damage_rolls=(6,))
    projection = project_resolution(bundle, turn, first_event_sequence=1)
    assert projection.canonical_mutations == (CharacterHpMutation(turn.actor_id, 5, 0, None),)


def test_narration_cannot_invent_a_reaction_for_an_empty_sidecar() -> None:
    bundle, turn = _bundle()
    with pytest.raises(InvalidCommitBundleError, match="描写入力"):
        project_resolution(replace(bundle, enemy_reactions=()), turn, first_event_sequence=1)


def test_reaction_id_cannot_reuse_the_second_player_action_id() -> None:
    bundle, turn = _bundle(heal=False)
    first = bundle.actions[0]
    second = replace(first, command=first.command.model_copy(
        update={"action_id": uuid4(), "ordinal": 2},
    ))
    reaction = bundle.enemy_reactions[0]
    reaction = reaction.model_copy(update={"command": reaction.command.model_copy(
        update={"action_id": second.command.action_id},
    )})
    bundle = _with_reaction(replace(
        bundle, actions=(first, second),
        narration_input=bundle.narration_input.model_copy(update={
            "output_limits": OutputLimits(max_actions=2),
            "resolved_actions": [ResolvedAction(
                action_id=action.command.action_id,
                ordinal=action.command.ordinal, result=action.result,
            ) for action in (first, second)],
        }),
    ), reaction)
    with pytest.raises(InvalidCommitBundleError, match="identity or parents"):
        project_resolution(bundle, replace(turn, max_actions=2), first_event_sequence=1)
