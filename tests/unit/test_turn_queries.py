"""認可付きTurn公開DTO取得ユースケース。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ai_rpg.application import (
    AuthenticatedPrincipal,
    AuthorizationError,
    AuthorizationPolicy,
    TurnNotFoundError,
    TurnQueryService,
)
from ai_rpg.contracts import TurnResponse


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        principal_id=uuid4(),
        issuer="test",
        subject="player",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset(),
    )


def _response() -> TurnResponse:
    return TurnResponse.model_validate(
        {
            "turn_id": uuid4(),
            "route": None,
            "resolution_status": "pending",
            "narration_status": "pending",
            "committed_state_version": None,
            "narration": None,
            "choices": [],
            "action_results": [],
            "recovery": {"fallback": False, "reason": None},
        }
    )


@pytest.mark.asyncio
async def test_get_returns_repository_public_projection() -> None:
    principal = _principal()
    campaign_id = uuid4()
    response = _response()
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = True
    unit_of_work = AsyncMock()
    unit_of_work.__aenter__.return_value = unit_of_work
    unit_of_work.turns = AsyncMock()
    unit_of_work.turns.get_response.return_value = response
    service = TurnQueryService(authorization, MagicMock(return_value=unit_of_work))

    assert await service.get(principal, campaign_id, response.turn_id) == response
    unit_of_work.turns.get_response.assert_awaited_once_with(campaign_id, response.turn_id)
    unit_of_work.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_rejects_unauthorized_campaign_before_repository() -> None:
    principal = _principal()
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = False
    unit_of_work_factory = MagicMock()
    service = TurnQueryService(authorization, unit_of_work_factory)

    with pytest.raises(AuthorizationError):
        await service.get(principal, uuid4(), uuid4())

    unit_of_work_factory.assert_not_called()


@pytest.mark.asyncio
async def test_get_reports_missing_turn_without_leaking_internal_rows() -> None:
    principal = _principal()
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = True
    unit_of_work = AsyncMock()
    unit_of_work.__aenter__.return_value = unit_of_work
    unit_of_work.turns = AsyncMock()
    unit_of_work.turns.get_response.return_value = None
    service = TurnQueryService(authorization, MagicMock(return_value=unit_of_work))

    with pytest.raises(TurnNotFoundError):
        await service.get(principal, uuid4(), uuid4())

    unit_of_work.commit.assert_not_awaited()
