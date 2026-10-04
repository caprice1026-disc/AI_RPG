"""Transactional story storage. Every mutation serializes requests then locks its story.

Integration: import_builtin_stories(sessions) is a separate, repeatable startup operation.
The adventure creator calls record_story_playtest in its transaction for kind=playtest.
Debug tools must set StoryPlaytestRecordModel.debug_modified=True before altering a run.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.application.stories import (
    BUILTIN_KEYS,
    VALIDATOR_VERSION,
    StoryError,
    assign_missing_refs,
    compile_draft,
    content_hash,
)
from ai_rpg.contracts.common import Contract
from ai_rpg.contracts.stories import (
    AuthoringDraft,
    CreateStoryPlaytestRequest,
    CreateStoryRequest,
    DuplicateStoryRequest,
    MyStoriesResponse,
    PublishStoryRequest,
    RestoreStoryRequest,
    SaveStoryDraftRequest,
    StoryDraftResponse,
    StoryMetadata,
    StoryOwnerSummary,
    StoryPlaytestDebug,
    StoryPlaytestVersionResponse,
    StoryPublicDetail,
    StoryRevision,
    StoryRevisionsResponse,
    StorySettingsRequest,
    StoryValidationReport,
)
from ai_rpg.infrastructure.postgres.models import PrincipalModel
from ai_rpg.infrastructure.postgres.story_models import (
    BuiltinScenarioVersionModel,
    StoryDraftModel,
    StoryDraftRevisionModel,
    StoryModel,
    StoryPlaytestRecordModel,
    StoryRequestModel,
    StoryValidationReportModel,
    StoryVersionModel,
)
from ai_rpg.scenarios.catalog import BUILTIN_SCENARIOS
from ai_rpg.scenarios.models import ScenarioDefinition


def _draft_response(draft: StoryDraftModel) -> StoryDraftResponse:
    return StoryDraftResponse(
        story_id=draft.story_id,
        revision=draft.revision,
        draft=AuthoringDraft.model_validate(draft.payload),
        updated_at=draft.updated_at,
    )


def _owner_summary(story: StoryModel, draft: StoryDraftModel) -> StoryOwnerSummary:
    return StoryOwnerSummary.model_validate(
        {
            "story_id": story.id,
            "revision": draft.revision,
            "metadata": AuthoringDraft.model_validate(draft.payload).metadata,
            "visibility": story.visibility,
            "lifecycle": story.lifecycle,
            "current_release_id": story.current_release_id,
            "updated_at": story.updated_at,
        }
    )


def _public_detail(story: StoryModel, version: StoryVersionModel) -> StoryPublicDetail:
    return StoryPublicDetail.model_validate(
        {
            "story_id": story.id,
            "story_version_id": version.id,
            "ruleset_ref": version.ruleset_ref,
            "release_number": version.release_number,
            "visibility": story.visibility,
            "lifecycle": story.lifecycle,
            "metadata": version.public_metadata,
            "created_at": version.created_at,
        }
    )


async def _owned(
    session: AsyncSession, owner: UUID, story_id: UUID, *, lock: bool = False
) -> StoryModel:
    query = select(StoryModel).where(
        StoryModel.id == story_id, StoryModel.owner_principal_id == owner
    )
    if lock:
        query = query.with_for_update()
    story = await session.scalar(query)
    if story is None:
        raise StoryError("story_not_found", 404)
    return story


def _editable(story: StoryModel) -> None:
    if story.lifecycle in {"blocked", "archived"}:
        raise StoryError("story_unavailable", 403)


async def _draft(
    session: AsyncSession, story_id: UUID, expected_revision: int | None = None
) -> StoryDraftModel:
    draft = await session.get(StoryDraftModel, story_id)
    if draft is None:
        raise StoryError("draft_not_found", 404)
    if expected_revision is not None and draft.revision != expected_revision:
        raise StoryError("revision_conflict")
    return draft


async def _request_lock(session: AsyncSession, owner: UUID, request_id: UUID) -> None:
    # A per-request transaction lock also covers creates, which have no story row yet.
    lock_id = int(content_hash([str(owner), str(request_id)])[:16], 16)
    if lock_id >= 2**63:
        lock_id -= 2**64
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})


async def _replay(
    session: AsyncSession, owner: UUID, story_id: UUID, request: Contract, operation: str
) -> dict[str, object] | None:
    request_id = UUID(str(request.model_dump()["request_id"]))
    previous = await session.get(StoryRequestModel, (owner, request_id))
    if previous is None:
        return None
    if (
        previous.operation != operation
        or previous.story_id != story_id
        or (previous.input_hash != content_hash(request.model_dump(mode="json")))
    ):
        raise StoryError("idempotency_conflict")
    return previous.response


def _remember(
    session: AsyncSession,
    owner: UUID,
    story_id: UUID,
    request: Contract,
    operation: str,
    response: Contract,
) -> None:
    session.add(
        StoryRequestModel(
            principal_id=owner,
            request_id=UUID(str(request.model_dump()["request_id"])),
            story_id=story_id,
            operation=operation,
            input_hash=content_hash(request.model_dump(mode="json")),
            response=response.model_dump(mode="json"),
        )
    )


def _version(
    story_id: UUID,
    definition: ScenarioDefinition,
    draft: StoryDraftModel,
    kind: str,
    release_number: int | None = None,
) -> StoryVersionModel:
    authoring = AuthoringDraft.model_validate(draft.payload)
    return StoryVersionModel(
        id=uuid4(),
        story_id=story_id,
        kind=kind,
        release_number=release_number,
        source_draft_revision=draft.revision,
        schema_version=definition.schema_version,
        ruleset_ref=definition.ruleset_ref,
        ruleset_version=definition.ruleset_ref,
        capability_requirements=list(definition.required_capabilities),
        payload=definition.model_dump(mode="json"),
        public_metadata=authoring.metadata.model_dump(mode="json"),
        content_hash=content_hash(definition.model_dump(mode="json")),
        created_at=datetime.now(UTC),
    )


class PostgresStoryStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def playtest_debug(
        self,
        owner: UUID,
        story_id: UUID,
        campaign_id: UUID,
    ) -> StoryPlaytestDebug:
        from ai_rpg.application.scenarios import ScenarioProgressor
        from ai_rpg.infrastructure.postgres.models import CampaignMemberModel, CampaignModel
        from ai_rpg.infrastructure.postgres.repositories import PostgresScenarioRepository

        async with self.sessions.begin() as session:
            # Read membership after any concurrent revocation holding the Campaign lock.
            await session.execute(
                select(CampaignModel.id)
                .where(CampaignModel.id == campaign_id)
                .with_for_update()
            )
            evidence = await session.scalar(
                select(StoryPlaytestRecordModel)
                .join(
                    StoryVersionModel,
                    StoryVersionModel.id == StoryPlaytestRecordModel.story_version_id,
                )
                .join(StoryModel, StoryModel.id == StoryVersionModel.story_id)
                .join(
                    CampaignMemberModel,
                    CampaignMemberModel.campaign_id == StoryPlaytestRecordModel.campaign_id,
                )
                .where(
                    StoryModel.id == story_id,
                    StoryModel.owner_principal_id == owner,
                    StoryPlaytestRecordModel.campaign_id == campaign_id,
                    StoryPlaytestRecordModel.actor_id == owner,
                    StoryVersionModel.kind == "playtest",
                    CampaignMemberModel.principal_id == owner,
                    CampaignMemberModel.active.is_(True),
                )
            )
            if evidence is None:
                raise StoryError("playtest_not_found", 404)
            run = await PostgresScenarioRepository(session).snapshot(campaign_id)
            if run is None or run.story_version_id is None:
                raise StoryError("playtest_not_found", 404)
            progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
            definition, scene = progressor.definition_for(run), progressor.scene_for(run)
            return StoryPlaytestDebug(
                campaign_id=campaign_id,
                story_version_id=run.story_version_id,
                current_scene_ref=scene.scene_ref,
                status=run.status,
                flags=sorted(run.flags),
                actions=[
                    {
                        "definition": action.model_dump(mode="json"),
                        "missing_flags": sorted(set(action.required_flags) - run.flags),
                        "blocking_flags": sorted(set(action.disabled_flags) & run.flags),
                    }
                    for action in scene.actions
                ],
                endings=[
                    {
                        "definition": ending.model_dump(mode="json"),
                        "missing_flags": sorted(set(ending.required_flags) - run.flags),
                    }
                    for ending in definition.endings
                ],
            )

    @asynccontextmanager
    async def _mutation(
        self, owner: UUID, story_id: UUID, request_id: UUID
    ) -> AsyncIterator[tuple[AsyncSession, StoryModel]]:
        async with self.sessions() as session, session.begin():
            await _request_lock(session, owner, request_id)
            story = await _owned(session, owner, story_id, lock=True)
            yield session, story

    async def replay(
        self, owner: UUID, story_id: UUID, request: Contract, operation: str
    ) -> dict[str, object] | None:
        async with self.sessions() as session:
            story = await _owned(session, owner, story_id)
            _editable(story)
            return await _replay(session, owner, story_id, request, operation)

    async def create(
        self, owner: UUID, request: CreateStoryRequest, draft: AuthoringDraft
    ) -> StoryDraftResponse:
        story_id = uuid5(NAMESPACE_URL, f"ai-rpg:story:{owner}:{request.request_id}")
        async with self.sessions() as session, session.begin():
            await _request_lock(session, owner, request.request_id)
            previous = await _replay(session, owner, story_id, request, "create")
            if previous is not None:
                await _owned(session, owner, story_id)
                return StoryDraftResponse.model_validate(previous)
            await session.execute(insert(PrincipalModel).values(id=owner).on_conflict_do_nothing())
            response = await self._insert_story(
                session,
                owner,
                story_id,
                draft,
                request.template_id,
            )
            _remember(session, owner, story_id, request, "create", response)
            return response

    async def _insert_story(
        self,
        session: AsyncSession,
        owner: UUID,
        story_id: UUID,
        draft: AuthoringDraft,
        template_id: str | None,
    ) -> StoryDraftResponse:
        from ai_rpg.infrastructure.postgres.community import reserve_usage

        await reserve_usage(session, owner, "story_create", str(story_id))
        now = datetime.now(UTC)
        payload = draft.model_dump(mode="json")
        payload["scenario"]["scenario_ref"] = "story_" + story_id.hex
        payload["scenario"]["version"] = 1
        normalized = assign_missing_refs(AuthoringDraft.model_validate(payload))
        session.add(
            StoryModel(
                id=story_id,
                owner_principal_id=owner,
                visibility="private",
                lifecycle="active",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        model = StoryDraftModel(
            story_id=story_id,
            revision=1,
            authoring_schema_version=1,
            payload=normalized.model_dump(mode="json"),
            template_id=template_id,
            template_version=1 if template_id else None,
            updated_at=now,
        )
        session.add(model)
        session.add(
            StoryDraftRevisionModel(
                story_id=story_id,
                revision=1,
                payload=model.payload,
                actor_id=owner,
                source="create",
                created_at=now,
            )
        )
        await session.flush()
        return _draft_response(model)

    async def get_draft(self, owner: UUID, story_id: UUID) -> StoryDraftResponse:
        async with self.sessions() as session:
            await _owned(session, owner, story_id)
            return _draft_response(await _draft(session, story_id))

    async def list_owned(self, owner: UUID) -> MyStoriesResponse:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(StoryModel, StoryDraftModel)
                    .join(
                        StoryDraftModel,
                        StoryDraftModel.story_id == StoryModel.id,
                    )
                    .where(StoryModel.owner_principal_id == owner)
                    .order_by(
                        StoryModel.updated_at.desc(),
                        StoryModel.id,
                    )
                )
            ).all()
            return MyStoriesResponse(
                stories=[_owner_summary(story, draft) for story, draft in rows]
            )

    async def save(
        self, owner: UUID, story_id: UUID, request: SaveStoryDraftRequest
    ) -> StoryDraftResponse:
        async with self._mutation(owner, story_id, request.request_id) as (session, story):
            _editable(story)
            previous = await _replay(session, owner, story_id, request, "save")
            if previous is not None:
                return StoryDraftResponse.model_validate(previous)
            draft = await _draft(session, story_id, request.expected_revision)
            response = self._revise(session, story, draft, owner, request.draft, "save")
            _remember(session, owner, story_id, request, "save", response)
            return response

    def _revise(
        self,
        session: AsyncSession,
        story: StoryModel,
        draft: StoryDraftModel,
        owner: UUID,
        payload: AuthoringDraft,
        source: str,
    ) -> StoryDraftResponse:
        now = datetime.now(UTC)
        values = payload.model_dump(mode="json")
        expected_ref = "story_" + story.id.hex
        if values["scenario"].get("scenario_ref", expected_ref) != expected_ref:
            raise StoryError("scenario_ref_is_immutable", 422)
        values["scenario"]["scenario_ref"] = expected_ref
        values["scenario"]["version"] = 1
        normalized = assign_missing_refs(AuthoringDraft.model_validate(values))
        draft.revision += 1
        draft.payload = normalized.model_dump(mode="json")
        draft.updated_at = now
        story.updated_at = now
        session.add(
            StoryDraftRevisionModel(
                story_id=story.id,
                revision=draft.revision,
                payload=draft.payload,
                actor_id=owner,
                source=source,
                created_at=now,
            )
        )
        return _draft_response(draft)

    async def revisions(self, owner: UUID, story_id: UUID) -> StoryRevisionsResponse:
        async with self.sessions() as session:
            await _owned(session, owner, story_id)
            rows = (
                await session.scalars(
                    select(StoryDraftRevisionModel)
                    .where(
                        StoryDraftRevisionModel.story_id == story_id,
                    )
                    .order_by(StoryDraftRevisionModel.revision.desc())
                    .limit(100)
                )
            ).all()
            return StoryRevisionsResponse(
                revisions=[
                    StoryRevision(
                        story_id=row.story_id,
                        revision=row.revision,
                        draft=AuthoringDraft.model_validate(row.payload),
                        updated_at=row.created_at,
                        actor_id=row.actor_id,
                        source=row.source,
                    )
                    for row in rows
                ]
            )

    async def restore(
        self, owner: UUID, story_id: UUID, request: RestoreStoryRequest
    ) -> StoryDraftResponse:
        async with self._mutation(owner, story_id, request.request_id) as (session, story):
            _editable(story)
            previous = await _replay(session, owner, story_id, request, "restore")
            if previous is not None:
                return StoryDraftResponse.model_validate(previous)
            draft = await _draft(session, story_id, request.expected_revision)
            historical = await session.get(StoryDraftRevisionModel, (story_id, request.revision))
            if historical is None:
                raise StoryError("revision_not_found", 404)
            response = self._revise(
                session,
                story,
                draft,
                owner,
                AuthoringDraft.model_validate(historical.payload),
                "restore",
            )
            _remember(session, owner, story_id, request, "restore", response)
            return response

    async def duplicate(
        self, owner: UUID, story_id: UUID, request: DuplicateStoryRequest
    ) -> StoryDraftResponse:
        async with self._mutation(owner, story_id, request.request_id) as (session, story):
            if story.lifecycle == "blocked":
                raise StoryError("story_unavailable", 403)
            previous = await _replay(session, owner, story_id, request, "duplicate")
            if previous is not None:
                return StoryDraftResponse.model_validate(previous)
            draft = await _draft(session, story_id)
            response = await self._insert_story(
                session,
                owner,
                uuid4(),
                AuthoringDraft.model_validate(draft.payload),
                draft.template_id,
            )
            _remember(session, owner, story_id, request, "duplicate", response)
            return response

    async def cached_validation(
        self, owner: UUID, story_id: UUID, revision: int,
    ) -> StoryValidationReport | None:
        from ai_rpg.infrastructure.postgres.community import reserve_usage

        async with self.sessions.begin() as session:
            story = await _owned(session, owner, story_id, lock=True)
            _editable(story)
            await _draft(session, story_id, revision)
            cached = await session.scalar(select(StoryValidationReportModel).where(
                StoryValidationReportModel.story_id == story_id,
                StoryValidationReportModel.draft_revision == revision,
                StoryValidationReportModel.validator_version == VALIDATOR_VERSION,
            ).order_by(StoryValidationReportModel.created_at.desc()).limit(1))
            if cached is not None:
                return StoryValidationReport.model_validate(cached.payload)
            await reserve_usage(session, owner, "story_validation",
                                f"{story_id}:{revision}:{VALIDATOR_VERSION}")
            return None

    async def save_validation(self, owner: UUID, report: StoryValidationReport) -> None:
        async with self.sessions() as session, session.begin():
            story = await _owned(session, owner, report.story_id, lock=True)
            _editable(story)
            draft = await _draft(session, report.story_id, report.draft_revision)
            if content_hash(draft.payload) != report.draft_hash:
                raise StoryError("revision_conflict")
            session.add(
                StoryValidationReportModel(
                    id=report.id,
                    story_id=report.story_id,
                    draft_revision=report.draft_revision,
                    content_hash=report.content_hash,
                    draft_hash=report.draft_hash,
                    validator_version=report.validator_version,
                    payload=report.model_dump(mode="json"),
                    created_at=report.created_at,
                )
            )

    async def create_playtest_version(
        self,
        owner: UUID,
        story_id: UUID,
        request: CreateStoryPlaytestRequest,
        definition: ScenarioDefinition,
        report: StoryValidationReport,
    ) -> StoryPlaytestVersionResponse:
        async with self._mutation(owner, story_id, request.request_id) as (session, story):
            _editable(story)
            previous = await _replay(session, owner, story_id, request, "playtest")
            if previous is not None:
                return StoryPlaytestVersionResponse.model_validate(previous)
            draft = await _draft(session, story_id, request.expected_revision)
            if report.errors or content_hash(draft.payload) != report.draft_hash:
                raise StoryError("validation_failed", 422)
            version = _version(story_id, definition, draft, "playtest")
            session.add(version)
            response = StoryPlaytestVersionResponse(
                story_id=story_id,
                story_version_id=version.id,
                content_hash=version.content_hash,
                draft_revision=draft.revision,
            )
            _remember(session, owner, story_id, request, "playtest", response)
            return response

    async def publish(
        self, owner: UUID, story_id: UUID, request: PublishStoryRequest
    ) -> StoryPublicDetail:
        previous = await self.replay(owner, story_id, request, "publish")
        if previous is not None:
            return StoryPublicDetail.model_validate(previous)
        snapshot = await self.get_draft(owner, story_id)
        if snapshot.revision != request.expected_revision:
            raise StoryError("revision_conflict")
        # Reuse validation across playtest/publish; a fresh traversal is quota-admitted.
        fresh = await self.cached_validation(owner, story_id, snapshot.revision)
        if fresh is None:
            _, fresh = compile_draft(snapshot.draft, story_id, snapshot.revision)
            await self.save_validation(owner, fresh)
        if fresh.errors:
            raise StoryError("validation_failed", 422)
        definition = ScenarioDefinition.model_validate(snapshot.draft.scenario)
        async with self._mutation(owner, story_id, request.request_id) as (session, story):
            _editable(story)
            if story.lifecycle != "active":
                raise StoryError("story_unavailable", 403)
            previous = await _replay(session, owner, story_id, request, "publish")
            if previous is not None:
                return StoryPublicDetail.model_validate(previous)
            draft = await _draft(session, story_id, request.expected_revision)
            stored = await session.get(StoryValidationReportModel, request.validation_report_id)
            if (
                stored is None
                or stored.story_id != story_id
                or (
                    stored.draft_revision != draft.revision
                    or stored.content_hash != fresh.content_hash
                    or stored.draft_hash != content_hash(draft.payload)
                    or stored.draft_hash != fresh.draft_hash
                    or stored.validator_version != VALIDATOR_VERSION
                )
            ):
                raise StoryError("validation_outdated")
            report = StoryValidationReport.model_validate(stored.payload)
            if report.errors:
                raise StoryError("validation_failed", 422)
            warning_codes = {warning.code for warning in report.warnings + fresh.warnings}
            if not warning_codes <= set(request.acknowledged_warning_codes):
                raise StoryError("warnings_not_acknowledged", 422)
            if not request.author_playtest_acknowledged:
                raise StoryError("author_acknowledgement_required", 422)
            completed = (
                await session.execute(
                    text("""
                SELECT p.campaign_id, r.ending_ref
                FROM story_playtest_records p
                JOIN story_versions v ON v.id=p.story_version_id
                JOIN mvp_scenario_runs r ON r.campaign_id=p.campaign_id
                    AND r.story_version_id=p.story_version_id
                JOIN campaign_members m ON m.campaign_id=p.campaign_id
                    AND m.principal_id=p.actor_id AND m.active AND m.role='player'
                WHERE v.story_id=:story AND v.kind='playtest' AND v.content_hash=:hash
                    AND p.actor_id=:owner AND NOT p.debug_modified
                    AND r.status='completed' AND r.ending_ref IS NOT NULL
                ORDER BY p.created_at DESC LIMIT 1
                FOR UPDATE OF p
            """),
                    {"story": story_id, "hash": fresh.content_hash, "owner": owner},
                )
            ).first()
            if completed is None or completed.ending_ref not in {
                ending.ending_ref for ending in definition.endings
            }:
                raise StoryError("author_playthrough_required", 422)
            record = await session.get(StoryPlaytestRecordModel, completed.campaign_id)
            assert record is not None
            record.author_acknowledged = True
            record.result = completed.ending_ref
            last = await session.scalar(
                select(func.max(StoryVersionModel.release_number)).where(
                    StoryVersionModel.story_id == story_id,
                )
            )
            version = _version(story_id, definition, draft, "release", (last or 0) + 1)
            session.add(version)
            await session.flush()
            story.current_release_id = version.id
            story.visibility = request.visibility
            story.updated_at = datetime.now(UTC)
            response = _public_detail(story, version)
            _remember(session, owner, story_id, request, "publish", response)
            return response

    async def detail(self, principal_id: UUID, story_id: UUID) -> StoryPublicDetail:
        async with self.sessions() as session:
            story = await session.get(StoryModel, story_id)
            if (
                story is None
                or story.current_release_id is None
                or (
                    story.lifecycle != "active"
                    or (story.visibility == "private" and story.owner_principal_id != principal_id)
                )
            ):
                raise StoryError("story_not_found", 404)
            version = await session.get(StoryVersionModel, story.current_release_id)
            assert version is not None
            return _public_detail(story, version)

    async def settings(
        self, owner: UUID, story_id: UUID, request: StorySettingsRequest
    ) -> StoryOwnerSummary:
        async with self._mutation(owner, story_id, request.request_id) as (session, story):
            if story.lifecycle == "blocked":
                raise StoryError("story_unavailable", 403)
            previous = await _replay(session, owner, story_id, request, "settings")
            if previous is not None:
                return StoryOwnerSummary.model_validate(previous)
            story.visibility = request.visibility
            story.lifecycle = request.lifecycle
            story.updated_at = datetime.now(UTC)
            response = _owner_summary(story, await _draft(session, story_id))
            _remember(session, owner, story_id, request, "settings", response)
            return response


async def record_story_playtest(
    session: AsyncSession,
    story_version_id: UUID,
    campaign_id: UUID,
    principal_id: UUID,
    *,
    debug_modified: bool = False,
) -> None:
    """Call after flushing the author-owned playtest run, in the same start transaction."""
    valid = await session.scalar(
        text("""
        SELECT v.id FROM story_versions v JOIN stories s ON s.id=v.story_id
        JOIN mvp_scenario_runs r ON r.story_version_id=v.id AND r.campaign_id=:campaign
        WHERE v.id=:version AND v.kind='playtest' AND s.owner_principal_id=:owner
    """),
        {"campaign": campaign_id, "version": story_version_id, "owner": principal_id},
    )
    if valid is None:
        raise StoryError("invalid_playtest_run", 403)
    await session.execute(
        insert(StoryPlaytestRecordModel)
        .values(
            campaign_id=campaign_id,
            story_version_id=story_version_id,
            actor_id=principal_id,
            debug_modified=debug_modified,
            author_acknowledged=False,
            notes="",
        )
        .on_conflict_do_nothing(index_elements=["campaign_id"])
    )
    if debug_modified:
        record = await session.get(StoryPlaytestRecordModel, campaign_id)
        assert record is not None
        record.debug_modified = True


def builtin_story_id(scenario_ref: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-rpg:builtin:story:{scenario_ref}")


def builtin_version_id(scenario_ref: str, version: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-rpg:builtin:version:{scenario_ref}:{version}")


async def import_builtin_stories(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Import all historical versions and backfill only NULL references. Never rebuild runs."""
    imported = 0
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(1529015)"))
        latest: dict[str, tuple[int, UUID]] = {}
        for ref, number in BUILTIN_KEYS:
            definition = BUILTIN_SCENARIOS.get(ref, number)
            story_id = builtin_story_id(ref)
            version_id = builtin_version_id(ref, number)
            story = await session.get(StoryModel, story_id)
            if story is None:
                story = StoryModel(
                    id=story_id,
                    owner_principal_id=None,
                    builtin_ref=ref,
                    visibility="public",
                    lifecycle="active",
                )
                session.add(story)
                await session.flush()
            version = await session.get(StoryVersionModel, version_id)
            payload = definition.model_dump(mode="json")
            if version is None:
                version = StoryVersionModel(
                    id=version_id,
                    story_id=story_id,
                    kind="release",
                    release_number=number,
                    source_draft_revision=None,
                    schema_version=definition.schema_version,
                    ruleset_ref=definition.ruleset_ref,
                    ruleset_version=definition.ruleset_ref,
                    capability_requirements=list(definition.required_capabilities),
                    payload=payload,
                    content_hash=content_hash(payload),
                    public_metadata=StoryMetadata(
                        title=definition.title,
                        synopsis=definition.objective,
                    ).model_dump(mode="json"),
                )
                session.add(version)
                await session.flush()
                imported += 1
            elif version.content_hash != content_hash(payload):
                raise StoryError("builtin_content_changed", 409)
            await session.execute(
                insert(BuiltinScenarioVersionModel)
                .values(
                    scenario_ref=ref,
                    scenario_version=number,
                    story_version_id=version_id,
                )
                .on_conflict_do_nothing()
            )
            if ref not in latest or number > latest[ref][0]:
                latest[ref] = (number, version_id)
        for ref, (_, version_id) in latest.items():
            story = await session.get(StoryModel, builtin_story_id(ref))
            assert story is not None
            if story.current_release_id is None:
                story.current_release_id = version_id
        updated = await session.execute(
            text("""
            WITH updated AS (
                UPDATE mvp_scenario_runs r SET story_version_id=b.story_version_id
                FROM builtin_scenario_versions b
                WHERE r.story_version_id IS NULL AND r.scenario_ref=b.scenario_ref
                    AND r.scenario_version=b.scenario_version RETURNING r.campaign_id
            ) SELECT count(*) FROM updated
        """)
        )
        backfilled = int(updated.scalar_one())
        unresolved = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM mvp_scenario_runs WHERE story_version_id IS NULL",
                    )
                )
            ).scalar_one()
        )
    return {
        "versions_imported": imported,
        "runs_backfilled": backfilled,
        "unresolved_runs": unresolved,
    }
