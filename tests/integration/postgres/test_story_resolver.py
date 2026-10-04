"""Pinned-reader integration, current-release authorization and transactional start races."""

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_adventures import PRINCIPAL, client_for
from test_migrations import _guard_empty_database, _postgres_sessions, _run_alembic

from ai_rpg.application import (
    NarrationWorker,
    ScenarioProgressor,
    SkillCheckResolutionWorker,
    WorkerPhasePolicy,
)
from ai_rpg.application.adventures import AdventureService
from ai_rpg.application.ports.repositories import (
    AuthorizationError,
    IdempotencyConflictError,
    NarrativeCommit,
)
from ai_rpg.application.ports.scenario_source import ResolvedScenario
from ai_rpg.application.stories import StoryError, content_hash
from ai_rpg.config import Settings
from ai_rpg.contracts import PlayerTurnInput
from ai_rpg.contracts.adventures import CreateAdventureRequest
from ai_rpg.contracts.community import ModerationRequest
from ai_rpg.engine import DiceEngine, MvpV1Ruleset, SeededRandomSource
from ai_rpg.infrastructure.postgres import PostgresUnitOfWork
from ai_rpg.infrastructure.postgres.adventures import PostgresAdventureStore
from ai_rpg.infrastructure.postgres.community import CommunityStore
from ai_rpg.infrastructure.postgres.models import (
    CampaignMemberModel,
    CampaignModel,
    MvpScenarioRunModel,
    PrincipalModel,
)
from ai_rpg.infrastructure.postgres.repositories import (
    PostgresCanonicalRepository,
    PostgresLLMCallRepository,
    PostgresNarrationRepository,
    PostgresScenarioRepository,
    PostgresTurnRepository,
)
from ai_rpg.infrastructure.postgres.scenario_source import (
    PostgresScenarioSource,
    authorize_story_run,
    decode_version,
    load_pinned_definition,
    published_title,
    resolve_start,
)
from ai_rpg.infrastructure.postgres.stories import (
    PostgresStoryStore,
    builtin_version_id,
    import_builtin_stories,
)
from ai_rpg.infrastructure.postgres.story_models import StoryModel, StoryVersionModel
from ai_rpg.llm import ScriptedFakeLLM
from ai_rpg.scenarios import BUILTIN_SCENARIOS, ScenarioDefinition

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="Test DB required")]
AUTHOR = PRINCIPAL
PLAYER = replace(PRINCIPAL, principal_id=uuid4(), subject="second-player")


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


def definition() -> ScenarioDefinition:
    return ScenarioDefinition.model_validate(
        {
            "schema_version": 2,
            "scenario_ref": "test_" + uuid4().hex,
            "version": 1,
            "title": "In-game title",
            "objective": "Leave the observatory.",
            "ruleset_ref": "mvp_v1",
            "required_capabilities": [],
            "initialization": {},
            "flags": [],
            "scenes": [
                {
                    "scene_ref": "observatory",
                    "sequence": 1,
                    "title": "Observatory",
                    "description": "A brass door opens to the garden.",
                    "actions": [
                        {
                            "kind": "scenario_action",
                            "action_ref": "leave",
                            "label": "Leave",
                            "public_fact": "You reach the garden.",
                            "success": {"ending_ref": "home"},
                        }
                    ],
                }
            ],
            "endings": [{"ending_ref": "home", "title": "Home", "summary": "You are home."}],
        }
    )


async def seed_release(
    sessions: async_sessionmaker[AsyncSession],
    *,
    visibility: str = "public",
) -> tuple[UUID, UUID, ScenarioDefinition]:
    story_id, version_id = uuid4(), uuid4()
    scenario = definition()
    payload = scenario.model_dump(mode="json")
    async with sessions.begin() as session:
        await session.execute(
            insert(PrincipalModel).values(id=AUTHOR.principal_id).on_conflict_do_nothing()
        )
        await session.execute(
            insert(StoryModel).values(
                id=story_id,
                owner_principal_id=AUTHOR.principal_id,
                visibility=visibility,
                lifecycle="active",
            )
        )
        await session.execute(
            insert(StoryVersionModel).values(
                id=version_id,
                story_id=story_id,
                kind="release",
                release_number=1,
                schema_version=2,
                ruleset_ref="mvp_v1",
                ruleset_version="mvp_v1",
                capability_requirements=[],
                payload=payload,
                public_metadata={"title": "Public v1"},
                content_hash=content_hash(payload),
            )
        )
        await session.execute(
            update(StoryModel)
            .where(StoryModel.id == story_id)
            .values(current_release_id=version_id)
        )
    return story_id, version_id, scenario


def start_request(version_id: UUID) -> CreateAdventureRequest:
    return CreateAdventureRequest(
        request_id=uuid4(), story_version_id=version_id, preset_ref="scout", player_name="Reader"
    )


def game_writes(database: Engine) -> dict[str, object]:
    """Compare durable game output and budget, excluding worker lease/retry bookkeeping."""
    tables = (
        "campaigns",
        "scenes",
        "entities",
        "mvp_characters",
        "mvp_character_abilities",
        "mvp_inventory",
        "mvp_scene_entities",
        "mvp_scenario_runs",
        "mvp_scenario_flags",
        "mvp_scenario_facts",
        "mvp_action_proposals",
        "actions",
        "events",
        "turn_choices",
        "usage_reservations",
    )
    with database.connect() as connection:
        result = {
            table: connection.scalar(
                text(f"SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM {table} t")
            )
            for table in tables
        }
        result["turn_output"] = connection.scalar(
            text(
                "SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM ("
                "SELECT id,committed_state_version,narration,narration_input,llm_call_count "
                "FROM turns) t"
            )
        )
        return result


async def moderate_story(sessions, story_id: UUID, *, blocked: bool = True) -> None:
    await CommunityStore(
        sessions,
        Settings(
            _env_file=None,
            admin_principal_ids=str(AUTHOR.principal_id),
        ),
    ).moderate(
        AUTHOR.principal_id,
        story_id,
        ModerationRequest(
            request_id=uuid4(),
            blocked=blocked,
            reason="Resolver concurrency regression",
        ),
    )


def resolution_worker(sessions, llm=None) -> SkillCheckResolutionWorker:
    return SkillCheckResolutionWorker(
        lambda: PostgresUnitOfWork(sessions),
        llm if llm is not None else ScriptedFakeLLM([]),
        MvpV1Ruleset(DiceEngine(SeededRandomSource(1))),
        WorkerPhasePolicy(60, 3, 120, "fake"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )


async def accept_action(client, ids, *, content=None) -> UUID:
    response = await client.post(
        f"/campaigns/{ids['campaign_id']}/turns",
        json={
            "request_id": str(uuid4()),
            "expected_state_version": 0,
            "actor_id": ids["actor_id"],
            "content": content or {"kind": "scenario_action", "action_ref": "leave"},
        },
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["turn_id"])


def test_authored_release_update_keeps_active_player_pinned_through_workers(
    database: Engine,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions, client_for(sessions, AUTHOR) as author:
            payload = definition().model_dump(mode="json")
            created = await author.post(
                "/stories",
                json={
                    "request_id": str(uuid4()),
                    "draft": {"scenario": payload, "metadata": {"title": "Published v1"}},
                },
            )
            assert created.status_code == 201, created.text
            story = created.json()
            story_id = story["story_id"]
            path = f"/stories/{story_id}"
            trial = await author.post(
                path + "/playtests", json={"request_id": str(uuid4()), "expected_revision": 1}
            )
            assert trial.status_code == 201, trial.text
            trial_ids = trial.json()
            uow = lambda: PostgresUnitOfWork(sessions)  # noqa: E731
            resolver = SkillCheckResolutionWorker(
                uow,
                ScriptedFakeLLM([]),
                MvpV1Ruleset(DiceEngine(SeededRandomSource(1))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
            )
            narrator = NarrationWorker(
                uow,
                ScriptedFakeLLM(
                    [
                        {"narration": "You reach the garden.", "choices": []},
                        {"narration": "You reach the garden.", "choices": []},
                    ]
                ),
                WorkerPhasePolicy(60, 3, 120, "fake"),
            )

            async def finish(client, ids) -> None:
                accepted = await client.post(
                    f"/campaigns/{ids['campaign_id']}/turns",
                    json={
                        "request_id": str(uuid4()),
                        "expected_state_version": 0,
                        "actor_id": ids["actor_id"],
                        "content": {"kind": "scenario_action", "action_ref": "leave"},
                    },
                )
                assert accepted.status_code == 202, accepted.text
                turn_id = UUID(accepted.json()["turn_id"])
                assert await resolver.run_once(turn_id)
                assert await narrator.run_once(turn_id)

            await finish(author, trial_ids)
            report = (await author.post(path + "/validate", json={"expected_revision": 1})).json()
            release = await author.post(
                path + "/publish",
                json={
                    "request_id": str(uuid4()),
                    "expected_revision": 1,
                    "validation_report_id": report["id"],
                    "visibility": "public",
                    "acknowledged_warning_codes": [
                        warning["code"] for warning in report["warnings"]
                    ],
                    "author_playtest_acknowledged": True,
                },
            )
            assert release.status_code == 200, release.text
            first = release.json()
            async with client_for(sessions, PLAYER) as player:
                request = start_request(UUID(first["story_version_id"]))
                started = await player.post("/adventures", json=request.model_dump(mode="json"))
                assert started.status_code == 201, started.text
                player_ids = started.json()
                state_url = f"/campaigns/{player_ids['campaign_id']}/state"
                assert (await player.get(state_url)).json()["adventure"]["title"] == "Published v1"
                updated = story["draft"]
                updated["metadata"]["title"] = "Published v2"
                saved = await author.put(
                    path + "/draft",
                    json={"request_id": str(uuid4()), "expected_revision": 1, "draft": updated},
                )
                assert saved.status_code == 200
                report2 = (
                    await author.post(path + "/validate", json={"expected_revision": 2})
                ).json()
                assert report2["content_hash"] == report["content_hash"]
                release2 = await author.post(
                    path + "/publish",
                    json={
                        "request_id": str(uuid4()),
                        "expected_revision": 2,
                        "validation_report_id": report2["id"],
                        "visibility": "public",
                        "acknowledged_warning_codes": [w["code"] for w in report2["warnings"]],
                        "author_playtest_acknowledged": True,
                    },
                )
                assert release2.status_code == 200, release2.text
                second = release2.json()
                entries = (await player.get("/adventures/catalog")).json()["scenarios"]
                current = next(item for item in entries if item["story_id"] == story_id)
                assert current["story_version_id"] == second["story_version_id"]
                assert current["title"] == "Published v2"
                replay = await player.post("/adventures", json=request.model_dump(mode="json"))
                assert replay.json() == player_ids
                late = await player.post(
                    "/adventures",
                    json=request.model_copy(update={"request_id": uuid4()}).model_dump(mode="json"),
                )
                assert late.status_code == 409
                async with sessions() as session:
                    pinned = await PostgresScenarioRepository(session).snapshot(
                        UUID(player_ids["campaign_id"])
                    )
                    assert pinned is not None and pinned.definition is not None
                    assert str(pinned.story_version_id) == first["story_version_id"]
                    assert (
                        pinned.definition.scenario_ref == story["draft"]["scenario"]["scenario_ref"]
                    )
                    assert pinned.definition.title == "In-game title"
                    assert pinned.public_title == "Published v1"
                    assert (
                        content_hash(pinned.definition.model_dump(mode="json"))
                        == report["content_hash"]
                    )
                ongoing = await player.get(state_url)
                assert ongoing.status_code == 200, ongoing.text
                assert ongoing.json()["adventure"]["title"] == "Published v1"
                new_start = await player.post(
                    "/adventures",
                    json=start_request(UUID(second["story_version_id"])).model_dump(mode="json"),
                )
                assert new_start.status_code == 201, new_start.text
                new_state = await player.get(f"/campaigns/{new_start.json()['campaign_id']}/state")
                assert new_state.json()["adventure"]["title"] == "Published v2"
                await finish(player, player_ids)
                resumed = await player.get(f"/campaigns/{player_ids['campaign_id']}/state")
                assert resumed.status_code == 200, resumed.text
                assert resumed.json()["adventure"]["title"] == "Published v1"
                assert resumed.json()["adventure"]["ending"]["ending_ref"] == "home"
                saved_runs = (await player.get("/adventures")).json()["adventures"]
                old_run = next(
                    item for item in saved_runs if item["campaign_id"] == player_ids["campaign_id"]
                )
                assert old_run["title"] == "Published v1"

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize(
    "lifecycle,visibility,can_replay,can_continue",
    [
        ("active", "private", False, True),
        ("withdrawn", "unlisted", True, True),
        ("archived", "public", True, True),
        ("blocked", "public", False, False),
    ],
)
def test_lifecycle_distinguishes_new_starts_replays_and_pinned_continuation(
    database: Engine,
    lifecycle: str,
    visibility: str,
    can_replay: bool,
    can_continue: bool,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            story_id, version_id, _ = await seed_release(sessions)
            source = PostgresScenarioSource(sessions)
            adventures = AdventureService(PostgresAdventureStore(sessions), source)
            request = start_request(version_id)
            original = await adventures.create(PLAYER, request)
            async with sessions.begin() as session:
                await session.execute(
                    update(StoryModel)
                    .where(StoryModel.id == story_id)
                    .values(lifecycle=lifecycle, visibility=visibility)
                )
            with pytest.raises((StoryError, AuthorizationError)):
                await adventures.create(PLAYER, start_request(version_id))
            if can_replay:
                assert await adventures.create(PLAYER, request) == original
            else:
                with pytest.raises((StoryError, AuthorizationError)):
                    await adventures.create(PLAYER, request)
            async with sessions.begin() as session:
                if can_continue:
                    pinned = await load_pinned_definition(session, version_id)
                    assert pinned.scenario_ref.startswith("test_")
                    await authorize_story_run(session, original.campaign_id)
                else:
                    with pytest.raises(AuthorizationError):
                        await load_pinned_definition(session, version_id)
                    with pytest.raises(AuthorizationError):
                        await authorize_story_run(session, original.campaign_id)
            assert not await source.catalog(PLAYER.principal_id)
            if lifecycle == "active":
                assert (await source.catalog(AUTHOR.principal_id))[0].story_id == story_id
                assert (await adventures.create(AUTHOR, start_request(version_id))).campaign_id

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_unlisted_is_not_discoverable_and_replay_cannot_cross_stories(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            story_id, version_id, scenario = await seed_release(sessions, visibility="unlisted")
            _, foreign_version, _ = await seed_release(sessions, visibility="unlisted")
            source = PostgresScenarioSource(sessions)
            adventures = AdventureService(PostgresAdventureStore(sessions), source)
            assert await source.catalog(PLAYER.principal_id) == []
            request = start_request(version_id)
            started = await adventures.create(PLAYER, request)
            with pytest.raises(StoryError, match="STORY_REFERENCE_MISMATCH"):
                await source.for_start(
                    PLAYER.principal_id,
                    request.model_copy(
                        update={
                            "request_id": uuid4(),
                            "scenario_ref": "wrong_story",
                            "scenario_version": 1,
                        }
                    ),
                )
            with pytest.raises(IdempotencyConflictError):
                await source.for_start(
                    PLAYER.principal_id,
                    request.model_copy(update={"story_version_id": foreign_version}),
                )
            async with sessions.begin() as session:
                await session.execute(
                    update(MvpScenarioRunModel)
                    .where(MvpScenarioRunModel.campaign_id == started.campaign_id)
                    .values(story_version_id=foreign_version)
                )
            with pytest.raises(StoryError, match="STORY_VERSION_CORRUPT"):
                await source.for_start(PLAYER.principal_id, request)
            async with sessions.begin() as session:
                await session.execute(
                    update(MvpScenarioRunModel)
                    .where(MvpScenarioRunModel.campaign_id == started.campaign_id)
                    .values(
                        story_version_id=version_id, scenario_ref=scenario.scenario_ref + "_bad"
                    )
                )
            with pytest.raises(StoryError, match="STORY_VERSION_CORRUPT"):
                await source.for_start(PLAYER.principal_id, request)
            assert (await source.catalog(AUTHOR.principal_id))[0].story_id in {story_id}

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_locked_start_refreshes_stale_identity_map_and_locks_only_its_story(
    database: Engine,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            story_id, version_id, _ = await seed_release(sessions)
            other_id, _, _ = await seed_release(sessions)
            request = start_request(version_id)
            async with sessions.begin() as reader:
                stale_story = await reader.get(StoryModel, story_id)
                assert stale_story is not None and stale_story.visibility == "public"
                await resolve_start(reader, PLAYER.principal_id, request)
                async with sessions.begin() as writer:
                    await writer.execute(
                        update(StoryModel)
                        .where(StoryModel.id == story_id)
                        .values(visibility="private")
                    )
                with pytest.raises(StoryError, match="STORY_VERSION_NOT_FOUND"):
                    await resolve_start(reader, PLAYER.principal_id, request, lock=True)
                assert stale_story.visibility == "private"
            async with sessions.begin() as reader:
                await resolve_start(reader, AUTHOR.principal_id, request, lock=True)
                # Unrelated stories remain writable while the authorizing SHARE lock is held.
                async with sessions.begin() as writer:
                    await writer.execute(text("SET LOCAL lock_timeout='150ms'"))
                    await writer.execute(
                        update(StoryModel)
                        .where(StoryModel.id == other_id)
                        .values(lifecycle="withdrawn")
                    )
                with pytest.raises(DBAPIError) as blocked:
                    async with sessions.begin() as writer:
                        await writer.execute(text("SET LOCAL lock_timeout='150ms'"))
                        await writer.execute(
                            update(StoryModel)
                            .where(StoryModel.id == story_id)
                            .values(lifecycle="blocked")
                        )
                assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
            async with sessions.begin() as writer:
                await writer.execute(
                    update(StoryModel).where(StoryModel.id == story_id).values(lifecycle="blocked")
                )
            with pytest.raises(AuthorizationError):
                await PostgresScenarioSource(sessions).for_start(AUTHOR.principal_id, request)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_reader_rejects_hash_schema_ruleset_and_capability_corruption(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            _, version_id, scenario = await seed_release(sessions)
            async with sessions() as session:
                version = await session.get(StoryVersionModel, version_id)
                assert version is not None
                session.expunge(version)
            assert decode_version(version) == scenario
            assert published_title(version, scenario) == "Public v1"
            original = version.payload
            version.payload = {**original, "title": "Tampered"}
            with pytest.raises(StoryError, match="STORY_VERSION_CORRUPT"):
                decode_version(version)
            version.payload = original
            for field, changed in (
                ("schema_version", 1),
                ("ruleset_ref", "mvp_v2"),
                ("ruleset_version", "mvp_v2"),
                ("capability_requirements", ["combat"]),
            ):
                before = getattr(version, field)
                setattr(version, field, changed)
                with pytest.raises(StoryError, match="STORY_VERSION_CORRUPT"):
                    decode_version(version)
                setattr(version, field, before)
            version.schema_version = 999
            with pytest.raises(StoryError, match="UNSUPPORTED_STORY_SCHEMA"):
                decode_version(version)
            version.schema_version = 2
            invalid = {**original, "scenes": []}
            version.payload, version.content_hash = invalid, content_hash(invalid)
            with pytest.raises(StoryError, match="STORY_VERSION_CORRUPT"):
                decode_version(version)
            version.public_metadata = {"title": ["invalid"]}
            with pytest.raises(StoryError, match="STORY_VERSION_CORRUPT"):
                published_title(version, scenario)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_legacy_builtin_selection_keeps_historical_versions_without_latest_fallback(
    database: Engine,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            await import_builtin_stories(sessions)
            source = PostgresScenarioSource(sessions)
            request = CreateAdventureRequest(
                request_id=uuid4(),
                scenario_ref="ruined_chapel",
                scenario_version=1,
                preset_ref="scout",
                player_name="Legacy",
            )
            resolved = await source.for_start(PLAYER.principal_id, request)
            assert resolved.version_id == builtin_version_id("ruined_chapel", 1)
            assert resolved.definition.version == 1
            with pytest.raises(StoryError, match="STORY_RELEASE_CHANGED"):
                await source.for_start(PLAYER.principal_id, start_request(resolved.version_id))
            with pytest.raises(StoryError, match="STORY_VERSION_NOT_FOUND"):
                await source.for_start(
                    PLAYER.principal_id, request.model_copy(update={"scenario_version": 999})
                )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize(
    "change,code",
    [
        ("withdrawn", "STORY_UNAVAILABLE"),
        ("private", "STORY_VERSION_NOT_FOUND"),
        ("new_release", "STORY_RELEASE_CHANGED"),
    ],
)
def test_start_transaction_rechecks_a_change_after_source_preflight(
    database: Engine,
    change: str,
    code: str,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions:
            story_id, version_id, scenario = await seed_release(sessions)

            class ChangingSource(PostgresScenarioSource):
                async def for_start(
                    self,
                    principal_id: UUID,
                    request: CreateAdventureRequest,
                ) -> ResolvedScenario:
                    resolved = await super().for_start(principal_id, request)
                    async with sessions.begin() as session:
                        if change == "new_release":
                            new_id = uuid4()
                            payload = scenario.model_dump(mode="json")
                            await session.execute(
                                insert(StoryVersionModel).values(
                                    id=new_id,
                                    story_id=story_id,
                                    kind="release",
                                    release_number=2,
                                    schema_version=2,
                                    ruleset_ref="mvp_v1",
                                    ruleset_version="mvp_v1",
                                    capability_requirements=[],
                                    payload=payload,
                                    public_metadata={"title": "Public v2"},
                                    content_hash=content_hash(payload),
                                )
                            )
                            await session.execute(
                                update(StoryModel)
                                .where(StoryModel.id == story_id)
                                .values(current_release_id=new_id)
                            )
                        elif change == "private":
                            await session.execute(
                                update(StoryModel)
                                .where(StoryModel.id == story_id)
                                .values(visibility="private")
                            )
                        else:
                            await session.execute(
                                update(StoryModel)
                                .where(StoryModel.id == story_id)
                                .values(lifecycle="withdrawn")
                            )
                    return resolved

            service = AdventureService(PostgresAdventureStore(sessions), ChangingSource(sessions))
            with pytest.raises(StoryError, match=code):
                await service.create(PLAYER, start_request(version_id))
            with database.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM adventure_start_requests")) == 0
                assert connection.scalar(text("SELECT count(*) FROM campaigns")) == 0

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize("moment", ["before_snapshot", "after_snapshot", "before_llm"])
def test_blocked_resolution_cannot_write_from_an_earlier_snapshot(
    database: Engine,
    monkeypatch: pytest.MonkeyPatch,
    moment: str,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions, client_for(sessions) as client:
            story_id, version_id, _ = await seed_release(sessions)
            started = await client.post(
                "/adventures",
                json=start_request(version_id).model_dump(
                    mode="json",
                ),
            )
            assert started.status_code == 201, started.text
            content = (
                {"kind": "text", "text": "I look at the door."} if moment == "before_llm" else None
            )
            turn_id = await accept_action(client, started.json(), content=content)
            before = game_writes(database)
            original = PostgresCanonicalRepository.snapshot
            original_commit = PostgresUnitOfWork.commit
            snapshots = []
            pending_block = False

            async def block_after_snapshot(repository, campaign_id, scene_id):
                nonlocal pending_block
                snapshot = await original(repository, campaign_id, scene_id)
                snapshots.append(snapshot)
                pending_block = True
                return snapshot

            async def block_after_read_transaction(unit_of_work):
                nonlocal pending_block
                await original_commit(unit_of_work)
                if pending_block:
                    pending_block = False
                    # Release snapshot read locks before moderation. The stale snapshot
                    # remains in the worker, but its resolution has not been committed.
                    await moderate_story(sessions, story_id)

            if moment == "before_snapshot":
                await moderate_story(sessions, story_id)
            else:
                monkeypatch.setattr(PostgresCanonicalRepository, "snapshot", block_after_snapshot)
                monkeypatch.setattr(PostgresUnitOfWork, "commit", block_after_read_transaction)
            fake = ScriptedFakeLLM(
                [
                    {
                        "kind": "narrative",
                        "narration": "The brass door is closed.",
                        "choices": [],
                    }
                ]
            )
            error = None
            try:
                await resolution_worker(sessions, fake).run_once(turn_id)
            except AuthorizationError as caught:
                error = caught
            assert fake.request_count == 0
            assert game_writes(database) == before
            assert error is None, "A blocked snapshot must stop the worker, not escape its loop"
            if moment != "before_snapshot":
                assert len(snapshots) == 1
                assert snapshots[0].scenario_run.story_version_id == version_id

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize("moment", ["before_reserve", "before_save"])
def test_blocked_narration_cannot_save_or_reserve_more_calls(
    database: Engine,
    monkeypatch: pytest.MonkeyPatch,
    moment: str,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions, client_for(sessions) as client:
            story_id, version_id, _ = await seed_release(sessions)
            started = await client.post(
                "/adventures",
                json=start_request(version_id).model_dump(
                    mode="json",
                ),
            )
            assert started.status_code == 201, started.text
            turn_id = await accept_action(client, started.json())
            assert await resolution_worker(sessions).run_once(turn_id)
            before = game_writes(database)
            original = PostgresNarrationRepository.save_conditionally
            saves = []

            async def block_before_save(repository, *args, **kwargs):
                nonlocal before
                # The in-flight provider call was already legitimately reserved. Blocking
                # prevents its output and all further calls; it cannot refund that call.
                before = game_writes(database)
                saves.append(args)
                await moderate_story(sessions, story_id)
                return await original(repository, *args, **kwargs)

            if moment == "before_reserve":
                await moderate_story(sessions, story_id)
            else:
                monkeypatch.setattr(
                    PostgresNarrationRepository, "save_conditionally", block_before_save
                )
            fake = ScriptedFakeLLM([{"narration": "You reach the garden.", "choices": []}])
            worker = NarrationWorker(
                lambda: PostgresUnitOfWork(sessions),
                fake,
                WorkerPhasePolicy(60, 3, 120, "fake"),
            )
            error = None
            try:
                await worker.run_once(turn_id)
            except AuthorizationError as caught:
                error = caught
            assert fake.request_count == (1 if moment == "before_save" else 0)
            if moment == "before_save":
                assert len(saves) == 1
            assert game_writes(database) == before
            async with sessions.begin() as session:
                with pytest.raises(AuthorizationError):
                    await PostgresLLMCallRepository(session).reserve(
                        turn_id,
                        phase="narration",
                        worker_epoch=1,
                    )
            assert game_writes(database) == before
            assert error is None, "Moderation must abort narration without escaping the worker"

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_blocked_core_guards_are_read_only_and_unblocking_does_not_republish(
    database: Engine,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions, client_for(sessions) as client:
            story_id, version_id, _ = await seed_release(sessions)
            started = await client.post(
                "/adventures",
                json=start_request(version_id).model_dump(
                    mode="json",
                ),
            )
            assert started.status_code == 201, started.text
            ids = started.json()
            campaign_id = UUID(ids["campaign_id"])
            turn_id = await accept_action(client, ids)
            async with sessions.begin() as session:
                lease = await PostgresTurnRepository(session).acquire_lease(
                    turn_id,
                    lease_seconds=60,
                    max_attempts=3,
                    deadline_seconds=120,
                )
                assert lease is not None
            await moderate_story(sessions, story_id)
            before = game_writes(database)
            turn = PlayerTurnInput.model_validate(
                {
                    "request_id": str(uuid4()),
                    "expected_state_version": 0,
                    "actor_id": ids["actor_id"],
                    "content": {"kind": "scenario_action", "action_ref": "leave"},
                }
            )
            operations = (
                lambda session: PostgresTurnRepository(session).get_campaign_state(
                    campaign_id,
                    AUTHOR.principal_id,
                ),
                lambda session: PostgresTurnRepository(session).accept_pending(
                    campaign_id,
                    AUTHOR.principal_id,
                    turn,
                    max_actions=3,
                ),
                lambda session: PostgresTurnRepository(session).commit_narrative(
                    NarrativeCommit(
                        campaign_id,
                        lease.turn.scene_id,
                        turn_id,
                        lease.turn.worker_epoch,
                        0,
                        "This must not be saved.",
                        (),
                    )
                ),
                lambda session: PostgresNarrationRepository(session).save_conditionally(
                    campaign_id,
                    turn_id,
                    1,
                    "This fallback must not be saved.",
                    (),
                    fallback_reason="UNKNOWN",
                ),
                lambda session: PostgresLLMCallRepository(session).reserve(
                    turn_id,
                    phase="resolution",
                    worker_epoch=lease.turn.worker_epoch,
                ),
            )
            for operation in operations:
                # Catch inside the transaction and COMMIT: denial must precede writes,
                # not merely rely on an exception rolling those writes back elsewhere.
                async with sessions.begin() as session:
                    with pytest.raises(AuthorizationError):
                        await operation(session)
                assert game_writes(database) == before
            await moderate_story(sessions, story_id, blocked=False)
            async with sessions.begin() as session:
                assert (await session.get(StoryModel, story_id)).lifecycle == "withdrawn"
                await authorize_story_run(session, campaign_id)
                assert (await load_pinned_definition(session, version_id)).title == "In-game title"
            with pytest.raises(StoryError, match="STORY_UNAVAILABLE"):
                await PostgresScenarioSource(sessions).for_start(
                    AUTHOR.principal_id, start_request(version_id)
                )
            assert not any(
                item.story_version_id == version_id
                for item in (await PostgresScenarioSource(sessions).catalog(AUTHOR.principal_id))
            )
            assert game_writes(database) == before

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_playtest_debug_requires_matching_owner_story_and_active_membership(
    database: Engine,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as sessions, client_for(sessions) as client:
            payload = definition().model_dump(mode="json")
            payload["flags"] = [{"flag_ref": "seen", "public_fact": "You inspected the door."}]
            payload["scenes"][0]["actions"][0]["required_flags"] = ["seen"]
            payload["scenes"][0]["actions"].append(
                {
                    "kind": "scenario_action",
                    "action_ref": "inspect",
                    "label": "Inspect",
                    "public_fact": "You inspected the door.",
                    "disabled_flags": ["seen"],
                    "success": {"add_flags": ["seen"]},
                }
            )
            created = await client.post(
                "/stories",
                json={
                    "request_id": str(uuid4()),
                    "draft": {"scenario": payload},
                },
            )
            assert created.status_code == 201, created.text
            story_id = UUID(created.json()["story_id"])
            trial = await client.post(
                f"/stories/{story_id}/playtests",
                json={
                    "request_id": str(uuid4()),
                    "expected_revision": 1,
                },
            )
            assert trial.status_code == 201, trial.text
            campaign_id = UUID(trial.json()["campaign_id"])
            store = PostgresStoryStore(sessions)
            before = game_writes(database)
            state = await client.get(f"/campaigns/{campaign_id}/state")
            assert state.status_code == 200, state.text
            assert state.json()["adventure"]["title"] == "In-game title"
            debug = await store.playtest_debug(AUTHOR.principal_id, story_id, campaign_id)
            assert debug.current_scene_ref == "observatory"
            assert debug.flags == []
            assert debug.actions[0]["missing_flags"] == ["seen"]
            assert debug.actions[1]["blocking_flags"] == []
            for owner, target_story, target_campaign in (
                (PLAYER.principal_id, story_id, campaign_id),
                (AUTHOR.principal_id, uuid4(), campaign_id),
                (AUTHOR.principal_id, story_id, uuid4()),
            ):
                with pytest.raises(StoryError, match="playtest_not_found"):
                    await store.playtest_debug(owner, target_story, target_campaign)
            assert game_writes(database) == before
            turn_id = await accept_action(
                client,
                trial.json(),
                content={
                    "kind": "scenario_action",
                    "action_ref": "inspect",
                },
            )
            assert await resolution_worker(sessions).run_once(turn_id)
            before = game_writes(database)
            debug = await store.playtest_debug(AUTHOR.principal_id, story_id, campaign_id)
            assert debug.flags == ["seen"]
            assert debug.actions[0]["missing_flags"] == []
            assert debug.actions[1]["blocking_flags"] == ["seen"]
            assert game_writes(database) == before
            waiting = asyncio.Event()
            engine = sessions.kw["bind"].sync_engine

            def attempted_campaign_lock(conn, cursor, statement, parameters, context, many):
                if "FROM campaigns" in statement and "FOR UPDATE" in statement:
                    waiting.set()

            async with sessions.begin() as revoker:
                await revoker.execute(
                    select(CampaignModel.id)
                    .where(
                        CampaignModel.id == campaign_id,
                    )
                    .with_for_update()
                )
                event.listen(engine, "before_cursor_execute", attempted_campaign_lock)
                debug_task = asyncio.create_task(
                    store.playtest_debug(
                        AUTHOR.principal_id,
                        story_id,
                        campaign_id,
                    )
                )
                try:
                    await asyncio.wait_for(waiting.wait(), timeout=5)
                    assert not debug_task.done()
                    await revoker.execute(
                        update(CampaignMemberModel)
                        .where(
                            CampaignMemberModel.campaign_id == campaign_id,
                            CampaignMemberModel.principal_id == AUTHOR.principal_id,
                        )
                        .values(active=False)
                    )
                    await revoker.commit()
                    with pytest.raises(StoryError, match="playtest_not_found"):
                        await asyncio.wait_for(debug_task, timeout=5)
                finally:
                    event.remove(engine, "before_cursor_execute", attempted_campaign_lock)
                    if not debug_task.done():
                        debug_task.cancel()
                        await asyncio.gather(debug_task, return_exceptions=True)
            with pytest.raises(StoryError, match="playtest_not_found"):
                await store.playtest_debug(AUTHOR.principal_id, story_id, campaign_id)
            assert game_writes(database) == before

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
