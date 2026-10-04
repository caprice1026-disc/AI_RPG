"""Explicit, bounded maintenance of unreferenced draft history (never version payloads)."""

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.infrastructure.postgres.story_models import StoryDraftRevisionModel

# Request/replay records intentionally are not pruned: they contain their own response
# snapshots. A repeated save/restore/apply must work even after its history row expires.
_ELIGIBLE = """
    r.created_at < :cutoff
    AND EXISTS (
        SELECT 1 FROM story_drafts d
        WHERE d.story_id=r.story_id AND d.revision>r.revision
    )
    AND NOT EXISTS (
        SELECT 1 FROM story_versions v
        WHERE v.story_id=r.story_id AND v.source_draft_revision=r.revision
    )
    AND NOT EXISTS (
        SELECT 1 FROM story_validation_reports v
        WHERE v.story_id=r.story_id AND v.draft_revision=r.revision
    )
    AND NOT EXISTS (
        SELECT 1 FROM authoring_jobs j
        WHERE j.story_id=r.story_id AND j.base_revision=r.revision
    )
    AND NOT EXISTS (
        SELECT 1 FROM authoring_proposals p
        WHERE p.story_id=r.story_id
          AND (p.base_revision=r.revision OR p.applied_revision=r.revision)
    )
"""


async def prune_draft_history(
    sessions: async_sessionmaker[AsyncSession],
    *,
    retention_days: int = 30,
    batch_size: int = 100,
    apply: bool = False,
) -> dict[str, object]:
    """Preview one batch by default; explicit apply deletes only its history rows.

    Take Story locks first, matching save/restore/validation/publication/job adoption.
    Never acquire request, quota or job locks, so their existing order is unchanged.
    Busy stories are skipped. Re-evaluate eligibility after acquiring the locks; a
    preview is not a promise that a later invocation will select the same revisions.
    """
    if type(retention_days) is not int or not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if type(apply) is not bool:
        raise ValueError("apply must be a boolean")

    async with sessions.begin() as session:
        now = await session.scalar(select(func.current_timestamp()))
        assert isinstance(now, datetime)
        cutoff = now - timedelta(days=retention_days)
        parameters = {"cutoff": cutoff, "batch_size": batch_size}
        story_ids = list(
            await session.scalars(
                text(f"""
            SELECT s.id FROM stories s
            WHERE EXISTS (
                SELECT 1 FROM story_draft_revisions r
                WHERE r.story_id=s.id AND {_ELIGIBLE}
            )
            ORDER BY s.id
            LIMIT :batch_size
            FOR UPDATE OF s SKIP LOCKED
        """),
                parameters,
            )
        )
        candidates = []
        deleted_count = 0
        if story_ids:
            candidates = list(
                (
                    await session.execute(
                        text(f"""
                SELECT r.story_id, r.revision FROM story_draft_revisions r
                WHERE r.story_id=ANY(CAST(:story_ids AS uuid[])) AND {_ELIGIBLE}
                ORDER BY r.created_at, r.story_id, r.revision
                LIMIT :batch_size
            """),
                        {**parameters, "story_ids": story_ids},
                    )
                ).all()
            )
            if apply and candidates:
                deleted = await session.execute(
                    delete(StoryDraftRevisionModel)
                    .where(
                        tuple_(
                            StoryDraftRevisionModel.story_id,
                            StoryDraftRevisionModel.revision,
                        ).in_([(row.story_id, row.revision) for row in candidates])
                    )
                    .returning(StoryDraftRevisionModel.revision)
                )
                deleted_count = len(deleted.all())
    return {
        "dry_run": not apply,
        "retention_days": retention_days,
        "batch_size": batch_size,
        "cutoff": cutoff.isoformat(),
        "eligible_count": len(candidates),
        "deleted_count": deleted_count,
        "revisions": [
            {"story_id": str(row.story_id), "revision": row.revision} for row in candidates
        ],
    }
