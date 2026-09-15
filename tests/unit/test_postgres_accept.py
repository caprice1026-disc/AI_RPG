"""Turn受付とPostgreSQL adapter間の契約をDB接続なしで検証する。"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_rpg.application import AuthorizationError, AuthorizationPolicy
from ai_rpg.application.turns import RuntimePolicy, TurnService
from ai_rpg.contracts import PlayerTurnInput
from ai_rpg.infrastructure.postgres.repositories import PostgresUnitOfWork


@pytest.mark.asyncio
@pytest.mark.parametrize("max_actions", [3, 5])
async def test_accept_preserves_policy_and_returns_valid_pending_response(max_actions: int) -> None:
    campaign_id, scene_id, principal_id, turn_id = (uuid4() for _ in range(4))
    turn = PlayerTurnInput.model_validate(
        {
            "request_id": uuid4(),
            "actor_id": uuid4(),
            "expected_state_version": 0,
            "content": {"kind": "text", "text": "進む"},
        }
    )
    existing_result = MagicMock()
    existing_result.mappings.return_value.one_or_none.return_value = None
    access_result = MagicMock()
    access_result.scalar_one.return_value = True
    campaign_result = MagicMock()
    campaign_result.mappings.return_value.one.return_value = {
        "state_version": 0,
        "status": "active",
    }
    actor_result = MagicMock()
    actor_result.scalar_one.return_value = True
    scene_result = MagicMock()
    scene_result.scalar_one.return_value = scene_id
    unresolved_result = MagicMock()
    unresolved_result.scalar_one_or_none.return_value = None
    insert_result = MagicMock()
    insert_result.mappings.return_value.one_or_none.return_value = {
        "id": turn_id,
        "campaign_id": campaign_id,
        "scene_id": scene_id,
        "created_by": principal_id,
        "request_id": turn.request_id,
        "resolution_status": "pending",
        "worker_epoch": 0,
    }
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        access_result,
        existing_result,
        campaign_result,
        access_result,
        existing_result,
        actor_result,
        unresolved_result,
        scene_result,
        MagicMock(),
        insert_result,
    ]
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = True
    session_factory = MagicMock(return_value=session)
    service = TurnService(
        authorization,
        lambda: PostgresUnitOfWork(session_factory),
        RuntimePolicy(max_actions, 1, 3),
    )

    response = await service.accept(principal_id, campaign_id, turn)

    assert response.turn_id == turn_id
    assert response.resolution_status == "pending"
    assert response.narration_status == "pending"
    assert response.committed_state_version is None
    assert response.narration is None
    assert response.choices == []
    assert response.action_results == []
    assert not response.recovery.fallback
    assert response.recovery.reason is None
    assert session.execute.call_args_list[-1].args[1]["max_actions"] == max_actions
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_campaign_access_is_checked_before_opening_unit_of_work() -> None:
    principal_id, campaign_id = uuid4(), uuid4()
    turn = PlayerTurnInput.model_validate(
        {
            "request_id": uuid4(),
            "actor_id": uuid4(),
            "expected_state_version": 0,
            "content": {"kind": "text", "text": "進む"},
        }
    )
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = False
    unit_of_work_factory = MagicMock()
    service = TurnService(
        authorization,
        unit_of_work_factory,
        RuntimePolicy(3, 1, 3),
    )

    with pytest.raises(AuthorizationError):
        await service.accept(principal_id, campaign_id, turn)

    authorization.can_access_campaign.assert_awaited_once_with(principal_id, campaign_id)
    unit_of_work_factory.assert_not_called()
