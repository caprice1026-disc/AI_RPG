"""Moderation suspends work without consuming another story's execution slots."""

import asyncio
import os
from uuid import uuid4

import pytest
import test_community
from sqlalchemy import Engine, text
from test_adventures import client_for
from test_migrations import _postgres_sessions
from test_story_resolver import (
    accept_action,
    moderate_story,
    resolution_worker,
    seed_release,
    start_request,
)

from ai_rpg.application import NarrationWorker, WorkerPhasePolicy
from ai_rpg.infrastructure.postgres import PostgresUnitOfWork
from ai_rpg.infrastructure.postgres.repositories import (
    PostgresNarrationRepository,
    PostgresTurnRepository,
)
from ai_rpg.llm import ScriptedFakeLLM

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="Test DB required")]
database = test_community.database


@pytest.mark.parametrize("phase", ["resolution", "narration"])
@pytest.mark.parametrize("state", ["queued", "expired_lease", "terminal_cleanup"])
def test_blocked_turns_release_slots_and_are_not_reclaimed(
    database: Engine, phase: str, state: str,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions, client_for(sessions) as client:
            story_id, version_id, _ = await seed_release(sessions)
            _, healthy_version, _ = await seed_release(sessions)
            turn_ids = []
            for _ in range(3):
                started = await client.post(
                    "/adventures", json=start_request(version_id).model_dump(mode="json"),
                )
                assert started.status_code == 201, started.text
                turn_id = await accept_action(client, started.json())
                turn_ids.append(turn_id)
                if phase == "narration":
                    assert await resolution_worker(sessions).run_once(turn_id)
                if state != "queued":
                    async with sessions.begin() as session:
                        repository = (
                            PostgresTurnRepository(session) if phase == "resolution"
                            else PostgresNarrationRepository(session)
                        )
                        assert await repository.acquire_lease(
                            turn_id, lease_seconds=60, max_attempts=3, deadline_seconds=120,
                        ) is not None
                        lease = "lease_until" if phase == "resolution" else "narration_lease_until"
                        deadline = (
                            f", {phase}_deadline=clock_timestamp()-interval '1 second'"
                            if state == "terminal_cleanup" else ""
                        )
                        await session.execute(text(
                            f"UPDATE turns SET {lease}=clock_timestamp()-interval '1 second'"
                            f"{deadline} WHERE id=:id"
                        ), {"id": turn_id})

            # Moderation must not invert the worker's Campaign -> Story lock order.
            async with sessions.begin() as session:
                await session.execute(text(
                    "SELECT id FROM campaigns WHERE id=:id FOR UPDATE"
                ), {"id": started.json()["campaign_id"]})
                await asyncio.wait_for(moderate_story(sessions, story_id), timeout=3)
            with database.connect() as connection:
                before = connection.scalar(text(
                    "SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM turns t"
                ))
            # Both targeted retries and queue polling must leave bookkeeping/statuses alone.
            for _ in range(2):
                async with sessions.begin() as session:
                    for repository in (
                        PostgresTurnRepository(session), PostgresNarrationRepository(session),
                    ):
                        for turn_id in (None, *turn_ids):
                            assert await repository.acquire_lease(
                                turn_id, lease_seconds=60, max_attempts=3, deadline_seconds=120,
                            ) is None
            with database.connect() as connection:
                assert connection.scalar(text(
                    "SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM turns t"
                )) == before

            # Three suspended turns no longer exhaust the account's default three slots.
            healthy = await client.post(
                "/adventures", json=start_request(healthy_version).model_dump(mode="json"),
            )
            assert healthy.status_code == 201, healthy.text
            await accept_action(client, healthy.json())
            assert await resolution_worker(sessions).run_once()
            fake = ScriptedFakeLLM([{"narration": "You reach the garden.", "choices": []}])
            narrator = NarrationWorker(
                lambda: PostgresUnitOfWork(sessions), fake, WorkerPhasePolicy(60, 3, 120, "fake"),
            )
            assert await narrator.run_once()
            assert fake.request_count == 1
            assert not await resolution_worker(sessions).run_once()
            assert not await narrator.run_once()

            # Unblocking restores the existing work; expired deadlines still mean cleanup.
            await moderate_story(sessions, story_id, blocked=False)
            async with sessions.begin() as session:
                repository = (
                    PostgresTurnRepository(session) if phase == "resolution"
                    else PostgresNarrationRepository(session)
                )
                restored = await repository.acquire_lease(
                    turn_ids[0], lease_seconds=60, max_attempts=3, deadline_seconds=120,
                )
                assert restored is not None
                assert restored.terminal_cleanup == (state == "terminal_cleanup")

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize("lifecycle", ["withdrawn", "archived", "legacy"])
def test_nonblocked_turns_still_consume_concurrency_slots(
    database: Engine, lifecycle: str,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions, client_for(sessions) as client:
            story_id, version_id, _ = await seed_release(sessions)
            _, healthy_version, _ = await seed_release(sessions)
            for _ in range(3):
                started = await client.post(
                    "/adventures", json=start_request(version_id).model_dump(mode="json"),
                )
                assert started.status_code == 201, started.text
                await accept_action(client, started.json())
            async with sessions.begin() as session:
                if lifecycle == "legacy":
                    await session.execute(text(
                        "UPDATE mvp_scenario_runs SET story_version_id=NULL, "
                        "scenario_ref='ruined_chapel',scenario_version=1"
                    ))
                else:
                    await session.execute(text(
                        "UPDATE stories SET lifecycle=:lifecycle WHERE id=:id"
                    ), {"id": story_id, "lifecycle": lifecycle})
            healthy = await client.post(
                "/adventures", json=start_request(healthy_version).model_dump(mode="json"),
            )
            assert healthy.status_code == 201, healthy.text
            rejected = await client.post(
                f"/campaigns/{healthy.json()['campaign_id']}/turns",
                json={
                    "request_id": str(uuid4()),
                    "expected_state_version": 0,
                    "actor_id": healthy.json()["actor_id"],
                    "content": {"kind": "scenario_action", "action_ref": "leave"},
                },
            )
            assert rejected.status_code == 429, rejected.text
            assert rejected.json()["detail"]["code"] == "concurrent_usage_limit"

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize("phase", ["resolution", "narration"])
def test_block_after_claim_stops_without_retrying_or_terminalizing(
    database: Engine, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions, client_for(sessions) as client:
            story_id, version_id, _ = await seed_release(sessions)
            started = await client.post(
                "/adventures", json=start_request(version_id).model_dump(mode="json"),
            )
            assert started.status_code == 201, started.text
            turn_id = await accept_action(client, started.json())
            if phase == "narration":
                assert await resolution_worker(sessions).run_once(turn_id)
            original = PostgresUnitOfWork.commit
            after_claim = None

            async def block_after_claim(unit_of_work):
                nonlocal after_claim
                await original(unit_of_work)
                if after_claim is None:
                    with database.connect() as connection:
                        after_claim = connection.scalar(text(
                            "SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM turns t"
                        ))
                    await moderate_story(sessions, story_id)

            monkeypatch.setattr(PostgresUnitOfWork, "commit", block_after_claim)
            fake = ScriptedFakeLLM([])
            worker = (
                resolution_worker(sessions, fake) if phase == "resolution"
                else NarrationWorker(
                    lambda: PostgresUnitOfWork(sessions), fake, WorkerPhasePolicy(60, 3, 120, "fake"),
                )
            )
            assert not await worker.run_once(turn_id)
            assert after_claim is not None
            assert not await worker.run_once(turn_id)
            assert fake.request_count == 0
            with database.connect() as connection:
                assert connection.scalar(text(
                    "SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM turns t"
                )) == after_claim

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
