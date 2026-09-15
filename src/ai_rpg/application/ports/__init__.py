"""Applicationから外部機構へ向くport。"""

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from ai_rpg.application.ports.repositories import (
    ActionRecord,
    AuthorizationError,
    CanonicalRepository,
    CanonicalSnapshot,
    ChoiceDraft,
    ChoiceNotAvailableError,
    CommitBundle,
    EventRecord,
    IdempotencyConflictError,
    Lease,
    NarrationRepository,
    RepositorySet,
    StateVersionConflictError,
    TurnInProgressError,
    TurnRepository,
    TurnRow,
    UnitOfWork,
)


class AuthorizationPolicy(Protocol):
    """Campaign参照権を検査するport。"""

    async def can_access_campaign(self, principal_id: UUID, campaign_id: UUID) -> bool:
        """Campaignを参照できる場合だけ真を返す。"""


class PublicEventSource(Protocol):
    """認可済み公開イベントをcursor以降から購読するport。"""

    def subscribe(self, campaign_id: UUID, after: int) -> AsyncIterator[str]:
        """transport非依存のイベント表現を返す。"""


__all__ = [
    "ActionRecord",
    "AuthorizationError",
    "AuthorizationPolicy",
    "CanonicalRepository",
    "CanonicalSnapshot",
    "ChoiceDraft",
    "ChoiceNotAvailableError",
    "CommitBundle",
    "EventRecord",
    "IdempotencyConflictError",
    "Lease",
    "NarrationRepository",
    "PublicEventSource",
    "RepositorySet",
    "StateVersionConflictError",
    "TurnInProgressError",
    "TurnRepository",
    "TurnRow",
    "UnitOfWork",
]
