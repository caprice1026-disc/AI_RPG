"""Mount with create_stories_router(principal_provider=..., sessions=...).

Optional playtest_creator(principal, story_version_id, request) starts an adventure using
the parent's ordinary idempotent creation path. Without it POST /playtests returns the
immutable version and the client can POST /adventures with that story_version_id.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.ports.adventures import InvalidAdventureError
from ai_rpg.application.ports.repositories import AuthorizationError, IdempotencyConflictError
from ai_rpg.application.stories import StoryError, StoryService
from ai_rpg.contracts.adventures import CreateAdventureResponse
from ai_rpg.contracts.stories import (
    CreateStoryPlaytestRequest,
    CreateStoryRequest,
    DuplicateStoryRequest,
    MyStoriesResponse,
    PublishStoryRequest,
    RestoreStoryRequest,
    SaveStoryDraftRequest,
    StoryDraftResponse,
    StoryOwnerSummary,
    StoryPlaytestResponse,
    StoryPlaytestVersionResponse,
    StoryPublicDetail,
    StoryRevisionsResponse,
    StorySettingsRequest,
    StoryTemplatesResponse,
    StoryValidationReport,
    ValidateStoryRequest,
)
from ai_rpg.infrastructure.postgres.stories import PostgresStoryStore

PlaytestCreator = Callable[
    [AuthenticatedPrincipal, UUID, CreateStoryPlaytestRequest],
    Awaitable[CreateAdventureResponse],
]
Result = TypeVar("Result")


async def _result(operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except StoryError as error:
        raise HTTPException(error.status_code, detail={"code": error.code}) from error
    except InvalidAdventureError as error:
        raise HTTPException(422, detail={"code": error.code}) from error
    except AuthorizationError as error:
        raise HTTPException(403, detail={"code": error.code}) from error
    except IdempotencyConflictError as error:
        raise HTTPException(409, detail={"code": error.code}) from error


def create_stories_router(
    *,
    principal_provider: Callable[..., Awaitable[AuthenticatedPrincipal]],
    sessions: async_sessionmaker[AsyncSession] | None = None,
    service: StoryService | None = None,
    playtest_creator: PlaytestCreator | None = None,
) -> APIRouter:
    if service is None:
        if sessions is None:
            raise ValueError("Story sessions or service must be provided")
        service = StoryService(PostgresStoryStore(sessions))
    stories = service
    router = APIRouter(prefix="/stories", tags=["stories"])

    @router.get("/templates", response_model=StoryTemplatesResponse)
    async def list_templates(
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryTemplatesResponse:
        return stories.templates()

    @router.get("/mine", response_model=MyStoriesResponse)
    async def mine(
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> MyStoriesResponse:
        return await _result(stories.list_owned(principal))

    @router.post("", response_model=StoryDraftResponse, status_code=201)
    async def create(
        request: CreateStoryRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryDraftResponse:
        return await _result(stories.create(principal, request))

    @router.get("/{story_id}/draft", response_model=StoryDraftResponse)
    async def draft(
        story_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryDraftResponse:
        return await _result(stories.get_draft(principal, story_id))

    @router.put("/{story_id}/draft", response_model=StoryDraftResponse)
    async def save(
        story_id: UUID,
        request: SaveStoryDraftRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryDraftResponse:
        return await _result(stories.save(principal, story_id, request))

    @router.get("/{story_id}/revisions", response_model=StoryRevisionsResponse)
    async def revisions(
        story_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryRevisionsResponse:
        return await _result(stories.revisions(principal, story_id))

    @router.post("/{story_id}/restore", response_model=StoryDraftResponse)
    async def restore(
        story_id: UUID,
        request: RestoreStoryRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryDraftResponse:
        return await _result(stories.restore(principal, story_id, request))

    @router.post("/{story_id}/duplicate", response_model=StoryDraftResponse, status_code=201)
    async def duplicate(
        story_id: UUID,
        request: DuplicateStoryRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryDraftResponse:
        return await _result(stories.duplicate(principal, story_id, request))

    @router.post("/{story_id}/validate", response_model=StoryValidationReport)
    async def validate(
        story_id: UUID,
        request: ValidateStoryRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryValidationReport:
        return await _result(stories.validate(principal, story_id, request))

    @router.post(
        "/{story_id}/playtests",
        response_model=StoryPlaytestResponse | StoryPlaytestVersionResponse,
        status_code=201,
    )
    async def playtest(
        story_id: UUID,
        request: CreateStoryPlaytestRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryPlaytestResponse | StoryPlaytestVersionResponse:
        version = await _result(stories.create_playtest_version(principal, story_id, request))
        if playtest_creator is None:
            return version
        adventure = await _result(playtest_creator(principal, version.story_version_id, request))
        return StoryPlaytestResponse(
            story_version_id=version.story_version_id,
            content_hash=version.content_hash,
            campaign_id=adventure.campaign_id,
            actor_id=adventure.actor_id,
        )

    @router.post("/{story_id}/publish", response_model=StoryPublicDetail)
    async def publish(
        story_id: UUID,
        request: PublishStoryRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryPublicDetail:
        return await _result(stories.publish(principal, story_id, request))

    @router.put("/{story_id}/settings", response_model=StoryOwnerSummary)
    async def settings(
        story_id: UUID,
        request: StorySettingsRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryOwnerSummary:
        return await _result(stories.settings(principal, story_id, request))

    @router.get("/{story_id}", response_model=StoryPublicDetail)
    async def detail(
        story_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryPublicDetail:
        return await _result(stories.detail(principal, story_id))

    return router
