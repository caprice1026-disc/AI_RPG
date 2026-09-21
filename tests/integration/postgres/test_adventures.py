"""Atomic adventure creation and public resume against a dedicated PostgreSQL DB."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, create_engine, event, text
from test_migrations import (
    _guard_empty_database,
    _postgres_sessions,
    _run_alembic,
    _SequenceRandom,
)

from ai_rpg.api import create_app
from ai_rpg.application import (
    AuthenticatedPrincipal,
    EventStreamService,
    NarrationWorker,
    RuntimePolicy,
    ScenarioProgressor,
    SkillCheckResolutionWorker,
    TurnQueryService,
    TurnService,
    WorkerPhasePolicy,
)
from ai_rpg.engine import DiceEngine, MvpV1Ruleset
from ai_rpg.infrastructure.postgres import PostgresAuthorizationPolicy, PostgresUnitOfWork
from ai_rpg.llm import DevelopmentFakeTransport, ScriptedFakeTransport
from ai_rpg.scenarios import BUILTIN_SCENARIOS

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="Test DB required")]
PRINCIPAL = AuthenticatedPrincipal(
    principal_id=UUID(int=21),
    issuer="test",
    subject="secret-subject",
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


def payload(**changes: object) -> dict[str, object]:
    return {
        "request_id": str(uuid4()),
        "scenario_ref": "ruined_chapel",
        "scenario_version": 1,
        "preset_ref": "scout",
        "player_name": "ミナ",
        **changes,
    }


@asynccontextmanager
async def client_for(
    factory: object, principal: AuthenticatedPrincipal = PRINCIPAL
) -> AsyncIterator[AsyncClient]:
    from ai_rpg.application.adventures import AdventureService
    from ai_rpg.infrastructure.postgres.adventures import PostgresAdventureStore

    authorization = PostgresAuthorizationPolicy(factory)
    uow = lambda: PostgresUnitOfWork(factory)  # noqa: E731

    async def authenticate() -> AuthenticatedPrincipal:
        return principal

    app = create_app(
        adventure_service=AdventureService(PostgresAdventureStore(factory)),
        turn_service=TurnService(authorization, uow, RuntimePolicy(3, 1, 3)),
        turn_query_service=TurnQueryService(authorization, uow, BUILTIN_SCENARIOS),
        event_stream_service=EventStreamService(authorization, uow),
        principal_provider=authenticate,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def test_start_concurrent_replay_conflict_and_principal_scope(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            request = payload()
            responses = await asyncio.gather(
                *(client.post("/adventures", json=request) for _ in range(6))
            )
            assert all(r.status_code == 201 for r in responses)
            first = responses[0].json()
            assert all(r.json() == first for r in responses)
            for change in (
                {"player_name": "別名"},
                {"preset_ref": "guardian"},
                {"player_name": " ミナ"},
            ):
                conflict = await client.post("/adventures", json={**request, **change})
                assert conflict.status_code == 409
                assert conflict.json() == {"detail": {"code": "IDEMPOTENCY_CONFLICT"}}
            other = replace(PRINCIPAL, principal_id=uuid4())
            async with client_for(factory, other) as other_client:
                created = await other_client.post("/adventures", json=request)
                assert created.status_code == 201
                assert created.json() != first
                assert (
                    await other_client.get(f"/campaigns/{first['campaign_id']}/state")
                ).status_code == 403
                assert (
                    await other_client.get(f"/campaigns/{first['campaign_id']}/history")
                ).status_code == 403
            listing = (await client.get("/adventures")).json()["adventures"]
            assert len(listing) == 1
            assert listing[0]["campaign_id"] == first["campaign_id"]
            assert listing[0]["player_name"] == "ミナ"
            assert "secret-subject" not in str(listing)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
    with database.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM campaigns")) == 2
        assert c.scalar(text("SELECT count(*) FROM adventure_start_requests")) == 2
        assert c.scalar(text("SELECT count(*) FROM scenes")) == 6
        assert c.scalar(text("SELECT count(*) FROM principal_identities")) == 0


def test_differing_concurrent_starts_commit_one_and_rollback_retry(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            request = payload()
            responses = await asyncio.gather(
                client.post("/adventures", json=request),
                client.post("/adventures", json={**request, "preset_ref": "guardian"}),
            )
            assert sorted(r.status_code for r in responses) == [201, 409]
            failed_request = payload()
            engine = factory.kw["bind"].sync_engine

            def fail_run_insert(
                conn: object, cursor: object, statement: str, *args: object
            ) -> None:
                if statement.startswith("INSERT INTO mvp_scenario_runs"):
                    raise RuntimeError("injected failure")

            event.listen(engine, "before_cursor_execute", fail_run_insert)
            try:
                with pytest.raises(RuntimeError, match="injected failure"):
                    await client.post("/adventures", json=failed_request)
            finally:
                event.remove(engine, "before_cursor_execute", fail_run_insert)
            with database.connect() as c:
                for table, count in (
                    ("campaigns", 1),
                    ("adventure_start_requests", 1),
                    ("entities", 4),
                    ("scenes", 3),
                    ("mvp_inventory", 2),
                ):
                    assert c.scalar(text(f"SELECT count(*) FROM {table}")) == count
            assert (await client.post("/adventures", json=failed_request)).status_code == 201

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_new_adventure_fake_progress_state_and_paginated_history(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            created = await client.post("/adventures", json=payload(preset_ref="guardian"))
            assert created.status_code == 201
            ids = created.json()
            base = f"/campaigns/{ids['campaign_id']}"
            initial = (await client.get(base + "/state")).json()
            assert initial["state_version"] == 0 and initial["latest_turn"] is None
            assert initial["player"]["name"] == "ミナ"
            assert initial["player"]["current_hp"] == initial["player"]["max_hp"] == 14
            assert initial["player"]["actor_id"] == ids["actor_id"]
            assert {i["item_ref"]: i["quantity"] for i in initial["player"]["inventory"]} == {
                "iron_sword": 1,
                "healing_potion": 2,
            }
            assert (await client.get(base + "/history")).json() == {
                "items": [],
                "next_before_turn_id": None,
            }
            assert initial["adventure"]["current_scene"]["scene_ref"] == "entrance"
            assert not any(
                secret in str(initial)
                for secret in ("goblin", "flags", "difficulty", "defense", "attack_bonus")
            )
            uow = lambda: PostgresUnitOfWork(factory)  # noqa: E731
            transport = DevelopmentFakeTransport()
            resolution = SkillCheckResolutionWorker(
                uow,
                transport,
                MvpV1Ruleset(DiceEngine(_SequenceRandom([20, 20, 1]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
                rng_source="seeded_test",
            )
            narration = NarrationWorker(uow, transport, WorkerPhasePolicy(60, 3, 120, "fake"))
            turns = []
            labels = ["礼拝堂に入る", "広間を調べる", "守衛を倒す", "撤退する"]
            for version, label in enumerate(labels):
                accepted = await client.post(
                    base + "/turns",
                    json={
                        "request_id": str(uuid4()),
                        "expected_state_version": version,
                        "actor_id": ids["actor_id"],
                        "content": {"kind": "text", "text": label},
                    },
                )
                assert accepted.status_code == 202, (version, label, accepted.json())
                turn_id = UUID(accepted.json()["turn_id"])
                pending = (await client.get(base + "/history?limit=1")).json()
                assert pending["items"][0]["turn"]["turn_id"] == str(turn_id)
                assert pending["items"][0]["player_input"] == label
                assert await resolution.run_once(turn_id)
                assert await narration.run_once(turn_id)
                turn = (await client.get(base + f"/turns/{turn_id}")).json()
                assert turn["resolution_status"] == "committed"
                assert turn["route"] == "mechanical"
                turns.append(turn)
            assert (
                turns[2]["action_results"][0]["result"]["state_changes"][0]["kind"]
                == "damage_applied"
            )
            newest = (await client.get(base + "/history?limit=2")).json()
            assert [i["turn"] for i in newest["items"]] == turns[2:]
            older = (
                await client.get(
                    base + "/history",
                    params={
                        "limit": 2,
                        "before_turn_id": newest["next_before_turn_id"],
                    },
                )
            ).json()
            assert [i["turn"] for i in older["items"]] == turns[:2]
            assert older["next_before_turn_id"] is None
            assert [i["player_input"] for i in older["items"]] == labels[:2]
            for query in ("limit=0", "limit=101", f"before_turn_id={uuid4()}"):
                assert (await client.get(base + "/history?" + query)).status_code == 422
            final = (await client.get(base + "/state")).json()
            assert final["adventure"]["status"] == "completed"
            assert final["state_version"] == 4
            assert final["latest_turn"] == turns[-1]
            assert (await client.get("/adventures")).json()["adventures"][0][
                "status"
            ] == "completed"
            assert not any(
                secret in str(newest)
                for secret in ("input_payload", "request_hash", "worker_epoch", "secret-subject")
            )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize(
    "restriction", ["member", "controller", "archived_pc", "archived_campaign"]
)
def test_listing_only_contains_own_playable_adventures(database: Engine, restriction: str) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            request = payload()
            created = (await client.post("/adventures", json=request)).json()
            with database.begin() as c:
                statement = {
                    "member": "UPDATE campaign_members SET active=false WHERE campaign_id=:id",
                    "controller": "UPDATE entities SET controller_id=NULL WHERE campaign_id=:id",
                    "archived_pc": "UPDATE entities SET archived_at=now() WHERE campaign_id=:id",
                    "archived_campaign": "UPDATE campaigns SET status='archived' WHERE id=:id",
                }[restriction]
                c.execute(text(statement), {"id": created["campaign_id"]})
            assert (await client.get("/adventures")).json() == {"adventures": []}
            if restriction == "member":
                assert (
                    await client.get(f"/campaigns/{created['campaign_id']}/state")
                ).status_code == 403
                assert (
                    await client.get(f"/campaigns/{created['campaign_id']}/history")
                ).status_code == 403
                assert (await client.post("/adventures", json=request)).status_code == 403

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_resume_reads_live_pc_and_choice_labels_with_stable_history_cursor(
    database: Engine,
) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            ids = (await client.post("/adventures", json=payload())).json()
            base = f"/campaigns/{ids['campaign_id']}"
            with database.begin() as c:
                c.execute(
                    text("UPDATE mvp_characters SET current_hp=4 WHERE entity_id=:actor"),
                    {"actor": ids["actor_id"]},
                )
                c.execute(
                    text(
                        "UPDATE mvp_inventory SET quantity=1 WHERE owner_id=:actor AND NOT equipped"
                    ),
                    {"actor": ids["actor_id"]},
                )
            state = (await client.get(base + "/state")).json()
            assert state["player"]["current_hp"] == 4
            assert state["player"]["max_hp"] == 10
            assert {i["item_ref"]: i["quantity"] for i in state["player"]["inventory"]} == {
                "iron_sword": 1,
                "healing_potion": 1,
            }
            first = await client.post(
                base + "/turns",
                json={
                    "request_id": str(uuid4()),
                    "expected_state_version": 0,
                    "actor_id": ids["actor_id"],
                    "content": {"kind": "text", "text": "こんにちは"},
                },
            )
            first_id = UUID(first.json()["turn_id"])
            worker = SkillCheckResolutionWorker(
                lambda: PostgresUnitOfWork(factory),
                ScriptedFakeTransport(
                    [
                        {
                            "kind": "narrative",
                            "narration": "静かな時間が流れる。",
                            "choices": [{"label": "少し休む"}],
                        }
                    ]
                ),
                MvpV1Ruleset(DiceEngine(_SequenceRandom([]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
                rng_source="seeded_test",
            )
            assert await worker.run_once(first_id)
            completed = (await client.get(base + f"/turns/{first_id}")).json()
            choice = completed["choices"][0]
            second = await client.post(
                base + "/turns",
                json={
                    "request_id": str(uuid4()),
                    "expected_state_version": 0,
                    "actor_id": ids["actor_id"],
                    "content": {"kind": "choice", "choice_id": choice["id"]},
                },
            )
            assert second.status_code == 202
            second_id = second.json()["turn_id"]
            history = (await client.get(base + "/history")).json()
            assert [i["player_input"] for i in history["items"]] == ["こんにちは", choice["label"]]
            assert history["items"][-1]["turn"] == second.json()
            with database.begin() as c:
                c.execute(
                    text(
                        "UPDATE turns SET created_at='2026-01-01T00:00:00Z' WHERE campaign_id=:campaign"
                    ),
                    {"campaign": ids["campaign_id"]},
                )
            page = (await client.get(base + "/history?limit=1")).json()
            older = (
                await client.get(
                    base + "/history",
                    params={
                        "limit": 1,
                        "before_turn_id": page["next_before_turn_id"],
                    },
                )
            ).json()
            assert [
                older["items"][0]["turn"]["turn_id"],
                page["items"][0]["turn"]["turn_id"],
            ] == sorted([str(first_id), second_id])
            assert older["next_before_turn_id"] is None
            foreign = (await client.post("/adventures", json=payload())).json()
            assert (
                await client.get(
                    f"/campaigns/{foreign['campaign_id']}/history",
                    params={
                        "before_turn_id": str(first_id),
                    },
                )
            ).status_code == 422
            with database.begin() as c:
                c.execute(
                    text("UPDATE entities SET controller_id=NULL WHERE id=:actor"),
                    {"actor": ids["actor_id"]},
                )
            assert (await client.get(base + "/state")).json()["player"] is None

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
