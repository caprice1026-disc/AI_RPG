"""Applicationから外部機構へ向くport。"""

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from ai_rpg.application.ports.repositories import (
    ActionRecord,
    CanonicalRepository,
    CanonicalSnapshot,
    ChoiceDraft,
    CommitBundle,
    EventRecord,
    IdempotencyConflictError,
    Lease,
    NarrationRepository,
    RepositorySet,
    TurnInProgressError,
    TurnRepository,
    TurnRow,
    UnitOfWork,
)


class AuthorizationPolicy(Protocol):
    """Campaign単位の操作権限を検査するport。"""

    async def can_control(self, principal_id: UUID, campaign_id: UUID, actor_id: UUID) -> bool:
        """actorを操作できる場合だけ真を返す。"""


class PublicEventSource(Protocol):
    """認可済み公開イベントをcursor以降から購読するport。"""

    def subscribe(self, campaign_id: UUID, after: int) -> AsyncIterator[str]:
        """transport非依存のイベント表現を返す。"""


__all__ = [
    "ActionRecord",
    "AuthorizationPolicy",
    "CanonicalRepository",
    "CanonicalSnapshot",
    "ChoiceDraft",
    "CommitBundle",
    "EventRecord",
    "IdempotencyConflictError",
    "Lease",
    "NarrationRepository",
    "PublicEventSource",
    "RepositorySet",
    "TurnInProgressError",
    "TurnRepository",
    "TurnRow",
    "UnitOfWork",
]
