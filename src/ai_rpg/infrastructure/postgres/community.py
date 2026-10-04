"""Public projections, admission limits and audited operational controls."""

import base64
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.application.stories import StoryError, content_hash
from ai_rpg.config import Settings, get_settings
from ai_rpg.contracts.community import (
    ModerationRequest,
    ProfileRequest,
    ProfileResponse,
    StoryDiscoveryResponse,
    StoryReportRequest,
    StoryReportResponse,
    StoryStats,
    UsagePauseRequest,
    UsageResponse,
)
from ai_rpg.contracts.stories import StoryPublicDetail
from ai_rpg.infrastructure.postgres.community_models import (
    ModerationAuditModel,
    ProfileModel,
    ServiceControlModel,
    StoryReportModel,
    UsageReservationModel,
)
from ai_rpg.infrastructure.postgres.models import MvpScenarioRunModel, TurnModel
from ai_rpg.infrastructure.postgres.stories import _public_detail, _request_lock
from ai_rpg.infrastructure.postgres.story_models import StoryModel, StoryVersionModel

_USAGE_LOCK = 294028407


def usage_limits(settings: Settings) -> dict[str, int]:
    return {
        "turn": settings.daily_turn_limit,
        "story_create": settings.daily_story_limit,
        "story_validation": settings.daily_story_validation_limit,
        "authoring_job": settings.daily_authoring_job_limit,
        "llm_game": settings.daily_game_llm_limit,
        "llm_authoring": settings.daily_authoring_llm_limit,
    }


async def reserve_usage(
    session: AsyncSession,
    principal_id: UUID,
    purpose: str,
    request_key: str,
    *,
    settings: Settings | None = None,
) -> None:
    """Reserve inside the caller's transaction, after object locks and replay checks.

    A short shared admission lock serializes counts across processes. No network call
    occurs under it. The ledger survives restarts; a retry with the same key is free.
    """
    settings = settings or get_settings()
    limits = usage_limits(settings)
    if purpose not in limits:
        raise ValueError("Unknown usage purpose")
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _USAGE_LOCK})
    if await session.get(UsageReservationModel, (principal_id, purpose, request_key)) is not None:
        return
    if await session.scalar(
        select(ServiceControlModel.usage_paused).where(
            ServiceControlModel.id == 1,
        )
    ):
        raise StoryError("usage_paused", 503)
    now = await session.scalar(select(func.current_timestamp()))
    assert now is not None
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    count = await session.scalar(
        select(func.count())
        .select_from(UsageReservationModel)
        .where(
            UsageReservationModel.principal_id == principal_id,
            UsageReservationModel.purpose == purpose,
            UsageReservationModel.created_at >= day,
        )
    )
    if (count or 0) >= limits[purpose]:
        raise StoryError("usage_limit_exceeded", 429)
    if purpose.startswith("llm_"):
        total = await session.scalar(
            select(func.count())
            .select_from(UsageReservationModel)
            .where(
                UsageReservationModel.purpose.in_(("llm_game", "llm_authoring")),
                UsageReservationModel.created_at >= day,
            )
        )
        if (total or 0) >= settings.daily_global_llm_limit:
            raise StoryError("usage_limit_exceeded", 429)
    if purpose == "turn":
        active = await session.scalar(
            select(func.count())
            .select_from(TurnModel)
            .where(
                TurnModel.created_by == principal_id,
                ~select(MvpScenarioRunModel.campaign_id)
                .join(
                    StoryVersionModel,
                    StoryVersionModel.id == MvpScenarioRunModel.story_version_id,
                )
                .join(StoryModel, StoryModel.id == StoryVersionModel.story_id)
                .where(
                    MvpScenarioRunModel.campaign_id == TurnModel.campaign_id,
                    StoryModel.lifecycle == "blocked",
                )
                .exists(),
                or_(
                    TurnModel.resolution_status.in_(("pending", "resolving")),
                    TurnModel.narration_status.in_(("pending", "generating")),
                ),
            )
        )
        if (active or 0) >= settings.concurrent_turn_limit:
            raise StoryError("concurrent_usage_limit", 429)
    session.add(
        UsageReservationModel(
            principal_id=principal_id,
            purpose=purpose,
            request_key=request_key,
        )
    )
    await session.flush()


class CommunityStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings or get_settings()

    def require_admin(self, principal_id: UUID) -> None:
        if str(principal_id) not in self.settings.admin_principal_ids.split(","):
            raise StoryError("administrator_required", 403)

    async def discover(
        self,
        *,
        query: str = "",
        tag: str = "",
        cursor: str | None = None,
        limit: int = 20,
    ) -> StoryDiscoveryResponse:
        if not 1 <= limit <= 50 or len(query) > 100 or len(tag) > 80:
            raise StoryError("invalid_search", 422)
        statement = (
            select(StoryModel, StoryVersionModel)
            .join(
                StoryVersionModel,
                StoryVersionModel.id == StoryModel.current_release_id,
            )
            .where(StoryModel.visibility == "public", StoryModel.lifecycle == "active")
        )
        if query:
            statement = statement.where(
                or_(
                    StoryVersionModel.public_metadata["title"].astext.icontains(
                        query, autoescape=True
                    ),
                    StoryVersionModel.public_metadata["synopsis"].astext.icontains(
                        query, autoescape=True
                    ),
                )
            )
        if tag:
            statement = statement.where(StoryVersionModel.public_metadata["tags"].contains([tag]))
        if cursor:
            try:
                if len(cursor) > 1024:
                    raise ValueError
                value = json.loads(base64.urlsafe_b64decode(cursor))
                if value["search"] != content_hash([query, tag]):
                    raise ValueError
                timestamp, story_id = datetime.fromisoformat(value["at"]), UUID(value["id"])
                if timestamp.tzinfo is None:
                    raise ValueError
            except (ValueError, KeyError, TypeError) as error:
                raise StoryError("invalid_cursor", 422) from error
            statement = statement.where(
                tuple_(StoryVersionModel.created_at, StoryModel.id)
                < (
                    timestamp,
                    story_id,
                )
            )
        statement = statement.order_by(StoryVersionModel.created_at.desc(), StoryModel.id.desc())
        async with self.sessions() as session:
            rows = (await session.execute(statement.limit(limit + 1))).all()
        next_cursor = None
        if len(rows) > limit:
            story, version = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "at": version.created_at.isoformat(),
                        "id": str(story.id),
                        "search": content_hash([query, tag]),
                    }
                ).encode()
            ).decode()
        return StoryDiscoveryResponse(
            stories=[_public_detail(story, version) for story, version in rows[:limit]],
            next_cursor=next_cursor,
        )

    async def public_detail(self, story_id: UUID) -> StoryPublicDetail:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(StoryModel, StoryVersionModel)
                    .join(
                        StoryVersionModel,
                        StoryVersionModel.id == StoryModel.current_release_id,
                    )
                    .where(
                        StoryModel.id == story_id,
                        StoryModel.visibility.in_(("public", "unlisted")),
                        StoryModel.lifecycle == "active",
                    )
                )
            ).one_or_none()
            if row is None:
                raise StoryError("story_not_found", 404)
            return _public_detail(row[0], row[1])

    async def profile(
        self, principal_id: UUID, request: ProfileRequest | None = None
    ) -> ProfileResponse:
        async with self.sessions.begin() as session:
            if request is not None:
                await session.execute(
                    insert(ProfileModel)
                    .values(
                        principal_id=principal_id,
                        display_name=request.display_name,
                    )
                    .on_conflict_do_update(
                        index_elements=[ProfileModel.principal_id],
                        set_={
                            "display_name": request.display_name,
                        },
                    )
                )
            name = await session.scalar(
                select(ProfileModel.display_name).where(
                    ProfileModel.principal_id == principal_id,
                )
            )
            return ProfileResponse(principal_id=principal_id, display_name=name or "冒険者")

    async def usage(self, principal_id: UUID) -> UsageResponse:
        async with self.sessions() as session:
            now = await session.scalar(select(func.current_timestamp()))
            assert now is not None
            day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            rows = (
                await session.execute(
                    select(
                        UsageReservationModel.purpose,
                        func.count(),
                    )
                    .where(
                        UsageReservationModel.principal_id == principal_id,
                        UsageReservationModel.created_at >= day,
                    )
                    .group_by(
                        UsageReservationModel.purpose,
                    )
                )
            ).all()
            paused = await session.scalar(
                select(ServiceControlModel.usage_paused).where(
                    ServiceControlModel.id == 1,
                )
            )
            return UsageResponse(
                day=day.date().isoformat(),
                counts={key: count for key, count in rows},
                limits=usage_limits(self.settings),
                paused=bool(paused),
            )

    async def report(
        self,
        principal_id: UUID,
        story_id: UUID,
        request: StoryReportRequest,
    ) -> StoryReportResponse:
        await self.public_detail(story_id)
        async with self.sessions.begin() as session:
            await _request_lock(session, principal_id, request.request_id)
            previous = await session.scalar(
                select(StoryReportModel).where(
                    StoryReportModel.reporter_id == principal_id,
                    StoryReportModel.request_id == request.request_id,
                )
            )
            if previous is not None:
                if previous.story_id != story_id or previous.reason != request.reason:
                    raise StoryError("idempotency_conflict")
                return StoryReportResponse(report_id=previous.id, received_at=previous.created_at)
            record = StoryReportModel(
                id=uuid4(),
                story_id=story_id,
                reporter_id=principal_id,
                request_id=request.request_id,
                reason=request.reason,
            )
            session.add(record)
            await session.flush()
            return StoryReportResponse(report_id=record.id, received_at=record.created_at)

    async def _audit_replay(
        self,
        session: AsyncSession,
        actor: UUID,
        request_id: UUID,
        story_id: UUID | None,
        operation: str,
        reason: str,
        next_value: str,
    ) -> bool:
        previous = await session.scalar(
            select(ModerationAuditModel).where(
                ModerationAuditModel.actor_id == actor,
                ModerationAuditModel.request_id == request_id,
            )
        )
        if previous is None:
            return False
        if (previous.story_id, previous.operation, previous.reason, previous.next_value) != (
            story_id,
            operation,
            reason,
            next_value,
        ):
            raise StoryError("idempotency_conflict")
        return True

    async def moderate(self, actor: UUID, story_id: UUID, request: ModerationRequest) -> None:
        self.require_admin(actor)
        value = "blocked" if request.blocked else "withdrawn"
        async with self.sessions.begin() as session:
            await _request_lock(session, actor, request.request_id)
            if await self._audit_replay(
                session, actor, request.request_id, story_id, "moderate", request.reason, value
            ):
                return
            story = await session.scalar(
                select(StoryModel)
                .where(
                    StoryModel.id == story_id,
                )
                .with_for_update()
            )
            if story is None:
                raise StoryError("story_not_found", 404)
            session.add(
                ModerationAuditModel(
                    id=uuid4(),
                    actor_id=actor,
                    request_id=request.request_id,
                    story_id=story_id,
                    operation="moderate",
                    reason=request.reason,
                    previous_value=story.lifecycle,
                    next_value=value,
                )
            )
            # Unblocking does not silently republish. The owner chooses active again.
            story.lifecycle = value

    async def pause_usage(self, actor: UUID, request: UsagePauseRequest) -> None:
        self.require_admin(actor)
        value = str(request.paused)
        async with self.sessions.begin() as session:
            await _request_lock(session, actor, request.request_id)
            if await self._audit_replay(
                session, actor, request.request_id, None, "pause_usage", request.reason, value
            ):
                return
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _USAGE_LOCK})
            control = await session.get(ServiceControlModel, 1)
            assert control is not None
            session.add(
                ModerationAuditModel(
                    id=uuid4(),
                    actor_id=actor,
                    request_id=request.request_id,
                    story_id=None,
                    operation="pause_usage",
                    reason=request.reason,
                    previous_value=str(control.usage_paused),
                    next_value=value,
                )
            )
            control.usage_paused = request.paused

    async def stats(self, principal_id: UUID, story_id: UUID) -> StoryStats:
        async with self.sessions() as session:
            owner = await session.scalar(
                select(StoryModel.owner_principal_id).where(
                    StoryModel.id == story_id,
                )
            )
            if owner != principal_id:
                raise StoryError("story_not_found", 404)
            rows = (
                await session.execute(
                    select(MvpScenarioRunModel.status, func.count())
                    .join(
                        StoryVersionModel,
                        StoryVersionModel.id == MvpScenarioRunModel.story_version_id,
                    )
                    .where(
                        StoryVersionModel.story_id == story_id, StoryVersionModel.kind == "release"
                    )
                    .group_by(MvpScenarioRunModel.status)
                )
            ).all()
            counts = {key: count for key, count in rows}
            return StoryStats(starts=sum(counts.values()), completions=counts.get("completed", 0))

    async def reports(self, actor: UUID) -> list[dict[str, object]]:
        self.require_admin(actor)
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(StoryReportModel)
                    .order_by(
                        StoryReportModel.created_at.desc(),
                    )
                    .limit(100)
                )
            ).all()
            return [
                {
                    "report_id": row.id,
                    "story_id": row.story_id,
                    "reason": row.reason,
                    "received_at": row.created_at,
                }
                for row in rows
            ]
