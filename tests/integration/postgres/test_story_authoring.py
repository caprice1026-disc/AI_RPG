"""Story persistence and publication invariants against a dedicated PostgreSQL database."""

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import IntegrityError
from test_migrations import _guard_empty_database, _postgres_sessions, _run_alembic

import ai_rpg.application.stories as story_module
from ai_rpg.api.stories import create_stories_router
from ai_rpg.application.adventures import AdventureService
from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.stories import StoryError, StoryService
from ai_rpg.contracts.adventures import CreateAdventureRequest, CreateAdventureResponse
from ai_rpg.contracts.stories import (
    AuthoringDraft,
    CreateStoryPlaytestRequest,
    CreateStoryRequest,
    DuplicateStoryRequest,
    PublishStoryRequest,
    RestoreStoryRequest,
    SaveStoryDraftRequest,
    StoryMetadata,
    StorySettingsRequest,
    ValidateStoryRequest,
)
from ai_rpg.infrastructure.postgres.adventures import PostgresAdventureStore
from ai_rpg.infrastructure.postgres.scenario_source import PostgresScenarioSource
from ai_rpg.infrastructure.postgres.stories import (
    PostgresStoryStore,
    builtin_version_id,
    import_builtin_stories,
    record_story_playtest,
)
from ai_rpg.infrastructure.postgres.story_models import StoryDraftModel, StoryVersionModel

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="Test DB required")]
OWNER = AuthenticatedPrincipal(
    principal_id=uuid4(),
    issuer="test",
    subject="author",
    authenticated_at=datetime.now(UTC),
    auth_context=frozenset(),
)
OTHER = AuthenticatedPrincipal(
    principal_id=uuid4(),
    issuer="test",
    subject="other",
    authenticated_at=datetime.now(UTC),
    auth_context=frozenset(),
)


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


def test_cas_parallel_retry_restore_duplicate_and_owner_denial(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            service = StoryService(PostgresStoryStore(sessions))
            create = CreateStoryRequest(request_id=uuid4())
            first, same = await asyncio.gather(
                service.create(OWNER, create), service.create(OWNER, create)
            )
            assert first == same
            assert first.draft.scenario["scenario_ref"] == "story_" + first.story_id.hex
            requests = [
                SaveStoryDraftRequest(
                    request_id=uuid4(), expected_revision=1, draft=AuthoringDraft(notes=str(index))
                )
                for index in range(2)
            ]
            results = await asyncio.gather(
                *(service.save(OWNER, first.story_id, request) for request in requests),
                return_exceptions=True,
            )
            assert sum(isinstance(result, StoryError) for result in results) == 1
            winner = next(
                index for index, result in enumerate(results) if not isinstance(result, Exception)
            )
            saved = await service.save(OWNER, first.story_id, requests[winner])
            assert saved == results[winner]
            assert saved.revision == 2
            with pytest.raises(StoryError, match="idempotency_conflict"):
                await service.save(
                    OWNER,
                    first.story_id,
                    requests[winner].model_copy(
                        update={
                            "draft": AuthoringDraft(notes="changed retry"),
                        }
                    ),
                )
            for operation in (
                service.get_draft(OTHER, first.story_id),
                service.revisions(OTHER, first.story_id),
                service.save(OTHER, first.story_id, requests[0]),
            ):
                with pytest.raises(StoryError, match="story_not_found"):
                    await operation
            restored = await service.restore(
                OWNER,
                first.story_id,
                RestoreStoryRequest(
                    request_id=uuid4(),
                    expected_revision=2,
                    revision=1,
                ),
            )
            assert restored.revision == 3 and restored.draft.notes == ""
            assert len((await service.revisions(OWNER, first.story_id)).revisions) == 3
            clone = await service.duplicate(
                OWNER, first.story_id, DuplicateStoryRequest(request_id=uuid4())
            )
            assert clone.story_id != first.story_id
            assert clone.draft.scenario["scenario_ref"] == "story_" + clone.story_id.hex
            assert clone.revision == 1
            with pytest.raises(StoryError, match="scenario_ref_is_immutable"):
                await service.save(
                    OWNER,
                    first.story_id,
                    SaveStoryDraftRequest(
                        request_id=uuid4(),
                        expected_revision=3,
                        draft=AuthoringDraft(scenario={"scenario_ref": "ruined_chapel"}),
                    ),
                )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_template_version_and_copy_survive_registry_changes_and_create_replay(
    database: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = replace(story_module.TEMPLATE_REGISTRY["mist_lighthouse"], version=7)
    monkeypatch.setitem(story_module.TEMPLATE_REGISTRY, "third_template", entry)

    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            service = StoryService(PostgresStoryStore(sessions))

            async def principal() -> AuthenticatedPrincipal:
                return OWNER

            app = FastAPI()
            app.include_router(create_stories_router(principal_provider=principal, service=service))
            request = {"request_id": str(uuid4()), "template_id": "third_template"}
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as client:
                response = await client.post("/stories", json=request)
                assert response.status_code == 201
                first = response.json()
                story_id = UUID(first["story_id"])
                async with sessions() as session:
                    stored = await session.get(StoryDraftModel, story_id)
                    assert stored is not None
                    assert (stored.template_id, stored.template_version) == ("third_template", 7)
                    assert stored.payload == first["draft"]

                monkeypatch.setitem(story_module.TEMPLATE_REGISTRY, "third_template", replace(
                    entry, version=8, source_ref="ruined_chapel", source_version=3,
                ))
                assert (await client.get(f"/stories/{story_id}/draft")).json() == first
                replay = await client.post("/stories", json=request)
                assert replay.status_code == 201 and replay.json() == first
                conflict = await client.post("/stories", json={
                    **request, "template_id": "ruined_chapel",
                })
                assert conflict.status_code == 409

                next_response = await client.post("/stories", json={
                    **request, "request_id": str(uuid4()),
                })
                assert next_response.status_code == 201
                next_story = next_response.json()
                assert next_story["draft"]["scenario"]["title"] != first["draft"]["scenario"]["title"]
                clone = await service.duplicate(
                    OWNER, story_id, DuplicateStoryRequest(request_id=uuid4()),
                )
                assert clone.draft.scenario["title"] == first["draft"]["scenario"]["title"]
                blank = await service.create(OWNER, CreateStoryRequest(request_id=uuid4()))
                async with sessions() as session:
                    stored = await session.get(StoryDraftModel, story_id)
                    assert stored is not None
                    assert stored.template_version == 7 and stored.payload == first["draft"]
                    newer = await session.get(StoryDraftModel, UUID(next_story["story_id"]))
                    assert newer is not None and newer.template_version == 8
                    copied = await session.get(StoryDraftModel, clone.story_id)
                    assert copied is not None
                    assert (copied.template_id, copied.template_version) == ("third_template", 7)
                    empty = await session.get(StoryDraftModel, blank.story_id)
                    assert empty is not None
                    assert (empty.template_id, empty.template_version) == (None, None)
                    assert await session.scalar(text(
                        "SELECT count(*) FROM story_requests WHERE operation='create'"
                    )) == 3

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_playtest_validation_shares_cached_report_and_persistent_quota(database, monkeypatch):
    from ai_rpg.config import Settings
    from ai_rpg.infrastructure.postgres import community

    monkeypatch.setattr(community, "get_settings", lambda: Settings(
        _env_file=None, daily_story_validation_limit=1,
    ))

    async def run():
        async with _postgres_sessions(URL) as sessions:
            service = StoryService(PostgresStoryStore(sessions))
            draft = await service.create(
                OWNER, CreateStoryRequest(request_id=uuid4(), template_id="mist_lighthouse"),
            )
            first = await service.validate(OWNER, draft.story_id, ValidateStoryRequest(
                expected_revision=1,
            ))
            await service.create_playtest_version(OWNER, draft.story_id, CreateStoryPlaytestRequest(
                request_id=uuid4(), expected_revision=1,
            ))
            assert await service.validate(OWNER, draft.story_id, ValidateStoryRequest(
                expected_revision=1,
            )) == first
            await service.save(OWNER, draft.story_id, SaveStoryDraftRequest(
                request_id=uuid4(), expected_revision=1,
                draft=draft.draft.model_copy(update={"notes": "A new draft revision"}),
            ))
            with pytest.raises(StoryError, match="usage_limit_exceeded"):
                await service.create_playtest_version(
                    OWNER, draft.story_id,
                    CreateStoryPlaytestRequest(request_id=uuid4(), expected_revision=2),
                )
            async with sessions() as session:
                assert await session.scalar(text(
                    "SELECT count(*) FROM usage_reservations WHERE purpose='story_validation'"
                )) == 1
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_immutable_version_and_current_release_same_story_and_kind(database: Engine) -> None:
    async def prepare() -> tuple[UUID, UUID, UUID]:
        async with _postgres_sessions(URL) as sessions:
            await import_builtin_stories(sessions)
            service = StoryService(PostgresStoryStore(sessions))
            first = await service.create(
                OWNER, CreateStoryRequest(request_id=uuid4(), template_id="mist_lighthouse")
            )
            other = await service.create(OWNER, CreateStoryRequest(request_id=uuid4()))
            playtest = await service.create_playtest_version(
                OWNER,
                first.story_id,
                CreateStoryPlaytestRequest(request_id=uuid4(), expected_revision=1),
            )
            return first.story_id, other.story_id, playtest.story_version_id

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        first, other, version = runner.run(prepare())
    for sql, args in (
        ("UPDATE story_versions SET payload='{}' WHERE id=:version", {"version": version}),
        ("DELETE FROM story_versions WHERE id=:version", {"version": version}),
        (
            "UPDATE stories SET current_release_id=:version WHERE id=:story",
            {"version": version, "story": first},
        ),
        (
            "UPDATE stories SET current_release_id=:version WHERE id=:story",
            {"version": version, "story": other},
        ),
        (
            "UPDATE stories SET current_release_id=:version WHERE id=:story",
            {"version": builtin_version_id("mist_lighthouse", 1), "story": other},
        ),
    ):
        with pytest.raises(IntegrityError), database.begin() as connection:
            connection.execute(text(sql), args)


def test_publication_requires_same_content_author_completion_and_acknowledgement(
    database: Engine,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            service = StoryService(PostgresStoryStore(sessions))
            created = await service.create(
                OWNER, CreateStoryRequest(request_id=uuid4(), template_id="mist_lighthouse")
            )
            story_id = created.story_id
            report = await service.validate(
                OWNER, story_id, ValidateStoryRequest(expected_revision=1)
            )
            playtest_request = CreateStoryPlaytestRequest(request_id=uuid4(), expected_revision=1)
            version = await service.create_playtest_version(OWNER, story_id, playtest_request)
            assert (
                await service.create_playtest_version(OWNER, story_id, playtest_request) == version
            )
            publish = PublishStoryRequest(
                request_id=uuid4(),
                expected_revision=1,
                validation_report_id=report.id,
                acknowledged_warning_codes=[warning.code for warning in report.warnings],
                author_playtest_acknowledged=True,
            )
            with pytest.raises(StoryError, match="author_playthrough_required"):
                await service.publish(OWNER, story_id, publish)
            campaign_id = uuid4()
            async with sessions() as session, session.begin():
                await session.execute(
                    text("""
                    INSERT INTO campaigns(id,ruleset_version) VALUES(:campaign,'mvp_v2');
                """),
                    {"campaign": campaign_id},
                )
                await session.execute(
                    text("""
                    INSERT INTO campaign_members(campaign_id,principal_id,role)
                    VALUES(:campaign,:owner,'player')
                """),
                    {"campaign": campaign_id, "owner": OWNER.principal_id},
                )
                await session.execute(
                    text("""
                    INSERT INTO mvp_scenario_runs(campaign_id,scenario_ref,scenario_version,
                        status,ending_ref,story_version_id)
                    VALUES(:campaign,:ref,1,'completed','retreated',:version)
                """),
                    {
                        "campaign": campaign_id,
                        "ref": created.draft.scenario["scenario_ref"],
                        "version": version.story_version_id,
                    },
                )
                await record_story_playtest(
                    session,
                    version.story_version_id,
                    campaign_id,
                    OWNER.principal_id,
                    debug_modified=True,
                )
            with pytest.raises(StoryError, match="author_playthrough_required"):
                await service.publish(OWNER, story_id, publish)
            # Fixtures model a second genuinely completed run; API cannot forge this evidence.
            with database.begin() as connection:
                connection.execute(text("UPDATE story_playtest_records SET debug_modified=false"))
            with pytest.raises(StoryError, match="author_acknowledgement_required"):
                await service.publish(
                    OWNER,
                    story_id,
                    publish.model_copy(update={"author_playtest_acknowledged": False}),
                )
            with pytest.raises(StoryError, match="warnings_not_acknowledged"):
                await service.publish(
                    OWNER, story_id, publish.model_copy(update={"acknowledged_warning_codes": []})
                )
            published = await service.publish(OWNER, story_id, publish)
            assert await service.publish(OWNER, story_id, publish) == published
            assert published.release_number == 1
            # Concurrent retries return the same immutable release, without consuming a number.
            retries = await asyncio.gather(
                *(service.publish(OWNER, story_id, publish) for _ in range(3))
            )
            assert all(retry == published for retry in retries)
            public = await service.detail(OTHER, story_id)
            assert "scenario" not in public.model_dump() and "payload" not in public.model_dump()
            changed = created.draft.model_copy(
                update={"metadata": StoryMetadata(title="New title")}
            )
            saved = await service.save(
                OWNER,
                story_id,
                SaveStoryDraftRequest(request_id=uuid4(), expected_revision=1, draft=changed),
            )
            with pytest.raises(StoryError, match="validation_outdated"):
                await service.publish(
                    OWNER,
                    story_id,
                    publish.model_copy(update={"request_id": uuid4(), "expected_revision": 2}),
                )
            report2 = await service.validate(
                OWNER, story_id, ValidateStoryRequest(expected_revision=2)
            )
            assert report2.content_hash == report.content_hash
            published2 = await service.publish(
                OWNER,
                story_id,
                publish.model_copy(
                    update={
                        "request_id": uuid4(),
                        "expected_revision": 2,
                        "validation_report_id": report2.id,
                    }
                ),
            )
            assert published2.release_number == 2
            async with sessions() as session:
                pinned = await session.scalar(
                    select(StoryVersionModel).where(
                        StoryVersionModel.id == published.story_version_id
                    )
                )
                assert pinned is not None and pinned.public_metadata["title"] != "New title"
            scenario = saved.draft.model_dump(mode="json")
            scenario["scenario"]["objective"] = "A changed playable objective"
            await service.save(
                OWNER,
                story_id,
                SaveStoryDraftRequest(
                    request_id=uuid4(),
                    expected_revision=2,
                    draft=AuthoringDraft.model_validate(scenario),
                ),
            )
            report3 = await service.validate(
                OWNER, story_id, ValidateStoryRequest(expected_revision=3)
            )
            with pytest.raises(StoryError, match="author_playthrough_required"):
                await service.publish(
                    OWNER,
                    story_id,
                    publish.model_copy(
                        update={
                            "request_id": uuid4(),
                            "expected_revision": 3,
                            "validation_report_id": report3.id,
                        }
                    ),
                )
            await service.settings(
                OWNER,
                story_id,
                StorySettingsRequest(
                    request_id=uuid4(), visibility="unlisted", lifecycle="withdrawn"
                ),
            )
            with pytest.raises(StoryError, match="story_not_found"):
                await service.detail(OTHER, story_id)

            # A lock held by a concurrent edit prevents an old compiled candidate publishing.
            await service.settings(
                OWNER,
                story_id,
                StorySettingsRequest(request_id=uuid4(), visibility="private", lifecycle="active"),
            )
            with pytest.raises(StoryError, match="story_not_found"):
                await service.detail(OTHER, story_id)
            assert (
                await service.detail(OWNER, story_id)
            ).story_version_id == published2.story_version_id
            async with sessions() as session, session.begin():
                await session.execute(
                    text("SELECT id FROM stories WHERE id=:id FOR UPDATE"), {"id": story_id}
                )
                waiting = asyncio.create_task(
                    service.publish(
                        OWNER,
                        story_id,
                        publish.model_copy(
                            update={
                                "request_id": uuid4(),
                                "expected_revision": 3,
                                "validation_report_id": report3.id,
                            }
                        ),
                    )
                )
                # Retire the story while the publishing transaction waits for this row.
                await session.execute(
                    text("UPDATE stories SET lifecycle='withdrawn' WHERE id=:id"), {"id": story_id}
                )
            with pytest.raises(StoryError, match="story_unavailable"):
                await waiting

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_builtin_import_replay_backfills_without_regenerating_run_ids(database: Engine) -> None:
    campaign_id = uuid4()
    with database.begin() as connection:
        connection.execute(
            text("INSERT INTO campaigns(id,ruleset_version) VALUES(:id,'mvp_v1')"),
            {"id": campaign_id},
        )
        connection.execute(
            text("""
            INSERT INTO mvp_scenario_runs(campaign_id,scenario_ref,scenario_version,status)
            VALUES(:id,'ruined_chapel',1,'active')
        """),
            {"id": campaign_id},
        )

    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            first = await import_builtin_stories(sessions)
            second = await import_builtin_stories(sessions)
            assert first == {"versions_imported": 4, "runs_backfilled": 1, "unresolved_runs": 0}
            assert second == {"versions_imported": 0, "runs_backfilled": 0, "unresolved_runs": 0}

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
    with database.connect() as connection:
        run = connection.execute(
            text("SELECT campaign_id,status,story_version_id FROM mvp_scenario_runs")
        ).one()
        assert run.campaign_id == campaign_id and run.status == "active"
        assert run.story_version_id == builtin_version_id("ruined_chapel", 1)
        assert connection.scalar(text("SELECT count(*) FROM story_versions")) == 4


def test_http_authorization_and_revision_cas_on_real_storage(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:

            async def principal(
                x_other: str | None = Header(default=None),
            ) -> AuthenticatedPrincipal:
                return OTHER if x_other else OWNER

            app = FastAPI()
            app.include_router(
                create_stories_router(principal_provider=principal, sessions=sessions)
            )
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                request = {"request_id": str(uuid4()), "draft": {"scenario": {"endings": []}}}
                response = await client.post("/stories", json=request)
                assert response.status_code == 201
                created = response.json()
                assert (await client.post("/stories", json=request)).json() == created
                path = f"/stories/{created['story_id']}"
                for suffix in ("/draft", "/revisions"):
                    assert (
                        await client.get(path + suffix, headers={"x-other": "yes"})
                    ).status_code == 404
                body = {
                    "request_id": str(uuid4()),
                    "expected_revision": 1,
                    "draft": {"notes": "unfinished"},
                }
                saved = await client.put(path + "/draft", json=body)
                assert saved.status_code == 200 and saved.json()["revision"] == 2
                assert (await client.put(path + "/draft", json=body)).json() == saved.json()
                conflict = await client.put(
                    path + "/draft", json={**body, "request_id": str(uuid4())}
                )
                assert conflict.status_code == 409
                assert (
                    await client.post(path + "/validate", json={"expected_revision": 2})
                ).json()["errors"]
                forbidden = await client.put(path + "/draft", json=body, headers={"x-other": "yes"})
                assert forbidden.status_code == 404

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_router_playtest_hook_pins_real_adventure_and_records_owner_atomically(
    database: Engine,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            stories = StoryService(PostgresStoryStore(sessions))
            adventures = AdventureService(
                PostgresAdventureStore(sessions), PostgresScenarioSource(sessions)
            )

            async def principal() -> AuthenticatedPrincipal:
                return OWNER

            async def start(
                author: AuthenticatedPrincipal, version: UUID, request: CreateStoryPlaytestRequest
            ) -> CreateAdventureResponse:
                return await adventures.create(
                    author,
                    CreateAdventureRequest(
                        request_id=request.request_id,
                        story_version_id=version,
                        preset_ref=request.preset_ref,
                        player_name=request.player_name,
                        ability_points=request.ability_points,
                        specialty_skill=request.specialty_skill,
                    ),
                )

            created = await stories.create(
                OWNER, CreateStoryRequest(request_id=uuid4(), template_id="mist_lighthouse")
            )
            app = FastAPI()
            app.include_router(
                create_stories_router(
                    principal_provider=principal, service=stories, playtest_creator=start
                )
            )
            request = {
                "request_id": str(uuid4()),
                "expected_revision": 1,
                "ability_points": {"strength": 1, "agility": 0, "insight": 1, "presence": 0},
                "specialty_skill": "perception",
            }
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                path = f"/stories/{created.story_id}/playtests"
                response = await client.post(path, json=request)
                assert response.status_code == 201, response.text
                ids = response.json()
                assert (await client.post(path, json=request)).json() == ids
                no_allocation = await client.post(
                    path, json={"request_id": str(uuid4()), "expected_revision": 1}
                )
                assert no_allocation.status_code == 422
            with database.connect() as connection:
                record = connection.execute(
                    text("""
                    SELECT r.story_version_id,p.actor_id,p.debug_modified,p.author_acknowledged
                    FROM mvp_scenario_runs r JOIN story_playtest_records p USING(campaign_id)
                    WHERE r.campaign_id=:campaign
                """),
                    {"campaign": ids["campaign_id"]},
                ).one()
                assert str(record.story_version_id) == ids["story_version_id"]
                assert record.actor_id == OWNER.principal_id
                assert not record.debug_modified and not record.author_acknowledged
            with pytest.raises(StoryError, match="STORY_VERSION_NOT_FOUND"):
                await adventures.create(
                    OTHER,
                    CreateAdventureRequest(
                        request_id=uuid4(),
                        story_version_id=UUID(ids["story_version_id"]),
                        preset_ref="scout",
                        player_name="other",
                    ),
                )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
