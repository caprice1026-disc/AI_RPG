"""Owner-only S8/S9 API. Paths use /@scene_ref=foo (never array offsets).

POST /stories/{story_id}/authoring-jobs -> AuthoringJob (202)
GET /stories/{story_id}/authoring-jobs -> AuthoringJobsResponse (latest 20, newest first)
GET /authoring-jobs/{job_id} -> AuthoringJob, including proposal/outline
POST /authoring-jobs/{job_id}/cancel -> AuthoringJob
PUT /authoring-jobs/{job_id}/outline -> AuthoringJob (invalidates approval)
POST /authoring-jobs/{job_id}/outline/approve -> AuthoringJob
POST /stories/{story_id}/proposals/{proposal_id}/apply -> StoryDraftResponse

Concretize requires an explicitly approved outline revision. Adoption is one
decision selecting change IDs; unselected changes are not applied. Stale base
revisions return 409; regenerate or manually edit through the draft API.
"""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from ai_rpg.contracts.common import Contract, NonNegativeInt, PositiveInt
from ai_rpg.contracts.stories import StoryValidationReport, ValidationFinding

JobKind = Literal["check", "fill", "outline", "concretize"]
JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class CreateAuthoringJobRequest(Contract):
    request_id: UUID
    base_revision: PositiveInt
    kind: JobKind
    instructions: str = Field(default="", max_length=8000)
    outline_job_id: UUID | None = None
    approved_outline_revision: PositiveInt | None = None

    @model_validator(mode="after")
    def outline_required(self) -> Self:
        supplied = self.outline_job_id is not None and self.approved_outline_revision is not None
        if self.kind == "concretize":
            if not supplied:
                raise ValueError("Concretize requires an approved outline revision")
        elif self.outline_job_id is not None or self.approved_outline_revision is not None:
            raise ValueError("Only concretize accepts an outline")
        return self


class StoryOutline(Contract):
    title: str = Field(max_length=500)
    premise: str = Field(max_length=4000)
    scenes: list[str] = Field(min_length=1, max_length=12)
    characters: list[str] = Field(default_factory=list, max_length=30)
    endings: list[str] = Field(min_length=1, max_length=12)
    undecided: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(self.model_dump_json().encode()) > 24000:
            raise ValueError("Outline exceeds 24000 bytes")
        return self


class EditStoryOutlineRequest(Contract):
    request_id: UUID
    expected_outline_revision: PositiveInt
    outline: StoryOutline


class ApproveStoryOutlineRequest(Contract):
    request_id: UUID
    expected_outline_revision: PositiveInt


class ApplyStoryProposalRequest(Contract):
    request_id: UUID
    expected_revision: PositiveInt
    change_ids: list[UUID] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_changes(self) -> Self:
        if len(set(self.change_ids)) != len(self.change_ids):
            raise ValueError("Duplicate change IDs")
        return self


class StoryProposalChange(Contract):
    id: UUID
    field_path: str
    operation: Literal["set", "remove"]
    before_exists: bool
    before: JsonValue = None
    after: JsonValue = None
    reason: str
    policy: Literal["fillable", "undecided"] = "fillable"


class StoryProposal(Contract):
    id: UUID
    job_id: UUID
    story_id: UUID
    base_revision: PositiveInt
    changes: list[StoryProposalChange]
    findings: list[ValidationFinding]
    validation: StoryValidationReport
    decision: Literal["pending", "applied"] = "pending"
    applied_revision: PositiveInt | None = None
    adopted_change_ids: list[UUID] = Field(default_factory=list)


class AuthoringJob(Contract):
    """physical_requests counts durable reservations, including uncertain sends.

    usage_complete=False means provider accounting was unavailable, not zero cost.
    An outline may also have a findings-only proposal; neither changes the draft.
    """

    id: UUID
    story_id: UUID
    base_revision: PositiveInt
    snapshot_hash: str
    kind: JobKind
    state: JobState
    model_id: str
    actual_model: str | None = None
    attempts: NonNegativeInt
    physical_requests: NonNegativeInt
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    usage_complete: bool
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    proposal: StoryProposal | None = None
    outline: StoryOutline | None = None
    outline_revision: NonNegativeInt = 0
    approved_outline_revision: PositiveInt | None = None


class AuthoringJobsResponse(Contract):
    jobs: list[AuthoringJob] = Field(max_length=20)


class GeneratedStoryChange(Contract):
    """JSON text keeps the provider's native output schema finite/nonrecursive."""

    field_path: str = Field(min_length=1, max_length=1000)
    operation: Literal["set", "remove"] = "set"
    value_json: str = Field(default="null", max_length=100000)
    reason: str = Field(min_length=1, max_length=2000)


class GeneratedStoryOutput(Contract):
    changes: list[GeneratedStoryChange] = Field(default_factory=list, max_length=200)
    findings: list[ValidationFinding] = Field(default_factory=list, max_length=100)
    outline: StoryOutline | None = None
