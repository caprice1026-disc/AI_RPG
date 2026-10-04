"""Usage, profile and operational data separated from story payloads."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_rpg.infrastructure.postgres.models import Base


class UsageReservationModel(Base):
    __tablename__ = "usage_reservations"
    principal_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    purpose: Mapped[str] = mapped_column(Text, primary_key=True)
    request_key: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        index=True,
    )


class ServiceControlModel(Base):
    __tablename__ = "service_controls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usage_paused: Mapped[bool] = mapped_column(Boolean, default=False)


class ProfileModel(Base):
    __tablename__ = "profiles"
    principal_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)


class StoryReportModel(Base):
    __tablename__ = "story_reports"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    story_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("stories.id"))
    reporter_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ModerationAuditModel(Base):
    __tablename__ = "moderation_audit"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    story_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("stories.id"))
    operation: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    previous_value: Mapped[str] = mapped_column(Text)
    next_value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
