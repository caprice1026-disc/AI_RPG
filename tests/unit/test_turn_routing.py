"""ADR-0009の決定的Turn routing契約。"""

import pytest

from ai_rpg.application.routing import RuleBasedTurnRouter


@pytest.mark.parametrize(
    ("player_text", "expected_route", "expected_reason"),
    [
        ("周囲を注意深く観察する", "mechanical", "skill_attempt"),
        ("1d20を振って隠密判定をする", "mechanical", "dice_or_check"),
        ("剣でゴブリンを攻撃しながら叫ぶ", "mechanical", "attack_action"),
        ("回復ポーションを飲む", "mechanical", "item_or_state_change"),
        ("I attack the goblin", "mechanical", "attack_action"),
        ("What is attack?", "narrative", "question_or_explanation"),
        ("What is attack? Then attack the goblin", "mechanical", "attack_action"),
        ("攻撃とは何ですか?", "narrative", "question_or_explanation"),
        (
            "攻撃とは何かを教えて。その後ゴブリンを攻撃する",
            "mechanical",
            "attack_action",
        ),
        ("ポーションを見せて", "narrative", "narrative_or_fallback"),
        ("ゴブリンを攻撃しないで話しかける", "narrative", "narrative_or_fallback"),
        ("ポーションは使わない", "narrative", "narrative_or_fallback"),
        ("ダイスは振らない", "narrative", "narrative_or_fallback"),
        ("I do not attack the goblin", "narrative", "narrative_or_fallback"),
        ("『攻撃する』と彼は言った", "narrative", "narrative_or_fallback"),
        ("今日は静かだね", "narrative", "narrative_or_fallback"),
    ],
)
def test_rule_based_router_classifies_regression_fixtures(
    player_text: str,
    expected_route: str,
    expected_reason: str,
) -> None:
    decision = RuleBasedTurnRouter().decide(player_text)

    assert decision.route == expected_route
    assert decision.rule_version == "mvp_v1"
    assert expected_reason in decision.reason_codes


def test_router_rejects_blank_input_even_if_external_schema_was_bypassed() -> None:
    with pytest.raises(ValueError, match="空"):
        RuleBasedTurnRouter().decide("  ")
