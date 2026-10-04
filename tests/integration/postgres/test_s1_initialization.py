"""S1 initialization and independent scenario execution on a dedicated test DB."""

import asyncio
import json
import os
from importlib.resources import files
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, insert, text, update
from test_adventures import PRINCIPAL, database  # noqa: F401
from test_migrations import _postgres_sessions

from ai_rpg.api import create_app
from ai_rpg.application import (
    NarrationWorker,
    RuntimePolicy,
    ScenarioProgressor,
    SkillCheckResolutionWorker,
    TurnQueryService,
    TurnService,
    WorkerPhasePolicy,
)
from ai_rpg.application.adventures import PRESETS
from ai_rpg.application.stories import content_hash
from ai_rpg.contracts.adventures import CreateAdventureRequest
from ai_rpg.engine import DiceEngine, MvpV1Ruleset, SeededRandomSource
from ai_rpg.infrastructure.postgres import PostgresAuthorizationPolicy, PostgresUnitOfWork
from ai_rpg.infrastructure.postgres.adventures import PostgresAdventureStore
from ai_rpg.infrastructure.postgres.models import PrincipalModel
from ai_rpg.infrastructure.postgres.repositories import PostgresCanonicalRepository
from ai_rpg.infrastructure.postgres.story_models import StoryModel, StoryVersionModel
from ai_rpg.llm import ScriptedFakeLLM
from ai_rpg.scenarios import ScenarioCatalog, ScenarioDefinition

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="Test DB required")]


def test_independent_scenario_starts_finishes_and_resumes(database: Engine) -> None:  # noqa: F811
    definition = ScenarioDefinition.model_validate_json(
        files("ai_rpg.scenarios").joinpath("clockwork_garden.json").read_text()
    )
    catalog = ScenarioCatalog((definition,))

    async def run() -> None:
        async with _postgres_sessions(URL) as factory:
            store = PostgresAdventureStore(factory)
            story_id, version_id = uuid4(), uuid4()
            payload = definition.model_dump(mode="json")
            async with factory.begin() as session:
                await session.execute(insert(PrincipalModel).values(id=PRINCIPAL.principal_id))
                await session.execute(insert(StoryModel).values(
                    id=story_id, owner_principal_id=PRINCIPAL.principal_id, visibility="public",
                ))
                await session.execute(insert(StoryVersionModel).values(
                    id=version_id, story_id=story_id, kind="release", release_number=1,
                    schema_version=2, ruleset_ref="mvp_v2", ruleset_version="mvp_v2",
                    capability_requirements=list(definition.required_capabilities),
                    payload=payload, public_metadata={"title": definition.title},
                    content_hash=content_hash(payload),
                ))
                await session.execute(update(StoryModel).where(StoryModel.id == story_id).values(
                    current_release_id=version_id,
                ))
            request = CreateAdventureRequest(
                request_id=uuid4(), scenario_ref=definition.scenario_ref,
                story_version_id=version_id,
                scenario_version=1, preset_ref="scout", player_name="Ada",
                ability_points={"strength": 0, "agility": 1, "insight": 1, "presence": 0},
                specialty_skill="perception",
            )
            created = await store.create(
                PRINCIPAL.principal_id, request, definition, PRESETS[0], story_version_id=version_id,
            )
            assert await store.create(
                PRINCIPAL.principal_id, request, definition, PRESETS[0], story_version_id=version_id,
            ) == created
            # A newer release removes the tonic effect; the existing run must retain it.
            newer_id = uuid4()
            newer_payload = definition.model_dump(mode="json")
            newer_payload["initialization"]["items"][1]["effect_ref"] = None
            async with factory.begin() as session:
                await session.execute(insert(StoryVersionModel).values(
                    id=newer_id, story_id=story_id, kind="release", release_number=2,
                    schema_version=2, ruleset_ref="mvp_v2", ruleset_version="mvp_v2",
                    capability_requirements=list(definition.required_capabilities),
                    payload=newer_payload, public_metadata={"title": definition.title},
                    content_hash=content_hash(newer_payload),
                ))
                await session.execute(update(StoryModel).where(StoryModel.id == story_id).values(
                    current_release_id=newer_id,
                ))
            uow = lambda: PostgresUnitOfWork(factory)  # noqa: E731
            authorization = PostgresAuthorizationPolicy(factory)
            query = TurnQueryService(authorization, uow, catalog)
            state = await query.get_campaign_state(PRINCIPAL, created.campaign_id)
            assert state.player is not None
            assert {item.item_ref: item.quantity for item in state.player.inventory} == {
                "pruning_hook": 1, "leaf_tonic": 2,
            }
            assert {item.item_ref: item.effect_ref for item in state.player.inventory} == {
                "pruning_hook": None, "leaf_tonic": "healing_potion",
            }
            assert "Author-only" not in state.model_dump_json()
            with database.connect() as connection:
                assert connection.scalar(text(
                    "SELECT count(*) FROM entities WHERE campaign_id=:c AND ref='goblin'"
                ), {"c": created.campaign_id}) == 0
                scene_id = connection.scalar(text(
                    "SELECT id FROM scenes WHERE campaign_id=:c AND sequence=2"
                ), {"c": created.campaign_id})
            async with factory.begin() as session:
                snapshot = await PostgresCanonicalRepository(session).snapshot(
                    created.campaign_id, scene_id,
                )
                assert snapshot.scenario_run.story_version_id == version_id
                assert snapshot.scenario_run.definition == definition
                moth_id = next(row["id"] for row in snapshot.entities if row["ref"] == "brass_moth")
                moth = next(row for row in snapshot.characters if row["entity_id"] == moth_id)
                assert (moth["current_hp"], moth["defense"]) == (4, 8)
                assert snapshot.skill_checks[0]["difficulty"] == "easy"

            async def principal():
                return PRINCIPAL

            app = create_app(
                turn_service=TurnService(authorization, uow, RuntimePolicy(3, 1, 3)),
                turn_query_service=query, principal_provider=principal,
            )
            item_llm = ScriptedFakeLLM([{"kind": "action_plan", "actions": [{
                "kind": "use_item", "item_ref": "leaf_tonic", "target_ref": None,
            }]}])
            worker = SkillCheckResolutionWorker(
                uow, item_llm, MvpV1Ruleset(DiceEngine(SeededRandomSource(1))),
                WorkerPhasePolicy(60, 3, 120, "unused"),
                scenario_progressor=ScenarioProgressor(catalog),
            )
            narrator = NarrationWorker(
                uow, ScriptedFakeLLM([
                    {"narration": "You entered the greenhouse.", "choices": []},
                    {"narration": "The leaf tonic restored health.", "choices": []},
                    {"narration": "The seed pod was collected.", "choices": []},
                ]), WorkerPhasePolicy(60, 3, 120, "scripted"),
            )
            with database.begin() as connection:
                connection.execute(text(
                    "UPDATE mvp_characters SET current_hp=3 WHERE entity_id=:actor"
                ), {"actor": created.actor_id})
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                for version, content in enumerate((
                    {"kind": "scenario_action", "action_ref": "enter_greenhouse"},
                    {"kind": "text", "text": "葉の霊薬を飲んで回復する"},
                    {"kind": "scenario_action", "action_ref": "collect_seed"},
                )):
                    response = await client.post(f"/campaigns/{created.campaign_id}/turns", json={
                        "request_id": str(uuid4()), "expected_state_version": version,
                        "actor_id": str(created.actor_id), "content": content,
                    })
                    assert response.status_code == 202, response.text
                    turn_id = UUID(response.json()["turn_id"])
                    assert await worker.run_once(turn_id)
                    state = await query.get_campaign_state(PRINCIPAL, created.campaign_id)
                    if state.latest_turn.risk_preview is not None:
                        confirmed = await client.post(f"/campaigns/{created.campaign_id}/turns", json={
                            "request_id": str(uuid4()), "expected_state_version": version,
                            "actor_id": str(created.actor_id),
                            "content": {"kind": "confirm_action", "proposal_id": str(
                                state.latest_turn.risk_preview.proposal_id
                            )},
                        })
                        assert confirmed.status_code == 202, confirmed.text
                        turn_id = UUID(confirmed.json()["turn_id"])
                        assert await worker.run_once(turn_id)
                    assert await narrator.run_once(turn_id)
                    if content["kind"] == "text":
                        state = await query.get_campaign_state(PRINCIPAL, created.campaign_id)
                        assert state.player.current_hp == 7
                        tonic = next(item for item in state.player.inventory
                                     if item.item_ref == "leaf_tonic")
                        assert tonic.quantity == 1
                        assert tonic.effect_ref == "healing_potion"
                        result = state.latest_turn.action_results[0].result
                        assert result.kind == "applied"
                        assert result.dice[0].expression == "1d6+2"
                        assert not await worker.run_once(turn_id)
                        context = json.loads(item_llm.calls[0].input_data)
                        item = next(row for row in json.loads(context["pc_view"]["content"])
                                    ["inventory"] if row["item_ref"] == "leaf_tonic")
                        assert item["effect_ref"] == "healing_potion"
                        scene = json.loads(context["scene_view"]["content"])
                        assert scene["world"]["protected_facts"] == []
                        assert "Author-only planning note" not in item_llm.calls[0].input_data
                resumed = await TurnQueryService(authorization, uow, catalog).get_campaign_state(
                    PRINCIPAL, created.campaign_id,
                )
                assert resumed.adventure.status == "completed"
                assert resumed.adventure.ending.ending_ref == "harvested"
                assert resumed.state_version == 3
                assert resumed.player.current_hp == 7
                assert next(item for item in resumed.player.inventory
                            if item.item_ref == "leaf_tonic").quantity == 1

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
