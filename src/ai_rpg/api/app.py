"""FastAPI application factory。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Message, Send

from ai_rpg.api.browser_auth import BrowserAuthenticator
from ai_rpg.application import (
    AdventureCompletedError,
    AuthenticatedPrincipal,
    AuthorizationError,
    ChoiceNotAvailableError,
    EventStreamService,
    IdempotencyConflictError,
    StateVersionConflictError,
    TurnInProgressError,
    TurnNotFoundError,
    TurnQueryService,
    TurnService,
)
from ai_rpg.application.adventures import AdventureService
from ai_rpg.application.ports.adventures import InvalidAdventureError, InvalidHistoryCursorError
from ai_rpg.application.turns import RuntimePolicy
from ai_rpg.config import get_settings
from ai_rpg.contracts import CampaignStateResponse, PlayerTurnInput, TurnResponse
from ai_rpg.contracts.adventures import (
    AdventureCatalogResponse,
    AdventureHistoryResponse,
    AdventureListResponse,
    CreateAdventureRequest,
    CreateAdventureResponse,
)
from ai_rpg.infrastructure.database import create_session_factory
from ai_rpg.infrastructure.postgres import PostgresAuthorizationPolicy, PostgresUnitOfWork
from ai_rpg.infrastructure.postgres.adventures import PostgresAdventureStore
from ai_rpg.scenarios import BUILTIN_SCENARIOS

PrincipalProvider = Callable[..., Awaitable[AuthenticatedPrincipal]]
ApplicationError = (
    AdventureCompletedError
    | AuthorizationError
    | ChoiceNotAvailableError
    | IdempotencyConflictError
    | StateVersionConflictError
    | TurnInProgressError
    | TurnNotFoundError
    | InvalidAdventureError
    | InvalidHistoryCursorError
)
_PLAY_ASSETS = Path(__file__).with_name("static") / "vue"
_PLAY_SCREEN = _PLAY_ASSETS / "index.html"


class _TimedStreamingResponse(StreamingResponse):
    """書込みが詰まったsubscriberを切り、DB pollを保持し続けない。"""

    send_timeout_seconds = 5.0

    async def stream_response(self, send: Send) -> None:
        async def timed_send(message: Message) -> None:
            async with asyncio.timeout(self.send_timeout_seconds):
                await send(message)

        try:
            await timed_send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            async for chunk in self.body_iterator:
                if not isinstance(chunk, bytes | memoryview):
                    chunk = chunk.encode(self.charset)
                await timed_send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                )
            await timed_send(
                {"type": "http.response.body", "body": b"", "more_body": False}
            )
        except TimeoutError:
            return


async def _unconfigured_principal() -> AuthenticatedPrincipal:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHENTICATED"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _application_error(error: ApplicationError) -> HTTPException:
    if isinstance(error, AuthorizationError):
        status_code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, TurnNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, InvalidAdventureError | InvalidHistoryCursorError):
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(status_code=status_code, detail={"code": error.code})


def create_app(
    *,
    turn_service: TurnService | None = None,
    turn_query_service: TurnQueryService | None = None,
    event_stream_service: EventStreamService | None = None,
    adventure_service: AdventureService | None = None,
    principal_provider: PrincipalProvider = _unconfigured_principal,
    browser_auth: BrowserAuthenticator | None = None,
    development_mode: bool = False,
    event_poll_seconds: float = 0.5,
    event_heartbeat_seconds: float = 15.0,
    event_send_timeout_seconds: float = 5.0,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """DB接続をrequest時まで遅延したHTTP applicationを構築する。"""

    if (
        event_poll_seconds < 0
        or event_heartbeat_seconds <= 0
        or event_send_timeout_seconds <= 0
    ):
        raise ValueError("event pollとheartbeatの間隔が不正です")
    if (
        turn_service is None
        or turn_query_service is None
        or event_stream_service is None
        or adventure_service is None
    ):
        settings = get_settings()
        sessions = create_session_factory(settings.database_url)
        authorization = PostgresAuthorizationPolicy(sessions)
        if adventure_service is None:
            adventure_service = AdventureService(PostgresAdventureStore(sessions))

        def unit_of_work_factory() -> PostgresUnitOfWork:
            return PostgresUnitOfWork(sessions)

        if turn_service is None:
            turn_service = TurnService(
                authorization,
                unit_of_work_factory,
                RuntimePolicy(
                    settings.max_actions_per_turn,
                    settings.narrative_call_budget,
                    settings.mechanical_call_budget,
                ),
            )
        if turn_query_service is None:
            turn_query_service = TurnQueryService(
                authorization, unit_of_work_factory, BUILTIN_SCENARIOS
            )
        if event_stream_service is None:
            event_stream_service = EventStreamService(
                authorization, unit_of_work_factory
            )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if browser_auth is not None:
                await browser_auth.close()

    app = FastAPI(title="AI RPG API", version="0.1.0", lifespan=lifespan)
    app.mount("/static/vue", StaticFiles(directory=_PLAY_ASSETS), name="vue")
    if browser_auth is not None:
        browser_auth.mount(app)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[..., Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/auth/session", tags=["認証"])
    async def session_info(
        request: Request, principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> dict[str, object]:
        session = getattr(request.state, "browser_session", None)
        return {
            "principal_id": str(principal.principal_id),
            "mode": (
                "development" if development_mode else getattr(request.state, "auth_mode", "bearer")
            ),
            "csrf_token": session.csrf_token if session is not None else None,
            "expires_at": principal.credential_expires_at,
        }

    @app.get("/", include_in_schema=False, response_class=FileResponse)
    async def play_screen() -> FileResponse:
        return FileResponse(_PLAY_SCREEN, media_type="text/html")

    @app.get("/health", tags=["運用"])
    async def health() -> dict[str, str]:
        """processがHTTP requestを処理できることを返す。"""

        return {"status": "ok"}

    @app.post(
        "/adventures",
        tags=["adventures"],
        status_code=status.HTTP_201_CREATED,
        response_model=CreateAdventureResponse,
    )
    async def create_adventure(
        adventure: CreateAdventureRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> CreateAdventureResponse:
        assert adventure_service is not None
        try:
            return await adventure_service.create(principal, adventure)
        except (AuthorizationError, IdempotencyConflictError, InvalidAdventureError) as error:
            raise _application_error(error) from error

    @app.get("/adventures/catalog", tags=["adventures"], response_model=AdventureCatalogResponse)
    async def adventure_catalog(
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> AdventureCatalogResponse:
        assert adventure_service is not None
        return adventure_service.catalog()

    @app.get("/adventures", tags=["adventures"], response_model=AdventureListResponse)
    async def list_adventures(
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> AdventureListResponse:
        assert adventure_service is not None
        return await adventure_service.list_owned(principal)

    @app.get(
        "/campaigns/{campaign_id}/history", tags=["campaigns"],
        response_model=AdventureHistoryResponse,
    )
    async def adventure_history(
        campaign_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before_turn_id: UUID | None = None,
    ) -> AdventureHistoryResponse:
        assert adventure_service is not None
        try:
            return await adventure_service.history(principal, campaign_id, limit, before_turn_id)
        except (AuthorizationError, InvalidHistoryCursorError) as error:
            raise _application_error(error) from error

    @app.post(
        "/campaigns/{campaign_id}/turns",
        tags=["turns"],
        status_code=status.HTTP_202_ACCEPTED,
        response_model=TurnResponse,
    )
    async def accept_turn(
        campaign_id: UUID,
        turn: PlayerTurnInput,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> TurnResponse:
        assert turn_service is not None
        try:
            return await turn_service.accept(principal, campaign_id, turn)
        except (
            AdventureCompletedError,
            AuthorizationError,
            ChoiceNotAvailableError,
            IdempotencyConflictError,
            StateVersionConflictError,
            TurnInProgressError,
        ) as error:
            raise _application_error(error) from error

    @app.get(
        "/campaigns/{campaign_id}/turns/{turn_id}",
        tags=["turns"],
        response_model=TurnResponse,
    )
    async def get_turn(
        campaign_id: UUID,
        turn_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> TurnResponse:
        assert turn_query_service is not None
        try:
            return await turn_query_service.get(principal, campaign_id, turn_id)
        except (AuthorizationError, TurnNotFoundError) as error:
            raise _application_error(error) from error

    @app.get(
        "/campaigns/{campaign_id}/state",
        tags=["campaigns"],
        response_model=CampaignStateResponse,
    )
    async def get_campaign_state(
        campaign_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> CampaignStateResponse:
        assert turn_query_service is not None
        try:
            return await turn_query_service.get_campaign_state(principal, campaign_id)
        except AuthorizationError as error:
            raise _application_error(error) from error

    @app.get("/campaigns/{campaign_id}/events", tags=["events"])
    async def stream_events(
        campaign_id: UUID,
        request: Request,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        assert event_stream_service is not None
        try:
            cursor = 0 if last_event_id is None else int(last_event_id)
            if cursor < 0:
                raise ValueError
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_EVENT_CURSOR"},
            ) from error

        try:
            initial = await event_stream_service.poll(principal, campaign_id, cursor)
        except AuthorizationError as error:
            raise _application_error(error) from error

        async def event_body() -> AsyncIterator[str]:
            nonlocal cursor
            pending = initial
            loop = asyncio.get_running_loop()
            heartbeat_at = loop.time() + event_heartbeat_seconds

            def credential_expired() -> bool:
                expires_at = principal.credential_expires_at
                return expires_at is not None and utc_now() >= expires_at

            while True:
                if browser_auth is not None and not await browser_auth.stream_is_valid(request):
                    return
                if credential_expired() or await request.is_disconnected():
                    return
                events = pending
                pending = ()
                if not events:
                    try:
                        events = await event_stream_service.poll(
                            principal, campaign_id, cursor
                        )
                    except AuthorizationError:
                        return
                    if credential_expired() or (
                        browser_auth is not None and not await browser_auth.stream_is_valid(request)
                    ):
                        return
                emitted = False
                for event in events:
                    if credential_expired() or await request.is_disconnected():
                        return
                    if event.id <= cursor:
                        continue
                    cursor = event.id
                    emitted = True
                    yield (
                        f"id: {event.id}\n"
                        f"event: {event.type}\n"
                        f"data: {event.model_dump_json()}\n\n"
                    )
                if emitted:
                    heartbeat_at = loop.time() + event_heartbeat_seconds
                    continue
                if loop.time() >= heartbeat_at:
                    yield ": heartbeat\n\n"
                    heartbeat_at = loop.time() + event_heartbeat_seconds
                await asyncio.sleep(event_poll_seconds)

        response = _TimedStreamingResponse(
            event_body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        response.send_timeout_seconds = event_send_timeout_seconds
        return response

    return app
