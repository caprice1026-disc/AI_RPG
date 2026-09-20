"""Engineの確定結果を永続化単位へ投影する純粋なApplication処理。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

from ai_rpg.application.ports.repositories import (
    ActionRecord,
    CommitBundle,
    InvalidCommitBundleError,
    ScenarioProgressUpdate,
)
from ai_rpg.contracts.responses import MechanicalNarrationInput
from ai_rpg.domain.events import (
    ActionResolvedEvent,
    ActionResolvedPayload,
    DamageAppliedEvent,
    DiceRolledEvent,
    DiceRolledPayload,
    DomainEventV1,
    HealingAppliedEvent,
    ItemConsumedEvent,
    ScenarioProgressedEvent,
    ScenarioProgressedPayload,
)
from ai_rpg.domain.results import (
    AppliedResult,
    DamageApplied,
    HealingApplied,
    ItemConsumed,
    ResolvedAction,
)


@dataclass(frozen=True, slots=True)
class TurnCommitContext:
    scene_id: UUID
    actor_id: UUID
    max_actions: int


@dataclass(frozen=True, slots=True)
class CharacterHpMutation:
    entity_id: UUID
    hp_before: int
    hp_after: int
    max_hp: int | None


@dataclass(frozen=True, slots=True)
class InventoryQuantityMutation:
    owner_id: UUID
    item_id: UUID
    quantity_before: int
    quantity_after: int


CanonicalMutation = CharacterHpMutation | InventoryQuantityMutation


@dataclass(frozen=True, slots=True)
class ResolutionProjection:
    actions: tuple[ActionRecord, ...]
    events: tuple[DomainEventV1, ...]
    narration_input: MechanicalNarrationInput
    canonical_mutations: tuple[CanonicalMutation, ...]
    scenario_update: ScenarioProgressUpdate | None
    committed_state_version: int


def _canonical_mutations(actions: tuple[ActionRecord, ...]) -> tuple[CanonicalMutation, ...]:
    hp: dict[UUID, CharacterHpMutation] = {}
    inventory: dict[tuple[UUID, UUID], InventoryQuantityMutation] = {}
    order: list[tuple[str, object]] = []

    for action in actions:
        if not isinstance(action.result, AppliedResult):
            continue
        for change in action.result.state_changes:
            if isinstance(change, (DamageApplied, HealingApplied)):
                previous = hp.get(change.target_id)
                if previous is not None and previous.hp_after != change.hp_before:
                    raise InvalidCommitBundleError("HP遷移がAction間で連続していません")
                max_hp = change.max_hp if isinstance(change, HealingApplied) else None
                if previous is not None:
                    if (
                        previous.max_hp is not None
                        and max_hp is not None
                        and previous.max_hp != max_hp
                    ):
                        raise InvalidCommitBundleError("HP遷移のmax_hpが一致しません")
                    max_hp = previous.max_hp if previous.max_hp is not None else max_hp
                else:
                    order.append(("hp", change.target_id))
                hp[change.target_id] = CharacterHpMutation(
                    change.target_id,
                    previous.hp_before if previous is not None else change.hp_before,
                    change.hp_after,
                    max_hp,
                )
            elif isinstance(change, ItemConsumed):
                key = (change.owner_id, change.item_id)
                previous_item = inventory.get(key)
                if (
                    previous_item is not None
                    and previous_item.quantity_after != change.quantity_before
                ):
                    raise InvalidCommitBundleError("在庫遷移がAction間で連続していません")
                if previous_item is None:
                    order.append(("inventory", key))
                inventory[key] = InventoryQuantityMutation(
                    change.owner_id,
                    change.item_id,
                    previous_item.quantity_before
                    if previous_item is not None
                    else change.quantity_before,
                    change.quantity_after,
                )

    mutations: list[CanonicalMutation] = []
    for kind, order_key in order:
        mutation: CanonicalMutation = (
            hp[cast(UUID, order_key)]
            if kind == "hp"
            else inventory[cast(tuple[UUID, UUID], order_key)]
        )
        before = (
            mutation.hp_before
            if isinstance(mutation, CharacterHpMutation)
            else mutation.quantity_before
        )
        after = (
            mutation.hp_after
            if isinstance(mutation, CharacterHpMutation)
            else mutation.quantity_after
        )
        if before != after:
            mutations.append(mutation)
    return tuple(mutations)


def _events(
    actions: tuple[ActionRecord, ...],
    bundle: CommitBundle,
    *,
    first_sequence: int,
    state_version: int,
    event_id_factory: Callable[[], UUID],
) -> tuple[DomainEventV1, ...]:
    events: list[DomainEventV1] = []
    event_ids: set[UUID] = set()

    def values(action_id: UUID | None) -> dict[str, object]:
        event_id = event_id_factory()
        if event_id in event_ids:
            raise InvalidCommitBundleError("Event IDが重複しています")
        event_ids.add(event_id)
        return {
            "id": event_id,
            "campaign_id": bundle.campaign_id,
            "scene_id": bundle.scene_id,
            "turn_id": bundle.turn_id,
            "action_id": action_id,
            "sequence": first_sequence + len(events),
            "state_version": state_version,
            "schema_version": 1,
        }

    for action in actions:
        action_id = action.command.action_id
        if isinstance(action.result, AppliedResult):
            if len(action.rng) != len(action.result.dice):
                raise InvalidCommitBundleError("Dice結果とRNG metadataの件数が一致しません")
            for roll, rng in zip(action.result.dice, action.rng, strict=True):
                events.append(
                    DiceRolledEvent(
                        type="DiceRolled",
                        payload=DiceRolledPayload(roll=roll, rng=rng),
                        **values(action_id),
                    )
                )
            for change in action.result.state_changes:
                event: DomainEventV1
                if isinstance(change, DamageApplied):
                    event = DamageAppliedEvent(
                        type="DamageApplied", payload=change, **values(action_id)
                    )
                elif isinstance(change, HealingApplied):
                    event = HealingAppliedEvent(
                        type="HealingApplied", payload=change, **values(action_id)
                    )
                else:
                    event = ItemConsumedEvent(
                        type="ItemConsumed", payload=change, **values(action_id)
                    )
                events.append(event)
        elif action.rng:
            raise InvalidCommitBundleError("not_applicable結果はRNG metadataを持てません")
        events.append(
            ActionResolvedEvent(
                type="ActionResolved",
                payload=ActionResolvedPayload(result=action.result),
                **values(action_id),
            )
        )
    if bundle.scenario_update is not None:
        update = bundle.scenario_update
        events.append(
            ScenarioProgressedEvent(
                type="ScenarioProgressed",
                payload=ScenarioProgressedPayload(
                    from_scene_id=update.from_scene_id,
                    to_scene_id=update.to_scene_id,
                    add_flags=update.add_flags,
                    ending_ref=update.ending_ref,
                ),
                **values(None),
            )
        )
    return tuple(events)


def project_resolution(
    bundle: CommitBundle,
    turn: TurnCommitContext,
    *,
    first_event_sequence: int,
    event_id_factory: Callable[[], UUID] = uuid4,
) -> ResolutionProjection:
    """型付きBundleの相互関係を検証し、永続化projectionを返す。"""

    actions = tuple(bundle.actions)
    if bundle.scene_id != turn.scene_id:
        raise InvalidCommitBundleError("BundleのSceneがTurnと一致しません")
    if not 1 <= len(actions) <= turn.max_actions:
        raise InvalidCommitBundleError("Action件数がTurnの上限外です")
    if [action.command.ordinal for action in actions] != list(range(1, len(actions) + 1)):
        raise InvalidCommitBundleError("Action ordinalは1から連続する必要があります")
    if len({action.command.action_id for action in actions}) != len(actions):
        raise InvalidCommitBundleError("Action IDが重複しています")
    for action in actions:
        command = action.command
        if (
            command.campaign_id != bundle.campaign_id
            or command.turn_id != bundle.turn_id
            or command.actor_id != turn.actor_id
        ):
            raise InvalidCommitBundleError("Action Commandの親IDが一致しません")

    mutations = _canonical_mutations(actions)
    changed = bool(mutations) or bundle.scenario_update is not None
    version = bundle.base_state_version + int(changed)
    resolved_actions = [
        ResolvedAction(
            action_id=action.command.action_id,
            ordinal=action.command.ordinal,
            result=action.result,
        )
        for action in actions
    ]
    narration = bundle.narration_input
    if (
        narration.committed_state_version != version
        or narration.output_limits.max_actions != turn.max_actions
        or narration.resolved_actions != resolved_actions
    ):
        raise InvalidCommitBundleError("描写入力が確定内容と一致しません")

    events = _events(
        actions,
        bundle,
        first_sequence=first_event_sequence,
        state_version=version,
        event_id_factory=event_id_factory,
    )
    return ResolutionProjection(
        actions=actions,
        events=events,
        narration_input=narration,
        canonical_mutations=mutations,
        scenario_update=bundle.scenario_update,
        committed_state_version=version,
    )
