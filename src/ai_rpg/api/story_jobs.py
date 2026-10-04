"""Mount this router with the same authenticated principal dependency as stories.

All endpoints are private to the owner, including completed/cancelled results.
Error responses intentionally contain codes only, never model exception text.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.stories import StoryError
from ai_rpg.contracts.stories import StoryDraftResponse
from ai_rpg.contracts.story_jobs import (
    ApplyStoryProposalRequest,
    ApproveStoryOutlineRequest,
    AuthoringJob,
    AuthoringJobsResponse,
    CreateAuthoringJobRequest,
    EditStoryOutlineRequest,
)
from ai_rpg.infrastructure.postgres.story_jobs import PostgresStoryJobStore

Result = TypeVar("Result")


async def _result(operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except StoryError as error:
        raise HTTPException(error.status_code, detail={"code": error.code}) from error


def create_story_jobs_router(
    *,
    principal_provider: Callable[..., Awaitable[AuthenticatedPrincipal]],
    store: PostgresStoryJobStore,
) -> APIRouter:
    router = APIRouter(tags=["story-authoring-jobs"])

    @router.post("/stories/{story_id}/authoring-jobs", response_model=AuthoringJob, status_code=202)
    async def enqueue(
        story_id: UUID,
        request: CreateAuthoringJobRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> AuthoringJob:
        return await _result(store.enqueue(principal.principal_id, story_id, request))

    @router.get("/stories/{story_id}/authoring-jobs", response_model=AuthoringJobsResponse)
    async def list_jobs(
        story_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> AuthoringJobsResponse:
        return await _result(store.list(principal.principal_id, story_id))

    @router.get("/authoring-jobs/{job_id}", response_model=AuthoringJob)
    async def get(
        job_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> AuthoringJob:
        return await _result(store.get(principal.principal_id, job_id))

    @router.post("/authoring-jobs/{job_id}/cancel", response_model=AuthoringJob)
    async def cancel(
        job_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> AuthoringJob:
        return await _result(store.cancel(principal.principal_id, job_id))

    @router.put("/authoring-jobs/{job_id}/outline", response_model=AuthoringJob)
    async def edit_outline(
        job_id: UUID,
        request: EditStoryOutlineRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> AuthoringJob:
        return await _result(store.edit_outline(principal.principal_id, job_id, request))

    @router.post("/authoring-jobs/{job_id}/outline/approve", response_model=AuthoringJob)
    async def approve_outline(
        job_id: UUID,
        request: ApproveStoryOutlineRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> AuthoringJob:
        return await _result(
            store.edit_outline(principal.principal_id, job_id, request, approve=True)
        )

    @router.post(
        "/stories/{story_id}/proposals/{proposal_id}/apply", response_model=StoryDraftResponse
    )
    async def apply(
        story_id: UUID,
        proposal_id: UUID,
        request: ApplyStoryProposalRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_provider)],
    ) -> StoryDraftResponse:
        return await _result(store.apply(principal.principal_id, story_id, proposal_id, request))

    return router
