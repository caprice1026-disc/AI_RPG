"""公開event取得時の継続認可。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ai_rpg.application import AuthenticatedPrincipal, AuthorizationError, EventStreamService


@pytest.mark.asyncio
async def test_event_stream_rechecks_membership_on_every_poll() -> None:
    authorization = AsyncMock()
    authorization.can_access_campaign.side_effect = [True, False]
    repository = AsyncMock()
    repository.list_after.return_value = ()

    class UnitOfWork:
        events = repository

        async def __aenter__(self) -> "UnitOfWork":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    principal = AuthenticatedPrincipal(
        principal_id=uuid4(),
        issuer="test",
        subject="player",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset(),
    )
    campaign_id = uuid4()
    service = EventStreamService(authorization, UnitOfWork, batch_size=25)  # type: ignore[arg-type]

    assert await service.poll(principal, campaign_id, 4) == ()
    with pytest.raises(AuthorizationError):
        await service.poll(principal, campaign_id, 4)

    repository.list_after.assert_awaited_once_with(campaign_id, 4, limit=25)
