"""Applicationが認証・参照解決後に生成するCommand契約。"""

from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import Field

from ai_rpg.contracts.common import Contract, PositiveInt, Ref, ShortText


class CommandBase(Contract):
    action_id: UUID
    campaign_id: UUID
    turn_id: UUID
    actor_id: UUID
    ordinal: PositiveInt


class AttackCommand(CommandBase):
    kind: Literal["attack"]
    target_id: UUID
    weapon_id: UUID | None


class SkillCheckCommand(CommandBase):
    kind: Literal["skill_check"]
    skill_ref: Ref
    objective: ShortText
    target_id: UUID | None


class UseItemCommand(CommandBase):
    kind: Literal["use_item"]
    item_id: UUID
    target_id: UUID | None


Command: TypeAlias = Annotated[
    AttackCommand | SkillCheckCommand | UseItemCommand, Field(discriminator="kind")
]
