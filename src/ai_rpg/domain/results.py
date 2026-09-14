"""Engineだけが生成する確定結果の契約。"""

from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import Field

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


class DamageFact(Contract):
    target_id: UUID
    amount: NonNegativeInt
    hp_before: SignedInt
    hp_after: SignedInt


class AppliedResult(Contract):
    kind: Literal["applied"]
    outcome: Literal["success", "failure", "neutral"]
    facts: list[ShortText]
    dice: list[DiceResult]
    damage: list[DamageFact]


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
