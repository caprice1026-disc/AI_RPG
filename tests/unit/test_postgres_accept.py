"""Turn受付とPostgreSQL adapter間の契約をDB接続なしで検証する。"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
    scene_result = MagicMock()
    scene_result.scalar_one.return_value = scene_id
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
    session.execute.side_effect = [existing_result, scene_result, MagicMock(), insert_result]
    authorization = AsyncMock()
    authorization.can_control.return_value = True
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
