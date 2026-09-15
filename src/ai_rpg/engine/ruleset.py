"""バージョン固定されたゲーム規則。"""

from dataclasses import dataclass
from typing import Literal

from ai_rpg.domain.models import ActionResult, CharacterState, SkillCheckCommand
from ai_rpg.engine.dice import DiceEngine


@dataclass(frozen=True, slots=True)
class MvpV1Ruleset:
    """ADR-0007で固定した最小ruleset。"""

    dice: DiceEngine
    ruleset_id: str = "mvp_v1"

    _skills = frozenset({"athletics", "acrobatics", "perception", "stealth", "persuasion"})

    def resolve_skill_check(
        self, command: SkillCheckCommand, actor: CharacterState
    ) -> ActionResult:
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
        return ActionResult(command.action_id, outcome, (fact,), (roll,))
