"""HTTP adapterのIntegration Test。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from ai_rpg.api import create_app
from ai_rpg.application import (
    AdventureCompletedError,
    AuthenticatedPrincipal,
    AuthorizationError,
    IdempotencyConflictError,
    TurnInProgressError,
)
from ai_rpg.contracts import (
    CampaignStateResponse,
    PublicEvent,
    PublicTurnEventPayload,
    TurnResponse,
)

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
async def test_play_screen_is_served_with_accessible_core_controls() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<label for="campaign-id">Campaign ID</label>' in response.text
    assert '<label for="actor-id">Actor ID</label>' in response.text
    assert '<label for="action-text">行動を入力</label>' in response.text
    assert 'id="send-action"' in response.text
    assert 'role="status"' in response.text
    assert "aria-live=\"polite\"" in response.text
    assert '<script src="/static/play-state.js"></script>' in response.text
    assert "new EventSource" in response.text
    assert "pollTurn" in response.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_play_state_script_is_served() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/static/play-state.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "AiRpgPending" in response.text


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
    assert response.headers["www-authenticate"] == "Bearer"


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
async def test_get_campaign_state_returns_authoritative_version_and_latest_turn() -> None:
    turn_service = AsyncMock()
    query_service = AsyncMock()
    state = CampaignStateResponse(state_version=4, latest_turn=_pending_response())
    query_service.get_campaign_state.return_value = state
    app = create_app(
        turn_service=turn_service,
        turn_query_service=query_service,
        principal_provider=_authenticated,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/campaigns/{CAMPAIGN_ID}/state")

    assert response.status_code == 200
    assert response.json() == state.model_dump(mode="json")
    principal, campaign_id = query_service.get_campaign_state.await_args.args
    assert principal.principal_id == PRINCIPAL_ID
    assert campaign_id == CAMPAIGN_ID


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_accept_turn_maps_adventure_completed_to_409() -> None:
    turn_service = AsyncMock()
    turn_service.accept.side_effect = AdventureCompletedError()
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

    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "ADVENTURE_COMPLETED"}}


def _public_event(event_id: int) -> PublicEvent:
    return PublicEvent(
        id=event_id,
        type="turn.updated",
        schema_version=1,
        payload=PublicTurnEventPayload(turn=_pending_response()),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sse_replays_after_last_event_id_and_deduplicates() -> None:
    event_stream = AsyncMock()
    event_stream.poll.side_effect = [
        (_public_event(6),),
        (_public_event(6), _public_event(7)),
        AuthorizationError(),
    ]
    app = create_app(
        turn_service=AsyncMock(),
        turn_query_service=AsyncMock(),
        event_stream_service=event_stream,
        principal_provider=_authenticated,
        event_poll_seconds=0,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/campaigns/{CAMPAIGN_ID}/events",
            headers={"Last-Event-ID": "5"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text.count("id: 6\n") == 1
    assert response.text.count("id: 7\n") == 1
    assert "event: turn.updated\n" in response.text
    assert '"schema_version":1' in response.text
    assert [call.args[2] for call in event_stream.poll.await_args_list] == [5, 6, 7]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sse_emits_heartbeat_and_closes_after_membership_revocation() -> None:
    event_stream = AsyncMock()
    event_stream.poll.side_effect = [(), (), AuthorizationError()]
    app = create_app(
        turn_service=AsyncMock(),
        turn_query_service=AsyncMock(),
        event_stream_service=event_stream,
        principal_provider=_authenticated,
        event_poll_seconds=0,
        event_heartbeat_seconds=0.000001,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/campaigns/{CAMPAIGN_ID}/events")

    assert response.status_code == 200
    assert response.text == ": heartbeat\n\n"
    assert event_stream.poll.await_count == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sse_closes_when_authenticated_credential_expires() -> None:
    authenticated_at = datetime(2026, 9, 18, tzinfo=UTC)
    expires_at = authenticated_at + timedelta(seconds=1)
    principal = replace(
        _principal(),
        authenticated_at=authenticated_at,
        credential_expires_at=expires_at,
    )

    async def authenticated() -> AuthenticatedPrincipal:
        return principal

    event_stream = AsyncMock()
    event_stream.poll.return_value = ()
    app = create_app(
        turn_service=AsyncMock(),
        turn_query_service=AsyncMock(),
        event_stream_service=event_stream,
        principal_provider=authenticated,
        event_poll_seconds=0,
        utc_now=lambda: expires_at,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/campaigns/{CAMPAIGN_ID}/events")

    assert response.status_code == 200
    assert response.text == ""
    assert event_stream.poll.await_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sse_rechecks_credential_expiry_before_emitting_event() -> None:
    authenticated_at = datetime(2026, 9, 18, tzinfo=UTC)
    expires_at = authenticated_at + timedelta(seconds=1)
    principal = replace(
        _principal(),
        authenticated_at=authenticated_at,
        credential_expires_at=expires_at,
    )

    async def authenticated() -> AuthenticatedPrincipal:
        return principal

    utc_now = Mock(side_effect=[authenticated_at, expires_at, expires_at])
    event_stream = AsyncMock()
    event_stream.poll.return_value = (_public_event(1),)
    app = create_app(
        turn_service=AsyncMock(),
        turn_query_service=AsyncMock(),
        event_stream_service=event_stream,
        principal_provider=authenticated,
        event_poll_seconds=0,
        utc_now=utc_now,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/campaigns/{CAMPAIGN_ID}/events")

    assert response.status_code == 200
    assert response.text == ""
    assert event_stream.poll.await_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sse_does_not_emit_heartbeat_after_expiry_during_empty_poll() -> None:
    authenticated_at = datetime(2026, 9, 18, tzinfo=UTC)
    expires_at = authenticated_at + timedelta(seconds=1)
    principal = replace(
        _principal(),
        authenticated_at=authenticated_at,
        credential_expires_at=expires_at,
    )

    async def authenticated() -> AuthenticatedPrincipal:
        return principal

    event_stream = AsyncMock()

    async def empty_poll(*_: object) -> tuple[()]:
        if event_stream.poll.await_count == 2:
            await asyncio.sleep(0.001)
        return ()

    event_stream.poll.side_effect = empty_poll
    utc_now = Mock(side_effect=[authenticated_at, expires_at])
    app = create_app(
        turn_service=AsyncMock(),
        turn_query_service=AsyncMock(),
        event_stream_service=event_stream,
        principal_provider=authenticated,
        event_poll_seconds=0,
        event_heartbeat_seconds=0.000001,
        utc_now=utc_now,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/campaigns/{CAMPAIGN_ID}/events")

    assert response.status_code == 200
    assert response.text == ""
    assert event_stream.poll.await_count == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sse_rejects_invalid_cursor_and_initially_forbidden_campaign() -> None:
    event_stream = AsyncMock()
    event_stream.poll.side_effect = AuthorizationError()
    app = create_app(
        turn_service=AsyncMock(),
        turn_query_service=AsyncMock(),
        event_stream_service=event_stream,
        principal_provider=_authenticated,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.get(
            f"/campaigns/{CAMPAIGN_ID}/events",
            headers={"Last-Event-ID": "not-a-number"},
        )
        forbidden = await client.get(f"/campaigns/{CAMPAIGN_ID}/events")

    assert invalid.status_code == 400
    assert invalid.json() == {"detail": {"code": "INVALID_EVENT_CURSOR"}}
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": {"code": "FORBIDDEN"}}
