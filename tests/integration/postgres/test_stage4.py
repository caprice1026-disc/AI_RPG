"""Playable v2 endings, atomic retaliation, and public resume."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, event, text
from test_adventures import URL, client_for, payload, pytestmark  # noqa: F401
from test_adventures import database as database
from test_migrations import _postgres_sessions, _SequenceRandom

from ai_rpg.application import (
    NarrationWorker,
    ScenarioProgressor,
    SkillCheckResolutionWorker,
    WorkerPhasePolicy,
)
from ai_rpg.engine import DiceEngine, MvpV1Ruleset
from ai_rpg.infrastructure.postgres import PostgresUnitOfWork
from ai_rpg.llm import DevelopmentFakeLLM, ScriptedFakeLLM
from ai_rpg.scenarios import BUILTIN_SCENARIOS

PRELUDE = (
    "accept_quest",
    "enter_chapel",
    "search_hall",
    "read_archive",
    "prepare_return_route",
    "approach_sanctum",
)


class Game:
    def __init__(self, factory, client, ids, rolls):
        self.client, self.ids = client, ids
        self.base = f"/campaigns/{ids['campaign_id']}"
        self.uow = lambda: PostgresUnitOfWork(factory)
        self.fake = DevelopmentFakeLLM()
        self.resolution = SkillCheckResolutionWorker(
            self.uow,
            self.fake,
            MvpV1Ruleset(DiceEngine(_SequenceRandom(rolls))),
            WorkerPhasePolicy(60, 3, 120, "fake"),
            scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
            rng_source="seeded_test",
        )
        self.narration = NarrationWorker(self.uow, self.fake, WorkerPhasePolicy(60, 3, 120, "fake"))

    async def state(self):
        response = await self.client.get(self.base + "/state")
        assert response.status_code == 200, response.text
        return response.json()

    async def accept(self, action_ref=None, *, content=None):
        request = {
            "request_id": str(uuid4()),
            "actor_id": self.ids["actor_id"],
            "expected_state_version": (await self.state())["state_version"],
            "content": content or {"kind": "scenario_action", "action_ref": action_ref},
        }
        response = await self.client.post(self.base + "/turns", json=request)
        assert response.status_code == 202, response.text
        return UUID(response.json()["turn_id"]), request

    async def step(self, action_ref=None, *, content=None):
        turn_id, request = await self.accept(action_ref, content=content)
        assert await self.resolution.run_once(turn_id)
        await self.narration.run_once(turn_id)
        result = (await self.client.get(self.base + f"/turns/{turn_id}")).json()
        assert result["resolution_status"] == "committed", result
        assert result["narration_status"] == "completed", result
        assert (await self.client.post(self.base + "/turns", json=request)).json() == result
        return result

    async def enter_combat_scene(self):
        for action in PRELUDE:
            assert not (await self.step(action))["enemy_reactions"]


@pytest.mark.parametrize(
    "ending,rolls,finish",
    [
        ("recovered", [20, 20, 20], "negotiate_guard"),
        ("costly_success", [1, 1, 1], "sneak_to_relic"),
        ("retreated", [20, 20], "retreat"),
    ],
)
def test_v2_start_to_ending_with_replay_resume_and_fail_forward(
    database: Engine, ending, rolls, finish
):
    async def run():
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            ids = (await client.post("/adventures", json=payload(scenario_version=2))).json()
            game = Game(factory, client, ids, rolls)
            initial = await game.state()
            assert initial["adventure"]["combat"] is None
            assert "goblin" not in str(initial)
            # A valid global ref is still unavailable until its prerequisites hold.
            denied = await client.post(
                game.base + "/turns",
                json={
                    "request_id": str(uuid4()),
                    "actor_id": ids["actor_id"],
                    "expected_state_version": 0,
                    "content": {"kind": "scenario_action", "action_ref": "enter_chapel"},
                },
            )
            assert denied.status_code == 409
            await game.enter_combat_scene()
            combat = (await game.state())["adventure"]["combat"]
            assert combat["current_hp"] == combat["max_hp"] == 10 and not combat["active"]
            await game.step(finish)
            if ending != "retreated":
                await game.step("recover_relic")
                await game.step("return_relic")
            final = await game.state()
            assert final["adventure"]["ending"]["ending_ref"] == ending
            assert final["adventure"]["available_actions"] == []
            assert final["adventure"]["combat"] is None
            async with client_for(factory) as resumed:
                assert (await resumed.get(game.base + "/state")).json() == final
                history = (await resumed.get(game.base + "/history")).json()["items"]
                assert len(history) == (7 if ending == "retreated" else 9)
            rejected = await client.post(
                game.base + "/turns",
                json={
                    "request_id": str(uuid4()),
                    "actor_id": ids["actor_id"],
                    "expected_state_version": final["state_version"],
                    "content": {"kind": "text", "text": "続ける"},
                },
            )
            assert (
                rejected.status_code == 409
                and rejected.json()["detail"]["code"] == "ADVENTURE_COMPLETED"
            )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_retaliation_healing_and_narration_retry_never_duplicate_damage(database: Engine):
    async def run():
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            ids = (await client.post("/adventures", json=payload(scenario_version=2))).json()
            game = Game(factory, client, ids, [20, 20, 1, 20, 4, 4, 20, 4])
            await game.enter_combat_scene()
            turn_id, request = await game.accept("defeat_guard")
            assert await game.resolution.run_once(turn_id)
            before = await game.state()
            assert before["player"]["current_hp"] == 6
            assert before["adventure"]["combat"]["active"]
            assert {a["action_ref"] for a in before["adventure"]["available_actions"]} == {
                "defeat_guard",
                "retreat",
            }
            fake = ScriptedFakeLLM(
                [
                    {"narration": "999ダメージ", "choices": []},
                    {"narration": "敵の反撃で4ダメージを受けた。", "choices": []},
                ]
            )
            narration = NarrationWorker(game.uow, fake, WorkerPhasePolicy(60, 3, 120, "fake"))
            assert await narration.run_once(turn_id)
            assert await narration.run_once(turn_id)
            assert not await game.resolution.run_once(turn_id)
            assert not await narration.run_once(turn_id)
            after = await game.state()
            assert after["player"] == before["player"]
            assert after["state_version"] == before["state_version"]
            reaction = after["latest_turn"]["enemy_reactions"][0]
            assert reaction["target_id"] == ids["actor_id"]
            assert reaction["result"]["state_changes"][0]["hp_after"] == 6
            assert (await client.post(game.base + "/turns", json=request)).json() == after[
                "latest_turn"
            ]
            healed = await game.step(content={"kind": "text", "text": "回復ポーションを飲む"})
            assert healed["enemy_reactions"][0]["result"]["state_changes"][0]["hp_before"] == 10
            state = await game.state()
            assert state["player"]["current_hp"] == 6
            assert (
                next(i for i in state["player"]["inventory"] if i["item_ref"] == "healing_potion")[
                    "quantity"
                ]
                == 1
            )
            assert not (await game.step("retreat"))["enemy_reactions"]
            with database.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM events WHERE type='EnemyReactionResolved'")
                    )
                    == 2
                )
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM actions WHERE turn_id=:id"), {"id": turn_id}
                    )
                    == 1
                )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_combat_defeat_and_victory_are_terminal_or_continue_to_return(database: Engine):
    async def run():
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            for victory in (False, True):
                ids = (await client.post("/adventures", json=payload(scenario_version=2))).json()
                rolls = (
                    [20, 20, 20, 6, 1, 20, 4] if victory else [20, 20, 1, 20, 4, 1, 20, 4, 1, 20, 4]
                )
                game = Game(factory, client, ids, rolls)
                await game.enter_combat_scene()
                for _ in range(2 if victory else 3):
                    last = await game.step("defeat_guard")
                state = await game.state()
                if victory:
                    assert last["enemy_reactions"] == []
                    assert state["adventure"]["current_scene"]["scene_ref"] == "return"
                    await game.step("recover_relic")
                    await game.step("return_relic")
                    assert (await game.state())["adventure"]["ending"]["ending_ref"] == "recovered"
                else:
                    assert state["player"]["current_hp"] == 0
                    assert state["adventure"]["ending"]["ending_ref"] == "defeated"
                    assert state["adventure"]["available_actions"] == []

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_enemy_event_insert_failure_rolls_back_hp_flags_and_player_action(database: Engine):
    async def run():
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            ids = (await client.post("/adventures", json=payload(scenario_version=2))).json()
            game = Game(factory, client, ids, [20, 20, 1, 20, 4])
            await game.enter_combat_scene()
            previous = await game.state()
            turn_id, _request = await game.accept("defeat_guard")

            def fail_reaction(conn, cursor, statement, parameters, context, executemany):
                if (
                    "INSERT INTO events" in statement
                    and parameters.get("type") == "EnemyReactionResolved"
                ):
                    raise RuntimeError("reaction insert failure")

            engine = factory.kw["bind"].sync_engine
            event.listen(engine, "before_cursor_execute", fail_reaction)
            try:
                with pytest.raises(RuntimeError, match="reaction insert failure"):
                    await game.resolution.run_once(turn_id)
            finally:
                event.remove(engine, "before_cursor_execute", fail_reaction)
            after = await game.state()
            assert after["state_version"] == previous["state_version"]
            assert after["player"] == previous["player"]
            assert not after["adventure"]["combat"]["active"]
            with database.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM actions WHERE turn_id=:id"), {"id": turn_id}
                    )
                    == 0
                )
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM events WHERE turn_id=:id"), {"id": turn_id}
                    )
                    == 0
                )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_stale_enemy_commit_is_rejected_and_exact_commit_replay_is_inert(database, monkeypatch):
    from ai_rpg.infrastructure.postgres.repositories import PostgresTurnRepository

    original = PostgresTurnRepository.commit_resolution
    captured = []
    invalidate = True

    async def intercept(repository, bundle):
        nonlocal invalidate
        if bundle.enemy_reactions:
            captured.append(bundle)
            if invalidate:
                invalidate = False
                with database.begin() as connection:
                    connection.execute(
                        text("UPDATE turns SET worker_epoch=worker_epoch+1 WHERE id=:id"),
                        {"id": bundle.turn_id},
                    )
        return await original(repository, bundle)

    monkeypatch.setattr(PostgresTurnRepository, "commit_resolution", intercept)

    async def run():
        async with _postgres_sessions(URL) as factory, client_for(factory) as client:
            ids = (await client.post("/adventures", json=payload(scenario_version=2))).json()
            game = Game(factory, client, ids, [20, 20, 1, 20, 4])
            await game.enter_combat_scene()
            before = await game.state()
            turn_id, _ = await game.accept("defeat_guard")
            with pytest.raises(RuntimeError, match="worker lease"):
                await game.resolution.run_once(turn_id)
            state = await game.state()
            assert state["player"] == before["player"]
            assert state["state_version"] == before["state_version"]
            assert not state["adventure"]["combat"]["active"]
            with database.begin() as connection:
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM events WHERE turn_id=:id"), {"id": turn_id}
                    )
                    == 0
                )
                connection.execute(
                    text(
                        "UPDATE turns SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"
                    ),
                    {"id": turn_id},
                )
            replacement = Game(factory, client, ids, [1, 20, 4])
            assert await replacement.resolution.run_once(turn_id)
            with database.connect() as connection:
                event_count = connection.scalar(
                    text("SELECT count(*) FROM events WHERE turn_id=:id"), {"id": turn_id}
                )
            async with game.uow() as unit:
                assert (
                    await unit.turns.commit_resolution(captured[-1]) == before["state_version"] + 1
                )
                await unit.commit()
            with database.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM events WHERE turn_id=:id"), {"id": turn_id}
                    )
                    == event_count
                )
            assert (await game.state())["player"]["current_hp"] == 6

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
