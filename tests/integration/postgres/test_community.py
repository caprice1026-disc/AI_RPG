"""Public projections and serialized usage/moderation boundaries."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from test_migrations import _guard_empty_database, _postgres_sessions, _run_alembic

from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.stories import StoryError, StoryService
from ai_rpg.config import Settings
from ai_rpg.contracts.community import ModerationRequest
from ai_rpg.contracts.stories import CreateStoryRequest
from ai_rpg.infrastructure.postgres.community import CommunityStore, reserve_usage
from ai_rpg.infrastructure.postgres.community_models import UsageReservationModel
from ai_rpg.infrastructure.postgres.stories import PostgresStoryStore, import_builtin_stories

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="Test DB required")]


def test_game_reservation_rechecks_lease_after_quota_wait(database, monkeypatch):
    from test_migrations import (
        _accept_from_database,
        _acquire_lease_from_database,
        _player_turn,
        _seed_members_entities_and_scene,
    )

    from ai_rpg.infrastructure.postgres import repositories

    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)
    accepted = _accept_from_database(database, _player_turn())
    lease = _acquire_lease_from_database(database, accepted.turn_id)

    async def expire_during_quota_wait(session, *args, **kwargs):
        await reserve_usage(session, *args, **kwargs)
        await session.execute(text(
            "UPDATE turns SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
        ), {"id": accepted.turn_id})

    monkeypatch.setattr(repositories, "reserve_usage", expire_during_quota_wait)

    async def run():
        async with _postgres_sessions(URL) as factory, factory.begin() as session:
            assert not await repositories.PostgresLLMCallRepository(session).reserve(
                accepted.turn_id, phase="resolution", worker_epoch=lease.turn.worker_epoch,
            )
            assert await session.scalar(text("SELECT llm_call_count FROM turns WHERE id=:id"),
                                        {"id": accepted.turn_id}) == 0
            assert await session.scalar(text(
                "SELECT count(*) FROM usage_reservations WHERE purpose='llm_game'"
            )) == 0
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.fixture
def database():
    assert URL
    for url in _guard_empty_database(URL):
        _run_alembic(url, "upgrade", "head")
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def test_quota_parallel_requests_and_retry_are_atomic(database):
    async def run():
        owner = uuid4()
        settings = Settings(_env_file=None, daily_turn_limit=3)
        async with _postgres_sessions(URL) as factory:
            async def reserve(key):
                try:
                    async with factory.begin() as session:
                        await reserve_usage(session, owner, "turn", key, settings=settings)
                    return True
                except StoryError as error:
                    assert error.code == "usage_limit_exceeded"
                    return False

            results = await asyncio.gather(*(reserve(str(i)) for i in range(10)))
            assert sum(results) == 3
            accepted = str(results.index(True))
            assert all(await asyncio.gather(*(reserve(accepted) for _ in range(5))))
            async with factory() as session:
                assert len((await session.scalars(select(UsageReservationModel))).all()) == 3
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_public_projection_filters_private_stories_and_block_is_admin_only(database):
    async def run():
        owner, admin, stranger = uuid4(), uuid4(), uuid4()
        async with _postgres_sessions(URL) as factory:
            await import_builtin_stories(factory)
            created = await StoryService(PostgresStoryStore(factory)).create(
                AuthenticatedPrincipal(owner, "test", "subject", datetime.now(UTC), frozenset()),
                CreateStoryRequest(request_id=uuid4(), template_id="ruined_chapel"),
            )
            store = CommunityStore(factory, Settings(_env_file=None, admin_principal_ids=str(admin)))
            listing = await store.discover(limit=1)
            assert len(listing.stories) == 1
            assert listing.next_cursor
            assert created.story_id not in [story.story_id for story in listing.stories]
            assert "scenario" not in listing.model_dump_json()
            request = ModerationRequest(request_id=uuid4(), blocked=True, reason="test")
            with pytest.raises(StoryError, match="administrator_required"):
                await store.moderate(stranger, created.story_id, request)
            await store.moderate(admin, created.story_id, request)
            await store.moderate(admin, created.story_id, request)
            with pytest.raises(StoryError):
                await store.stats(stranger, created.story_id)
            assert (await store.stats(owner, created.story_id)).starts == 0
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
