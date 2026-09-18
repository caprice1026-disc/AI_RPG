"""確定結果だけを使うMechanical描写の最小grounding検査。"""

from uuid import UUID

import pytest

from ai_rpg.application.narration_grounding import (
    NarrationGroundingError,
    validate_mechanical_narration,
)
from ai_rpg.contracts.context import EntityRef, OutputLimits
from ai_rpg.contracts.responses import MechanicalNarrationDraft, MechanicalNarrationInput
from ai_rpg.domain.results import AppliedResult, DiceResult, ResolvedAction


def _source() -> MechanicalNarrationInput:
    return MechanicalNarrationInput(
        player_text="ゴブリンを攻撃する",
        committed_state_version=2,
        resolved_actions=[
            ResolvedAction(
                action_id=UUID("00000000-0000-0000-0000-000000000001"),
                ordinal=1,
                result=AppliedResult(
                    kind="applied",
                    outcome="success",
                    facts=["ゴブリンに3ダメージを与えた"],
                    dice=[DiceResult(expression="1d6", rolls=[3], modifier=0, total=3)],
                    state_changes=[],
                ),
            )
        ],
        public_state_after=[],
        allowed_entity_refs=[
            EntityRef(ref="goblin", label="ゴブリン", entity_kind="npc")
        ],
        output_limits=OutputLimits(),
    )


def test_grounding_accepts_saved_numbers_and_allowed_refs() -> None:
    draft = MechanicalNarrationDraft(
        narration="@goblin に3ダメージを与えた。",
        choices=[],
    )

    validate_mechanical_narration(_source(), draft)


@pytest.mark.parametrize(
    "narration",
    [
        "@goblin に5ダメージを与えた。",
        "@dragon に3ダメージを与えた。",
        "00000000-0000-0000-0000-000000000001へ3ダメージを与えた。",
    ],
)
def test_grounding_rejects_unsaved_numbers_or_entity_references(
    narration: str,
) -> None:
    draft = MechanicalNarrationDraft(narration=narration, choices=[])

    with pytest.raises(NarrationGroundingError):
        validate_mechanical_narration(_source(), draft)


def test_grounding_does_not_trust_numbers_claimed_by_player() -> None:
    source = _source().model_copy(update={"player_text": "999ダメージを与える"})
    draft = MechanicalNarrationDraft(
        narration="@goblin に999ダメージを与えた。",
        choices=[],
    )

    with pytest.raises(NarrationGroundingError):
        validate_mechanical_narration(source, draft)
