"""追記専用ストアへ保存するDomain Event v1契約。"""

from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from ai_rpg.contracts.common import Contract, NarrationText, NonNegativeInt, PositiveInt, ShortText
from ai_rpg.domain.results import (
    ActionResult,
    DamageApplied,
    DiceResult,
    HealingApplied,
    ItemConsumed,
)


class RNGMetadata(Contract):
    source: Literal["secure", "seeded_test", "recorded_replay"]
    implementation_version: ShortText
    draw_index: NonNegativeInt


class DiceRolledPayload(Contract):
    roll: DiceResult
    rng: RNGMetadata


class ActionResolvedPayload(Contract):
    result: ActionResult


class ScenarioProgressedPayload(Contract):
    from_scene_id: UUID
    to_scene_id: UUID | None
    add_flags: tuple[str, ...]
    ending_ref: str | None


class NarrationGeneratedPayload(Contract):
    narration: NarrationText
    fallback: StrictBool


class EventBase(Contract):
    id: UUID
    campaign_id: UUID
    scene_id: UUID | None
    turn_id: UUID | None
    action_id: UUID | None
    sequence: PositiveInt
    state_version: NonNegativeInt
    schema_version: Literal[1]

    @model_validator(mode="after")
    def consistent_parents(self) -> Self:
        if self.turn_id is not None and self.scene_id is None:
            raise ValueError("Turn Eventにはscene_idが必要です")
        if self.action_id is not None and self.turn_id is None:
            raise ValueError("Action Eventにはturn_idが必要です")
        return self


class ActionEventBase(EventBase):
    scene_id: UUID
    turn_id: UUID
    action_id: UUID


class DiceRolledEvent(ActionEventBase):
    type: Literal["DiceRolled"]
    payload: DiceRolledPayload


class DamageAppliedEvent(ActionEventBase):
    type: Literal["DamageApplied"]
    payload: DamageApplied


class HealingAppliedEvent(ActionEventBase):
    type: Literal["HealingApplied"]
    payload: HealingApplied


class ItemConsumedEvent(ActionEventBase):
    type: Literal["ItemConsumed"]
    payload: ItemConsumed


class ActionResolvedEvent(ActionEventBase):
    type: Literal["ActionResolved"]
    payload: ActionResolvedPayload


class ScenarioProgressedEvent(EventBase):
    type: Literal["ScenarioProgressed"]
    scene_id: UUID
    turn_id: UUID
    action_id: None = None
    payload: ScenarioProgressedPayload


class NarrationGeneratedEvent(EventBase):
    type: Literal["GMNarrationGenerated"]
    scene_id: UUID
    turn_id: UUID
    action_id: None = None
    payload: NarrationGeneratedPayload


DomainEventV1: TypeAlias = Annotated[
    DiceRolledEvent
    | DamageAppliedEvent
    | HealingAppliedEvent
    | ItemConsumedEvent
    | ActionResolvedEvent
    | ScenarioProgressedEvent
    | NarrationGeneratedEvent,
    Field(discriminator="type"),
]
