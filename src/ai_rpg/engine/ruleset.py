"""バージョン固定されたゲーム規則。"""

from dataclasses import dataclass
from typing import ClassVar, Literal

from ai_rpg.domain.commands import AttackCommand, SkillCheckCommand, UseItemCommand
from ai_rpg.domain.models import CharacterState
from ai_rpg.domain.results import AppliedResult, DamageApplied, HealingApplied, ItemConsumed
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

    def resolve_attack(
        self,
        command: AttackCommand,
        actor: CharacterState,
        target: CharacterState,
    ) -> AppliedResult:
        """登録済み能力値とweapon値だけで単体攻撃を解決する。"""

        if command.actor_id != actor.id or command.target_id != target.id:
            raise ValueError("CommandとCharacterが一致しません")
        if actor.current_hp == 0:
            raise ValueError("行動不能なactorです")
        if target.current_hp == 0:
            raise ValueError("攻撃対象は行動不能です")

        attack_modifier = (
            f"+{command.attack_bonus}"
            if command.attack_bonus >= 0
            else str(command.attack_bonus)
        )
        attack_roll = self.dice.roll(f"1d20{attack_modifier}")
        if attack_roll.total < target.defense:
            return AppliedResult(
                kind="applied",
                outcome="failure",
                facts=[f"攻撃は失敗(合計{attack_roll.total})"],
                dice=[attack_roll],
                state_changes=[],
            )

        damage_roll = self.dice.roll(command.damage_expression)
        damage = max(0, damage_roll.total + command.damage_bonus)
        hp_after = max(0, target.current_hp - damage)
        return AppliedResult(
            kind="applied",
            outcome="success",
            facts=[f"攻撃が命中し{damage}ダメージを与えた"],
            dice=[attack_roll, damage_roll],
            state_changes=[
                DamageApplied(
                    kind="damage_applied",
                    target_id=target.id,
                    amount=damage,
                    hp_before=target.current_hp,
                    hp_after=hp_after,
                )
            ],
        )

    def resolve_use_item(
        self,
        command: UseItemCommand,
        actor: CharacterState,
        target: CharacterState,
        *,
        quantity: int,
    ) -> AppliedResult:
        """登録済みhealing potionを使用し、回復と在庫消費を同時に返す。"""

        if command.actor_id != actor.id or command.target_id != target.id:
            raise ValueError("CommandとCharacterが一致しません")
        if actor.current_hp == 0:
            raise ValueError("行動不能なactorです")
        if command.effect_ref != "healing_potion":
            raise ValueError("rulesetに存在しないitem効果です")
        if target.id != actor.id:
            raise ValueError("healing_potionの対象は使用者だけです")
        if quantity < 1:
            raise ValueError("使用可能な在庫がありません")

        healing_roll = self.dice.roll("1d6+2")
        hp_after = min(target.max_hp, target.current_hp + healing_roll.total)
        return AppliedResult(
            kind="applied",
            outcome="success",
            facts=[f"回復ポーションで{healing_roll.total}回復した"],
            dice=[healing_roll],
            state_changes=[
                HealingApplied(
                    kind="healing_applied",
                    target_id=target.id,
                    amount=healing_roll.total,
                    hp_before=target.current_hp,
                    hp_after=hp_after,
                    max_hp=target.max_hp,
                ),
                ItemConsumed(
                    kind="item_consumed",
                    owner_id=actor.id,
                    item_id=command.item_id,
                    quantity_before=quantity,
                    quantity_after=quantity - 1,
                ),
            ],
        )
