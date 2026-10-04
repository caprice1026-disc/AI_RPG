"""Discovery, self-profile and administrative operations."""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ai_rpg.application.auth import AuthenticatedPrincipal
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
from ai_rpg.contracts.stories import StoryPlaytestDebug, StoryPublicDetail
from ai_rpg.infrastructure.postgres.community import CommunityStore
from ai_rpg.infrastructure.postgres.stories import PostgresStoryStore


def create_community_router(
    store: CommunityStore,
    principal_provider: Callable[..., Awaitable[AuthenticatedPrincipal]],
) -> APIRouter:
    router = APIRouter(tags=["community"])
    principal_dependency = Depends(principal_provider)

    @router.get(
        "/stories/{story_id}/playtests/{campaign_id}/debug", response_model=StoryPlaytestDebug
    )
    async def debug(
        story_id: UUID,
        campaign_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, principal_dependency],
    ) -> StoryPlaytestDebug:
        return await PostgresStoryStore(store.sessions).playtest_debug(
            principal.principal_id,
            story_id,
            campaign_id,
        )

    @router.get("/public/stories", response_model=StoryDiscoveryResponse)
    async def discover(
        q: str = Query(default="", max_length=100),
        tag: str = Query(default="", max_length=80),
        cursor: str | None = Query(default=None, max_length=1024),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> StoryDiscoveryResponse:
        return await store.discover(query=q, tag=tag, cursor=cursor, limit=limit)

    @router.get("/public/stories/{story_id}", response_model=StoryPublicDetail)
    async def detail(story_id: UUID) -> StoryPublicDetail:
        return await store.public_detail(story_id)

    @router.get("/profile", response_model=ProfileResponse)
    async def profile(
        principal: Annotated[AuthenticatedPrincipal, principal_dependency],
    ) -> ProfileResponse:
        return await store.profile(principal.principal_id)

    @router.put("/profile", response_model=ProfileResponse)
    async def update_profile(
        request: ProfileRequest,
        principal: Annotated[AuthenticatedPrincipal, principal_dependency],
    ) -> ProfileResponse:
        return await store.profile(principal.principal_id, request)

    @router.get("/usage", response_model=UsageResponse)
    async def usage(
        principal: Annotated[AuthenticatedPrincipal, principal_dependency],
    ) -> UsageResponse:
        return await store.usage(principal.principal_id)

    @router.post("/stories/{story_id}/reports", response_model=StoryReportResponse)
    async def report(
        story_id: UUID,
        request: StoryReportRequest,
        principal: Annotated[AuthenticatedPrincipal, principal_dependency],
    ) -> StoryReportResponse:
        return await store.report(principal.principal_id, story_id, request)

    @router.get("/stories/{story_id}/stats", response_model=StoryStats)
    async def stats(
        story_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, principal_dependency],
    ) -> StoryStats:
        return await store.stats(principal.principal_id, story_id)

    @router.post("/admin/stories/{story_id}/moderation")
    async def moderate(
        story_id: UUID,
        request: ModerationRequest,
        principal: Annotated[AuthenticatedPrincipal, principal_dependency],
    ) -> dict[str, bool]:
        await store.moderate(principal.principal_id, story_id, request)
        return {"ok": True}

    @router.post("/admin/usage")
    async def pause(
        request: UsagePauseRequest,
        principal: Annotated[AuthenticatedPrincipal, principal_dependency],
    ) -> dict[str, bool]:
        await store.pause_usage(principal.principal_id, request)
        return {"ok": True}

    @router.get("/admin/reports")
    async def reports(
        principal: Annotated[AuthenticatedPrincipal, principal_dependency],
    ) -> list[dict[str, object]]:
        return await store.reports(principal.principal_id)

    return router
