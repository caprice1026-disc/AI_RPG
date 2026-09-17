"""バージョン固定されたゲーム規則。"""

from dataclasses import dataclass
from typing import ClassVar, Literal

from ai_rpg.domain.commands import SkillCheckCommand
from ai_rpg.domain.models import CharacterState
from ai_rpg.domain.results import AppliedResult
from ai_rpg.engine.dice import DiceEngine


@dataclass(frozen=True, slots=True)
class MvpV1Ruleset:
    """ADR-0007で固定した最小ruleset。"""

    dice: DiceEngine
    ruleset_id: str = "mvp_v1"

    _skills = frozenset({"athletics", "acrobatics", "perception", "stealth", "persuasion"})
    _difficulty_classes: ClassVar[dict[str, int]] = {
        "easy": 8,
        "normal": 12,
        "hard": 16,
    }

    def difficulty_class(self, difficulty: str) -> int:
        """保存済みの難易度名をこのrulesetのDCへ解決する。"""

        try:
            return self._difficulty_classes[difficulty]
        except KeyError as error:
            raise ValueError("rulesetに存在しない難易度です") from error

    def resolve_skill_check(
        self, command: SkillCheckCommand, actor: CharacterState
    ) -> AppliedResult:
        """技能とactorを検証してd20判定を解決する。"""

        if actor.current_hp == 0:
            raise ValueError("行動不能なactorです")
        if command.skill_ref not in self._skills:
            raise ValueError("rulesetに存在しない技能です")
        modifier = f"+{command.modifier}" if command.modifier >= 0 else str(command.modifier)
        roll = self.dice.roll(f"1d20{modifier}")
        outcome: Literal["success", "failure"] = (
            "success" if roll.total >= command.difficulty_class else "failure"
        )
        fact = f"技能判定は{outcome}(合計{roll.total})"
        return AppliedResult(
            kind="applied",
            outcome=outcome,
            facts=[fact],
            dice=[roll],
            state_changes=[],
        )
