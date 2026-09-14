"""Engineが操作する純粋なDomain model。"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CharacterState:
    """解決時点のキャラクター確定状態。"""

    id: UUID
    current_hp: int
    max_hp: int
    defense: int
    attack_bonus: int = 0

    def __post_init__(self) -> None:
        """HPの不変条件を検証する。"""

        if self.max_hp < 1 or not 0 <= self.current_hp <= self.max_hp:
            raise ValueError("HPは0以上max_hp以下である必要があります")


@dataclass(frozen=True, slots=True)
class AttackCommand:
    """Applicationが検証と参照解決を終えた攻撃Command。"""

    action_id: UUID
    actor_id: UUID
    target_id: UUID
    damage_expression: str
    damage_bonus: int = 0


@dataclass(frozen=True, slots=True)
class SkillCheckCommand:
    """定義済み技能と難易度を指定する技能判定Command。"""

    action_id: UUID
    actor_id: UUID
    skill_ref: str
    modifier: int
    difficulty_class: int


@dataclass(frozen=True, slots=True)
class DiceResult:
    """再生可能なダイス結果。"""

    expression: str
    rolls: tuple[int, ...]
    modifier: int
    total: int


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Engineが確定したActionの結果。"""

    action_id: UUID
    outcome: Literal["success", "failure", "neutral"]
    facts: tuple[str, ...]
    dice: tuple[DiceResult, ...] = ()


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """永続化adapterへ渡すフレームワーク非依存のDomain Event。"""

    event_type: str
    action_id: UUID | None
    payload: dict[str, object]
