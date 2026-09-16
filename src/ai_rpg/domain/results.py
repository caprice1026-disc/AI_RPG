"""Engineだけが生成する確定結果と状態変更の契約。"""

from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID

from pydantic import Field, model_validator

from ai_rpg.contracts.common import (
    Contract,
    NonNegativeInt,
    PositiveInt,
    ShortText,
    SignedInt,
)


class DiceResult(Contract):
    expression: ShortText
    rolls: list[PositiveInt] = Field(min_length=1, max_length=100)
    modifier: SignedInt
    total: SignedInt


class DamageApplied(Contract):
    kind: Literal["damage_applied"]
    target_id: UUID
    amount: NonNegativeInt
    hp_before: NonNegativeInt
    hp_after: NonNegativeInt

    @model_validator(mode="after")
    def valid_transition(self) -> Self:
        if self.hp_after != max(0, self.hp_before - self.amount):
            raise ValueError("damageとHP遷移が一致しません")
        return self


class HealingApplied(Contract):
    kind: Literal["healing_applied"]
    target_id: UUID
    amount: NonNegativeInt
    hp_before: NonNegativeInt
    hp_after: NonNegativeInt
    max_hp: PositiveInt

    @model_validator(mode="after")
    def valid_transition(self) -> Self:
        if self.hp_before > self.max_hp:
            raise ValueError("回復前HPがmax_hpを超えています")
        if self.hp_after != min(self.max_hp, self.hp_before + self.amount):
            raise ValueError("healingとHP遷移が一致しません")
        return self


class ItemConsumed(Contract):
    kind: Literal["item_consumed"]
    owner_id: UUID
    item_id: UUID
    quantity_before: PositiveInt
    quantity_after: NonNegativeInt

    @model_validator(mode="after")
    def valid_transition(self) -> Self:
        if self.quantity_after != self.quantity_before - 1:
            raise ValueError("MVPの消耗品は1個だけ消費します")
        return self


StateChange: TypeAlias = Annotated[
    DamageApplied | HealingApplied | ItemConsumed,
    Field(discriminator="kind"),
]


class AppliedResult(Contract):
    kind: Literal["applied"]
    outcome: Literal["success", "failure", "neutral"]
    facts: list[ShortText]
    dice: list[DiceResult]
    state_changes: list[StateChange]


class NotApplicableResult(Contract):
    kind: Literal["not_applicable"]
    reason: Literal["target_unavailable", "resource_unavailable", "rule_precondition"]


ActionResult: TypeAlias = Annotated[
    AppliedResult | NotApplicableResult, Field(discriminator="kind")
]


class ResolvedAction(Contract):
    action_id: UUID
    ordinal: PositiveInt
    result: ActionResult
