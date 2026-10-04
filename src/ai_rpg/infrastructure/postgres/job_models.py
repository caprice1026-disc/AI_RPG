"""Authoring tables; import registers them on the existing SQLAlchemy Base."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_rpg.infrastructure.postgres.models import Base


class AuthoringJobModel(Base):
    __tablename__ = "authoring_jobs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    story_id: Mapped[UUID] = mapped_column(ForeignKey("stories.id"), index=True)
    owner_principal_id: Mapped[UUID] = mapped_column(ForeignKey("principals.id"), index=True)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    input_hash: Mapped[str] = mapped_column(Text)
    base_revision: Mapped[int] = mapped_column(BigInteger)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    snapshot_hash: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)
    approved_outline: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    model_id: Mapped[str] = mapped_column(Text)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(Text, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    physical_requests: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    usage_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    actual_model: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    outline: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    outline_revision: Mapped[int] = mapped_column(Integer, default=0)
    approved_outline_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AuthoringProposalModel(Base):
    __tablename__ = "authoring_proposals"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("authoring_jobs.id"), unique=True)
    story_id: Mapped[UUID] = mapped_column(ForeignKey("stories.id"))
    base_revision: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    decision: Mapped[str] = mapped_column(Text, default="pending")
    applied_revision: Mapped[int | None] = mapped_column(BigInteger)
    adopted_change_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)


class AuthoringAttemptModel(Base):
    __tablename__ = "authoring_attempts"

    job_id: Mapped[UUID] = mapped_column(ForeignKey("authoring_jobs.id"), primary_key=True)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    request_reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
