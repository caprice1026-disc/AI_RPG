"""Short SQL transactions; never hold a transaction over a provider request.

Draft adoption intentionally uses the story store's transaction/revision helpers so
proposal decision, request replay and the new draft revision commit atomically.
Migration head must include 0017_community for quota reservations.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.application.ports.story_authoring import AuthoringUsage
from ai_rpg.application.stories import StoryError, content_hash
from ai_rpg.application.story_jobs import AuthoringLimits, adopt_changes
from ai_rpg.config import Settings, get_settings
from ai_rpg.contracts.stories import AuthoringDraft, StoryDraftResponse
from ai_rpg.contracts.story_jobs import (
    ApplyStoryProposalRequest,
    ApproveStoryOutlineRequest,
    AuthoringJob,
    AuthoringJobsResponse,
    CreateAuthoringJobRequest,
    EditStoryOutlineRequest,
    StoryOutline,
    StoryProposal,
)
from ai_rpg.infrastructure.postgres.community import reserve_usage
from ai_rpg.infrastructure.postgres.job_models import (
    AuthoringAttemptModel,
    AuthoringJobModel,
    AuthoringProposalModel,
)
from ai_rpg.infrastructure.postgres.stories import (
    PostgresStoryStore,
    _draft,
    _editable,
    _owned,
    _remember,
    _replay,
)

_ADMISSION_LOCK = 294028408


@dataclass(frozen=True)
class JobLease:
    id: UUID
    owner: UUID
    story_id: UUID
    base_revision: int
    epoch: int
    kind: str
    snapshot: AuthoringDraft
    instructions: str
    outline: dict[str, Any] | None
    model_id: str
    limits: AuthoringLimits


async def _now(session: AsyncSession) -> datetime:
    now = await session.scalar(select(func.clock_timestamp()))
    assert isinstance(now, datetime)
    return now


def _proposal(row: AuthoringProposalModel) -> StoryProposal:
    return StoryProposal.model_validate(
        {
            **row.payload,
            "decision": row.decision,
            "applied_revision": row.applied_revision,
            "adopted_change_ids": row.adopted_change_ids,
        }
    )


async def _response(session: AsyncSession, job: AuthoringJobModel) -> AuthoringJob:
    proposal = await session.scalar(
        select(AuthoringProposalModel).where(AuthoringProposalModel.job_id == job.id)
    )
    return AuthoringJob.model_validate(
        {key: getattr(job, key) for key in AuthoringJob.model_fields if key != "proposal"}
        | {"proposal": _proposal(proposal) if proposal else None}
    )


async def _job(
    session: AsyncSession, owner: UUID, job_id: UUID, *, lock: bool = False
) -> AuthoringJobModel:
    query = select(AuthoringJobModel).where(
        AuthoringJobModel.id == job_id, AuthoringJobModel.owner_principal_id == owner
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    job = await session.scalar(query)
    if job is None:
        raise StoryError("authoring_job_not_found", 404)
    return job


async def _end_attempt(
    session: AsyncSession, job: AuthoringJobModel, state: str, code: str | None, now: datetime
) -> None:
    if job.lease_epoch:
        attempt = await session.get(AuthoringAttemptModel, (job.id, job.lease_epoch))
        if attempt is not None:
            attempt.state, attempt.error_code, attempt.finished_at = state, code, now


class PostgresStoryJobStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        model_id: str,
        limits: AuthoringLimits | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.sessions = sessions
        self.stories = PostgresStoryStore(sessions)
        self.model_id = model_id
        self.settings = settings or get_settings()
        self.limits = limits or AuthoringLimits.from_settings(self.settings)

    async def enqueue(
        self, owner: UUID, story_id: UUID, request: CreateAuthoringJobRequest
    ) -> AuthoringJob:
        async with self.stories._mutation(owner, story_id, request.request_id) as (session, story):
            _editable(story)
            # Shared story request ledger also prevents cross-operation request ID reuse.
            previous = await _replay(session, owner, story_id, request, "authoring_job")
            if previous is not None:
                return await _response(
                    session, await _job(session, owner, UUID(str(previous["id"])))
                )
            draft = await _draft(session, story_id, request.base_revision)
            outline = None
            if request.outline_job_id is not None:
                source = await _job(session, owner, request.outline_job_id, lock=True)
                if (
                    source.story_id != story_id
                    or source.base_revision != draft.revision
                    or source.kind != "outline"
                    or source.state != "succeeded"
                    or source.approved_outline_revision != request.approved_outline_revision
                    or source.outline_revision != request.approved_outline_revision
                ):
                    raise StoryError("outline_not_approved")
                outline = source.outline
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK}
            )
            active = await session.scalar(
                select(func.count())
                .select_from(AuthoringJobModel)
                .where(
                    AuthoringJobModel.owner_principal_id == owner,
                    AuthoringJobModel.state.in_(("queued", "running")),
                )
            )
            if (active or 0) >= self.limits.max_active_per_owner:
                raise StoryError("concurrent_usage_limit", 429)
            job_id = uuid4()
            await reserve_usage(
                session, owner, "authoring_job", str(job_id), settings=self.settings
            )
            now = await _now(session)
            job = AuthoringJobModel(
                id=job_id,
                owner_principal_id=owner,
                story_id=story_id,
                request_id=request.request_id,
                input_hash=content_hash(request.model_dump(mode="json")),
                base_revision=draft.revision,
                snapshot=draft.payload,
                snapshot_hash=content_hash(draft.payload),
                kind=request.kind,
                instructions=request.instructions,
                approved_outline=outline,
                model_id=self.model_id,
                limits=asdict(self.limits),
                state="queued",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.flush()
            response = await _response(session, job)
            _remember(session, owner, story_id, request, "authoring_job", response)
            return response

    async def list(self, owner: UUID, story_id: UUID) -> AuthoringJobsResponse:
        async with self.sessions() as session:
            await _owned(session, owner, story_id)
            jobs = await session.scalars(
                select(AuthoringJobModel)
                .where(
                    AuthoringJobModel.story_id == story_id,
                    AuthoringJobModel.owner_principal_id == owner,
                )
                .order_by(AuthoringJobModel.created_at.desc(), AuthoringJobModel.id.desc())
                .limit(20)
            )
            return AuthoringJobsResponse(jobs=[await _response(session, job) for job in jobs])

    async def get(self, owner: UUID, job_id: UUID) -> AuthoringJob:
        async with self.sessions() as session:
            job = await _job(session, owner, job_id)
            await _owned(session, owner, job.story_id)
            return await _response(session, job)

    async def cancel(self, owner: UUID, job_id: UUID) -> AuthoringJob:
        async with self.sessions() as session, session.begin():
            job = await _job(session, owner, job_id)
            await _owned(session, owner, job.story_id, lock=True)
            job = await _job(session, owner, job_id, lock=True)
            if job.state in {"queued", "running"}:
                now = await _now(session)
                await _end_attempt(session, job, "cancelled", "cancelled", now)
                job.state, job.error_code = "cancelled", "cancelled"
                job.lease_until, job.updated_at = None, now
                job.lease_epoch += 1
            return await _response(session, job)

    async def edit_outline(
        self,
        owner: UUID,
        job_id: UUID,
        request: EditStoryOutlineRequest | ApproveStoryOutlineRequest,
        *,
        approve: bool = False,
    ) -> AuthoringJob:
        existing = await self.get(owner, job_id)
        operation = "approve_outline" if approve else "edit_outline"
        # Include job identity in the operation, as the generic ledger is story-scoped.
        operation += ":" + str(job_id)
        async with self.stories._mutation(owner, existing.story_id, request.request_id) as (
            s,
            story,
        ):
            _editable(story)
            replay = await _replay(s, owner, story.id, request, operation)
            if replay is not None:
                return AuthoringJob.model_validate(replay)
            job = await _job(s, owner, job_id, lock=True)
            if job.kind != "outline" or job.state != "succeeded" or job.outline is None:
                raise StoryError("outline_unavailable")
            if job.outline_revision != request.expected_outline_revision:
                raise StoryError("outline_revision_conflict")
            if approve:
                job.approved_outline_revision = job.outline_revision
            else:
                assert isinstance(request, EditStoryOutlineRequest)
                job.outline = request.outline.model_dump(mode="json")
                job.outline_revision += 1
                job.approved_outline_revision = None
            job.updated_at = await _now(s)
            response = await _response(s, job)
            _remember(s, owner, story.id, request, operation, response)
            return response

    async def apply(
        self, owner: UUID, story_id: UUID, proposal_id: UUID, request: ApplyStoryProposalRequest
    ) -> StoryDraftResponse:
        operation = "apply_proposal:" + str(proposal_id)
        async with self.stories._mutation(owner, story_id, request.request_id) as (session, story):
            _editable(story)
            row = await session.scalar(
                select(AuthoringProposalModel)
                .where(
                    AuthoringProposalModel.id == proposal_id,
                    AuthoringProposalModel.story_id == story_id,
                )
                .with_for_update()
            )
            if row is None:
                raise StoryError("proposal_not_found", 404)
            previous = await _replay(session, owner, story_id, request, operation)
            if previous is not None:
                return StoryDraftResponse.model_validate(previous)
            if row.decision != "pending":
                raise StoryError("proposal_already_applied")
            draft = await _draft(session, story_id, request.expected_revision)
            if row.base_revision != request.expected_revision:
                raise StoryError("proposal_base_conflict")
            payload = adopt_changes(
                AuthoringDraft.model_validate(draft.payload), _proposal(row), request.change_ids
            )
            response = self.stories._revise(session, story, draft, owner, payload, "ai_proposal")
            # Flush the referenced revision before recording its FK in the proposal decision.
            await session.flush()
            row.decision, row.applied_revision = "applied", response.revision
            row.adopted_change_ids = [str(value) for value in request.change_ids]
            _remember(session, owner, story_id, request, operation, response)
            return response

    async def claim(self) -> JobLease | None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK}
            )
            now = await _now(session)
            expired = (
                await session.scalars(
                    select(AuthoringJobModel)
                    .where(
                        AuthoringJobModel.state == "running", AuthoringJobModel.lease_until <= now
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for expired_job in expired:
                await _end_attempt(session, expired_job, "expired", "lease_expired", now)
                limits = AuthoringLimits(**expired_job.limits)
                exhausted = (
                    expired_job.physical_requests > 0 or expired_job.attempts >= limits.max_attempts
                )
                expired_job.state = "failed" if exhausted else "queued"
                expired_job.error_code = (
                    (
                        "request_outcome_unknown"
                        if expired_job.physical_requests
                        else "attempts_exhausted"
                    )
                    if exhausted
                    else None
                )
                expired_job.lease_until, expired_job.updated_at = None, now
                expired_job.lease_epoch += 1
            await session.flush()
            running = await session.scalar(
                select(func.count())
                .select_from(AuthoringJobModel)
                .where(AuthoringJobModel.state == "running")
            )
            if (running or 0) >= self.limits.max_running:
                return None
            job = await session.scalar(
                select(AuthoringJobModel)
                .where(AuthoringJobModel.state == "queued")
                .order_by(AuthoringJobModel.created_at, AuthoringJobModel.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            limits = AuthoringLimits(**job.limits)
            job.state, job.updated_at = "running", now
            job.attempts += 1
            job.lease_epoch += 1
            job.lease_until = now + timedelta(seconds=limits.lease_seconds)
            session.add(
                AuthoringAttemptModel(
                    job_id=job.id, lease_epoch=job.lease_epoch, state="claimed", created_at=now
                )
            )
            return JobLease(
                id=job.id,
                owner=job.owner_principal_id,
                story_id=job.story_id,
                base_revision=job.base_revision,
                epoch=job.lease_epoch,
                kind=job.kind,
                snapshot=AuthoringDraft.model_validate(job.snapshot),
                instructions=job.instructions,
                outline=job.approved_outline,
                model_id=job.model_id,
                limits=limits,
            )

    async def _fenced(self, session: AsyncSession, lease: JobLease) -> AuthoringJobModel | None:
        job = await session.scalar(
            select(AuthoringJobModel)
            .where(
                AuthoringJobModel.id == lease.id,
                AuthoringJobModel.state == "running",
                AuthoringJobModel.lease_epoch == lease.epoch,
                AuthoringJobModel.lease_until > func.clock_timestamp(),
            )
            .with_for_update()
        )
        # The row lock may have waited beyond the deadline after evaluating its WHERE.
        if job is not None and (job.lease_until is None or job.lease_until <= await _now(session)):
            return None
        return job

    async def reserve_call(self, lease: JobLease) -> bool:
        async with self.sessions() as session, session.begin():
            story = await _owned(session, lease.owner, lease.story_id, lock=True)
            _editable(story)
            job = await self._fenced(session, lease)
            if job is None or job.physical_requests:
                return False
            await reserve_usage(
                session,
                lease.owner,
                "llm_authoring",
                f"{job.id}:{job.attempts}",
                settings=self.settings,
            )
            now = await _now(session)
            if job.lease_until is None or job.lease_until <= now:
                raise StoryError("lease_expired")
            attempt = await session.get(AuthoringAttemptModel, (job.id, lease.epoch))
            assert attempt is not None
            attempt.request_reserved, attempt.state = True, "requested"
            job.physical_requests = 1
            job.updated_at = now
            return True

    async def record_usage(
        self,
        lease: JobLease,
        usage: AuthoringUsage,
        *,
        complete: bool,
        actual_model: str | None,
    ) -> None:
        """Accounting only: never unfence a cancelled/expired result or create a proposal."""
        async with self.sessions() as session, session.begin():
            job = await _job(session, lease.owner, lease.id, lock=True)
            attempt = await session.get(AuthoringAttemptModel, (lease.id, lease.epoch))
            if attempt is None or not attempt.request_reserved:
                return
            job.input_tokens = max(job.input_tokens, usage.input_tokens)
            job.output_tokens = max(job.output_tokens, usage.output_tokens)
            job.usage_complete = job.usage_complete or complete
            if actual_model is not None:
                job.actual_model = actual_model

    async def succeed(
        self,
        lease: JobLease,
        proposal: StoryProposal | None,
        outline: StoryOutline | None,
        usage: AuthoringUsage,
        actual_model: str | None,
    ) -> bool:
        async with self.sessions() as session, session.begin():
            story = await _owned(session, lease.owner, lease.story_id, lock=True)
            _editable(story)
            job = await self._fenced(session, lease)
            if job is None:
                return False
            if job.physical_requests != 1:
                raise StoryError("request_not_reserved")
            if proposal is not None:
                session.add(
                    AuthoringProposalModel(
                        id=proposal.id,
                        job_id=job.id,
                        story_id=job.story_id,
                        base_revision=job.base_revision,
                        payload=proposal.model_dump(mode="json"),
                    )
                )
            if outline is not None:
                job.outline = outline.model_dump(mode="json")
                job.outline_revision = 1
            await self._finish(session, job, "succeeded", None, usage, True, actual_model)
            return True

    async def fail(
        self,
        lease: JobLease,
        code: str,
        usage: AuthoringUsage,
        *,
        complete: bool = False,
        actual_model: str | None = None,
    ) -> bool:
        async with self.sessions() as session, session.begin():
            job = await self._fenced(session, lease)
            if job is None:
                return False
            await self._finish(session, job, "failed", code, usage, complete, actual_model)
            return True

    async def _finish(
        self,
        session: AsyncSession,
        job: AuthoringJobModel,
        state: str,
        code: str | None,
        usage: AuthoringUsage,
        complete: bool,
        actual_model: str | None,
    ) -> None:
        now = await _now(session)
        await _end_attempt(session, job, state, code, now)
        job.state, job.error_code, job.lease_until = state, code, None
        job.input_tokens, job.output_tokens = usage.input_tokens, usage.output_tokens
        job.usage_complete, job.actual_model, job.updated_at = complete, actual_model, now
