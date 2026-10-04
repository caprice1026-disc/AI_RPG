"""Story tables share the existing Base; import this module to register metadata."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_rpg.infrastructure.postgres.models import Base


class StoryModel(Base):
    __tablename__ = "stories"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    owner_principal_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("principals.id"),
        index=True,
    )
    visibility: Mapped[str] = mapped_column(Text, default="private")
    lifecycle: Mapped[str] = mapped_column(Text, default="active")
    current_release_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    current_release_kind: Mapped[str] = mapped_column(Text, default="release")
    builtin_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("visibility IN ('private','unlisted','public')"),
        CheckConstraint("lifecycle IN ('active','withdrawn','blocked','archived')"),
        CheckConstraint("current_release_kind='release'"),
        CheckConstraint("(owner_principal_id IS NULL) = (builtin_ref IS NOT NULL)"),
        ForeignKeyConstraint(
            ["id", "current_release_id", "current_release_kind"],
            ["story_versions.story_id", "story_versions.id", "story_versions.kind"],
            name="stories_current_release_fk",
            use_alter=True,
        ),
    )


class StoryDraftModel(Base):
    __tablename__ = "story_drafts"

    story_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stories.id"),
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(BigInteger)
    authoring_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    template_id: Mapped[str | None] = mapped_column(Text)
    template_version: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    __table_args__ = (CheckConstraint("revision>0"),)


class StoryDraftRevisionModel(Base):
    __tablename__ = "story_draft_revisions"

    story_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stories.id"),
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("principals.id"))
    source: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    __table_args__ = (CheckConstraint("revision>0"),)


class StoryVersionModel(Base):
    __tablename__ = "story_versions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    story_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("stories.id"))
    kind: Mapped[str] = mapped_column(Text)
    release_number: Mapped[int | None] = mapped_column(Integer)
    source_draft_revision: Mapped[int | None] = mapped_column(BigInteger)
    schema_version: Mapped[int] = mapped_column(Integer)
    ruleset_ref: Mapped[str] = mapped_column(Text)
    ruleset_version: Mapped[str] = mapped_column(Text)
    capability_requirements: Mapped[list[str]] = mapped_column(JSONB, default=list)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    public_metadata: Mapped[dict[str, object]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("story_id", "id", "kind"),
        UniqueConstraint("story_id", "release_number"),
        CheckConstraint("kind IN ('release','playtest')"),
        CheckConstraint("(kind='release') = (release_number IS NOT NULL)"),
        CheckConstraint("release_number IS NULL OR release_number>0"),
        CheckConstraint("schema_version IN (1,2)"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'"),
        ForeignKeyConstraint(
            ["story_id", "source_draft_revision"],
            ["story_draft_revisions.story_id", "story_draft_revisions.revision"],
        ),
    )


class StoryValidationReportModel(Base):
    __tablename__ = "story_validation_reports"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    story_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("stories.id"))
    draft_revision: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str | None] = mapped_column(Text)
    draft_hash: Mapped[str] = mapped_column(Text)
    validator_version: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["story_id", "draft_revision"],
            ["story_draft_revisions.story_id", "story_draft_revisions.revision"],
        ),
    )


class BuiltinScenarioVersionModel(Base):
    __tablename__ = "builtin_scenario_versions"

    scenario_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    scenario_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("story_versions.id"),
        unique=True,
    )


class StoryPlaytestRecordModel(Base):
    __tablename__ = "story_playtest_records"

    campaign_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("mvp_scenario_runs.campaign_id"),
        primary_key=True,
    )
    story_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("story_versions.id"),
    )
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("principals.id"))
    debug_modified: Mapped[bool] = mapped_column(Boolean, default=False)
    author_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class StoryRequestModel(Base):
    __tablename__ = "story_requests"

    principal_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("principals.id"),
        primary_key=True,
    )
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    operation: Mapped[str] = mapped_column(Text)
    story_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("stories.id"))
    input_hash: Mapped[str] = mapped_column(Text)
    response: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
