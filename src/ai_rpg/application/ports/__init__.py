"""Applicationから外部機構へ向くport。"""

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from ai_rpg.application.ports.repositories import (
    ActionRecord,
    AdventureCompletedError,
    AuthorizationError,
    CanonicalRepository,
    CanonicalSnapshot,
    ChoiceDraft,
    ChoiceNotAvailableError,
    CommitBundle,
    FailureDisposition,
    IdempotencyConflictError,
    InvalidCommitBundleError,
    Lease,
    LLMCallRepository,
    LLMPhase,
    NarrationLease,
    NarrationRepository,
    NarrationWorkItem,
    NarrativeCommit,
    PhaseDeadlineExceededError,
    PublicEventRepository,
    RecentMessage,
    RepositorySet,
    ResolutionWorkItem,
    ScenarioRepository,
    ScenarioRunSnapshot,
    ScenarioSceneSnapshot,
    StateVersionConflictError,
    TurnInProgressError,
    TurnRepository,
    TurnRow,
    UnitOfWork,
)
from ai_rpg.contracts import PublicEvent


class AuthorizationPolicy(Protocol):
    """Campaign参照権を検査するport。"""

    async def can_access_campaign(self, principal_id: UUID, campaign_id: UUID) -> bool:
        """Campaignを参照できる場合だけ真を返す。"""


class PublicEventSource(Protocol):
    """認可済み公開イベントをcursor以降から購読するport。"""

    def subscribe(self, campaign_id: UUID, after: int) -> AsyncIterator[PublicEvent]:
        """transport非依存のイベント表現を返す。"""


__all__ = [
    "ActionRecord",
    "AdventureCompletedError",
    "AuthorizationError",
    "AuthorizationPolicy",
    "CanonicalRepository",
    "CanonicalSnapshot",
    "ChoiceDraft",
    "ChoiceNotAvailableError",
    "CommitBundle",
    "FailureDisposition",
    "IdempotencyConflictError",
    "InvalidCommitBundleError",
    "LLMCallRepository",
    "LLMPhase",
    "Lease",
    "NarrationLease",
    "NarrationRepository",
    "NarrationWorkItem",
    "NarrativeCommit",
    "PhaseDeadlineExceededError",
    "PublicEventRepository",
    "PublicEventSource",
    "RecentMessage",
    "RepositorySet",
    "ResolutionWorkItem",
    "ScenarioRepository",
    "ScenarioRunSnapshot",
    "ScenarioSceneSnapshot",
    "StateVersionConflictError",
    "TurnInProgressError",
    "TurnRepository",
    "TurnRow",
    "UnitOfWork",
]
