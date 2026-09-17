"""開発用processを接続する最小runtime構成。"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.application import NarrationWorker, SkillCheckResolutionWorker, WorkerPhasePolicy
from ai_rpg.application.ports import UnitOfWork
from ai_rpg.config import Settings
from ai_rpg.engine import DiceEngine, MvpV1Ruleset, SecureRandomSource, SeededRandomSource
from ai_rpg.infrastructure.database import create_session_factory
from ai_rpg.infrastructure.postgres import PostgresUnitOfWork
from ai_rpg.llm import DevelopmentFakeTransport, OpenAIResponsesTransport
from ai_rpg.llm.structured import ProviderTransport


@dataclass(frozen=True, slots=True)
class DevelopmentFixture:
    campaign_id: UUID
    scene_id: UUID
    principal_id: UUID
    actor_id: UUID
    target_id: UUID
    weapon_id: UUID
    healing_potion_id: UUID


DEVELOPMENT_FIXTURE = DevelopmentFixture(
    campaign_id=UUID("10000000-0000-0000-0000-000000000001"),
    scene_id=UUID("10000000-0000-0000-0000-000000000011"),
    principal_id=UUID("10000000-0000-0000-0000-000000000021"),
    actor_id=UUID("10000000-0000-0000-0000-000000000031"),
    target_id=UUID("10000000-0000-0000-0000-000000000032"),
    weapon_id=UUID("10000000-0000-0000-0000-000000000041"),
    healing_potion_id=UUID("10000000-0000-0000-0000-000000000042"),
)


class RunnableWorker(Protocol):
    async def run_once(self, turn_id: UUID | None = None) -> bool: ...


def build_provider_transport(settings: Settings, *, fake: bool) -> ProviderTransport:
    if fake:
        return DevelopmentFakeTransport()
    if settings.openai_api_key is None:
        raise ValueError("AIRPG_OPENAI_API_KEY is required for real workers")
    return OpenAIResponsesTransport(
        settings.openai_api_key.get_secret_value(),
        settings.llm_timeout_seconds,
    )


def selector_event_loop() -> asyncio.AbstractEventLoop:
    """Windowsでもpsycopg asyncが利用できるevent loopを返す。"""

    return asyncio.SelectorEventLoop()


def migrate_database(database_url: str) -> None:
    """開発DBを現在のAlembic headへ更新する。"""

    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def seed_development_fixture(database_url: str) -> DevelopmentFixture:
    """Fake一往復に必要な固定fixtureを冪等に投入する。"""

    fixture = DEVELOPMENT_FIXTURE
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO campaigns(id,ruleset_version) VALUES(:id,'mvp_v1') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": fixture.campaign_id},
            )
            connection.execute(
                text(
                    "INSERT INTO campaign_members(campaign_id,principal_id,role) "
                    "VALUES(:campaign,:principal,'player') "
                    "ON CONFLICT (campaign_id,principal_id) DO NOTHING"
                ),
                {
                    "campaign": fixture.campaign_id,
                    "principal": fixture.principal_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO entities(id,campaign_id,kind,controller_id,ref,label) VALUES"
                    "(:actor,:campaign,'pc',:principal,'hero','主人公'),"
                    "(:target,:campaign,'npc',NULL,'goblin','ゴブリン'),"
                    "(:weapon,:campaign,'item',NULL,'iron_sword','鉄の剣'),"
                    "(:potion,:campaign,'item',NULL,'healing_potion','回復ポーション') "
                    "ON CONFLICT (id) DO UPDATE SET ref=EXCLUDED.ref,label=EXCLUDED.label"
                ),
                {
                    "actor": fixture.actor_id,
                    "target": fixture.target_id,
                    "weapon": fixture.weapon_id,
                    "potion": fixture.healing_potion_id,
                    "campaign": fixture.campaign_id,
                    "principal": fixture.principal_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO scenes(id,campaign_id,sequence,status) "
                    "VALUES(:scene,:campaign,1,'active') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"scene": fixture.scene_id, "campaign": fixture.campaign_id},
            )
            connection.execute(
                text(
                    "INSERT INTO mvp_characters("
                    "campaign_id,entity_id,current_hp,max_hp,defense,attack_bonus"
                    ") VALUES(:campaign,:actor,6,10,12,2),"
                    "(:campaign,:target,10,10,11,1) "
                    "ON CONFLICT (campaign_id,entity_id) DO NOTHING"
                ),
                {
                    "campaign": fixture.campaign_id,
                    "actor": fixture.actor_id,
                    "target": fixture.target_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO mvp_weapons("
                    "campaign_id,entity_id,damage_expression,damage_bonus"
                    ") VALUES(:campaign,:weapon,'1d6',0) "
                    "ON CONFLICT (campaign_id,entity_id) DO NOTHING"
                ),
                {"campaign": fixture.campaign_id, "weapon": fixture.weapon_id},
            )
            connection.execute(
                text(
                    "INSERT INTO mvp_inventory("
                    "campaign_id,owner_id,item_id,quantity,equipped"
                    ") VALUES(:campaign,:actor,:weapon,1,true),"
                    "(:campaign,:actor,:potion,2,false) "
                    "ON CONFLICT (campaign_id,owner_id,item_id) DO NOTHING"
                ),
                {
                    "campaign": fixture.campaign_id,
                    "actor": fixture.actor_id,
                    "weapon": fixture.weapon_id,
                    "potion": fixture.healing_potion_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO mvp_skill_modifiers("
                    "campaign_id,character_id,skill_ref,modifier"
                    ") VALUES(:campaign,:actor,'perception',2) "
                    "ON CONFLICT (campaign_id,character_id,skill_ref) DO NOTHING"
                ),
                {"campaign": fixture.campaign_id, "actor": fixture.actor_id},
            )
            connection.execute(
                text(
                    "INSERT INTO mvp_scene_skill_checks("
                    "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                    ") VALUES("
                    ":campaign,:scene,'observe_room','perception','normal',"
                    "'床に新しい足跡が残っている。'"
                    ") ON CONFLICT (campaign_id,scene_id,check_ref) DO NOTHING"
                ),
                {"campaign": fixture.campaign_id, "scene": fixture.scene_id},
            )
    finally:
        engine.dispose()
    return fixture


def _unit_of_work_factory(
    sessions: async_sessionmaker[AsyncSession],
) -> Callable[[], UnitOfWork]:
    def factory() -> UnitOfWork:
        return PostgresUnitOfWork(sessions)

    return factory


def build_resolution_worker(
    settings: Settings,
    transport: ProviderTransport,
    *,
    deterministic: bool = False,
) -> SkillCheckResolutionWorker:
    sessions = create_session_factory(settings.database_url)
    random_source = SeededRandomSource(7) if deterministic else SecureRandomSource()
    return SkillCheckResolutionWorker(
        _unit_of_work_factory(sessions),
        transport,
        MvpV1Ruleset(DiceEngine(random_source)),
        WorkerPhasePolicy(
            settings.worker_lease_seconds,
            settings.resolution_max_attempts,
            settings.resolution_deadline_seconds,
            settings.fast_model,
            settings.llm_timeout_seconds,
        ),
        recent_messages_limit=settings.recent_messages_limit,
        rng_source="seeded_test" if deterministic else "secure",
    )


def build_narration_worker(
    settings: Settings,
    transport: ProviderTransport,
) -> NarrationWorker:
    sessions = create_session_factory(settings.database_url)
    return NarrationWorker(
        _unit_of_work_factory(sessions),
        transport,
        WorkerPhasePolicy(
            settings.worker_lease_seconds,
            settings.narration_max_attempts,
            settings.narration_deadline_seconds,
            settings.quality_model,
            settings.llm_timeout_seconds,
        ),
    )


async def run_worker(
    worker: RunnableWorker,
    *,
    once: bool,
    poll_seconds: float,
) -> bool:
    """workerを一回、または空振り時だけ待つpoll loopとして実行する。"""

    if once:
        return await worker.run_once()
    while True:
        try:
            processed = await worker.run_once()
        except Exception:
            logging.exception("worker cycle failed")
            processed = False
        if not processed:
            await asyncio.sleep(poll_seconds)
