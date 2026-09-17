"""ADR-0009に基づくprovider非依存の決定的Turn router。"""

import re
from dataclasses import dataclass
from typing import Literal, Protocol

TurnRoute = Literal["narrative", "mechanical"]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: TurnRoute
    rule_version: str
    reason_codes: tuple[str, ...]


class TurnRouter(Protocol):
    def decide(self, player_text: str) -> RouteDecision:
        """プレイヤー入力を追加のprovider呼出しなしで分類する。"""


class RuleBasedTurnRouter:
    """MVPの明白な実行要求だけをMechanicalへ送る保守的router。"""

    rule_version = "mvp_v1"

    _quoted = re.compile(r"「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*'")
    _explanation = re.compile(
        r"(?:とは|って何|を教えて|について教えて|どういう意味)",
        re.IGNORECASE,
    )
    _english_explanation = re.compile(r"\b(?:what\s+is|how\s+does)\b", re.IGNORECASE)
    _negated = re.compile(
        r"(?:攻撃し(?:ない|ません)|攻撃するな|殴ら(?:ない|ず)|斬ら(?:ない|ず)|"
        r"撃た(?:ない|ず)|使わ(?:ない|ず)|飲ま(?:ない|ず)|振ら(?:ない|ず)|"
        r"判定し(?:ない|ません))|"
        r"(?:do\s+not|don't|never)\s+(?:attack|hit|use|drink|roll)",
        re.IGNORECASE,
    )
    _attack = re.compile(
        r"(?:攻撃(?:する|したい)?|殴(?:る|りたい)|斬(?:る|りたい)|撃(?:つ|ちたい)|"
        r"\b(?:attack|hit|damage)\b)",
        re.IGNORECASE,
    )
    _dice_or_check = re.compile(
        r"(?:\b\d+d\d+\b|ダイス(?:を)?振|技能判定|成否判定|判定(?:を)?(?:する|したい)|"
        r"\b(?:roll|check)\b)",
        re.IGNORECASE,
    )
    _skill_attempt = re.compile(
        r"(?:注意深く.{0,12}観察|周囲.{0,12}観察|調べ(?:る|たい)|探索(?:する|したい)|"
        r"隠密(?:する|したい)|説得(?:する|したい)|跳躍(?:する|したい))"
    )
    _item_or_state = re.compile(
        r"(?:(?:ポーション|薬).{0,12}(?:飲|使|消費)|"
        r"(?:飲|使|消費).{0,12}(?:ポーション|薬|アイテム)|"
        r"(?:回復|治療|heal)(?:する|したい|\b)|"
        r"(?:HP|体力).{0,12}(?:減ら|増や|回復))",
        re.IGNORECASE,
    )
    _clause_boundary = re.compile(
        r"(?:[\u3002.!?\uff01\uff1f\u3001,\n]+|その後|それから|\bthen\b)",
        re.IGNORECASE,
    )

    def decide(self, player_text: str) -> RouteDecision:
        if not player_text.strip():
            raise ValueError("player_textは空にできません")

        unquoted = self._quoted.sub(" ", player_text.strip().lower())
        candidate = self._negated.sub(" ", unquoted)
        patterns = (
            (self._attack, "attack_action"),
            (self._dice_or_check, "dice_or_check"),
            (self._skill_attempt, "skill_attempt"),
            (self._item_or_state, "item_or_state_change"),
        )
        has_explanation = False
        for clause in self._clause_boundary.split(candidate):
            if self._english_explanation.search(clause) is not None:
                has_explanation = True
                continue
            explanation = self._explanation.search(clause)
            if explanation is not None:
                has_explanation = True
                clause = clause[explanation.end() :]
            for pattern, reason in patterns:
                if pattern.search(clause):
                    return self._decision("mechanical", reason)
        if has_explanation:
            return self._decision("narrative", "question_or_explanation")
        return self._decision("narrative", "narrative_or_fallback")

    def _decision(self, route: TurnRoute, reason: str) -> RouteDecision:
        return RouteDecision(route, self.rule_version, (reason,))
