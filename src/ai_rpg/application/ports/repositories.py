"""永続化境界のフレームワーク非依存ProtocolとDTO。"""

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, TypeAlias
from uuid import UUID

from ai_rpg.contracts import PlayerTurnInput, PublicEvent, TurnResponse
from ai_rpg.contracts.responses import MechanicalNarrationInput
from ai_rpg.domain.commands import Command
from ai_rpg.domain.events import RNGMetadata
from ai_rpg.domain.results import ActionResult


class AuthorizationError(Exception):
    """認証済み主体にCampaignまたはActorの操作権限がない。"""

    code = "FORBIDDEN"


class ChoiceNotAvailableError(Exception):
    """指定Choiceが現在のTurn受付には利用できない。"""

    code = "CHOICE_NOT_AVAILABLE"


class IdempotencyConflictError(Exception):
    """同じrequest_idが異なる正規化入力へ再利用された。"""

    code = "IDEMPOTENCY_CONFLICT"


class InvalidCommitBundleError(ValueError):
    """Mechanical確定Bundleが契約または相互整合性を満たさない。"""

    code = "INVALID_COMMIT_BUNDLE"


class TurnInProgressError(Exception):
    """Campaignに別の未解決Turnが存在する。"""

    code = "TURN_IN_PROGRESS"


class StateVersionConflictError(Exception):
    """指定state versionが現在のCampaignと一致しない。"""

    code = "STATE_VERSION_CONFLICT"


class PhaseDeadlineExceededError(RuntimeError):
    """現在ownerの処理結果がphase deadline後に到着した。"""


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
    terminal_cleanup: bool = False


@dataclass(frozen=True, slots=True)
class NarrationLease:
    turn_id: UUID
    campaign_id: UUID
    worker_epoch: int
    lease_until: datetime
    terminal_cleanup: bool = False


LLMPhase: TypeAlias = Literal["resolution", "narration"]
FailureDisposition: TypeAlias = Literal["retry", "terminal"]


@dataclass(frozen=True, slots=True)
class RecentMessage:
    source: Literal["recent_player", "recent_action_result", "recent_gm"]
    content: str


@dataclass(frozen=True, slots=True)
class ResolutionWorkItem:
    turn_id: UUID
    campaign_id: UUID
    scene_id: UUID
    principal_id: UUID
    actor_id: UUID
    actor_authorized: bool
    worker_epoch: int
    max_actions: int
    expected_state_version: int
    player_text: str
    recent_messages: tuple[RecentMessage, ...]
    route: Literal["narrative", "mechanical"] | None


@dataclass(frozen=True, slots=True)
class NarrationWorkItem:
    turn_id: UUID
    campaign_id: UUID
    worker_epoch: int
    narration_input: MechanicalNarrationInput | None


@dataclass(frozen=True, slots=True)
class CanonicalSnapshot:
    campaign_id: UUID
    state_version: int
    characters: tuple[Mapping[str, object], ...]
    skills: tuple[Mapping[str, object], ...]
    equipment: tuple[Mapping[str, object], ...]
    inventory: tuple[Mapping[str, object], ...]
    skill_checks: tuple[Mapping[str, object], ...]
    entities: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class ActionRecord:
    command: Command
    result: ActionResult
    rng: tuple[RNGMetadata, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitBundle:
    campaign_id: UUID
    scene_id: UUID
    turn_id: UUID
    worker_epoch: int
    base_state_version: int
    actions: tuple[ActionRecord, ...]
    narration_input: MechanicalNarrationInput


@dataclass(frozen=True, slots=True)
class ChoiceDraft:
    id: UUID
    ordinal: int
    label: str


@dataclass(frozen=True, slots=True)
class NarrativeCommit:
    campaign_id: UUID
    scene_id: UUID
    turn_id: UUID
    worker_epoch: int
    base_state_version: int
    narration: str
    choices: tuple[ChoiceDraft, ...]


class TurnRepository(Protocol):
    async def add(
        self,
        campaign_id: UUID,
        principal_id: UUID,
        turn: PlayerTurnInput,
        max_actions: int,
        llm_call_budget: int = 3,
    ) -> TurnResponse: ...

    async def find_by_request_id(
        self, campaign_id: UUID, principal_id: UUID, request_id: UUID
    ) -> TurnRow | None: ...

    async def get_response(
        self, campaign_id: UUID, turn_id: UUID
    ) -> TurnResponse | None: ...

    async def accept_pending(
        self,
        campaign_id: UUID,
        principal_id: UUID,
        turn: PlayerTurnInput,
        *,
        max_actions: int,
        llm_call_budget: int = 3,
    ) -> TurnRow | None: ...

    async def acquire_lease(
        self,
        turn_id: UUID | None,
        *,
        lease_seconds: int,
        max_attempts: int,
        deadline_seconds: int,
    ) -> Lease | None: ...

    async def get_resolution_work(
        self,
        turn_id: UUID,
        worker_epoch: int,
        *,
        recent_messages_limit: int,
    ) -> ResolutionWorkItem | None: ...

    async def record_initial_route(
        self,
        turn_id: UUID,
        worker_epoch: int,
        route: Literal["narrative", "mechanical"],
        rule_version: str,
        reason_codes: Sequence[str],
    ) -> bool: ...

    async def promote_to_mechanical(self, turn_id: UUID, worker_epoch: int) -> bool: ...

    async def finalize_not_applied(
        self, turn_id: UUID, worker_epoch: int, narration: str
    ) -> bool: ...

    async def commit_resolution(self, bundle: CommitBundle) -> int: ...

    async def commit_narrative(self, commit: NarrativeCommit) -> int: ...


class CanonicalRepository(Protocol):
    async def snapshot(self, campaign_id: UUID) -> CanonicalSnapshot: ...

    async def update_with_campaign_lock(
        self, campaign_id: UUID, update: Callable[[], object]
    ) -> int: ...


class NarrationRepository(Protocol):
    async def acquire_lease(
        self,
        turn_id: UUID | None,
        *,
        lease_seconds: int,
        max_attempts: int,
        deadline_seconds: int,
    ) -> NarrationLease | None: ...

    async def get_work(
        self, turn_id: UUID, worker_epoch: int
    ) -> NarrationWorkItem | None: ...

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


class LLMCallRepository(Protocol):
    async def reserve(
        self,
        turn_id: UUID,
        *,
        phase: LLMPhase,
        worker_epoch: int,
    ) -> bool: ...

    async def record_failure(
        self,
        turn_id: UUID,
        *,
        phase: LLMPhase,
        worker_epoch: int,
        failure_code: str,
        max_attempts: int,
    ) -> FailureDisposition | None: ...


class PublicEventRepository(Protocol):
    async def list_after(
        self, campaign_id: UUID, after: int, *, limit: int
    ) -> tuple[PublicEvent, ...]: ...


class RepositorySet(Protocol):
    turns: TurnRepository
    canonical: CanonicalRepository
    narration: NarrationRepository
    llm_calls: LLMCallRepository
    events: PublicEventRepository


class UnitOfWork(RepositorySet, AbstractAsyncContextManager["UnitOfWork"], Protocol):
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
