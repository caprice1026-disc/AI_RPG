"""ターン受付ユースケース。"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from ai_rpg.application.ports import AuthorizationError as AuthorizationError
from ai_rpg.application.ports import AuthorizationPolicy, UnitOfWork
from ai_rpg.contracts import PlayerTurnInput, TurnResponse, make_decision_types


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """設定readerから切り離してApplicationへ渡す実効値。"""

    max_actions_per_turn: int
    narrative_call_budget: int
    mechanical_call_budget: int

    def __post_init__(self) -> None:
        if type(self.max_actions_per_turn) is not int or self.max_actions_per_turn < 1:
            raise ValueError("max_actions_per_turnは正の整数である必要があります")


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
        # 保存値とSchemaがずれないよう、同じpolicy snapshotから一度だけ生成する。
        self.narrative_decision, self.mechanical_decision = make_decision_types(
            policy.max_actions_per_turn
        )

    async def accept(
        self, principal_id: UUID, campaign_id: UUID, turn: PlayerTurnInput
    ) -> TurnResponse:
        """権限を検証し、受付時の実効設定と共にTurnを保存する。"""

        allowed = await self._authorization.can_access_campaign(principal_id, campaign_id)
        if not allowed:
            raise AuthorizationError("Campaignを参照する権限がありません")
        async with self._unit_of_work_factory() as unit_of_work:
            response = await unit_of_work.turns.add(
                campaign_id,
                principal_id,
                turn,
                self._policy.max_actions_per_turn,
            )
            await unit_of_work.commit()
        return response
