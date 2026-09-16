"""外部サービスを使わないEngineのUnit Test。"""

from uuid import uuid4

import pytest

from ai_rpg.domain import CharacterState, SkillCheckCommand
from ai_rpg.engine import DiceEngine, MvpV1Ruleset, SeededRandomSource


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
