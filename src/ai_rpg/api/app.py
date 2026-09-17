"""FastAPI application factory。"""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, status

from ai_rpg.application import (
    AuthenticatedPrincipal,
    AuthorizationError,
    ChoiceNotAvailableError,
    IdempotencyConflictError,
    StateVersionConflictError,
    TurnInProgressError,
    TurnNotFoundError,
    TurnQueryService,
    TurnService,
)
from ai_rpg.application.turns import RuntimePolicy
from ai_rpg.config import get_settings
from ai_rpg.contracts import PlayerTurnInput, TurnResponse
from ai_rpg.infrastructure.database import create_session_factory
from ai_rpg.infrastructure.postgres import PostgresAuthorizationPolicy, PostgresUnitOfWork

PrincipalProvider = Callable[[], Awaitable[AuthenticatedPrincipal]]
ApplicationError = (
    AuthorizationError
    | ChoiceNotAvailableError
    | IdempotencyConflictError
    | StateVersionConflictError
    | TurnInProgressError
    | TurnNotFoundError
)


async def _unconfigured_principal() -> AuthenticatedPrincipal:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHENTICATED"},
    )


def _application_error(error: ApplicationError) -> HTTPException:
    if isinstance(error, AuthorizationError):
        status_code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, TurnNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    else:
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(status_code=status_code, detail={"code": error.code})


def create_app(
    *,
    turn_service: TurnService | None = None,
    turn_query_service: TurnQueryService | None = None,
    principal_provider: PrincipalProvider = _unconfigured_principal,
) -> FastAPI:
    """DB接続をrequest時まで遅延したHTTP applicationを構築する。"""

    if turn_service is None or turn_query_service is None:
        settings = get_settings()
        sessions = create_session_factory(settings.database_url)
        authorization = PostgresAuthorizationPolicy(sessions)

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
            turn_query_service = TurnQueryService(authorization, unit_of_work_factory)

    app = FastAPI(title="AI RPG API", version="0.1.0")

    @app.get("/health", tags=["運用"])
    async def health() -> dict[str, str]:
        """processがHTTP requestを処理できることを返す。"""

        return {"status": "ok"}

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

    return app
