"""Engineが操作する確定状態。"""

from dataclasses import dataclass
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
