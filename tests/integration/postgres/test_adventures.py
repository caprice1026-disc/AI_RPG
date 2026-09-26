"""Atomic adventure creation and public resume against a dedicated PostgreSQL DB."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
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
from ai_rpg.llm import DevelopmentFakeLLM, ScriptedFakeLLM
from ai_rpg.llm.pydantic_ai import PydanticAILLM
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


def test_v3_creation_persists_ruleset_and_abilities(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            request = payload(
                scenario_version=3,
                ability_points={"strength": 0, "agility": 1, "insight": 1, "presence": 0},
                specialty_skill="perception",
            )
            created = await client.post("/adventures", json=request)
            assert created.status_code == 201, created.text
            ids = created.json()
            assert (await client.post("/adventures", json=request)).json() == ids
            different = await client.post(
                "/adventures", json={**request, "specialty_skill": "stealth"}
            )
            assert different.status_code == 409
            with database.connect() as connection:
                ruleset = connection.scalar(
                    text("SELECT ruleset_version FROM campaigns WHERE id=:id"),
                    {"id": ids["campaign_id"]},
                )
                scores = connection.execute(
                    text("SELECT strength, agility, insight, presence, specialty_skill "
                         "FROM mvp_character_abilities WHERE character_id=:id"),
                    {"id": ids["actor_id"]},
                ).one_or_none()
            assert ruleset == "mvp_v2"
            assert tuple(scores) == (0, 3, 2, 0, "perception")

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_v3_open_action_persists_shortcut_pressure_and_fact(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            created = (await client.post("/adventures", json=payload(
                scenario_version=3, ability_points={"strength": 0, "agility": 1,
                                                    "insight": 1, "presence": 0},
                specialty_skill="acrobatics",
            ))).json()
            base = f"/campaigns/{created['campaign_id']}"
            uow = lambda: PostgresUnitOfWork(factory)  # noqa: E731
            first_worker = SkillCheckResolutionWorker(
                uow, ScriptedFakeLLM([]),
                MvpV1Ruleset(DiceEngine(_SequenceRandom([10]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
            )
            entered = await client.post(base + "/turns", json={
                "request_id": str(uuid4()), "expected_state_version": 0,
                "actor_id": created["actor_id"],
                "content": {"kind": "scenario_action", "action_ref": "enter_chapel"},
            })
            assert entered.status_code == 202, entered.text
            assert await first_worker.run_once(UUID(entered.json()["turn_id"]))
            narrator = NarrationWorker(
                uow, ScriptedFakeLLM([{"narration": "広間に入った。", "choices": []}]),
                WorkerPhasePolicy(60, 3, 120, "fake"),
            )
            assert await narrator.run_once(UUID(entered.json()["turn_id"]))
            first = await client.post(base + "/turns", json={
                "request_id": str(uuid4()), "expected_state_version": 1,
                "actor_id": created["actor_id"],
                "content": {"kind": "text", "text": "長椅子を動かして高窓への道を作る"},
            })
            assert first.status_code == 202, first.text
            fake = ScriptedFakeLLM([{"kind": "action_plan", "actions": [{
                "kind": "open_action", "approach": "長椅子を足場に高窓への道を作る",
                "check": None,
                "success": {"next_scene_ref": "passage", "add_flags": ["shortcut_found"],
                            "facts": [{"fact_ref": "high_window_step", "kind": "route",
                                       "public_text": "高窓に通じる足場"}]},
                "failure": None,
            }]}])
            worker = SkillCheckResolutionWorker(
                lambda: PostgresUnitOfWork(factory), fake,
                MvpV1Ruleset(DiceEngine(_SequenceRandom([10]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
            )
            assert await worker.run_once(UUID(first.json()["turn_id"]))
            assert fake.request_count == 1
            state = (await client.get(base + "/state")).json()
            assert state["latest_turn"]["resolution_status"] == "committed", state["latest_turn"]
            assert state["state_version"] == 2, state
            assert state["adventure"]["current_scene"]["scene_ref"] == "passage"
            assert state["adventure"]["elapsed_actions"] == 2
            assert "高窓に通じる足場" in state["adventure"]["discovered_facts"]
            assert state["adventure"]["generated_facts"] == [{
                "fact_ref": "high_window_step", "kind": "route",
                "public_text": "高窓に通じる足場", "scene_ref": "hall",
            }]
            assert fake.request_count == 1
            assert not await worker.run_once(UUID(first.json()["turn_id"]))

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_v3_risk_preview_waits_for_confirm_without_recalling_model(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            created = (await client.post("/adventures", json=payload(
                scenario_version=3, ability_points={"strength": 0, "agility": 1,
                                                    "insight": 1, "presence": 0},
                specialty_skill="perception",
            ))).json()
            base = f"/campaigns/{created['campaign_id']}"
            requested = await client.post(base + "/turns", json={
                "request_id": str(uuid4()), "expected_state_version": 0,
                "actor_id": created["actor_id"],
                "content": {"kind": "text", "text": "礼拝堂を離れて村へ戻る"},
            })
            fake = ScriptedFakeLLM([{"kind": "action_plan", "actions": [{
                "kind": "open_action", "approach": "村へ戻る", "check": None,
                "success": {"ending_ref": "retreated"}, "failure": None,
                "major_risk": "礼拝堂を離れると依頼を中断します。",
            }]}])
            worker = SkillCheckResolutionWorker(
                lambda: PostgresUnitOfWork(factory), fake,
                MvpV1Ruleset(DiceEngine(_SequenceRandom([10]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
            )
            assert await worker.run_once(UUID(requested.json()["turn_id"]))
            before = (await client.get(base + "/state")).json()
            assert before["state_version"] == 0
            assert before["adventure"]["elapsed_actions"] == 0
            preview = before["latest_turn"]["risk_preview"]
            assert preview["risk_text"] == "この行動は冒険の結末を確定する可能性があります。"
            assert fake.request_count == 1
            confirm_body = {
                "request_id": str(uuid4()), "expected_state_version": 0,
                "actor_id": created["actor_id"],
                "content": {"kind": "confirm_action", "proposal_id": preview["proposal_id"]},
            }
            confirmed = await client.post(base + "/turns", json=confirm_body)
            assert confirmed.status_code == 202, confirmed.text
            assert await worker.run_once(UUID(confirmed.json()["turn_id"]))
            assert (await client.post(base + "/turns", json=confirm_body)).json() == (
                await client.get(base + f"/turns/{confirmed.json()['turn_id']}")
            ).json()
            after = (await client.get(base + "/state")).json()
            assert after["state_version"] == 1
            assert after["adventure"]["status"] == "completed"
            assert after["adventure"]["ending"]["ending_ref"] == "retreated"
            assert fake.request_count == 1

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_v3_registered_retreat_previews_risk_without_model(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            created = (await client.post("/adventures", json=payload(
                scenario_version=3,
                ability_points={"strength": 0, "agility": 1, "insight": 1, "presence": 0},
                specialty_skill="perception",
            ))).json()
            base = f"/campaigns/{created['campaign_id']}"
            fake = ScriptedFakeLLM([])
            worker = SkillCheckResolutionWorker(
                lambda: PostgresUnitOfWork(factory), fake,
                MvpV1Ruleset(DiceEngine(_SequenceRandom([]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
            )
            requested = await client.post(base + "/turns", json={
                "request_id": str(uuid4()), "expected_state_version": 0,
                "actor_id": created["actor_id"],
                "content": {"kind": "scenario_action", "action_ref": "leave_entrance"},
            })
            assert requested.status_code == 202, requested.text
            assert await worker.run_once(UUID(requested.json()["turn_id"]))
            before = (await client.get(base + "/state")).json()
            assert before["state_version"] == 0
            assert before["adventure"]["status"] == "active"
            assert before["adventure"]["elapsed_actions"] == 0
            proposal_id = before["latest_turn"]["risk_preview"]["proposal_id"]
            confirmed = await client.post(base + "/turns", json={
                "request_id": str(uuid4()), "expected_state_version": 0,
                "actor_id": created["actor_id"],
                "content": {"kind": "confirm_action", "proposal_id": proposal_id},
            })
            assert confirmed.status_code == 202, confirmed.text
            assert await worker.run_once(UUID(confirmed.json()["turn_id"]))
            after = (await client.get(base + "/state")).json()
            assert after["state_version"] == 1
            assert after["adventure"]["ending"]["ending_ref"] == "retreated"
            assert after["adventure"]["elapsed_actions"] == 1
            assert fake.request_count == 0

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_v3_attack_risk_confirmation_reuses_registered_intent(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            created = (await client.post("/adventures", json=payload(
                scenario_version=3,
                ability_points={"strength": 0, "agility": 1, "insight": 1, "presence": 0},
                specialty_skill="acrobatics",
            ))).json()
            base = f"/campaigns/{created['campaign_id']}"
            fake = ScriptedFakeLLM([])
            uow = lambda: PostgresUnitOfWork(factory)  # noqa: E731
            worker = SkillCheckResolutionWorker(
                uow, fake, MvpV1Ruleset(DiceEngine(_SequenceRandom([20, 1, 1]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
            )
            narrator = NarrationWorker(
                uow, DevelopmentFakeLLM(), WorkerPhasePolicy(60, 3, 120, "fake"),
            )

            async def registered(action_ref: str, version: int) -> dict:
                accepted = await client.post(base + "/turns", json={
                    "request_id": str(uuid4()), "expected_state_version": version,
                    "actor_id": created["actor_id"],
                    "content": {"kind": "scenario_action", "action_ref": action_ref},
                })
                assert accepted.status_code == 202, accepted.text
                turn_id = UUID(accepted.json()["turn_id"])
                assert await worker.run_once(turn_id)
                state = (await client.get(base + "/state")).json()
                if state["latest_turn"]["resolution_status"] == "committed":
                    assert await narrator.run_once(turn_id)
                return (await client.get(base + "/state")).json()

            for version, action_ref in enumerate((
                "enter_chapel", "walk_to_archive", "walk_to_passage", "approach_sanctum",
            )):
                state = await registered(action_ref, version)
                assert state["state_version"] == version + 1
            assert state["adventure"]["current_scene"]["scene_ref"] == "sanctum"
            preview_state = await registered("defeat_guard", 4)
            assert preview_state["state_version"] == 4
            assert preview_state["latest_turn"]["resolution_status"] == "not_applied"
            assert "HP" in preview_state["latest_turn"]["risk_preview"]["risk_text"]
            proposal_id = preview_state["latest_turn"]["risk_preview"]["proposal_id"]
            confirmed = await client.post(base + "/turns", json={
                "request_id": str(uuid4()), "expected_state_version": 4,
                "actor_id": created["actor_id"],
                "content": {"kind": "confirm_action", "proposal_id": proposal_id},
            })
            assert confirmed.status_code == 202, confirmed.text
            assert await worker.run_once(UUID(confirmed.json()["turn_id"]))
            after = (await client.get(base + "/state")).json()
            assert after["state_version"] == 5
            assert after["latest_turn"]["resolution_status"] == "committed"
            assert after["latest_turn"]["action_results"]
            assert fake.request_count == 0

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_v3_failed_check_changes_world_and_retry_costs_time(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            created = (await client.post("/adventures", json=payload(
                scenario_version=3, ability_points={"strength": 0, "agility": 1,
                                                    "insight": 1, "presence": 0},
                specialty_skill="stealth",
            ))).json()
            base = f"/campaigns/{created['campaign_id']}"
            plan = {"kind": "action_plan", "actions": [{
                "kind": "open_action", "approach": "扉の隙間を静かに通る",
                "check": {"ability": "agility", "skill_ref": "stealth", "difficulty": "hard"},
                "success": {"next_scene_ref": "hall"},
                "failure": {"alert_delta": 1, "add_flags": ["alerted"]},
            }]}
            fake = ScriptedFakeLLM([plan, plan])
            worker = SkillCheckResolutionWorker(
                lambda: PostgresUnitOfWork(factory), fake,
                MvpV1Ruleset(DiceEngine(_SequenceRandom([1, 20]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
            )
            narrator = NarrationWorker(
                lambda: PostgresUnitOfWork(factory),
                ScriptedFakeLLM([{"narration": "音を立ててしまった。", "choices": []}]),
                WorkerPhasePolicy(60, 3, 120, "fake"),
            )
            for version, scene_ref, elapsed, alert in (
                (0, "entrance", 1, 1), (1, "hall", 2, 1),
            ):
                accepted = await client.post(base + "/turns", json={
                    "request_id": str(uuid4()), "expected_state_version": version,
                    "actor_id": created["actor_id"],
                    "content": {"kind": "text", "text": "扉の隙間を静かに通る"},
                })
                assert accepted.status_code == 202, accepted.text
                turn_id = UUID(accepted.json()["turn_id"])
                assert await worker.run_once(turn_id)
                if version == 0:
                    assert await narrator.run_once(turn_id)
                state = (await client.get(base + "/state")).json()
                assert state["state_version"] == version + 1
                assert state["adventure"]["current_scene"]["scene_ref"] == scene_ref
                assert state["adventure"]["elapsed_actions"] == elapsed
                assert state["adventure"]["alert_level"] == alert
            assert fake.request_count == 2

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_v3_full_shortcut_failure_resume_and_alternative_ending(database: Engine) -> None:
    async def run() -> None:
        assert URL
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            created = (await client.post("/adventures", json=payload(
                scenario_version=3,
                ability_points={"strength": 0, "agility": 1, "insight": 1, "presence": 0},
                specialty_skill="acrobatics",
            ))).json()
            base = f"/campaigns/{created['campaign_id']}"
            shortcut = {"kind": "action_plan", "actions": [{
                "kind": "open_action", "approach": "長椅子から高窓へ登る",
                "check": {"ability": "agility", "skill_ref": "acrobatics", "difficulty": "normal"},
                "success": {"next_scene_ref": "passage", "add_flags": ["shortcut_found"],
                            "facts": [{"fact_ref": "window_ladder", "kind": "route",
                                       "public_text": "高窓に通じる足場"}]},
                "failure": {"alert_delta": 1, "add_flags": ["alerted"]},
            }]}
            alternative = {"kind": "action_plan", "actions": [{
                "kind": "open_action", "approach": "見張りと別の解決を結ぶ",
                "check": None, "success": {"add_flags": ["alternative_resolved"],
                                        "ending_ref": "alternative_resolution"},
                "failure": None,
            }]}
            fake = ScriptedFakeLLM([shortcut, shortcut, alternative])
            uow = lambda: PostgresUnitOfWork(factory)  # noqa: E731
            worker = SkillCheckResolutionWorker(
                uow, fake, MvpV1Ruleset(DiceEngine(_SequenceRandom([1, 20]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
            )
            narrator = NarrationWorker(
                uow, DevelopmentFakeLLM(), WorkerPhasePolicy(60, 3, 120, "fake"),
            )

            async def act(version: int, content: dict[str, str], *, narrate: bool = True) -> dict:
                accepted = await client.post(base + "/turns", json={
                    "request_id": str(uuid4()), "expected_state_version": version,
                    "actor_id": created["actor_id"], "content": content,
                })
                assert accepted.status_code == 202, accepted.text
                turn_id = UUID(accepted.json()["turn_id"])
                assert await worker.run_once(turn_id)
                if narrate:
                    assert await narrator.run_once(turn_id)
                return (await client.get(base + "/state")).json()

            state = await act(0, {"kind": "scenario_action", "action_ref": "enter_chapel"})
            assert state["adventure"]["current_scene"]["scene_ref"] == "hall"
            state = await act(1, {"kind": "text", "text": "長椅子から高窓へ登る"})
            assert state["adventure"]["current_scene"]["scene_ref"] == "hall"
            assert state["adventure"]["alert_level"] == 1
            state = await act(2, {"kind": "text", "text": "もう一度長椅子から高窓へ登る"})
            assert state["adventure"]["current_scene"]["scene_ref"] == "passage"
            assert state["adventure"]["generated_facts"][0]["fact_ref"] == "window_ladder"
            assert state["adventure"]["elapsed_actions"] == 3

            async with client_for(factory) as resumed:
                resumed_state = (await resumed.get(base + "/state")).json()
                assert resumed_state["adventure"] == state["adventure"]
                advanced = await resumed.post(base + "/turns", json={
                    "request_id": str(uuid4()), "expected_state_version": 3,
                    "actor_id": created["actor_id"],
                    "content": {"kind": "scenario_action", "action_ref": "approach_sanctum"},
                })
                assert advanced.status_code == 202, advanced.text
                advanced_id = UUID(advanced.json()["turn_id"])
                assert await worker.run_once(advanced_id)
                assert await narrator.run_once(advanced_id)
                ending = await resumed.post(base + "/turns", json={
                    "request_id": str(uuid4()), "expected_state_version": 4,
                    "actor_id": created["actor_id"],
                    "content": {"kind": "text", "text": "見張りと別の解決を結ぶ"},
                })
                assert ending.status_code == 202, ending.text
                ending_id = UUID(ending.json()["turn_id"])
                assert await worker.run_once(ending_id)
                preview = (await resumed.get(base + "/state")).json()
                assert preview["state_version"] == 4
                proposal_id = preview["latest_turn"]["risk_preview"]["proposal_id"]
                assert fake.request_count == 3
                confirmed = await resumed.post(base + "/turns", json={
                    "request_id": str(uuid4()), "expected_state_version": 4,
                    "actor_id": created["actor_id"],
                    "content": {"kind": "confirm_action", "proposal_id": proposal_id},
                })
                assert confirmed.status_code == 202, confirmed.text
                confirmed_id = UUID(confirmed.json()["turn_id"])
                assert await worker.run_once(confirmed_id)
                assert await narrator.run_once(confirmed_id)
                final = (await resumed.get(base + "/state")).json()
                assert final["state_version"] == 5
                assert final["adventure"]["status"] == "completed"
                assert final["adventure"]["ending"]["ending_ref"] == "alternative_resolution"
                assert final["adventure"]["elapsed_actions"] == 5
                assert final["adventure"]["alert_level"] == 1
                assert final["player"]["abilities"] == {
                    "strength": 0, "agility": 3, "insight": 2, "presence": 0,
                }
                assert final["player"]["specialty_skill"] == "acrobatics"
                assert fake.request_count == 3

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


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


@pytest.mark.parametrize("use_agent", [False, True], ids=["fake", "pydantic-ai"])
def test_new_adventure_fake_progress_state_and_paginated_history(
    database: Engine, use_agent: bool,
) -> None:
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
            fake = DevelopmentFakeLLM()
            model_calls = []

            async def model_response(messages, info):
                # The real Agent supplies the actual public input, never game tools.
                input_json = messages[-1].parts[-1].content
                data = json.loads(input_json)
                purpose = "result_narration" if "resolved_actions" in data else "intent"
                model_calls.append(purpose)
                assert not info.function_tools
                if len(model_calls) == 2:
                    # Invalid first narration must not replay the committed action.
                    return ModelResponse(parts=[TextPart("invalid")], finish_reason="stop")
                raw = await fake.request("fake", purpose, "", input_json, {})
                return ModelResponse(parts=[TextPart(json.dumps({"result": raw}))], finish_reason="stop")

            transport = PydanticAILLM({"fake": FunctionModel(model_response)}) if use_agent else fake
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
                if use_agent and version == 0:
                    assert await narration.run_once(turn_id)
                assert not await resolution.run_once(turn_id)
                assert not await narration.run_once(turn_id)
                turn = (await client.get(base + f"/turns/{turn_id}")).json()
                assert turn["resolution_status"] == "committed"
                assert turn["narration_status"] == "completed"
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
            assert final["adventure"]["elapsed_actions"] is None
            assert final["adventure"]["alert_level"] is None
            assert final["state_version"] == 4
            if use_agent:
                assert model_calls.count("intent") == 4
                assert model_calls.count("result_narration") == 5
                with database.connect() as connection:
                    assert connection.execute(text(
                        "SELECT sum(llm_call_count) FROM turns WHERE campaign_id=:id"
                    ), {"id": ids["campaign_id"]}).scalar_one() == 9
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
                ScriptedFakeLLM(
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
