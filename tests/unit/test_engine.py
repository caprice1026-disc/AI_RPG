"""外部サービスを使わないEngineのUnit Test。"""

from collections.abc import Iterator
from uuid import uuid4

import pytest

from ai_rpg.domain import AttackCommand, CharacterState, SkillCheckCommand, UseItemCommand
from ai_rpg.domain.results import DamageApplied, HealingApplied, ItemConsumed
from ai_rpg.engine import DiceEngine, MvpV1Ruleset, SeededRandomSource


class SequenceRandom:
    def __init__(self, values: list[int]) -> None:
        self._values: Iterator[int] = iter(values)

    def randint(self, lower: int, upper: int) -> int:
        value = next(self._values)
        assert lower <= value <= upper
        return value


def test_seeded_dice_is_reproducible() -> None:
    """同じseedと式は同じ結果を返す。"""

    first = DiceEngine(SeededRandomSource(42)).roll("2D6 + 3")
    second = DiceEngine(SeededRandomSource(42)).roll("2d6+3")
    assert first == second
    assert first.expression == "2d6+3"


@pytest.mark.parametrize("expression", ["d20", "21d6", "1d101", "1d20+101", "1d20*2"])
def test_dice_rejects_unsupported_expressions(expression: str) -> None:
    """ruleset外の式を評価しない。"""

    with pytest.raises(ValueError, match="未対応"):
        DiceEngine(SeededRandomSource(1)).roll(expression)


def test_ruleset_resolves_skill_check_without_llm() -> None:
    """LLMやDBなしで技能判定を完結できる。"""

    actor_id = uuid4()
    actor = CharacterState(actor_id, current_hp=10, max_hp=10, defense=12)
    command = SkillCheckCommand(
        action_id=uuid4(),
        campaign_id=uuid4(),
        turn_id=uuid4(),
        actor_id=actor_id,
        ordinal=1,
        kind="skill_check",
        skill_ref="perception",
        objective="扉を調べる",
        target_id=None,
        modifier=2,
        difficulty_class=8,
    )
    result = MvpV1Ruleset(DiceEngine(SeededRandomSource(7))).resolve_skill_check(command, actor)
    assert result.outcome in {"success", "failure"}
    assert len(result.dice) == 1


@pytest.mark.parametrize(
    ("difficulty", "expected"),
    [("easy", 8), ("normal", 12), ("hard", 16)],
)
def test_ruleset_owns_difficulty_classes(difficulty: str, expected: int) -> None:
    ruleset = MvpV1Ruleset(DiceEngine(SeededRandomSource(1)))

    assert ruleset.difficulty_class(difficulty) == expected


def test_ruleset_rejects_unknown_difficulty() -> None:
    ruleset = MvpV1Ruleset(DiceEngine(SeededRandomSource(1)))

    with pytest.raises(ValueError, match="難易度"):
        ruleset.difficulty_class("legendary")


def test_ruleset_resolves_attack_and_clamps_hp_at_zero() -> None:
    actor = CharacterState(uuid4(), current_hp=10, max_hp=10, defense=12, attack_bonus=2)
    target = CharacterState(uuid4(), current_hp=3, max_hp=10, defense=11)
    command = AttackCommand(
        action_id=uuid4(),
        campaign_id=uuid4(),
        turn_id=uuid4(),
        actor_id=actor.id,
        ordinal=1,
        kind="attack",
        target_id=target.id,
        weapon_id=None,
        attack_bonus=2,
        damage_expression="1d6",
        damage_bonus=0,
    )

    result = MvpV1Ruleset(DiceEngine(SequenceRandom([9, 5]))).resolve_attack(
        command, actor, target
    )

    assert result.outcome == "success"
    assert [die.total for die in result.dice] == [11, 5]
    assert result.state_changes == [
        DamageApplied(
            kind="damage_applied",
            target_id=target.id,
            amount=5,
            hp_before=3,
            hp_after=0,
        )
    ]


def test_ruleset_attack_miss_does_not_roll_damage() -> None:
    actor = CharacterState(uuid4(), current_hp=10, max_hp=10, defense=12, attack_bonus=1)
    target = CharacterState(uuid4(), current_hp=8, max_hp=8, defense=20)
    command = AttackCommand(
        action_id=uuid4(),
        campaign_id=uuid4(),
        turn_id=uuid4(),
        actor_id=actor.id,
        ordinal=1,
        kind="attack",
        target_id=target.id,
        weapon_id=None,
        attack_bonus=1,
        damage_expression="1d2",
        damage_bonus=0,
    )

    result = MvpV1Ruleset(DiceEngine(SequenceRandom([2]))).resolve_attack(
        command, actor, target
    )

    assert result.outcome == "failure"
    assert len(result.dice) == 1
    assert result.state_changes == []


def test_ruleset_healing_potion_caps_hp_and_consumes_one_item() -> None:
    actor = CharacterState(uuid4(), current_hp=8, max_hp=10, defense=12)
    command = UseItemCommand(
        action_id=uuid4(),
        campaign_id=uuid4(),
        turn_id=uuid4(),
        actor_id=actor.id,
        ordinal=1,
        kind="use_item",
        item_id=uuid4(),
        target_id=actor.id,
        effect_ref="healing_potion",
    )

    result = MvpV1Ruleset(DiceEngine(SequenceRandom([4]))).resolve_use_item(
        command, actor, actor, quantity=2
    )

    assert result.outcome == "success"
    assert result.dice[0].total == 6
    assert result.facts == ["回復ポーションでHPが2回復した（8→10）"]  # noqa: RUF001
    assert result.state_changes == [
        HealingApplied(
            kind="healing_applied",
            target_id=actor.id,
            amount=6,
            hp_before=8,
            hp_after=10,
            max_hp=10,
        ),
        ItemConsumed(
            kind="item_consumed",
            owner_id=actor.id,
            item_id=command.item_id,
            quantity_before=2,
            quantity_after=1,
        ),
    ]


@pytest.mark.parametrize("quantity", [0, -1])
def test_ruleset_rejects_unavailable_healing_potion(quantity: int) -> None:
    actor = CharacterState(uuid4(), current_hp=5, max_hp=10, defense=12)
    command = UseItemCommand(
        action_id=uuid4(),
        campaign_id=uuid4(),
        turn_id=uuid4(),
        actor_id=actor.id,
        ordinal=1,
        kind="use_item",
        item_id=uuid4(),
        target_id=actor.id,
        effect_ref="healing_potion",
    )

    with pytest.raises(ValueError, match="在庫"):
        MvpV1Ruleset(DiceEngine(SequenceRandom([]))).resolve_use_item(
            command, actor, actor, quantity=quantity
        )
