"""認可を毎回再検証する公開イベント取得ユースケース。"""

from collections.abc import Callable
from uuid import UUID

from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.ports import AuthorizationError, AuthorizationPolicy, UnitOfWork
from ai_rpg.contracts import PublicEvent


class EventStreamService:
    def __init__(
        self,
        authorization: AuthorizationPolicy,
        unit_of_work_factory: Callable[[], UnitOfWork],
        *,
        batch_size: int = 100,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_sizeは1以上である必要があります")
        self._authorization = authorization
        self._unit_of_work_factory = unit_of_work_factory
        self._batch_size = batch_size

    async def poll(
        self,
        principal: AuthenticatedPrincipal,
        campaign_id: UUID,
        after: int,
    ) -> tuple[PublicEvent, ...]:
        if after < 0:
            raise ValueError("event cursorは0以上である必要があります")
        if not await self._authorization.can_access_campaign(
            principal.principal_id, campaign_id
        ):
            raise AuthorizationError("Campaignを参照する権限がありません")
        async with self._unit_of_work_factory() as unit_of_work:
            events = await unit_of_work.events.list_after(
                campaign_id, after, limit=self._batch_size
            )
            await unit_of_work.commit()
        return events
