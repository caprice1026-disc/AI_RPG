"""Applicationが認証・参照解決後に生成するCommand契約。"""

from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import Field

from ai_rpg.contracts.common import Contract, PositiveInt, Ref, ShortText, SignedInt


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
    attack_bonus: SignedInt
    damage_expression: ShortText
    damage_bonus: SignedInt


class SkillCheckCommand(CommandBase):
    kind: Literal["skill_check"]
    skill_ref: Ref
    objective: ShortText
    target_id: UUID | None
    modifier: SignedInt
    difficulty_class: PositiveInt


class UseItemCommand(CommandBase):
    kind: Literal["use_item"]
    item_id: UUID
    target_id: UUID | None
    effect_ref: Ref


class ScenarioActionCommand(CommandBase):
    kind: Literal["scenario_action"]
    action_ref: Ref


class OpenActionCommand(CommandBase):
    kind: Literal["open_action"]
    approach: ShortText
    ability: Literal["strength", "agility", "insight", "presence"] | None
    skill_ref: Ref | None
    modifier: SignedInt | None
    difficulty_class: PositiveInt | None


Command: TypeAlias = Annotated[
    AttackCommand | SkillCheckCommand | UseItemCommand | ScenarioActionCommand | OpenActionCommand,
    Field(discriminator="kind"),
]
