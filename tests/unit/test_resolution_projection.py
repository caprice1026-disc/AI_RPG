"""Engine結果から永続化projectionを作るApplication境界のテスト。"""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ai_rpg.application.ports.repositories import (
    ActionRecord,
    CommitBundle,
    InvalidCommitBundleError,
)
from ai_rpg.application.resolution import (
    CharacterHpMutation,
    InventoryQuantityMutation,
    TurnCommitContext,
    project_resolution,
)
from ai_rpg.contracts.context import OutputLimits
from ai_rpg.contracts.responses import MechanicalNarrationInput
from ai_rpg.domain.commands import AttackCommand, UseItemCommand
from ai_rpg.domain.events import RNGMetadata
from ai_rpg.domain.results import (
    AppliedResult,
    DamageApplied,
    DiceResult,
    HealingApplied,
    ItemConsumed,
    ResolvedAction,
)


def _narration(action: ActionRecord, version: int) -> MechanicalNarrationInput:
    return MechanicalNarrationInput(
        player_text="行動する",
        committed_state_version=version,
        resolved_actions=[
            ResolvedAction(
                action_id=action.command.action_id,
                ordinal=action.command.ordinal,
                result=action.result,
            )
        ],
        public_state_after=[],
        allowed_entity_refs=[],
        output_limits=OutputLimits(max_actions=3, max_choices=5),
    )


def test_damage_projection_derives_hp_mutation_and_version() -> None:
    campaign_id, scene_id, turn_id = uuid4(), uuid4(), uuid4()
    actor_id, target_id, action_id = uuid4(), uuid4(), uuid4()
    command = AttackCommand(
        action_id=action_id,
        campaign_id=campaign_id,
        turn_id=turn_id,
        actor_id=actor_id,
        ordinal=1,
        kind="attack",
        target_id=target_id,
        weapon_id=None,
        attack_bonus=2,
        damage_expression="1d6",
        damage_bonus=0,
    )
    result = AppliedResult(
        kind="applied",
        outcome="success",
        facts=["3ダメージ"],
        dice=[DiceResult(expression="1d6", rolls=[3], modifier=0, total=3)],
        state_changes=[
            DamageApplied(
                kind="damage_applied",
                target_id=target_id,
                amount=3,
                hp_before=10,
                hp_after=7,
            )
        ],
    )
    action = ActionRecord(
        command=command,
        result=result,
        rng=(
            RNGMetadata(
                source="seeded_test",
                implementation_version="mvp_v1",
                draw_index=0,
            ),
        ),
    )
    bundle = CommitBundle(
        campaign_id=campaign_id,
        scene_id=scene_id,
        turn_id=turn_id,
        worker_epoch=1,
        base_state_version=4,
        actions=(action,),
        narration_input=_narration(action, 5),
    )

    projection = project_resolution(
        bundle,
        TurnCommitContext(scene_id=scene_id, actor_id=actor_id, max_actions=3),
        first_event_sequence=8,
    )

    assert projection.committed_state_version == 5
    assert projection.canonical_mutations == (
        CharacterHpMutation(target_id, hp_before=10, hp_after=7, max_hp=None),
    )
    assert [event.sequence for event in projection.events] == [8, 9, 10]
    assert [event.type for event in projection.events] == [
        "DiceRolled",
        "DamageApplied",
        "ActionResolved",
    ]


def test_healing_and_item_consumption_are_typed_canonical_mutations() -> None:
    campaign_id, scene_id, turn_id = uuid4(), uuid4(), uuid4()
    actor_id, item_id, action_id = uuid4(), uuid4(), uuid4()
    command = UseItemCommand(
        action_id=action_id,
        campaign_id=campaign_id,
        turn_id=turn_id,
        actor_id=actor_id,
        ordinal=1,
        kind="use_item",
        item_id=item_id,
        target_id=actor_id,
        effect_ref="healing_potion",
    )
    result = AppliedResult(
        kind="applied",
        outcome="success",
        facts=["4回復"],
        dice=[],
        state_changes=[
            HealingApplied(
                kind="healing_applied",
                target_id=actor_id,
                amount=4,
                hp_before=5,
                hp_after=9,
                max_hp=10,
            ),
            ItemConsumed(
                kind="item_consumed",
                owner_id=actor_id,
                item_id=item_id,
                quantity_before=2,
                quantity_after=1,
            ),
        ],
    )
    action = ActionRecord(command=command, result=result)
    bundle = CommitBundle(
        campaign_id=campaign_id,
        scene_id=scene_id,
        turn_id=turn_id,
        worker_epoch=1,
        base_state_version=0,
        actions=(action,),
        narration_input=_narration(action, 1),
    )

    projection = project_resolution(
        bundle,
        TurnCommitContext(scene_id=scene_id, actor_id=actor_id, max_actions=3),
        first_event_sequence=1,
    )

    assert projection.canonical_mutations == (
        CharacterHpMutation(actor_id, hp_before=5, hp_after=9, max_hp=10),
        InventoryQuantityMutation(
            actor_id,
            item_id,
            quantity_before=2,
            quantity_after=1,
        ),
    )
    assert [event.type for event in projection.events] == [
        "HealingApplied",
        "ItemConsumed",
        "ActionResolved",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {
            "kind": "damage_applied",
            "target_id": UUID(int=1),
            "amount": 3,
            "hp_before": 10,
            "hp_after": 8,
        },
        {
            "kind": "healing_applied",
            "target_id": UUID(int=1),
            "amount": 4,
            "hp_before": 5,
            "hp_after": 10,
            "max_hp": 10,
        },
        {
            "kind": "item_consumed",
            "owner_id": UUID(int=1),
            "item_id": UUID(int=2),
            "quantity_before": 2,
            "quantity_after": 0,
        },
    ],
)
def test_state_change_rejects_engine_arithmetic_mismatch(change: dict[str, object]) -> None:
    model = {
        "damage_applied": DamageApplied,
        "healing_applied": HealingApplied,
        "item_consumed": ItemConsumed,
    }[str(change["kind"])]

    with pytest.raises(ValidationError):
        model.model_validate(change)


def test_projection_rejects_missing_rng_metadata() -> None:
    campaign_id, scene_id, turn_id = uuid4(), uuid4(), uuid4()
    actor_id, target_id, action_id = uuid4(), uuid4(), uuid4()
    command = AttackCommand(
        action_id=action_id,
        campaign_id=campaign_id,
        turn_id=turn_id,
        actor_id=actor_id,
        ordinal=1,
        kind="attack",
        target_id=target_id,
        weapon_id=None,
        attack_bonus=2,
        damage_expression="1d6",
        damage_bonus=0,
    )
    result = AppliedResult(
        kind="applied",
        outcome="success",
        facts=["3ダメージ"],
        dice=[DiceResult(expression="1d6", rolls=[3], modifier=0, total=3)],
        state_changes=[],
    )
    action = ActionRecord(command=command, result=result)
    bundle = CommitBundle(
        campaign_id=campaign_id,
        scene_id=scene_id,
        turn_id=turn_id,
        worker_epoch=1,
        base_state_version=0,
        actions=(action,),
        narration_input=_narration(action, 0),
    )

    with pytest.raises(InvalidCommitBundleError, match="RNG metadata"):
        project_resolution(
            bundle,
            TurnCommitContext(scene_id=scene_id, actor_id=actor_id, max_actions=3),
            first_event_sequence=1,
        )
