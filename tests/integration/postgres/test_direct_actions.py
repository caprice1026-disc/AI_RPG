"""Registered actions skip interpretation without weakening replay or authorization."""

import asyncio
import subprocess
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from test_adventures import URL, client_for, payload, pytestmark  # noqa: F401
from test_adventures import database as database
from test_migrations import _postgres_sessions, _run_alembic, _SequenceRandom

from ai_rpg.application import (
    NarrationWorker,
    ScenarioProgressor,
    SkillCheckResolutionWorker,
    WorkerPhasePolicy,
)
from ai_rpg.engine import DiceEngine, MvpV1Ruleset
from ai_rpg.infrastructure.postgres import PostgresUnitOfWork
from ai_rpg.llm import DevelopmentFakeLLM
from ai_rpg.scenarios import BUILTIN_SCENARIOS


def test_registered_actions_skip_intent_and_replay_after_scene_change(database: Engine) -> None:
    class NarrationOnly(DevelopmentFakeLLM):
        async def extract_intent(self, *args, **kwargs):
            raise AssertionError("Registered actions must not ask an LLM for intent")

        async def generate_narrative(self, *args, **kwargs):
            raise AssertionError("Registered actions must not use the narrative route")

    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            ids = (await client.post("/adventures", json=payload())).json()
            base = f"/campaigns/{ids['campaign_id']}"
            uow = lambda: PostgresUnitOfWork(factory)  # noqa: E731
            fake = NarrationOnly()
            resolution = SkillCheckResolutionWorker(
                uow,
                fake,
                MvpV1Ruleset(DiceEngine(_SequenceRandom([20]))),
                WorkerPhasePolicy(60, 3, 120, "fake"),
                scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
                rng_source="seeded_test",
            )
            narration = NarrationWorker(uow, fake, WorkerPhasePolicy(60, 3, 120, "fake"))
            for version, action_ref in enumerate(("enter_chapel", "search_hall", "retreat")):
                request = {
                    "request_id": str(uuid4()),
                    "actor_id": ids["actor_id"],
                    "expected_state_version": version,
                    "content": {"kind": "scenario_action", "action_ref": action_ref},
                }
                accepted = await client.post(base + "/turns", json=request)
                assert accepted.status_code == 202, accepted.text
                turn_id = UUID(accepted.json()["turn_id"])
                assert await resolution.run_once(turn_id)
                with database.connect() as connection:
                    assert (
                        connection.scalar(
                            text("SELECT llm_call_count FROM turns WHERE id=:id"), {"id": turn_id}
                        )
                        == 0
                    )
                assert await narration.run_once(turn_id)
                result = (await client.get(base + f"/turns/{turn_id}")).json()
                assert result["resolution_status"] == "committed"
                assert result["narration_status"] == "completed"
                replay = await client.post(base + "/turns", json=request)
                assert replay.status_code == 202 and replay.json() == result
                assert not await resolution.run_once(turn_id)
                assert not await narration.run_once(turn_id)
                conflict = await client.post(
                    base + "/turns",
                    json={
                        **request,
                        "content": {"kind": "scenario_action", "action_ref": "forged"},
                    },
                )
                assert conflict.status_code == 409
                assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
            final = (await client.get(base + "/state")).json()
            assert final["adventure"]["ending"]["ending_ref"] == "retreated"
            history = (await client.get(base + "/history")).json()["items"]
            assert [entry["player_input"] for entry in history] == [
                "礼拝堂に入る",
                "広間を調べる",
                "撤退する",
            ]
            with database.connect() as connection:
                assert connection.scalar(text("SELECT sum(llm_call_count) FROM turns")) == 3

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize("invalidation", ["flags", "version", "authorization"])
def test_registered_action_rechecks_authority_and_state_before_resolution(database, invalidation):
    from test_stage4 import Game

    async def run():
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            ids = (await client.post("/adventures", json=payload(scenario_version=2))).json()
            game = Game(factory, client, ids, [])
            turn_id, request = await game.accept("accept_quest")
            # A parallel acceptance returns the same unresolved operation.
            duplicates = await asyncio.gather(
                *(client.post(game.base + "/turns", json=request) for _ in range(2))
            )
            assert all(
                r.status_code == 202 and r.json()["turn_id"] == str(turn_id) for r in duplicates
            )
            with database.begin() as connection:
                connection.execute(
                    text(
                        {
                            "flags": "INSERT INTO mvp_scenario_flags(campaign_id,flag_ref) VALUES(:id,'quest_accepted')",
                            "version": "UPDATE campaigns SET state_version=state_version+1 WHERE id=:id",
                            "authorization": "UPDATE campaign_members SET active=false WHERE campaign_id=:id",
                        }[invalidation]
                    ),
                    {"id": UUID(ids["campaign_id"])},
                )
            assert await game.resolution.run_once(turn_id)
            with database.connect() as connection:
                row = connection.execute(
                    text("SELECT resolution_status,llm_call_count FROM turns WHERE id=:id"),
                    {"id": turn_id},
                ).one()
                assert tuple(row) == ("not_applied", 0)
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM actions WHERE turn_id=:id"), {"id": turn_id}
                    )
                    == 0
                )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_registered_input_downgrade_refuses_without_losing_turn(database):
    async def run():
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            ids = (await client.post("/adventures", json=payload())).json()
            response = await client.post(
                f"/campaigns/{ids['campaign_id']}/turns",
                json={
                    "request_id": str(uuid4()),
                    "actor_id": ids["actor_id"],
                    "expected_state_version": 0,
                    "content": {"kind": "scenario_action", "action_ref": "enter_chapel"},
                },
            )
            assert response.status_code == 202

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
    with pytest.raises(subprocess.CalledProcessError):
        _run_alembic(URL, "downgrade", "0011_adventure_starts")
    with database.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0012_registered_action_input"
        )
        assert connection.scalar(text("SELECT selected_action_ref FROM turns")) == "enter_chapel"


def test_registered_action_rejects_unavailable_refs_before_any_turn_write(database: Engine) -> None:
    async def run() -> None:
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            ids = (await client.post("/adventures", json=payload())).json()
            base = f"/campaigns/{ids['campaign_id']}"
            for action_ref in ("forged", "search_hall", "defeat_guard"):
                response = await client.post(
                    base + "/turns",
                    json={
                        "request_id": str(uuid4()),
                        "actor_id": ids["actor_id"],
                        "expected_state_version": 0,
                        "content": {"kind": "scenario_action", "action_ref": action_ref},
                    },
                )
                assert response.status_code == 409, response.text
                assert response.json()["detail"]["code"] == "SCENARIO_ACTION_NOT_AVAILABLE"
            with database.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM turns")) == 0
                assert connection.scalar(text("SELECT count(*) FROM events")) == 0

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
