"""Public discovery and operations DTOs never contain a draft or play transcript."""

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from ai_rpg.contracts.common import Contract
from ai_rpg.contracts.stories import StoryPublicDetail


class StoryDiscoveryResponse(Contract):
    stories: list[StoryPublicDetail]
    next_cursor: str | None = None


class ProfileRequest(Contract):
    display_name: str = Field(min_length=1, max_length=40)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Invalid display name")
        return value.strip()


class ProfileResponse(ProfileRequest):
    principal_id: UUID


class StoryReportRequest(Contract):
    request_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class StoryReportResponse(Contract):
    report_id: UUID
    received_at: datetime


class ModerationRequest(Contract):
    request_id: UUID
    blocked: bool
    reason: str = Field(min_length=1, max_length=2000)


class UsagePauseRequest(Contract):
    request_id: UUID
    paused: bool
    reason: str = Field(min_length=1, max_length=2000)


class StoryStats(Contract):
    starts: int
    completions: int


class UsageResponse(Contract):
    day: str
    counts: dict[str, int]
    limits: dict[str, int]
    paused: bool
