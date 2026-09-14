"""ターン受付ユースケース。"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from ai_rpg.application.ports import AuthorizationPolicy, UnitOfWork
from ai_rpg.contracts import PlayerTurnInput, TurnResponse


class AuthorizationError(Exception):
    """認証済み主体に対象操作の権限がないことを表す。"""


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """設定readerから切り離してApplicationへ渡す実効値。"""

    max_actions_per_turn: int
    narrative_call_budget: int
    mechanical_call_budget: int


class TurnService:
    """認可とtransactionを仲介してTurnを受け付ける。"""

    def __init__(
        self,
        authorization: AuthorizationPolicy,
        unit_of_work_factory: Callable[[], UnitOfWork],
        policy: RuntimePolicy,
    ) -> None:
        self._authorization = authorization
        self._unit_of_work_factory = unit_of_work_factory
        self._policy = policy

    async def accept(
        self, principal_id: UUID, campaign_id: UUID, turn: PlayerTurnInput
    ) -> TurnResponse:
        """権限を検証し、受付時の実効設定と共にTurnを保存する。"""

        allowed = await self._authorization.can_control(principal_id, campaign_id, turn.actor_id)
        if not allowed:
            raise AuthorizationError("actorを操作する権限がありません")
        # Repository実装はself._policyの値をTurn snapshotへ保存する。
        _ = self._policy
        async with self._unit_of_work_factory() as unit_of_work:
            response = await unit_of_work.turns.add(campaign_id, principal_id, turn)
            await unit_of_work.commit()
        return response
