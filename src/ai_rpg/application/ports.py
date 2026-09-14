"""Applicationから外部機構へ向くport。"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from ai_rpg.contracts import PlayerTurnInput, TurnResponse


class AuthorizationPolicy(Protocol):
    """Campaign単位の操作権限を検査するport。"""

    async def can_control(self, principal_id: UUID, campaign_id: UUID, actor_id: UUID) -> bool:
        """actorを操作できる場合だけ真を返す。"""


class TurnRepository(Protocol):
    """Turnの永続化を抽象化するrepository port。"""

    async def add(
        self, campaign_id: UUID, principal_id: UUID, turn: PlayerTurnInput
    ) -> TurnResponse:
        """冪等性制約の下でTurnを追加する。"""


class UnitOfWork(AbstractAsyncContextManager["UnitOfWork"], Protocol):
    """Canonical更新とEvent追記をまとめるtransaction境界。"""

    turns: TurnRepository

    async def commit(self) -> None:
        """現在のtransactionを確定する。"""


class PublicEventSource(Protocol):
    """認可済み公開イベントをcursor以降から購読するport。"""

    def subscribe(self, campaign_id: UUID, after: int) -> AsyncIterator[str]:
        """transport非依存のイベント表現を返す。"""
