"""差し替え可能な乱数源を用いるダイス処理。"""

import random
import re
import secrets
from typing import Protocol

from ai_rpg.domain.models import DiceResult

_DICE_PATTERN = re.compile(
    r"^(?P<count>[1-9]|1[0-9]|20)d(?P<sides>[2-9]|[1-9][0-9]|100)"
    r"(?P<modifier>[+-](?:0|[1-9][0-9]?|100))?$"
)


class RandomSource(Protocol):
    """Dice Engineが必要とする最小乱数port。"""

    def randint(self, lower: int, upper: int) -> int:
        """両端を含む整数を返す。"""


class SeededRandomSource:
    """Unit Testで結果を再現する乱数源。"""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)

    def randint(self, lower: int, upper: int) -> int:
        """固定seedから次の整数を返す。"""

        return self._random.randint(lower, upper)


class SecureRandomSource:
    """本番のゲーム処理向け乱数源。"""

    def randint(self, lower: int, upper: int) -> int:
        """OS由来の乱数から整数を返す。"""

        return secrets.randbelow(upper - lower + 1) + lower


class DiceEngine:
    """`mvp_v1`で許可された式だけを評価する。"""

    def __init__(self, random_source: RandomSource) -> None:
        self._random_source = random_source

    def roll(self, expression: str) -> DiceResult:
        """ダイス式を正規化、検証して評価する。"""

        normalized = expression.replace(" ", "").lower()
        match = _DICE_PATTERN.fullmatch(normalized)
        if match is None:
            raise ValueError("未対応のダイス式です")
        count = int(match.group("count"))
        sides = int(match.group("sides"))
        modifier = int(match.group("modifier") or 0)
        rolls = tuple(self._random_source.randint(1, sides) for _ in range(count))
        return DiceResult(normalized, rolls, modifier, sum(rolls) + modifier)
