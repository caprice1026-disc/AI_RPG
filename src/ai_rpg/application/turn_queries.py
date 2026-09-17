"""認可付きTurn公開DTO取得ユースケース。"""

from collections.abc import Callable
from uuid import UUID

from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.ports import AuthorizationError, AuthorizationPolicy, UnitOfWork
from ai_rpg.contracts import TurnResponse


class TurnNotFoundError(Exception):
    """指定されたCampaign内に公開可能なTurnが存在しない。"""

    code = "TURN_NOT_FOUND"


class TurnQueryService:
    def __init__(
        self,
        authorization: AuthorizationPolicy,
        unit_of_work_factory: Callable[[], UnitOfWork],
    ) -> None:
        self._authorization = authorization
        self._unit_of_work_factory = unit_of_work_factory

    async def get(
        self,
        principal: AuthenticatedPrincipal,
        campaign_id: UUID,
        turn_id: UUID,
    ) -> TurnResponse:
        if not await self._authorization.can_access_campaign(
            principal.principal_id, campaign_id
        ):
            raise AuthorizationError("Campaignを参照する権限がありません")
        async with self._unit_of_work_factory() as unit_of_work:
            response = await unit_of_work.turns.get_response(campaign_id, turn_id)
            if response is None:
                raise TurnNotFoundError("Turnが存在しません")
            await unit_of_work.commit()
        return response
