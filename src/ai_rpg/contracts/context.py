"""LLMへ公開可能なコンテキスト契約。"""

from typing import Literal

from pydantic import Field

from ai_rpg.contracts.common import Contract, InputText, PositiveInt, Ref, ShortText


class EntityRef(Contract):
    """Turn内だけで有効な参照名。Canonical UUIDは含めない。"""

    ref: Ref
    label: ShortText
    entity_kind: Literal["pc", "npc", "item", "object"]


class ContextFragment(Contract):
    source: ShortText
    trust_level: Literal["trusted", "derived", "untrusted"]
    access_scope: Literal["public", "actor_private", "gm_private"]
    content: InputText


class OutputLimits(Contract):
    max_actions: PositiveInt = 3
    max_choices: int = Field(default=5, strict=True, ge=0, le=5)


class GMInput(Contract):
    player_text: InputText
    scene_view: ContextFragment
    pc_view: ContextFragment
    recent_messages: list[ContextFragment] = Field(max_length=100)
    allowed_entity_refs: list[EntityRef] = Field(max_length=100)
    output_limits: OutputLimits


class NarrativeInput(GMInput):
    pass


class MechanicalInput(GMInput):
    supported_action_types: list[
        Literal["attack", "skill_check", "use_item", "scenario_action"]
    ]
    supported_skill_refs: list[Ref]
