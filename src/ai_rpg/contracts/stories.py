"""Authoring wire contracts. Drafts deliberately permit incomplete scenario definitions."""

import json
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from ai_rpg.contracts.adventures import AbilityAllocation, CreateAdventureResponse
from ai_rpg.contracts.common import Contract, PositiveInt, Ref

Visibility = Literal["private", "unlisted", "public"]
Lifecycle = Literal["active", "withdrawn", "blocked", "archived"]


class StoryMetadata(Contract):
    title: str = Field(default="", max_length=500)
    synopsis: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    content_warnings: list[str] = Field(default_factory=list, max_length=20)


class AuthoringDraft(Contract):
    authoring_schema_version: Literal[1] = 1
    scenario: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: StoryMetadata = Field(default_factory=StoryMetadata)
    field_policies: dict[str, Literal["fixed", "fillable", "undecided"]] = Field(
        default_factory=dict,
        max_length=1000,
    )
    notes: str = Field(default="", max_length=20000)

    @model_validator(mode="after")
    def bounded_json(self) -> Self:
        encoded = json.dumps(self.model_dump(mode="json"), allow_nan=False, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 512_000:
            raise ValueError("Draft exceeds 512000 bytes")
        if any(not path.startswith("/") for path in self.field_policies):
            raise ValueError("Field policies use JSON pointers beginning with /")
        return self


class TemplateDefinition(Contract):
    template_id: Ref
    version: PositiveInt
    title: str
    description: str
    required_capabilities: list[str]
    sections: list[str]
    initial_draft: AuthoringDraft


class StoryTemplatesResponse(Contract):
    templates: list[TemplateDefinition]


class CreateStoryRequest(Contract):
    request_id: UUID
    template_id: Ref | None = None
    draft: AuthoringDraft | None = None

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if self.template_id is not None and self.draft is not None:
            raise ValueError("Choose a template or a draft")
        return self


class SaveStoryDraftRequest(Contract):
    request_id: UUID
    expected_revision: PositiveInt
    draft: AuthoringDraft


class StoryDraftResponse(Contract):
    story_id: UUID
    revision: PositiveInt
    draft: AuthoringDraft
    updated_at: datetime


class StoryRevision(StoryDraftResponse):
    actor_id: UUID
    source: str


class StoryRevisionsResponse(Contract):
    revisions: list[StoryRevision]


class RestoreStoryRequest(Contract):
    request_id: UUID
    expected_revision: PositiveInt
    revision: PositiveInt


class DuplicateStoryRequest(Contract):
    request_id: UUID


class ValidateStoryRequest(Contract):
    expected_revision: PositiveInt


class ValidationFinding(Contract):
    code: str
    severity: Literal["error", "warning"]
    field_path: str
    entity_ref: str | None = None
    message: str
    suggestion: str | None = None


class StoryValidationReport(Contract):
    id: UUID
    story_id: UUID
    draft_revision: PositiveInt
    content_hash: str | None
    draft_hash: str
    validator_version: str
    errors: list[ValidationFinding]
    warnings: list[ValidationFinding]
    coverage: dict[str, JsonValue]
    created_at: datetime


class CreateStoryPlaytestRequest(Contract):
    request_id: UUID
    expected_revision: PositiveInt
    preset_ref: Ref = "scout"
    player_name: str = Field(default="作者", min_length=1, max_length=40)
    ability_points: AbilityAllocation | None = None
    specialty_skill: (
        Literal[
            "athletics",
            "acrobatics",
            "perception",
            "stealth",
            "persuasion",
        ]
        | None
    ) = None

    @field_validator("player_name")
    @classmethod
    def printable_name(cls, value: str) -> str:
        if not value.strip() or any(not char.isprintable() for char in value):
            raise ValueError("Player name must contain printable non-whitespace text")
        return value

    @model_validator(mode="after")
    def paired_creation_choices(self) -> Self:
        if (self.ability_points is None) != (self.specialty_skill is None):
            raise ValueError("Ability points and specialty must be selected together")
        return self


class StoryPlaytestResponse(CreateAdventureResponse):
    story_version_id: UUID
    content_hash: str


class StoryPlaytestVersionResponse(Contract):
    story_version_id: UUID
    story_id: UUID
    content_hash: str
    draft_revision: PositiveInt


class PublishStoryRequest(Contract):
    request_id: UUID
    expected_revision: PositiveInt
    validation_report_id: UUID
    acknowledged_warning_codes: list[str] = Field(default_factory=list, max_length=100)
    author_playtest_acknowledged: bool = Field(default=False, strict=True)
    visibility: Visibility = "unlisted"


class StorySettingsRequest(Contract):
    request_id: UUID
    visibility: Visibility
    lifecycle: Literal["active", "withdrawn", "archived"]


class StoryPublicDetail(Contract):
    story_id: UUID
    story_version_id: UUID
    ruleset_ref: Literal["mvp_v1", "mvp_v2"]
    release_number: PositiveInt
    visibility: Visibility
    lifecycle: Lifecycle
    metadata: StoryMetadata
    created_at: datetime


class StoryOwnerSummary(Contract):
    story_id: UUID
    revision: PositiveInt
    metadata: StoryMetadata
    visibility: Visibility
    lifecycle: Lifecycle
    current_release_id: UUID | None
    updated_at: datetime


class MyStoriesResponse(Contract):
    stories: list[StoryOwnerSummary]


class StoryPlaytestDebug(Contract):
    campaign_id: UUID
    story_version_id: UUID
    current_scene_ref: str
    status: str
    flags: list[str]
    actions: list[dict[str, JsonValue]]
    endings: list[dict[str, JsonValue]]
