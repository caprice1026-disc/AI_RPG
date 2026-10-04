"""Pinned definition reader shared by adventure setup, API and workers."""

import json
from functools import lru_cache
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.application.ports.repositories import AuthorizationError, IdempotencyConflictError
from ai_rpg.application.ports.scenario_source import ResolvedScenario
from ai_rpg.application.stories import StoryError, content_hash
from ai_rpg.contracts.adventures import (
    CharacterCreationSummary,
    CreateAdventureRequest,
    ScenarioSummary,
)
from ai_rpg.contracts.stories import StoryMetadata
from ai_rpg.infrastructure.postgres.models import AdventureStartRequestModel, MvpScenarioRunModel
from ai_rpg.infrastructure.postgres.story_models import (
    BuiltinScenarioVersionModel,
    StoryModel,
    StoryVersionModel,
)
from ai_rpg.scenarios.models import ScenarioDefinition


@lru_cache(maxsize=128)
def _decode(version_id: UUID, digest: str, payload: str) -> ScenarioDefinition:
    try:
        value = json.loads(payload)
        if content_hash(value) != digest:
            raise StoryError("STORY_VERSION_CORRUPT", 503)
        return ScenarioDefinition.model_validate(value)
    except (ValueError, TypeError) as error:
        raise StoryError("STORY_VERSION_CORRUPT", 503) from error


def decode_version(version: StoryVersionModel) -> ScenarioDefinition:
    if version.schema_version not in (1, 2):
        raise StoryError("UNSUPPORTED_STORY_SCHEMA", 422)
    definition = _decode(
        version.id,
        version.content_hash,
        json.dumps(version.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    if (
        definition.schema_version != version.schema_version
        or definition.ruleset_ref != version.ruleset_ref
        or definition.ruleset_ref != version.ruleset_version
        or sorted(definition.required_capabilities) != sorted(version.capability_requirements)
    ):
        raise StoryError("STORY_VERSION_CORRUPT", 503)
    return definition


def published_title(version: StoryVersionModel, definition: ScenarioDefinition) -> str:
    """Public display metadata is pinned independently from the in-game title."""
    try:
        metadata = StoryMetadata.model_validate(version.public_metadata)
    except ValidationError as error:
        raise StoryError("STORY_VERSION_CORRUPT", 503) from error
    return metadata.title if metadata.title.strip() else definition.title


async def load_pinned_definition(
    session: AsyncSession,
    version_id: UUID,
    *,
    check_blocked: bool = True,
) -> ScenarioDefinition:
    row = (
        await session.execute(
            select(StoryVersionModel, StoryModel.lifecycle)
            .join(StoryModel, StoryModel.id == StoryVersionModel.story_id)
            .where(StoryVersionModel.id == version_id)
        )
    ).one_or_none()
    if row is None:
        raise StoryError("STORY_VERSION_NOT_FOUND", 404)
    if check_blocked and row[1] == "blocked":
        raise AuthorizationError("This story has been stopped by an administrator")
    return decode_version(row[0])


async def authorize_story_run(session: AsyncSession, campaign_id: UUID) -> None:
    """Call after the Campaign lock and before each mutation/reservation commit."""
    lifecycle = await session.scalar(
        select(StoryModel.lifecycle)
        .join(StoryVersionModel, StoryVersionModel.story_id == StoryModel.id)
        .join(MvpScenarioRunModel, MvpScenarioRunModel.story_version_id == StoryVersionModel.id)
        .where(MvpScenarioRunModel.campaign_id == campaign_id)
        .with_for_update(read=True, of=StoryModel)
    )
    if lifecycle == "blocked":
        raise AuthorizationError("This story has been stopped by an administrator")


async def resolve_start(
    session: AsyncSession,
    principal_id: UUID,
    request: CreateAdventureRequest,
    *,
    lock: bool = False,
    replay_version_id: UUID | None = None,
) -> ResolvedScenario:
    if (
        replay_version_id is not None
        and request.story_version_id is not None
        and replay_version_id != request.story_version_id
    ):
        raise StoryError("STORY_VERSION_CORRUPT", 503)
    version_id = replay_version_id or request.story_version_id
    if version_id is None:
        version_id = await session.scalar(
            select(BuiltinScenarioVersionModel.story_version_id).where(
                BuiltinScenarioVersionModel.scenario_ref == request.scenario_ref,
                BuiltinScenarioVersionModel.scenario_version == request.scenario_version,
            )
        )
    if version_id is None:
        raise StoryError("STORY_VERSION_NOT_FOUND", 404)
    statement = (
        select(StoryVersionModel, StoryModel)
        .join(StoryModel, StoryModel.id == StoryVersionModel.story_id)
        .where(StoryVersionModel.id == version_id)
    )
    if lock:
        statement = statement.with_for_update(read=True, of=StoryModel).execution_options(
            populate_existing=True,
        )
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise StoryError("STORY_VERSION_NOT_FOUND", 404)
    version, story = row
    is_owner = story.owner_principal_id == principal_id
    if story.lifecycle == "blocked":
        raise AuthorizationError("This story has been stopped by an administrator")
    if version.kind == "playtest":
        if not is_owner:
            raise StoryError("STORY_VERSION_NOT_FOUND", 404)
    elif story.visibility == "private" and not is_owner:
        raise StoryError("STORY_VERSION_NOT_FOUND", 404)
    if replay_version_id is None:
        if story.lifecycle != "active":
            raise StoryError("STORY_UNAVAILABLE", 409)
        # Legacy clients may explicitly start a retained built-in version.
        if (
            request.story_version_id is not None
            and version.kind == "release"
            and story.current_release_id != version.id
        ):
            raise StoryError("STORY_RELEASE_CHANGED", 409)
    definition = decode_version(version)
    if request.scenario_ref is not None and (
        request.scenario_ref != definition.scenario_ref
        or request.scenario_version != definition.version
    ):
        raise StoryError("STORY_REFERENCE_MISMATCH", 422)
    return ResolvedScenario(version.id, definition)


class PostgresScenarioSource:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def for_start(
        self,
        principal_id: UUID,
        request: CreateAdventureRequest,
    ) -> ResolvedScenario:
        async with self._sessions.begin() as session:
            previous = await session.get(
                AdventureStartRequestModel,
                (principal_id, request.request_id),
            )
            replay_version_id = None
            run = None
            if previous is not None:
                if previous.input_payload != request.model_dump(mode="json", exclude_none=True):
                    raise IdempotencyConflictError("Different adventure start payload")
                run = await session.scalar(
                    select(MvpScenarioRunModel).where(
                        MvpScenarioRunModel.campaign_id == previous.campaign_id
                    )
                )
                if run is None or (
                    run.story_version_id is None and request.story_version_id is not None
                ):
                    raise StoryError("STORY_VERSION_CORRUPT", 503)
                replay_version_id = run.story_version_id
            resolved = await resolve_start(
                session,
                principal_id,
                request,
                replay_version_id=replay_version_id,
            )
            if run is not None and (
                run.scenario_ref != resolved.definition.scenario_ref
                or run.scenario_version != resolved.definition.version
            ):
                raise StoryError("STORY_VERSION_CORRUPT", 503)
            return resolved

    async def catalog(self, principal_id: UUID) -> list[ScenarioSummary]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(StoryVersionModel, StoryModel.id)
                    .join(
                        StoryModel,
                        (StoryModel.current_release_id == StoryVersionModel.id)
                        & (StoryModel.id == StoryVersionModel.story_id),
                    )
                    .where(
                        StoryModel.lifecycle == "active",
                        StoryVersionModel.kind == "release",
                        or_(
                            StoryModel.visibility == "public",
                            StoryModel.owner_principal_id == principal_id,
                        ),
                    )
                    .order_by(StoryModel.created_at, StoryModel.id)
                )
            ).all()
            return [
                ScenarioSummary(
                    scenario_ref=(definition := decode_version(version)).scenario_ref,
                    scenario_version=definition.version,
                    title=published_title(version, definition),
                    objective=definition.objective,
                    story_version_id=version.id,
                    story_id=story_id,
                    character_creation=CharacterCreationSummary(
                        abilities=["strength", "agility", "insight", "presence"],
                        points=2,
                        specialties=[
                            "athletics",
                            "acrobatics",
                            "perception",
                            "stealth",
                            "persuasion",
                        ],
                    )
                    if definition.ruleset_ref == "mvp_v2"
                    else None,
                )
                for version, story_id in rows
            ]
