"""Retention is opt-in, bounded, reference-safe and compatible with request replay."""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, insert, select, text
from test_migrations import ROOT, _guard_empty_database, _postgres_sessions, _run_alembic

from ai_rpg.application.ports.story_authoring import AuthoringUsage
from ai_rpg.application.stories import StoryError, compile_draft, content_hash
from ai_rpg.application.story_jobs import prepare_proposal
from ai_rpg.contracts.stories import (
    AuthoringDraft,
    CreateStoryRequest,
    RestoreStoryRequest,
    SaveStoryDraftRequest,
)
from ai_rpg.contracts.story_jobs import (
    ApplyStoryProposalRequest,
    CreateAuthoringJobRequest,
    GeneratedStoryChange,
    GeneratedStoryOutput,
)
from ai_rpg.infrastructure.postgres import stories as story_storage
from ai_rpg.infrastructure.postgres.job_models import AuthoringJobModel, AuthoringProposalModel
from ai_rpg.infrastructure.postgres.stories import PostgresStoryStore
from ai_rpg.infrastructure.postgres.story_jobs import PostgresStoryJobStore
from ai_rpg.infrastructure.postgres.story_models import (
    StoryDraftRevisionModel,
    StoryModel,
    StoryValidationReportModel,
    StoryVersionModel,
)
from ai_rpg.scenarios import BUILTIN_SCENARIOS

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
OWNER = UUID(int=301)
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="Test DB required")]


@pytest.fixture
def database() -> Iterator[Engine]:
    assert URL
    for url in _guard_empty_database(URL):
        _run_alembic(url, "upgrade", "head")
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def run(coro):
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coro)


async def history(sessions, monkeypatch, count=4, *, recent_revision=None):
    store = PostgresStoryStore(sessions)
    created = datetime.now(UTC) - timedelta(days=60)
    request = CreateStoryRequest(request_id=uuid4(), draft=AuthoringDraft(notes="revision 1"))
    saved, requests = [], []
    with monkeypatch.context() as clock:
        clock.setattr(story_storage, "datetime", SimpleNamespace(now=lambda tz: created))
        first = await store.create(OWNER, request, request.draft)
        saved.append(first)
        for revision in range(2, count + 1):
            created = datetime.now(UTC) - timedelta(days=1 if revision == recent_revision else 60)
            save = SaveStoryDraftRequest(
                request_id=uuid4(),
                expected_revision=revision - 1,
                draft=AuthoringDraft(notes=f"revision {revision}"),
            )
            saved.append(await store.save(OWNER, first.story_id, save))
            requests.append(save)
    return store, request, saved, requests


def rows(database, table):
    # Table names are test literals; compare every persisted field, not just row counts.
    with database.connect() as connection:
        return connection.scalar(
            text(f"SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM {table} t")
        )


def revisions(database, story_id):
    with database.connect() as connection:
        return list(
            connection.scalars(
                select(StoryDraftRevisionModel.revision)
                .where(
                    StoryDraftRevisionModel.story_id == story_id,
                )
                .order_by(StoryDraftRevisionModel.revision)
            )
        )


def command(*args):
    # Always the disposable database already checked by _guard_empty_database.
    result = subprocess.run(
        [sys.executable, "-m", "ai_rpg.cli", "prune-draft-history", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={
            **os.environ,
            "AIRPG_DATABASE_URL": URL,
            "AIRPG_DRAFT_HISTORY_RETENTION_DAYS": "30",
            "AIRPG_DRAFT_HISTORY_PRUNE_BATCH_SIZE": "2",
        },
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cli_dry_run_is_read_only_and_apply_is_bounded_and_idempotent(database, monkeypatch):
    async def seed():
        async with _postgres_sessions(URL) as sessions:
            return (await history(sessions, monkeypatch))[2][0].story_id

    story_id = run(seed())
    before = rows(database, "story_draft_revisions")
    requests = rows(database, "story_requests")
    preview = command()
    assert preview["dry_run"] is True
    assert preview["retention_days"] == 30 and preview["batch_size"] == 2
    assert preview["eligible_count"] == 2 and preview["deleted_count"] == 0
    assert preview["revisions"] == [
        {"story_id": str(story_id), "revision": 1},
        {"story_id": str(story_id), "revision": 2},
    ]
    assert rows(database, "story_draft_revisions") == before
    extended = command("--retention-days", "3650", "--batch-size", "1")
    assert extended["retention_days"] == 3650 and extended["batch_size"] == 1
    assert extended["eligible_count"] == 0
    assert rows(database, "story_draft_revisions") == before
    assert command("--apply")["deleted_count"] == 2
    assert revisions(database, story_id) == [3, 4]
    assert command("--apply")["deleted_count"] == 1
    assert command("--apply")["deleted_count"] == 0
    assert revisions(database, story_id) == [4]
    assert rows(database, "story_requests") == requests


async def version(session, story_id, revision, *, kind="release"):
    definition = BUILTIN_SCENARIOS.get("mist_lighthouse", 1)
    payload = definition.model_dump(mode="json")
    await session.execute(
        insert(StoryVersionModel).values(
            id=uuid4(),
            story_id=story_id,
            source_draft_revision=revision,
            kind=kind,
            release_number=1 if kind == "release" else None,
            schema_version=definition.schema_version,
            ruleset_ref=definition.ruleset_ref,
            ruleset_version=definition.ruleset_ref,
            capability_requirements=list(definition.required_capabilities),
            payload=payload,
            public_metadata={"title": "Immutable public title"},
            content_hash=content_hash(payload),
        )
    )


async def job(session, saved, *, state="queued"):
    job_id = uuid4()
    snapshot = saved.draft.model_dump(mode="json")
    await session.execute(
        insert(AuthoringJobModel).values(
            id=job_id,
            story_id=saved.story_id,
            owner_principal_id=OWNER,
            request_id=uuid4(),
            input_hash=content_hash(snapshot),
            base_revision=saved.revision,
            snapshot=snapshot,
            snapshot_hash=content_hash(snapshot),
            kind="check",
            instructions="Retention fixture",
            model_id="test",
            limits={},
            state=state,
        )
    )
    return job_id


def test_all_references_recent_history_and_current_revision_survive(database, monkeypatch):
    async def scenario():
        from ai_rpg.infrastructure.postgres.maintenance import prune_draft_history

        async with _postgres_sessions(URL) as sessions:
            _, _, saved, _ = await history(sessions, monkeypatch, 10, recent_revision=9)
            story_id = saved[0].story_id
            async with sessions.begin() as session:
                await version(session, story_id, 1)
                await version(session, story_id, 2, kind="playtest")
                _, report = compile_draft(saved[2].draft, story_id, 3)
                await session.execute(
                    insert(StoryValidationReportModel).values(
                        id=report.id,
                        story_id=story_id,
                        draft_revision=3,
                        content_hash=report.content_hash,
                        draft_hash=report.draft_hash,
                        validator_version=report.validator_version,
                        payload=report.model_dump(mode="json"),
                    )
                )
                await job(session, saved[3], state="cancelled")
                for base, applied in ((5, None), (6, 7)):
                    job_id = await job(session, saved[base - 1], state="succeeded")
                    proposal = prepare_proposal(
                        job_id, story_id, base, saved[base - 1].draft, GeneratedStoryOutput()
                    )
                    await session.execute(
                        insert(AuthoringProposalModel).values(
                            id=proposal.id,
                            job_id=job_id,
                            story_id=story_id,
                            base_revision=base,
                            payload=proposal.model_dump(mode="json"),
                            decision="pending" if applied is None else "applied",
                            applied_revision=applied,
                        )
                    )
            protected = {
                table: rows(database, table)
                for table in (
                    "stories",
                    "story_drafts",
                    "story_versions",
                    "story_validation_reports",
                    "authoring_jobs",
                    "authoring_proposals",
                    "story_requests",
                )
            }
            assert (await prune_draft_history(sessions, retention_days=3650, apply=True))[
                "deleted_count"
            ] == 0
            result = await prune_draft_history(sessions, apply=True)
            assert result["revisions"] == [{"story_id": str(story_id), "revision": 8}]
            assert result["deleted_count"] == 1
            assert revisions(database, story_id) == [1, 2, 3, 4, 5, 6, 7, 9, 10]
            for table, before in protected.items():
                assert rows(database, table) == before

    run(scenario())


def test_pruned_revisions_do_not_break_save_restore_or_ai_request_replays(database, monkeypatch):
    async def scenario():
        from ai_rpg.infrastructure.postgres.maintenance import prune_draft_history

        async with _postgres_sessions(URL) as sessions:
            store, create, saved, saves = await history(sessions, monkeypatch, 2)
            story_id = saved[0].story_id
            restore = RestoreStoryRequest(request_id=uuid4(), expected_revision=2, revision=1)
            restored = await store.restore(OWNER, story_id, restore)
            jobs = PostgresStoryJobStore(sessions, model_id="test")
            enqueue = CreateAuthoringJobRequest(request_id=uuid4(), base_revision=3, kind="fill")
            queued = await jobs.enqueue(OWNER, story_id, enqueue)
            lease = await jobs.claim()
            assert lease is not None and await jobs.reserve_call(lease)
            proposal = prepare_proposal(
                queued.id,
                story_id,
                3,
                restored.draft,
                GeneratedStoryOutput(
                    changes=[
                        GeneratedStoryChange(
                            field_path="/metadata/title",
                            value_json='"AI title"',
                            reason="Author-approved title",
                        )
                    ],
                ),
            )
            assert await jobs.succeed(lease, proposal, None, AuthoringUsage(), "test")
            apply = ApplyStoryProposalRequest(
                request_id=uuid4(), expected_revision=3, change_ids=[proposal.changes[0].id]
            )
            applied = await jobs.apply(OWNER, story_id, proposal.id, apply)
            newest = await store.save(
                OWNER,
                story_id,
                SaveStoryDraftRequest(
                    request_id=uuid4(),
                    expected_revision=4,
                    draft=applied.draft,
                ),
            )
            job_before = await jobs.get(OWNER, queued.id)
            ledger = rows(database, "story_requests")
            assert (await prune_draft_history(sessions, apply=True))["deleted_count"] == 2
            assert revisions(database, story_id) == [3, 4, 5]
            assert await store.create(OWNER, create, create.draft) == saved[0]
            assert await store.save(OWNER, story_id, saves[0]) == saved[1]
            assert await store.restore(OWNER, story_id, restore) == restored
            assert await jobs.enqueue(OWNER, story_id, enqueue) == job_before
            assert await jobs.apply(OWNER, story_id, proposal.id, apply) == applied
            with pytest.raises(StoryError, match="idempotency_conflict"):
                await store.save(
                    OWNER,
                    story_id,
                    saves[0].model_copy(
                        update={
                            "draft": AuthoringDraft(notes="Different replay content"),
                        }
                    ),
                )
            with pytest.raises(StoryError, match="revision_not_found"):
                await store.restore(
                    OWNER,
                    story_id,
                    RestoreStoryRequest(
                        request_id=uuid4(),
                        expected_revision=5,
                        revision=1,
                    ),
                )
            assert await store.get_draft(OWNER, story_id) == newest
            assert rows(database, "story_requests") == ledger

    run(scenario())


@pytest.mark.parametrize("reference", ["release", "job"])
def test_prune_skips_story_writer_and_rechecks_new_references(database, monkeypatch, reference):
    async def scenario():
        from ai_rpg.infrastructure.postgres.maintenance import prune_draft_history

        async with _postgres_sessions(URL) as sessions:
            _, _, saved, _ = await history(sessions, monkeypatch, 3)
            story_id = saved[0].story_id
            _, _, other, _ = await history(sessions, monkeypatch, 2)
            async with sessions.begin() as writer:
                await writer.execute(
                    select(StoryModel.id)
                    .where(
                        StoryModel.id == story_id,
                    )
                    .with_for_update()
                )
                result = await asyncio.wait_for(
                    prune_draft_history(sessions, apply=True), timeout=5
                )
                assert result["revisions"] == [{"story_id": str(other[0].story_id), "revision": 1}]
                assert revisions(database, story_id) == [1, 2, 3]
                if reference == "release":
                    await version(writer, story_id, 1)
                else:
                    await job(writer, saved[0])
            result = await prune_draft_history(sessions, apply=True)
            assert result["revisions"] == [{"story_id": str(story_id), "revision": 2}]
            assert revisions(database, story_id) == [1, 3]

    run(scenario())


def test_concurrent_pruners_delete_each_eligible_revision_once(database, monkeypatch):
    async def scenario():
        from ai_rpg.infrastructure.postgres.maintenance import prune_draft_history

        async with _postgres_sessions(URL) as sessions:
            store, _, saved, _ = await history(sessions, monkeypatch, 6)
            results = await asyncio.gather(
                *(prune_draft_history(sessions, batch_size=5, apply=True) for _ in range(2))
            )
            assert sum(result["deleted_count"] for result in results) == 5
            assert revisions(database, saved[0].story_id) == [6]
            story_id = saved[0].story_id
            save = SaveStoryDraftRequest(
                request_id=uuid4(),
                expected_revision=6,
                draft=AuthoringDraft(notes="New edit"),
            )
            updated, _ = await asyncio.wait_for(
                asyncio.gather(
                    store.save(OWNER, story_id, save),
                    prune_draft_history(sessions, apply=True),
                ),
                timeout=5,
            )
            assert updated.revision == 7 and updated.draft.notes == "New edit"
            await prune_draft_history(sessions, apply=True)
            assert revisions(database, story_id) == [7]
            assert await store.get_draft(OWNER, story_id) == updated
            assert await store.save(OWNER, story_id, save) == updated

    run(scenario())
