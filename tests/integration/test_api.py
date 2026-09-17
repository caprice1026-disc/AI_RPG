"""HTTP adapterのIntegration Test。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from ai_rpg.api import create_app
from ai_rpg.application import (
    AuthenticatedPrincipal,
    IdempotencyConflictError,
    TurnInProgressError,
)
from ai_rpg.contracts import TurnResponse

CAMPAIGN_ID = UUID("00000000-0000-0000-0000-000000000001")
PRINCIPAL_ID = UUID("00000000-0000-0000-0000-000000000021")
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000031")
TURN_ID = UUID("00000000-0000-0000-0000-000000000041")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000071")


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        principal_id=PRINCIPAL_ID,
        issuer="test",
        subject="player-1",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset(),
    )


def _pending_response() -> TurnResponse:
    return TurnResponse.model_validate(
        {
            "turn_id": TURN_ID,
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


async def _authenticated() -> AuthenticatedPrincipal:
    return _principal()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    """application factoryが応答可能なASGI appを返す。"""

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_turn_endpoint_requires_configured_authenticator() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/campaigns/{CAMPAIGN_ID}/turns",
            json={
                "request_id": str(REQUEST_ID),
                "expected_state_version": 0,
                "actor_id": str(ACTOR_ID),
                "content": {"kind": "text", "text": "進む"},
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "UNAUTHENTICATED"}}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_accept_turn_returns_202_and_forwards_authenticated_principal() -> None:
    turn_service = AsyncMock()
    turn_service.accept.return_value = _pending_response()
    query_service = AsyncMock()
    app = create_app(
        turn_service=turn_service,
        turn_query_service=query_service,
        principal_provider=_authenticated,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/campaigns/{CAMPAIGN_ID}/turns",
            json={
                "request_id": str(REQUEST_ID),
                "expected_state_version": 0,
                "actor_id": str(ACTOR_ID),
                "content": {"kind": "text", "text": "進む"},
            },
        )

    assert response.status_code == 202
    assert response.json()["turn_id"] == str(TURN_ID)
    principal, campaign_id, turn = turn_service.accept.await_args.args
    assert principal.principal_id == PRINCIPAL_ID
    assert principal.issuer == "test"
    assert campaign_id == CAMPAIGN_ID
    assert turn.request_id == REQUEST_ID


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_turn_returns_public_response() -> None:
    turn_service = AsyncMock()
    query_service = AsyncMock()
    query_service.get.return_value = _pending_response()
    app = create_app(
        turn_service=turn_service,
        turn_query_service=query_service,
        principal_provider=_authenticated,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/campaigns/{CAMPAIGN_ID}/turns/{TURN_ID}")

    assert response.status_code == 200
    assert response.json() == _pending_response().model_dump(mode="json")
    principal, campaign_id, turn_id = query_service.get.await_args.args
    assert principal.principal_id == PRINCIPAL_ID
    assert campaign_id == CAMPAIGN_ID
    assert turn_id == TURN_ID


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (TurnInProgressError(), 409, "TURN_IN_PROGRESS"),
        (IdempotencyConflictError(), 409, "IDEMPOTENCY_CONFLICT"),
    ],
)
async def test_accept_turn_maps_application_conflicts(
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    turn_service = AsyncMock()
    turn_service.accept.side_effect = error
    app = create_app(
        turn_service=turn_service,
        turn_query_service=AsyncMock(),
        principal_provider=_authenticated,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/campaigns/{CAMPAIGN_ID}/turns",
            json={
                "request_id": str(REQUEST_ID),
                "expected_state_version": 0,
                "actor_id": str(ACTOR_ID),
                "content": {"kind": "text", "text": "進む"},
            },
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": {"code": code}}
