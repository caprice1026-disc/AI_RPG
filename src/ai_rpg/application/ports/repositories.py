"""永続化境界のフレームワーク非依存ProtocolとDTO。"""

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from ai_rpg.contracts import PlayerTurnInput, TurnResponse


@dataclass(frozen=True, slots=True)
class TurnRow:
    id: UUID
    campaign_id: UUID
    scene_id: UUID
    principal_id: UUID
    request_id: UUID
    resolution_status: str
    worker_epoch: int


@dataclass(frozen=True, slots=True)
class Lease:
    turn: TurnRow
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class CanonicalSnapshot:
    campaign_id: UUID
    state_version: int
    characters: tuple[Mapping[str, object], ...]
    skills: tuple[Mapping[str, object], ...]
    equipment: tuple[Mapping[str, object], ...]
    inventory: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class ActionRecord:
    id: UUID
    ordinal: int
    actor_id: UUID
    kind: Literal["attack", "skill_check", "use_item"]
    command: Mapping[str, object]
    result: Mapping[str, object]
    result_kind: Literal["applied", "not_applicable"]
    target_id: UUID | None = None
    item_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: UUID
    event_type: str
    payload: Mapping[str, object]
    action_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CommitBundle:
    campaign_id: UUID
    scene_id: UUID
    turn_id: UUID
    worker_epoch: int
    base_state_version: int
    canonical_changed: bool
    canonical_updates: Sequence[tuple[UUID, int, int]]
    actions: Sequence[ActionRecord]
    events: Sequence[EventRecord]
    narration_input: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ChoiceDraft:
    id: UUID
    ordinal: int
    label: str


class TurnRepository(Protocol):
    async def add(
        self,
        campaign_id: UUID,
        principal_id: UUID,
        turn: PlayerTurnInput,
        max_actions: int,
    ) -> TurnResponse: ...

    async def find_by_request_id(
        self, campaign_id: UUID, principal_id: UUID, request_id: UUID
    ) -> TurnRow | None: ...

    async def accept_pending(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        principal_id: UUID,
        turn: PlayerTurnInput,
        *,
        max_actions: int,
    ) -> TurnRow: ...

    async def acquire_lease(self, turn_id: UUID, *, lease_seconds: int) -> Lease | None: ...

    async def commit_resolution(self, bundle: CommitBundle) -> int: ...


class CanonicalRepository(Protocol):
    async def snapshot(self, campaign_id: UUID) -> CanonicalSnapshot: ...

    async def update_with_campaign_lock(
        self, campaign_id: UUID, update: Callable[[], object]
    ) -> int: ...


class NarrationRepository(Protocol):
    async def save_conditionally(
        self,
        campaign_id: UUID,
        turn_id: UUID,
        worker_epoch: int,
        narration: str,
        choices: Sequence[ChoiceDraft],
        *,
        fallback_reason: str | None = None,
    ) -> bool: ...


class RepositorySet(Protocol):
    turns: TurnRepository
    canonical: CanonicalRepository
    narration: NarrationRepository


class UnitOfWork(RepositorySet, AbstractAsyncContextManager["UnitOfWork"], Protocol):
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
