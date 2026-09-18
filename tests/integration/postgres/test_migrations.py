"""実PostgreSQLに対するmigration制約テスト。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Client
from sqlalchemy import Connection, Engine, RowMapping, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_rpg.api import create_app
from ai_rpg.application import (
    AuthenticatedPrincipal,
    AuthorizationError,
    ChoiceNotAvailableError,
    IdempotencyConflictError,
    InvalidCommitBundleError,
    NarrationWorker,
    RuntimePolicy,
    SkillCheckResolutionWorker,
    StateVersionConflictError,
    TurnInProgressError,
    TurnQueryService,
    TurnService,
    WorkerPhasePolicy,
)
from ai_rpg.application.ports import (
    ActionRecord,
    CanonicalSnapshot,
    ChoiceDraft,
    CommitBundle,
    Lease,
    NarrationLease,
)
from ai_rpg.contracts import PlayerTurnInput, TurnResponse
from ai_rpg.contracts.context import OutputLimits
from ai_rpg.contracts.responses import MechanicalNarrationInput
from ai_rpg.domain.commands import AttackCommand, SkillCheckCommand, UseItemCommand
from ai_rpg.domain.events import RNGMetadata
from ai_rpg.domain.results import (
    AppliedResult,
    DamageApplied,
    DiceResult,
    HealingApplied,
    ItemConsumed,
    ResolvedAction,
)
from ai_rpg.engine import DiceEngine, MvpV1Ruleset
from ai_rpg.infrastructure.postgres import PostgresAuthorizationPolicy, PostgresUnitOfWork
from ai_rpg.infrastructure.postgres.repositories import (
    PostgresCanonicalRepository,
    PostgresLLMCallRepository,
    PostgresNarrationRepository,
    PostgresPublicEventRepository,
    PostgresTurnRepository,
)
from ai_rpg.llm import ProviderRefusalError, ScriptedFakeTransport

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
ROOT = Path(__file__).parents[3]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="AIRPG_TEST_DATABASE_URLが未設定です"),
]

CAMPAIGN_A = "00000000-0000-0000-0000-000000000001"
CAMPAIGN_B = "00000000-0000-0000-0000-000000000002"
SCENE_A = "00000000-0000-0000-0000-000000000011"
SCENE_B = "00000000-0000-0000-0000-000000000012"
PRINCIPAL_A = "00000000-0000-0000-0000-000000000021"
PRINCIPAL_B = "00000000-0000-0000-0000-000000000022"
ACTOR_A = "00000000-0000-0000-0000-000000000031"
ACTOR_B = "00000000-0000-0000-0000-000000000032"
ACTOR_C = "00000000-0000-0000-0000-000000000033"
TURN_A = "00000000-0000-0000-0000-000000000041"
TURN_B = "00000000-0000-0000-0000-000000000042"
ACTION_A = "00000000-0000-0000-0000-000000000051"
EVENT_A = "00000000-0000-0000-0000-000000000061"
REQUEST_A = "00000000-0000-0000-0000-000000000071"
REQUEST_B = "00000000-0000-0000-0000-000000000072"
REQUEST_C = "00000000-0000-0000-0000-000000000073"
CHOICE_A = "00000000-0000-0000-0000-000000000081"
CHOICE_B = "00000000-0000-0000-0000-000000000082"
CHOICE_C = "00000000-0000-0000-0000-000000000083"
ITEM_A = "00000000-0000-0000-0000-000000000091"
WEAPON_A = "00000000-0000-0000-0000-000000000092"


def _run_alembic(url: str, *arguments: str) -> None:
    env = {**os.environ, "AIRPG_DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"url={url}", *arguments],
        check=True,
        cwd=ROOT,
        env=env,
    )


def _public_tables(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _public_functions(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text(
                    "SELECT routine_name FROM information_schema.routines "
                    "WHERE routine_schema = 'public'"
                )
            ).scalars()
        )


def _assert_sqlstate(error: DBAPIError, expected: str) -> None:
    assert getattr(error.orig, "sqlstate", None) == expected


def _seed_campaigns(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO campaigns(id,ruleset_version) "
            "VALUES(:campaign_a,'mvp_v1'),(:campaign_b,'mvp_v1')"
        ),
        {"campaign_a": CAMPAIGN_A, "campaign_b": CAMPAIGN_B},
    )


def _seed_members_entities_and_scene(connection: Connection) -> None:
    _seed_campaigns(connection)
    connection.execute(
        text(
            "INSERT INTO campaign_members(campaign_id,principal_id,role) "
            "VALUES(:campaign_a,:principal_a,'player'),(:campaign_b,:principal_b,'player')"
        ),
        {
            "campaign_a": CAMPAIGN_A,
            "campaign_b": CAMPAIGN_B,
            "principal_a": PRINCIPAL_A,
            "principal_b": PRINCIPAL_B,
        },
    )
    connection.execute(
        text(
            "INSERT INTO entities(id,campaign_id,kind,controller_id) "
            "VALUES(:actor_a,:campaign_a,'pc',:principal_a),"
            "(:actor_b,:campaign_b,'pc',:principal_b)"
        ),
        {
            "actor_a": ACTOR_A,
            "actor_b": ACTOR_B,
            "campaign_a": CAMPAIGN_A,
            "campaign_b": CAMPAIGN_B,
            "principal_a": PRINCIPAL_A,
            "principal_b": PRINCIPAL_B,
        },
    )
    connection.execute(
        text(
            "INSERT INTO scenes(id,campaign_id,sequence,status) "
            "VALUES(:scene,:campaign,1,'active')"
        ),
        {"scene": SCENE_A, "campaign": CAMPAIGN_A},
    )


def _seed_history(connection: Connection) -> None:
    _seed_members_entities_and_scene(connection)
    connection.execute(
        text(
            "INSERT INTO turns("
            "id,campaign_id,scene_id,request_id,created_by,actor_id,input_payload,"
            "request_hash,input_kind,input_text,expected_state_version,"
            "committed_state_version,route,resolution_status,committed_at"
            ") VALUES("
            ":turn,:campaign,:scene,:turn,:principal,:actor,'{}',"
            "decode(repeat('00',32),'hex'),'text','行動する',0,0,'mechanical','committed',now()"
            ")"
        ),
        {
            "turn": TURN_A,
            "campaign": CAMPAIGN_A,
            "scene": SCENE_A,
            "principal": PRINCIPAL_A,
            "actor": ACTOR_A,
        },
    )
    connection.execute(
        text(
            "INSERT INTO actions("
            "id,campaign_id,turn_id,ordinal,actor_id,kind,command,result,"
            "result_kind,ruleset_version"
            ") VALUES("
            ":action,:campaign,:turn,1,:actor,'skill_check','{}','{}','applied','mvp_v1'"
            ")"
        ),
        {
            "action": ACTION_A,
            "campaign": CAMPAIGN_A,
            "turn": TURN_A,
            "actor": ACTOR_A,
        },
    )
    connection.execute(
        text(
            "INSERT INTO events("
            "id,campaign_id,scene_id,turn_id,action_id,sequence,state_version,"
            "type,schema_version,payload"
            ") VALUES("
            ":event,:campaign,:scene,:turn,:action,1,0,'ActionResolved',1,'{}'"
            ")"
        ),
        {
            "event": EVENT_A,
            "campaign": CAMPAIGN_A,
            "scene": SCENE_A,
            "turn": TURN_A,
            "action": ACTION_A,
        },
    )


def _player_turn(
    text_value: str = "進む",
    request_id: str = REQUEST_A,
    *,
    expected_state_version: int = 0,
    actor_id: str = ACTOR_A,
    choice_id: str | None = None,
) -> PlayerTurnInput:
    content: dict[str, object]
    if choice_id is None:
        content = {"kind": "text", "text": text_value}
    else:
        content = {"kind": "choice", "choice_id": choice_id}
    return PlayerTurnInput.model_validate(
        {
            "request_id": request_id,
            "expected_state_version": expected_state_version,
            "actor_id": actor_id,
            "content": content,
        }
    )


@asynccontextmanager
async def _postgres_sessions(
    url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _accept_turn(
    factory: async_sessionmaker[AsyncSession],
    turn: PlayerTurnInput,
    principal_id: str = PRINCIPAL_A,
) -> TurnResponse:
    async with factory() as session:
        response = await PostgresTurnRepository(session).add(
            UUID(CAMPAIGN_A), UUID(principal_id), turn, 3
        )
        await session.commit()
        return response


class _SynchronizedTurnRepository(PostgresTurnRepository):
    def __init__(self, session: AsyncSession, barrier: asyncio.Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier
        self._first_lookup_complete = False

    async def _request_row(
        self, campaign_id: UUID, principal_id: UUID, request_id: UUID
    ) -> RowMapping | None:
        row = await super()._request_row(campaign_id, principal_id, request_id)
        if not self._first_lookup_complete:
            self._first_lookup_complete = True
            await self._barrier.wait()
        return row


def _accept_from_database(
    database: Engine,
    turn: PlayerTurnInput,
    principal_id: str = PRINCIPAL_A,
) -> TurnResponse:
    async def accept() -> TurnResponse:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            return await _accept_turn(factory, turn, principal_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(accept())


def _acquire_narration_lease_from_database(
    database: Engine,
    turn_id: UUID,
) -> NarrationLease:
    async def acquire() -> NarrationLease:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            lease = await PostgresNarrationRepository(session).acquire_lease(
                turn_id,
                lease_seconds=60,
                max_attempts=3,
                deadline_seconds=120,
            )
            await session.commit()
            assert lease is not None
            return lease

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(acquire())


def _save_narration_from_database(
    database: Engine,
    turn_id: UUID,
    choices: tuple[ChoiceDraft, ...],
    *,
    worker_epoch: int | None = None,
    event_id: UUID | None = None,
) -> bool:
    if worker_epoch is None:
        worker_epoch = _acquire_narration_lease_from_database(database, turn_id).worker_epoch

    async def save() -> bool:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            saved = await PostgresNarrationRepository(
                session,
                event_id_factory=(lambda: event_id) if event_id is not None else uuid4,
            ).save_conditionally(
                UUID(CAMPAIGN_A),
                turn_id,
                worker_epoch,
                "遅れて届いた描写",
                choices,
            )
            await session.commit()
            return saved

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(save())


def _accept_with_sql_listener(
    database: Engine,
    turn: PlayerTurnInput,
    listener: Callable[[Any, Any, str, Any, Any, bool], None],
) -> TurnResponse:
    async def accept() -> TurnResponse:
        url = database.url.render_as_string(hide_password=False)
        engine = create_async_engine(url)
        event.listen(engine.sync_engine, "before_cursor_execute", listener)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            return await _accept_turn(factory, turn)
        finally:
            await engine.dispose()

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(accept())


def _complete_turn_response(connection: Connection, turn_id: UUID) -> None:
    connection.execute(
        text(
            "UPDATE turns SET resolution_status='committed',route='mechanical',"
            "committed_state_version=0,committed_at=now(),"
            "narration_status='completed',narration='扉を開けた。' WHERE id=:turn"
        ),
        {"turn": turn_id},
    )
    connection.execute(
        text(
            "INSERT INTO actions("
            "id,campaign_id,turn_id,ordinal,actor_id,kind,command,result,"
            "result_kind,ruleset_version"
            ") VALUES("
            ":action,:campaign,:turn,1,:actor,'skill_check','{}',"
            "'{\"kind\":\"not_applicable\",\"reason\":\"rule_precondition\"}',"
            "'not_applicable','mvp_v1'"
            ")"
        ),
        {
            "action": ACTION_A,
            "campaign": CAMPAIGN_A,
            "turn": turn_id,
            "actor": ACTOR_A,
        },
    )
    connection.execute(
        text(
            "INSERT INTO turn_choices("
            "id,campaign_id,scene_id,source_turn_id,actor_id,ordinal,label,state_version,"
            "invalidated_at) VALUES(:choice_a,:campaign,:scene,:turn,:actor,1,'先へ進む',"
            "0,NULL),"
            "(:choice_b,:campaign,:scene,:turn,:actor,2,'古い選択肢',0,now())"
        ),
        {
            "choice_a": CHOICE_A,
            "choice_b": CHOICE_B,
            "campaign": CAMPAIGN_A,
            "scene": SCENE_A,
            "turn": turn_id,
            "actor": ACTOR_A,
        },
    )


def _insert_choice_source(
    connection: Connection,
    *,
    campaign_id: str,
    scene_id: str,
    principal_id: str,
    actor_id: str,
    narration_status: str = "completed",
) -> None:
    narration = "選択肢を提示した。" if narration_status == "completed" else None
    connection.execute(
        text(
            "INSERT INTO turns("
            "id,campaign_id,scene_id,request_id,created_by,actor_id,input_payload,"
            "request_hash,input_kind,input_text,expected_state_version,"
            "committed_state_version,route,resolution_status,committed_at,"
            "narration_status,narration"
            ") VALUES("
            ":turn,:campaign,:scene,:request,:principal,:actor,'{}',"
            "decode(repeat('00',32),'hex'),'text','調べる',0,0,'narrative','committed',"
            "now(),:narration_status,:narration"
            ")"
        ),
        {
            "turn": TURN_B,
            "campaign": campaign_id,
            "scene": scene_id,
            "request": REQUEST_C,
            "principal": principal_id,
            "actor": actor_id,
            "narration_status": narration_status,
            "narration": narration,
        },
    )
    connection.execute(
        text(
            "INSERT INTO turn_choices("
            "id,campaign_id,scene_id,source_turn_id,actor_id,ordinal,label,state_version"
            ") VALUES(:choice,:campaign,:scene,:turn,:actor,1,'調べ続ける',0)"
        ),
        {
            "choice": CHOICE_C,
            "campaign": campaign_id,
            "scene": scene_id,
            "turn": TURN_B,
            "actor": actor_id,
        },
    )


def _seed_resolution_state(connection: Connection) -> None:
    _seed_members_entities_and_scene(connection)
    connection.execute(
        text(
            "UPDATE entities SET ref='hero',label='主人公' "
            "WHERE campaign_id=:campaign AND id=:actor"
        ),
        {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
    )
    connection.execute(
        text(
            "INSERT INTO entities(id,campaign_id,kind,ref,label) "
            "VALUES(:actor,:campaign,'npc','goblin','ゴブリン'),"
            "(:item,:campaign,'item','healing_potion','回復ポーション'),"
            "(:weapon,:campaign,'item','iron_sword','鉄の剣')"
        ),
        {
            "actor": ACTOR_C,
            "item": ITEM_A,
            "weapon": WEAPON_A,
            "campaign": CAMPAIGN_A,
        },
    )
    connection.execute(
        text(
            "INSERT INTO mvp_characters("
            "campaign_id,entity_id,current_hp,max_hp,defense,attack_bonus"
            ") VALUES(:campaign,:actor,10,10,12,2),(:campaign,:target,10,10,11,1)"
        ),
        {"campaign": CAMPAIGN_A, "actor": ACTOR_A, "target": ACTOR_C},
    )
    connection.execute(
        text(
            "INSERT INTO mvp_inventory(campaign_id,owner_id,item_id,quantity,equipped) "
            "VALUES(:campaign,:owner,:item,2,false),"
            "(:campaign,:owner,:weapon,1,true)"
        ),
        {
            "campaign": CAMPAIGN_A,
            "owner": ACTOR_A,
            "item": ITEM_A,
            "weapon": WEAPON_A,
        },
    )
    connection.execute(
        text(
            "INSERT INTO mvp_weapons(campaign_id,entity_id,damage_expression,damage_bonus) "
            "VALUES(:campaign,:weapon,'1d6',0)"
        ),
        {"campaign": CAMPAIGN_A, "weapon": WEAPON_A},
    )


def _resolution_action(turn_id: UUID, ordinal: int) -> ActionRecord:
    action_id = UUID(int=1000 + ordinal)
    roll = DiceResult(expression="1d20+2", rolls=[10], modifier=2, total=12)
    result = AppliedResult(
        kind="applied",
        outcome="success",
        facts=["技能判定はsuccess(合計12)"],
        dice=[roll],
        state_changes=[],
    )
    return ActionRecord(
        command=SkillCheckCommand(
            action_id=action_id,
            campaign_id=UUID(CAMPAIGN_A),
            turn_id=turn_id,
            actor_id=UUID(ACTOR_A),
            ordinal=ordinal,
            kind="skill_check",
            skill_ref="perception",
            objective="扉を調べる",
            target_id=None,
            modifier=2,
            difficulty_class=8,
        ),
        result=result,
        rng=(
            RNGMetadata(
                source="seeded_test",
                implementation_version="mvp_v1",
                draw_index=0,
            ),
        ),
    )


def _attack_action(turn_id: UUID) -> ActionRecord:
    action_id = UUID(int=1101)
    result = AppliedResult(
        kind="applied",
        outcome="success",
        facts=["攻撃が命中し3ダメージを与えた"],
        dice=[
            DiceResult(expression="1d20+2", rolls=[12], modifier=2, total=14),
            DiceResult(expression="1d6", rolls=[3], modifier=0, total=3),
        ],
        state_changes=[
            DamageApplied(
                kind="damage_applied",
                target_id=UUID(ACTOR_C),
                amount=3,
                hp_before=10,
                hp_after=7,
            )
        ],
    )
    return ActionRecord(
        command=AttackCommand(
            action_id=action_id,
            campaign_id=UUID(CAMPAIGN_A),
            turn_id=turn_id,
            actor_id=UUID(ACTOR_A),
            ordinal=1,
            kind="attack",
            target_id=UUID(ACTOR_C),
            weapon_id=None,
            attack_bonus=2,
            damage_expression="1d6",
            damage_bonus=0,
        ),
        result=result,
        rng=tuple(
            RNGMetadata(
                source="seeded_test",
                implementation_version="mvp_v1",
                draw_index=index,
            )
            for index in range(2)
        ),
    )


def _healing_action(turn_id: UUID) -> ActionRecord:
    action_id = UUID(int=1201)
    return ActionRecord(
        command=UseItemCommand(
            action_id=action_id,
            campaign_id=UUID(CAMPAIGN_A),
            turn_id=turn_id,
            actor_id=UUID(ACTOR_A),
            ordinal=1,
            kind="use_item",
            item_id=UUID(ITEM_A),
            target_id=UUID(ACTOR_A),
            effect_ref="healing_potion",
        ),
        result=AppliedResult(
            kind="applied",
            outcome="success",
            facts=["回復ポーションで4回復した"],
            dice=[DiceResult(expression="1d6+2", rolls=[2], modifier=2, total=4)],
            state_changes=[
                HealingApplied(
                    kind="healing_applied",
                    target_id=UUID(ACTOR_A),
                    amount=4,
                    hp_before=5,
                    hp_after=9,
                    max_hp=10,
                ),
                ItemConsumed(
                    kind="item_consumed",
                    owner_id=UUID(ACTOR_A),
                    item_id=UUID(ITEM_A),
                    quantity_before=2,
                    quantity_after=1,
                ),
            ],
        ),
        rng=(
            RNGMetadata(
                source="seeded_test",
                implementation_version="mvp_v1",
                draw_index=0,
            ),
        ),
    )


def _resolution_bundle(
    turn_id: UUID,
    *,
    first_ordinal: int = 1,
    action_count: int = 1,
    scene_id: str = SCENE_A,
    narration_version: int = 0,
    include_narration_results: bool = True,
    attack: bool = False,
    healing: bool = False,
    action: ActionRecord | None = None,
) -> CommitBundle:
    actions = (
        (action,)
        if action is not None
        else (_healing_action(turn_id),)
        if healing
        else (_attack_action(turn_id),)
        if attack
        else tuple(
            _resolution_action(turn_id, ordinal)
            for ordinal in range(first_ordinal, first_ordinal + action_count)
        )
    )
    resolved_actions = [
        ResolvedAction(
            action_id=action.command.action_id,
            ordinal=action.command.ordinal,
            result=action.result,
        )
        for action in actions
    ]
    return CommitBundle(
        campaign_id=UUID(CAMPAIGN_A),
        scene_id=UUID(scene_id),
        turn_id=turn_id,
        worker_epoch=1,
        base_state_version=0,
        actions=actions,
        narration_input=MechanicalNarrationInput(
            player_text="扉を調べる",
            committed_state_version=narration_version,
            resolved_actions=resolved_actions if include_narration_results else [],
            public_state_after=[],
            allowed_entity_refs=[],
            output_limits=OutputLimits(max_actions=3, max_choices=5),
        ),
    )


def _event_types(bundle: CommitBundle) -> list[str]:
    event_types: list[str] = []
    for action in bundle.actions:
        if isinstance(action.result, AppliedResult):
            event_types.extend("DiceRolled" for _ in action.result.dice)
            event_types.extend(
                {
                    "damage_applied": "DamageApplied",
                    "healing_applied": "HealingApplied",
                    "item_consumed": "ItemConsumed",
                }[change.kind]
                for change in action.result.state_changes
            )
        event_types.append("ActionResolved")
    return event_types


def _acquire_lease_from_database(database: Engine, turn_id: UUID) -> Lease:
    async def acquire() -> Lease:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            lease = await PostgresTurnRepository(session).acquire_lease(
                turn_id,
                lease_seconds=60,
                max_attempts=3,
                deadline_seconds=120,
            )
            await session.commit()
            assert lease is not None
            return lease

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(acquire())


def _commit_resolution_from_database(
    database: Engine,
    bundle: CommitBundle,
    *,
    event_ids: Iterator[UUID] | None = None,
) -> int:
    async def commit() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            try:
                event_id_factory = uuid4 if event_ids is None else lambda: next(event_ids)
                version = await PostgresTurnRepository(
                    session,
                    event_id_factory=event_id_factory,
                ).commit_resolution(bundle)
                await session.commit()
                return version
            except BaseException:
                await session.rollback()
                raise

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(commit())


def _guard_empty_database(url: str) -> Generator[str, None, None]:
    database_name = make_url(url).database or ""
    if not database_name.startswith("ai_rpg_test"):
        pytest.fail("AIRPG_TEST_DATABASE_URLにはai_rpg_testで始まる専用DBを指定してください")

    validation_engine = create_engine(url)
    try:
        existing = _public_tables(validation_engine)
    finally:
        validation_engine.dispose()

    if existing:
        pytest.fail(f"テストDBが空ではありません: {sorted(existing)}")

    try:
        yield url
    finally:
        cleanup_engine = create_engine(url)
        try:
            if "alembic_version" in _public_tables(cleanup_engine):
                cleanup_engine.dispose()
                _run_alembic(url, "downgrade", "base")
                cleanup_engine = create_engine(url)
            with cleanup_engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        finally:
            cleanup_engine.dispose()


@pytest.fixture()
def empty_database_url() -> Iterator[str]:
    assert URL
    yield from _guard_empty_database(URL)


@pytest.fixture()
def database(empty_database_url: str) -> Iterator[Engine]:
    _run_alembic(empty_database_url, "upgrade", "head")
    engine = create_engine(empty_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_nonempty_database_is_rejected_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock(spec=Engine)
    run_alembic = MagicMock()
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "create_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(
        module,
        "_public_tables",
        MagicMock(return_value={"alembic_version", "campaigns"}),
    )
    monkeypatch.setattr(module, "_run_alembic", run_alembic)

    guard = _guard_empty_database(
        "postgresql+psycopg://postgres:postgres@localhost/ai_rpg_test_guard"
    )
    try:
        with pytest.raises(pytest.fail.Exception, match="空ではありません"):
            next(guard)
    finally:
        guard.close()

    run_alembic.assert_not_called()


def test_empty_database_upgrades_and_downgrades(empty_database_url: str) -> None:
    _run_alembic(empty_database_url, "upgrade", "head")
    engine = create_engine(empty_database_url)
    try:
        assert {
            "campaigns",
            "events",
            "mvp_inventory",
            "mvp_scene_skill_checks",
            "principals",
            "principal_identities",
        } <= _public_tables(engine)
    finally:
        engine.dispose()

    _run_alembic(empty_database_url, "downgrade", "base")
    engine = create_engine(empty_database_url)
    try:
        assert _public_tables(engine) == {"alembic_version"}
        assert _public_functions(engine) == set()
    finally:
        engine.dispose()


def test_principal_identity_lifecycle_is_pre_registered_and_immutable(
    database: Engine,
) -> None:
    from ai_rpg.infrastructure.database import create_session_factory
    from ai_rpg.infrastructure.postgres import (
        IdentityRegistrationConflict,
        PostgresIdentityStore,
    )

    principal_id = uuid4()
    store = PostgresIdentityStore(
        create_session_factory(database.url.render_as_string(hide_password=False))
    )

    async def exercise() -> tuple[object, object, object]:
        first = await store.register("https://idp.example.com/", "player-1", principal_id)
        repeated = await store.register(
            "https://idp.example.com/", "player-1", principal_id
        )
        assert await store.resolve("https://idp.example.com/", "player-1") == principal_id
        with pytest.raises(IdentityRegistrationConflict):
            await store.register("https://idp.example.com/", "player-1", uuid4())
        disabled = await store.disable("https://idp.example.com/", "player-1")
        assert await store.disable("https://idp.example.com/", "player-1") == disabled
        assert await store.resolve("https://idp.example.com/", "player-1") is None
        return first, repeated, disabled

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        first, repeated, disabled = runner.run(exercise())

    assert first == repeated
    assert disabled.disabled_at is not None
    with pytest.raises(DBAPIError), database.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM principal_identities "
                "WHERE issuer=:issuer AND subject=:subject"
            ),
            {"issuer": "https://idp.example.com/", "subject": "player-1"},
        )
    with pytest.raises(DBAPIError), database.begin() as connection:
        connection.execute(
            text(
                "UPDATE principal_identities SET subject='player-2' "
                "WHERE issuer=:issuer AND subject=:subject"
            ),
            {"issuer": "https://idp.example.com/", "subject": "player-1"},
        )


def test_principal_identity_registration_generates_id_and_missing_disable_fails(
    database: Engine,
) -> None:
    from ai_rpg.infrastructure.database import create_session_factory
    from ai_rpg.infrastructure.postgres import IdentityNotFound, PostgresIdentityStore

    store = PostgresIdentityStore(
        create_session_factory(database.url.render_as_string(hide_password=False))
    )

    async def exercise() -> None:
        registered = await store.register("https://idp.example.com/", "generated")
        assert await store.resolve("https://idp.example.com/", "generated") == (
            registered.principal_id
        )
        with pytest.raises(IdentityNotFound):
            await store.disable("https://idp.example.com/", "missing")

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_principal_identity_concurrent_registration_uses_canonical_winner(
    database: Engine,
) -> None:
    from ai_rpg.infrastructure.database import create_session_factory
    from ai_rpg.infrastructure.postgres import PostgresIdentityStore

    store = PostgresIdentityStore(
        create_session_factory(database.url.render_as_string(hide_password=False))
    )

    async def exercise() -> None:
        first, second = await asyncio.gather(
            store.register("https://idp.example.com/", "concurrent"),
            store.register("https://idp.example.com/", "concurrent"),
        )
        assert first == second
        assert await store.resolve("https://idp.example.com/", "concurrent") == (
            first.principal_id
        )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_identity_cli_register_disable_lifecycle(database: Engine) -> None:
    url = database.url.render_as_string(hide_password=False)
    issuer = "https://idp.example.com/"
    subject = "player-1"
    env = {
        **os.environ,
        "AIRPG_DATABASE_URL": url,
        "AIRPG_AUTH_ISSUER": issuer,
        "PYTHONPATH": str(ROOT / "src"),
    }

    def run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ai_rpg.cli", "auth", *arguments],
            cwd=ROOT,
            env=env,
            check=check,
            capture_output=True,
            text=True,
        )

    register_arguments = (
        "register",
        "--subject",
        subject,
        "--principal-id",
        PRINCIPAL_A,
    )
    expected_active = {
        "issuer": issuer,
        "subject": subject,
        "principal_id": PRINCIPAL_A,
        "status": "active",
    }
    assert json.loads(run(*register_arguments).stdout) == expected_active
    assert json.loads(run(*register_arguments).stdout) == expected_active

    expected_disabled = {**expected_active, "status": "disabled"}
    assert json.loads(run("disable", "--subject", subject).stdout) == expected_disabled
    with database.connect() as connection:
        disabled_row = connection.execute(
            text(
                "SELECT principal_id,created_at,disabled_at "
                "FROM principal_identities WHERE issuer=:issuer AND subject=:subject"
            ),
            {"issuer": issuer, "subject": subject},
        ).one()
    assert disabled_row.disabled_at is not None

    assert json.loads(run("disable", "--subject", subject).stdout) == expected_disabled
    with database.connect() as connection:
        repeated_row = connection.execute(
            text(
                "SELECT principal_id,created_at,disabled_at "
                "FROM principal_identities WHERE issuer=:issuer AND subject=:subject"
            ),
            {"issuer": issuer, "subject": subject},
        ).one()
    assert repeated_row == disabled_row

    rejected = run(*register_arguments, check=False)
    assert rejected.returncode == 2
    assert rejected.stdout == ""
    with database.connect() as connection:
        row_after_rejection = connection.execute(
            text(
                "SELECT principal_id,created_at,disabled_at "
                "FROM principal_identities WHERE issuer=:issuer AND subject=:subject"
            ),
            {"issuer": issuer, "subject": subject},
        ).one()
    assert row_after_rejection == disabled_row


def test_existing_revision_upgrades_with_worker_control_defaults(
    empty_database_url: str,
) -> None:
    _run_alembic(empty_database_url, "upgrade", "0002_mvp_v1_canonical")
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as connection:
            _seed_members_entities_and_scene(connection)
            connection.execute(
                text(
                    "INSERT INTO turns("
                    "id,campaign_id,scene_id,request_id,created_by,actor_id,input_payload,"
                    "request_hash,input_kind,input_text,expected_state_version"
                    ") VALUES("
                    ":turn,:campaign,:scene,:request,:principal,:actor,'{}',"
                    "decode(repeat('00',32),'hex'),'text','行動する',0"
                    ")"
                ),
                {
                    "turn": TURN_A,
                    "campaign": CAMPAIGN_A,
                    "scene": SCENE_A,
                    "request": REQUEST_A,
                    "principal": PRINCIPAL_A,
                    "actor": ACTOR_A,
                },
            )
    finally:
        engine.dispose()

    _run_alembic(empty_database_url, "upgrade", "head")
    engine = create_engine(empty_database_url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT llm_call_budget,resolution_attempt_count,"
                    "narration_worker_epoch,narration_attempt_count "
                    "FROM turns WHERE id=:turn"
                ),
                {"turn": TURN_A},
            ).one()
            indexes = set(
                connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname='public' AND tablename='turns'"
                    )
                ).scalars()
            )
        assert row == (3, 0, 0, 0)
        assert "one_open_turn" in indexes
        assert "one_unresolved_turn" not in indexes
    finally:
        engine.dispose()


def test_only_one_active_scene_per_campaign(database: Engine) -> None:
    with database.begin() as connection:
        _seed_campaigns(connection)
        connection.execute(
            text(
                "INSERT INTO scenes(id,campaign_id,sequence,status) "
                "VALUES(:scene,:campaign,1,'active')"
            ),
            {"scene": SCENE_A, "campaign": CAMPAIGN_A},
        )

    with pytest.raises(IntegrityError) as caught, database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO scenes(id,campaign_id,sequence,status) "
                "VALUES('00000000-0000-0000-0000-000000000012',:campaign,2,'active')"
            ),
            {"campaign": CAMPAIGN_A},
        )
    _assert_sqlstate(caught.value, "23505")


def test_turn_cannot_reference_scene_from_another_campaign(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    with pytest.raises(IntegrityError) as caught, database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO turns("
                "id,campaign_id,scene_id,request_id,created_by,actor_id,input_payload,"
                "request_hash,input_kind,input_text,expected_state_version"
                ") VALUES("
                ":turn,:campaign,:scene,:turn,:principal,:actor,'{}',"
                "decode(repeat('00',32),'hex'),'text','行動する',0"
                ")"
            ),
            {
                "turn": TURN_A,
                "campaign": CAMPAIGN_B,
                "scene": SCENE_A,
                "principal": PRINCIPAL_B,
                "actor": ACTOR_B,
            },
        )
    _assert_sqlstate(caught.value, "23503")


@pytest.mark.parametrize(
    "statement",
    [
        f"UPDATE actions SET result=jsonb_build_object('changed',true) WHERE id='{ACTION_A}'",
        f"DELETE FROM actions WHERE id='{ACTION_A}'",
    ],
)
def test_actions_are_append_only(database: Engine, statement: str) -> None:
    with database.begin() as connection:
        _seed_history(connection)

    with pytest.raises(DBAPIError) as caught, database.begin() as connection:
        connection.execute(text(statement))
    _assert_sqlstate(caught.value, "P0001")


@pytest.mark.parametrize(
    "statement",
    [
        f"UPDATE events SET payload=jsonb_build_object('changed',true) WHERE id='{EVENT_A}'",
        f"DELETE FROM events WHERE id='{EVENT_A}'",
    ],
)
def test_events_are_append_only(database: Engine, statement: str) -> None:
    with database.begin() as connection:
        _seed_history(connection)

    with pytest.raises(DBAPIError) as caught, database.begin() as connection:
        connection.execute(text(statement))
    _assert_sqlstate(caught.value, "P0001")


def test_constraint_failure_rolls_back_the_whole_transaction(database: Engine) -> None:
    with pytest.raises(IntegrityError) as caught, database.begin() as connection:
        _seed_campaigns(connection)
        connection.execute(
            text(
                "INSERT INTO scenes(id,campaign_id,sequence,status) "
                "VALUES(:first,:campaign,1,'active'),(:second,:campaign,2,'active')"
            ),
            {
                "first": SCENE_A,
                "second": "00000000-0000-0000-0000-000000000012",
                "campaign": CAMPAIGN_A,
            },
        )
    _assert_sqlstate(caught.value, "23505")

    with database.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM campaigns WHERE id IN (:campaign_a,:campaign_b)"),
            {"campaign_a": CAMPAIGN_A, "campaign_b": CAMPAIGN_B},
        ) == 0


def test_same_request_and_input_returns_existing_turn(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    first = _accept_from_database(database, _player_turn())
    replay = _accept_from_database(database, _player_turn())

    assert replay == first
    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 1


def test_get_response_is_scoped_to_requested_campaign(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)
    accepted = _accept_from_database(database, _player_turn())

    async def get(campaign_id: UUID) -> TurnResponse | None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            return await PostgresTurnRepository(session).get_response(
                campaign_id, accepted.turn_id
            )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        found = runner.run(get(UUID(CAMPAIGN_A)))
        missing = runner.run(get(UUID(CAMPAIGN_B)))

    assert found == accepted
    assert missing is None


def test_same_request_with_different_input_is_idempotency_conflict(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    _accept_from_database(database, _player_turn())
    with pytest.raises(IdempotencyConflictError) as caught:
        _accept_from_database(database, _player_turn("戻る"))

    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 1


@pytest.mark.parametrize(
    "tamper_statement",
    [
        "UPDATE turns SET request_hash=decode(repeat('ff',32),'hex') "
        "WHERE request_id=:request_id",
        "UPDATE turns SET input_payload=jsonb_set("
        "input_payload,'{content,text}',to_jsonb('戻る'::text)) "
        "WHERE request_id=:request_id",
    ],
    ids=["stored-hash", "stored-json"],
)
def test_replay_compares_stored_hash_and_json(
    database: Engine, tamper_statement: str
) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    _accept_from_database(database, _player_turn())

    with database.begin() as connection:
        connection.execute(text(tamper_statement), {"request_id": REQUEST_A})

    with pytest.raises(IdempotencyConflictError) as caught:
        _accept_from_database(database, _player_turn())

    assert caught.value.code == "IDEMPOTENCY_CONFLICT"


def test_different_request_while_turn_is_unresolved_is_turn_in_progress(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    _accept_from_database(database, _player_turn())
    with pytest.raises(TurnInProgressError) as caught:
        _accept_from_database(database, _player_turn(request_id=REQUEST_B))

    assert caught.value.code == "TURN_IN_PROGRESS"
    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 1


def test_different_request_while_narration_is_pending_is_turn_in_progress(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    accepted = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET resolution_status='committed',route='mechanical',"
                "committed_state_version=0,committed_at=now() WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        )

    with pytest.raises(TurnInProgressError):
        _accept_from_database(database, _player_turn(request_id=REQUEST_B))

    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET narration_status='completed',narration='完了した。' "
                "WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        )
    second = _accept_from_database(database, _player_turn(request_id=REQUEST_B))
    assert second.turn_id != accepted.turn_id


def test_llm_call_reservation_is_atomic_persistent_and_owned(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)
    accepted = _accept_from_database(database, _player_turn())
    lease = _acquire_lease_from_database(database, accepted.turn_id)
    with database.begin() as connection:
        connection.execute(
            text("UPDATE turns SET llm_call_budget=1 WHERE id=:turn"),
            {"turn": accepted.turn_id},
        )

    async def reserve(epoch: int) -> bool:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            reserved = await PostgresLLMCallRepository(session).reserve(
                accepted.turn_id,
                phase="resolution",
                worker_epoch=epoch,
            )
            await session.commit()
            return reserved

    async def reserve_twice(epoch: int) -> list[bool]:
        return list(await asyncio.gather(reserve(epoch), reserve(epoch)))

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        assert not runner.run(reserve(lease.turn.worker_epoch - 1))
        reservations = runner.run(reserve_twice(lease.turn.worker_epoch))
        assert sorted(reservations) == [False, True]
        assert not runner.run(reserve(lease.turn.worker_epoch))

    with database.connect() as connection:
        assert connection.scalar(
            text("SELECT llm_call_count FROM turns WHERE id=:turn"),
            {"turn": accepted.turn_id},
        ) == 1


def test_llm_call_reservation_rejects_expired_phase_deadline(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)
    accepted = _accept_from_database(database, _player_turn())
    lease = _acquire_lease_from_database(database, accepted.turn_id)
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET resolution_deadline=now()-interval '1 second' "
                "WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        )

    async def reserve() -> bool:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            reserved = await PostgresLLMCallRepository(session).reserve(
                accepted.turn_id,
                phase="resolution",
                worker_epoch=lease.turn.worker_epoch,
            )
            await session.commit()
            return reserved

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        assert not runner.run(reserve())

    with database.connect() as connection:
        assert connection.scalar(
            text("SELECT llm_call_count FROM turns WHERE id=:turn"),
            {"turn": accepted.turn_id},
        ) == 0


def test_resolution_lease_attempts_share_a_fixed_deadline(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)
    accepted = _accept_from_database(database, _player_turn())

    first = _acquire_lease_from_database(database, accepted.turn_id)
    with database.connect() as connection:
        first_state = connection.execute(
            text(
                "SELECT resolution_attempt_count,resolution_started_at,"
                "resolution_deadline FROM turns WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        ).one()
    with database.begin() as connection:
        connection.execute(
            text("UPDATE turns SET lease_until=now()-interval '1 second' WHERE id=:turn"),
            {"turn": accepted.turn_id},
        )

    async def acquire_again() -> Lease | None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            value = await PostgresTurnRepository(session).acquire_lease(
                accepted.turn_id,
                lease_seconds=60,
                max_attempts=2,
                deadline_seconds=120,
            )
            await session.commit()
            return value

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        second = runner.run(acquire_again())
    assert second is not None
    assert second.turn.worker_epoch == first.turn.worker_epoch + 1

    with database.connect() as connection:
        second_state = connection.execute(
            text(
                "SELECT resolution_attempt_count,resolution_started_at,"
                "resolution_deadline FROM turns WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        ).one()
    assert second_state[0] == 2
    assert second_state[1:] == first_state[1:]

    with database.begin() as connection:
        connection.execute(
            text("UPDATE turns SET lease_until=now()-interval '1 second' WHERE id=:turn"),
            {"turn": accepted.turn_id},
        )
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        cleanup = runner.run(acquire_again())
    assert cleanup is not None
    assert cleanup.terminal_cleanup
    with database.connect() as connection:
        assert connection.scalar(
            text("SELECT resolution_attempt_count FROM turns WHERE id=:turn"),
            {"turn": accepted.turn_id},
        ) == 2


def test_canonical_snapshot_locks_campaign_before_typed_reads(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','出口の痕跡が見える。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    async def snapshot() -> tuple[object, list[str]]:
        url = database.url.render_as_string(hide_password=False)
        engine = create_async_engine(url)
        statements: list[str] = []

        def record(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            statements.append(" ".join(statement.lower().split()))

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                value = await PostgresCanonicalRepository(session).snapshot(UUID(CAMPAIGN_A))
                await session.commit()
                return value, statements
        finally:
            await engine.dispose()

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        value, statements = runner.run(snapshot())

    assert value.state_version == 0
    assert value.skill_checks == (
        {
            "campaign_id": UUID(CAMPAIGN_A),
            "scene_id": UUID(SCENE_A),
            "check_ref": "observe_room",
            "skill_ref": "perception",
            "difficulty": "normal",
            "target_id": None,
            "public_description": "出口の痕跡が見える。",
        },
    )
    assert "from campaigns" in statements[0]
    assert "for update" in statements[0]
    assert any("from mvp_characters" in statement for statement in statements[1:])


def test_canonical_snapshot_waits_for_campaign_update_and_reads_one_version(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)

    snapshot_attempted = Event()

    def read_snapshot() -> CanonicalSnapshot:
        async def read() -> CanonicalSnapshot:
            url = database.url.render_as_string(hide_password=False)
            engine = create_async_engine(url)

            def record(
                _connection: Any,
                _cursor: Any,
                statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                normalized = " ".join(statement.lower().split())
                if "from campaigns" in normalized and "for update" in normalized:
                    snapshot_attempted.set()

            event.listen(engine.sync_engine, "before_cursor_execute", record)
            try:
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as session:
                    value = await PostgresCanonicalRepository(session).snapshot(
                        UUID(CAMPAIGN_A)
                    )
                    await session.commit()
                    return value
            finally:
                await engine.dispose()

        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(read())

    connection = database.connect()
    transaction = connection.begin()
    pool = ThreadPoolExecutor(max_workers=1)
    future = None
    try:
        connection.execute(
            text("SELECT id FROM campaigns WHERE id=:campaign FOR UPDATE"),
            {"campaign": CAMPAIGN_A},
        )
        future = pool.submit(read_snapshot)
        assert snapshot_attempted.wait(10)
        connection.execute(
            text(
                "UPDATE mvp_characters SET current_hp=7 "
                "WHERE campaign_id=:campaign AND entity_id=:actor"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text("UPDATE campaigns SET state_version=1 WHERE id=:campaign"),
            {"campaign": CAMPAIGN_A},
        )
        transaction.commit()
        snapshot = future.result(timeout=10)
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        pool.shutdown(wait=True)

    assert snapshot.state_version == 1
    actor = next(row for row in snapshot.characters if row["entity_id"] == UUID(ACTOR_A))
    assert actor["current_hp"] == 7


def test_concurrent_same_request_creates_one_turn(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    async def accept_concurrently() -> tuple[TurnResponse, TurnResponse]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            barrier = asyncio.Barrier(2)

            async def accept() -> TurnResponse:
                async with factory() as session:
                    response = await _SynchronizedTurnRepository(session, barrier).add(
                        UUID(CAMPAIGN_A), UUID(PRINCIPAL_A), _player_turn(), 3
                    )
                    await session.commit()
                    return response

            first, second = await asyncio.gather(accept(), accept())
        return first, second

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        first, second = runner.run(accept_concurrently())

    assert second == first
    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 1


def test_concurrent_same_request_with_different_input_conflicts(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    async def accept_concurrently() -> list[TurnResponse | BaseException]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            barrier = asyncio.Barrier(2)

            async def accept(turn: PlayerTurnInput) -> TurnResponse:
                async with factory() as session:
                    response = await _SynchronizedTurnRepository(session, barrier).add(
                        UUID(CAMPAIGN_A), UUID(PRINCIPAL_A), turn, 3
                    )
                    await session.commit()
                    return response

            return list(
                await asyncio.gather(
                    accept(_player_turn("進む")),
                    accept(_player_turn("戻る")),
                    return_exceptions=True,
                )
            )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        results = runner.run(accept_concurrently())

    assert sum(isinstance(result, TurnResponse) for result in results) == 1
    conflicts = [
        result for result in results if isinstance(result, IdempotencyConflictError)
    ]
    assert len(conflicts) == 1
    assert conflicts[0].code == "IDEMPOTENCY_CONFLICT"
    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 1


def test_replay_returns_current_committed_turn_response(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    accepted = _accept_from_database(database, _player_turn())

    with database.begin() as connection:
        _complete_turn_response(connection, accepted.turn_id)
        connection.execute(
            text("UPDATE scenes SET status='closed' WHERE id=:scene"),
            {"scene": SCENE_A},
        )
        connection.execute(
            text("UPDATE campaigns SET state_version=1 WHERE id=:campaign"),
            {"campaign": CAMPAIGN_A},
        )

    replay = _accept_from_database(database, _player_turn())

    assert replay.turn_id == accepted.turn_id
    assert replay.route == "mechanical"
    assert replay.resolution_status == "committed"
    assert replay.narration_status == "completed"
    assert replay.committed_state_version == 0
    assert replay.narration == "扉を開けた。"
    assert [(choice.id, choice.label) for choice in replay.choices] == [
        (UUID(CHOICE_A), "先へ進む")
    ]
    assert len(replay.action_results) == 1
    assert replay.action_results[0].action_id == UUID(ACTION_A)
    assert replay.action_results[0].ordinal == 1
    assert replay.action_results[0].result.kind == "not_applicable"
    assert replay.action_results[0].result.reason == "rule_precondition"


def test_replay_reads_turn_and_children_from_one_snapshot(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    accepted = _accept_from_database(database, _player_turn())
    projection_started = Event()
    release_projection = Event()

    def pause_before_projection(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if not projection_started.is_set() and "turn_choices" in statement:
            projection_started.set()
            assert release_projection.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _accept_with_sql_listener,
            database,
            _player_turn(),
            pause_before_projection,
        )
        assert projection_started.wait(10)
        try:
            with database.begin() as connection:
                _complete_turn_response(connection, accepted.turn_id)
        finally:
            release_projection.set()
        replay = future.result(timeout=10)

    assert replay.resolution_status == "committed"
    assert replay.narration_status == "completed"
    assert len(replay.action_results) == 1
    assert [(choice.id, choice.label) for choice in replay.choices] == [
        (UUID(CHOICE_A), "先へ進む")
    ]


def test_accept_selects_active_scene_after_campaign_lock(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    lock_attempted = Event()

    def record_campaign_lock(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if "select" in normalized and "from campaigns" in normalized and "for update" in normalized:
            lock_attempted.set()

    connection = database.connect()
    transaction = connection.begin()
    pool = ThreadPoolExecutor(max_workers=1)
    future = None
    try:
        connection.execute(
            text("SELECT id FROM campaigns WHERE id=:campaign FOR UPDATE"),
            {"campaign": CAMPAIGN_A},
        )
        future = pool.submit(
            _accept_with_sql_listener,
            database,
            _player_turn(),
            record_campaign_lock,
        )
        assert lock_attempted.wait(10)
        connection.execute(
            text("UPDATE scenes SET status='closed' WHERE id=:scene"),
            {"scene": SCENE_A},
        )
        connection.execute(
            text(
                "INSERT INTO scenes(id,campaign_id,sequence,status) "
                "VALUES(:scene,:campaign,2,'active')"
            ),
            {"scene": SCENE_B, "campaign": CAMPAIGN_A},
        )
        transaction.commit()
        response = future.result(timeout=10)
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        pool.shutdown(wait=True)

    with database.connect() as verification:
        scene_id = verification.scalar(
            text("SELECT scene_id FROM turns WHERE id=:turn"),
            {"turn": response.turn_id},
        )
    assert scene_id == UUID(SCENE_B)


def test_unresolved_turn_without_active_scene_is_turn_in_progress(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text("UPDATE scenes SET status='closed' WHERE id=:scene"),
            {"scene": SCENE_A},
        )

    with pytest.raises(TurnInProgressError) as caught:
        _accept_from_database(database, _player_turn(request_id=REQUEST_B))

    assert caught.value.code == "TURN_IN_PROGRESS"


def test_inactive_campaign_member_cannot_replay_turn(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    accepted = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE campaign_members SET active=false "
                "WHERE campaign_id=:campaign AND principal_id=:principal"
            ),
            {"campaign": CAMPAIGN_A, "principal": PRINCIPAL_A},
        )

    with pytest.raises(AuthorizationError):
        _accept_from_database(database, _player_turn())

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 1
        assert connection.scalar(
            text("SELECT id FROM turns WHERE id=:turn"), {"turn": accepted.turn_id}
        ) == accepted.turn_id


@pytest.mark.parametrize(
    "actor_mutation",
    [
        "UPDATE entities SET controller_id=:other WHERE id=:actor",
        "UPDATE entities SET archived_at=now() WHERE id=:actor",
    ],
    ids=["different-controller", "archived-actor"],
)
def test_new_turn_requires_current_actor_control(
    database: Engine, actor_mutation: str
) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)
        connection.execute(
            text(
                "INSERT INTO campaign_members(campaign_id,principal_id,role) "
                "VALUES(:campaign,:principal,'player')"
            ),
            {"campaign": CAMPAIGN_A, "principal": PRINCIPAL_B},
        )
        connection.execute(
            text(actor_mutation),
            {"actor": ACTOR_A, "other": PRINCIPAL_B},
        )

    with pytest.raises(AuthorizationError):
        _accept_from_database(database, _player_turn())

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 0


def test_actor_control_is_rechecked_after_campaign_lock(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    lock_attempted = Event()

    def record_campaign_lock(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if "select" in normalized and "from campaigns" in normalized and "for update" in normalized:
            lock_attempted.set()

    connection = database.connect()
    transaction = connection.begin()
    pool = ThreadPoolExecutor(max_workers=1)
    future = None
    try:
        connection.execute(
            text("SELECT id FROM campaigns WHERE id=:campaign FOR UPDATE"),
            {"campaign": CAMPAIGN_A},
        )
        future = pool.submit(
            _accept_with_sql_listener,
            database,
            _player_turn(),
            record_campaign_lock,
        )
        assert lock_attempted.wait(10)
        connection.execute(
            text(
                "UPDATE campaign_members SET active=false "
                "WHERE campaign_id=:campaign AND principal_id=:principal"
            ),
            {"campaign": CAMPAIGN_A, "principal": PRINCIPAL_A},
        )
        transaction.commit()
        with pytest.raises(AuthorizationError):
            future.result(timeout=10)
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        pool.shutdown(wait=True)

    with database.connect() as verification:
        assert verification.scalar(text("SELECT count(*) FROM turns")) == 0


def test_stale_state_version_preserves_existing_choices(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    source = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        _complete_turn_response(connection, source.turn_id)
        connection.execute(
            text("UPDATE campaigns SET state_version=1 WHERE id=:campaign"),
            {"campaign": CAMPAIGN_A},
        )

    with pytest.raises(StateVersionConflictError) as caught:
        _accept_from_database(database, _player_turn(request_id=REQUEST_B))

    assert caught.value.code == "STATE_VERSION_CONFLICT"
    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM turn_choices "
                "WHERE id=:choice AND invalidated_at IS NULL"
            ),
            {"choice": CHOICE_A},
        ) == 1


@pytest.mark.parametrize("choice_id", [None, CHOICE_A], ids=["text", "choice"])
def test_new_turn_invalidates_existing_choices(
    database: Engine, choice_id: str | None
) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    source = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        _complete_turn_response(connection, source.turn_id)

    accepted = _accept_from_database(
        database,
        _player_turn(request_id=REQUEST_B, choice_id=choice_id),
    )

    with database.connect() as connection:
        selected_choice = connection.scalar(
            text("SELECT selected_choice_id FROM turns WHERE id=:turn"),
            {"turn": accepted.turn_id},
        )
        active_choices = connection.scalar(
            text(
                "SELECT count(*) FROM turn_choices "
                "WHERE campaign_id=:campaign AND actor_id=:actor AND invalidated_at IS NULL"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
    assert selected_choice == (None if choice_id is None else UUID(choice_id))
    assert active_choices == 0


@pytest.mark.parametrize(
    "choice_mutation",
    [
        "UPDATE turn_choices SET invalidated_at=now() WHERE id=:choice",
        "UPDATE turn_choices SET state_version=1 WHERE id=:choice",
        "UPDATE turns SET resolution_status='failed',committed_state_version=NULL,"
        "committed_at=NULL WHERE id=:turn",
    ],
    ids=["invalidated", "stale-version", "invalid-source-turn"],
)
def test_unavailable_choice_is_rejected_before_turn_insert(
    database: Engine, choice_mutation: str
) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    source = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        _complete_turn_response(connection, source.turn_id)
        connection.execute(
            text(choice_mutation),
            {"choice": CHOICE_A, "turn": source.turn_id},
        )

    with pytest.raises(ChoiceNotAvailableError) as caught:
        _accept_from_database(
            database,
            _player_turn(request_id=REQUEST_B, choice_id=CHOICE_A),
        )

    assert caught.value.code == "CHOICE_NOT_AVAILABLE"
    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 1


def test_choice_for_different_actor_is_rejected_as_unavailable(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    source = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        _complete_turn_response(connection, source.turn_id)
        connection.execute(
            text(
                "INSERT INTO entities(id,campaign_id,kind,controller_id) "
                "VALUES(:actor,:campaign,'pc',:principal)"
            ),
            {"actor": ACTOR_C, "campaign": CAMPAIGN_A, "principal": PRINCIPAL_A},
        )
        connection.execute(
            text("UPDATE turn_choices SET actor_id=:actor WHERE id=:choice"),
            {"actor": ACTOR_C, "choice": CHOICE_A},
        )

    with pytest.raises(ChoiceNotAvailableError) as caught:
        _accept_from_database(
            database,
            _player_turn(request_id=REQUEST_B, choice_id=CHOICE_A),
        )

    assert caught.value.code == "CHOICE_NOT_AVAILABLE"
    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM turns")) == 1


@pytest.mark.parametrize(
    (
        "source_campaign",
        "source_scene",
        "source_principal",
        "source_actor",
        "narration_status",
        "expected_error",
    ),
    [
        (CAMPAIGN_B, SCENE_B, PRINCIPAL_B, ACTOR_B, "completed", ChoiceNotAvailableError),
        (CAMPAIGN_A, SCENE_B, PRINCIPAL_A, ACTOR_A, "completed", ChoiceNotAvailableError),
        (CAMPAIGN_A, SCENE_A, PRINCIPAL_A, ACTOR_A, "pending", TurnInProgressError),
    ],
    ids=["different-campaign", "different-scene", "unnarrated-source"],
)
def test_choice_scope_and_narration_are_required(
    database: Engine,
    source_campaign: str,
    source_scene: str,
    source_principal: str,
    source_actor: str,
    narration_status: str,
    expected_error: type[Exception],
) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)
        if source_scene == SCENE_B:
            connection.execute(
                text(
                    "INSERT INTO scenes(id,campaign_id,sequence,status) "
                    "VALUES(:scene,:campaign,:sequence,:status)"
                ),
                {
                    "scene": source_scene,
                    "campaign": source_campaign,
                    "sequence": 1 if source_campaign == CAMPAIGN_B else 2,
                    "status": "active" if source_campaign == CAMPAIGN_B else "closed",
                },
            )
        _insert_choice_source(
            connection,
            campaign_id=source_campaign,
            scene_id=source_scene,
            principal_id=source_principal,
            actor_id=source_actor,
            narration_status=narration_status,
        )

    with pytest.raises(expected_error) as caught:
        _accept_from_database(
            database,
            _player_turn(choice_id=CHOICE_C),
        )

    assert caught.value.code in {"CHOICE_NOT_AVAILABLE", "TURN_IN_PROGRESS"}
    with database.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM turns "
                "WHERE campaign_id=:campaign AND request_id=:request"
            ),
            {"campaign": CAMPAIGN_A, "request": REQUEST_A},
        ) == 0
        assert connection.scalar(
            text("SELECT invalidated_at FROM turn_choices WHERE id=:choice"),
            {"choice": CHOICE_C},
        ) is None


@pytest.mark.parametrize(
    "invalid_bundle",
    [
        "ordinal-gap",
        "too-many-actions",
        "different-scene",
        "narration-version",
        "missing-narration-result",
    ],
)
def test_commit_resolution_rejects_inconsistent_bundle_before_writes(
    database: Engine,
    invalid_bundle: str,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)

    options: dict[str, Any] = {}
    if invalid_bundle == "ordinal-gap":
        options["first_ordinal"] = 2
    elif invalid_bundle == "too-many-actions":
        options["action_count"] = 4
    elif invalid_bundle == "different-scene":
        options["scene_id"] = SCENE_B
    elif invalid_bundle == "narration-version":
        options["narration_version"] = 1
    elif invalid_bundle == "missing-narration-result":
        options["include_narration_results"] = False
    bundle = _resolution_bundle(accepted.turn_id, **options)

    with pytest.raises(InvalidCommitBundleError):
        _commit_resolution_from_database(database, bundle)

    with database.connect() as connection:
        campaign = connection.execute(
            text(
                "SELECT state_version,event_sequence FROM campaigns "
                "WHERE id=:campaign"
            ),
            {"campaign": CAMPAIGN_A},
        ).one()
        turn = connection.execute(
            text(
                "SELECT resolution_status,committed_state_version FROM turns "
                "WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        ).one()
        assert tuple(campaign) == (0, 0)
        assert tuple(turn) == ("resolving", None)
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.scalar(text("SELECT count(*) FROM events")) == 0
        assert connection.scalar(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:target"
            ),
            {"campaign": CAMPAIGN_A, "target": ACTOR_C},
        ) == 10


@pytest.mark.parametrize(
    "invalid_update",
    [
        "missing-character",
        "hp-before-mismatch",
        "max-hp-mismatch",
        "inventory-before-mismatch",
    ],
)
def test_commit_resolution_rejects_invalid_canonical_update(
    database: Engine,
    invalid_update: str,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)

    if invalid_update in {"missing-character", "hp-before-mismatch"}:
        target_id = UUID(int=99999) if invalid_update == "missing-character" else UUID(ACTOR_C)
        hp_before = 10 if invalid_update == "missing-character" else 9
        action = _attack_action(accepted.turn_id)
        result = action.result
        assert isinstance(result, AppliedResult)
        result = result.model_copy(
            update={
                "state_changes": [
                    DamageApplied(
                        kind="damage_applied",
                        target_id=target_id,
                        amount=3,
                        hp_before=hp_before,
                        hp_after=hp_before - 3,
                    )
                ]
            }
        )
        action = ActionRecord(command=action.command, result=result)
        bundle = _resolution_bundle(
            accepted.turn_id,
            narration_version=1,
            action=action,
        )
    else:
        with database.begin() as connection:
            if invalid_update == "max-hp-mismatch":
                connection.execute(
                    text(
                        "UPDATE mvp_characters SET current_hp=5 "
                        "WHERE campaign_id=:campaign AND entity_id=:actor"
                    ),
                    {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
                )
            else:
                connection.execute(
                    text(
                        "UPDATE mvp_inventory SET quantity=1 "
                        "WHERE campaign_id=:campaign AND owner_id=:actor AND item_id=:item"
                    ),
                    {"campaign": CAMPAIGN_A, "actor": ACTOR_A, "item": ITEM_A},
                )
        action = _healing_action(accepted.turn_id)
        if invalid_update == "max-hp-mismatch":
            result = action.result
            assert isinstance(result, AppliedResult)
            result = result.model_copy(
                update={
                    "state_changes": [
                        HealingApplied(
                            kind="healing_applied",
                            target_id=UUID(ACTOR_A),
                            amount=4,
                            hp_before=5,
                            hp_after=9,
                            max_hp=11,
                        ),
                        result.state_changes[1],
                    ]
                }
            )
            action = ActionRecord(command=action.command, result=result)
        bundle = _resolution_bundle(
            accepted.turn_id,
            narration_version=1,
            action=action,
        )

    with pytest.raises(InvalidCommitBundleError):
        _commit_resolution_from_database(database, bundle)

    with database.connect() as connection:
        campaign = connection.execute(
            text(
                "SELECT state_version,event_sequence FROM campaigns "
                "WHERE id=:campaign"
            ),
            {"campaign": CAMPAIGN_A},
        ).one()
        assert tuple(campaign) == (0, 0)
        assert connection.scalar(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:target"
            ),
            {"campaign": CAMPAIGN_A, "target": ACTOR_C},
        ) == 10
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.scalar(text("SELECT count(*) FROM events")) == 0


@pytest.mark.parametrize("state_changed", [False, True])
def test_commit_resolution_atomically_persists_valid_bundle(
    database: Engine,
    state_changed: bool,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)
    bundle = _resolution_bundle(
        accepted.turn_id,
        narration_version=int(state_changed),
        attack=state_changed,
    )

    version = _commit_resolution_from_database(database, bundle)

    assert version == int(state_changed)
    with database.connect() as connection:
        assert tuple(
            connection.execute(
                text(
                    "SELECT state_version,event_sequence FROM campaigns "
                    "WHERE id=:campaign"
                ),
                {"campaign": CAMPAIGN_A},
            ).one()
        ) == (version, len(_event_types(bundle)))
        turn = connection.execute(
            text(
                "SELECT resolution_status,route,committed_state_version,narration_input "
                "FROM turns WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        ).mappings().one()
        assert (turn["resolution_status"], turn["route"], turn["committed_state_version"]) == (
            "committed",
            "mechanical",
            version,
        )
        assert turn["narration_input"]["committed_state_version"] == version
        assert connection.scalar(text("SELECT count(*) FROM actions")) == len(bundle.actions)
        events = connection.execute(
            text(
                "SELECT sequence,state_version,type FROM events "
                "WHERE campaign_id=:campaign ORDER BY sequence"
            ),
            {"campaign": CAMPAIGN_A},
        ).all()
        assert events == [
            (index, version, event_type)
            for index, event_type in enumerate(_event_types(bundle), start=1)
        ]
        assert connection.scalar(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:target"
            ),
            {"campaign": CAMPAIGN_A, "target": ACTOR_C},
        ) == (7 if state_changed else 10)


def test_commit_resolution_persists_healing_and_item_consumption_together(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "UPDATE mvp_characters SET current_hp=5 "
                "WHERE campaign_id=:campaign AND entity_id=:actor"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)
    bundle = _resolution_bundle(
        accepted.turn_id,
        narration_version=1,
        healing=True,
    )

    assert _commit_resolution_from_database(database, bundle) == 1

    with database.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:actor"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        ) == 9
        assert connection.scalar(
            text(
                "SELECT quantity FROM mvp_inventory "
                "WHERE campaign_id=:campaign AND owner_id=:actor AND item_id=:item"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A, "item": ITEM_A},
        ) == 1
        assert connection.execute(
            text(
                "SELECT type FROM events WHERE campaign_id=:campaign ORDER BY sequence"
            ),
            {"campaign": CAMPAIGN_A},
        ).scalars().all() == [
            "DiceRolled",
            "HealingApplied",
            "ItemConsumed",
            "ActionResolved",
        ]


@pytest.mark.parametrize(
    "mismatch",
    ["command-parent", "rng-count"],
)
def test_commit_resolution_rejects_contract_mismatches(
    database: Engine,
    mismatch: str,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)
    bundle = _resolution_bundle(accepted.turn_id)

    if mismatch == "command-parent":
        action = ActionRecord(
            command=bundle.actions[0].command.model_copy(
                update={"turn_id": UUID(int=9999)}
            ),
            result=bundle.actions[0].result,
        )
        bundle = replace(bundle, actions=(action,))
    elif mismatch == "rng-count":
        action = replace(bundle.actions[0], rng=())
        bundle = replace(bundle, actions=(action,))

    with pytest.raises(InvalidCommitBundleError):
        _commit_resolution_from_database(database, bundle)

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.scalar(text("SELECT count(*) FROM events")) == 0


def test_commit_resolution_rolls_back_every_write_when_event_insert_fails(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "UPDATE mvp_characters SET current_hp=5 "
                "WHERE campaign_id=:campaign AND entity_id=:actor"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)
    bundle = _resolution_bundle(
        accepted.turn_id,
        narration_version=1,
        healing=True,
    )
    event_ids = tuple(UUID(int=9000 + index) for index in range(len(_event_types(bundle))))
    conflicting_event_id = event_ids[0]
    with database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO events("
                "id,campaign_id,sequence,state_version,type,schema_version,payload"
                ") VALUES(:event,:campaign,1,0,'ExistingEvent',1,'{}')"
            ),
            {"event": conflicting_event_id, "campaign": CAMPAIGN_B},
        )

    with pytest.raises(IntegrityError):
        _commit_resolution_from_database(database, bundle, event_ids=iter(event_ids))

    with database.connect() as connection:
        assert tuple(
            connection.execute(
                text(
                    "SELECT state_version,event_sequence FROM campaigns "
                    "WHERE id=:campaign"
                ),
                {"campaign": CAMPAIGN_A},
            ).one()
        ) == (0, 0)
        assert tuple(
            connection.execute(
                text(
                    "SELECT resolution_status,committed_state_version,narration_input "
                    "FROM turns WHERE id=:turn"
                ),
                {"turn": accepted.turn_id},
            ).one()
        ) == ("resolving", None, None)
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM events WHERE campaign_id=:campaign"),
            {"campaign": CAMPAIGN_A},
        ) == 0
        assert connection.scalar(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:target"
            ),
            {"campaign": CAMPAIGN_A, "target": ACTOR_A},
        ) == 5
        assert connection.scalar(
            text(
                "SELECT quantity FROM mvp_inventory "
                "WHERE campaign_id=:campaign AND owner_id=:actor AND item_id=:item"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A, "item": ITEM_A},
        ) == 2


@pytest.mark.parametrize("invalid_owner", ["stale-epoch", "expired-lease", "stale-version"])
def test_commit_resolution_rejects_stale_ownership(
    database: Engine,
    invalid_owner: str,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)
    bundle = _resolution_bundle(accepted.turn_id)

    if invalid_owner == "stale-epoch":
        bundle = replace(bundle, worker_epoch=0)
    elif invalid_owner == "expired-lease":
        with database.begin() as connection:
            connection.execute(
                text("UPDATE turns SET lease_until=now()-interval '1 second' WHERE id=:turn"),
                {"turn": accepted.turn_id},
            )
    elif invalid_owner == "stale-version":
        with database.begin() as connection:
            connection.execute(
                text("UPDATE campaigns SET state_version=1 WHERE id=:campaign"),
                {"campaign": CAMPAIGN_A},
            )

    error = StateVersionConflictError if invalid_owner == "stale-version" else RuntimeError
    with pytest.raises(error):
        _commit_resolution_from_database(database, bundle)

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.scalar(text("SELECT count(*) FROM events")) == 0


def test_commit_resolution_rejects_lease_that_expires_while_waiting_for_lock(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET resolution_status='resolving',route='mechanical',"
                "worker_epoch=1,lease_until=clock_timestamp()+interval '2 seconds' "
                "WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        )

    blocker = database.connect()
    transaction = blocker.begin()
    blocker.execute(
        text("SELECT id FROM campaigns WHERE id=:campaign FOR UPDATE"),
        {"campaign": CAMPAIGN_A},
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                _commit_resolution_from_database,
                database,
                _resolution_bundle(accepted.turn_id),
            )
            waiting = False
            for _ in range(100):
                with database.connect() as observer:
                    waiting = bool(
                        observer.scalar(
                            text(
                                "SELECT EXISTS("
                                "SELECT 1 FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND pid<>pg_backend_pid() "
                                "AND wait_event_type='Lock' "
                                "AND query LIKE '%FROM campaigns%FOR UPDATE%'"
                                ")"
                            )
                        )
                    )
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting, "commit transaction did not reach the campaign lock"
            time.sleep(2.1)
            transaction.commit()
            with pytest.raises(RuntimeError, match="lease"):
                future.result(timeout=10)
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.scalar(text("SELECT count(*) FROM events")) == 0


def test_commit_resolution_rechecks_lease_after_authorization_lock_wait(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET resolution_status='resolving',route='mechanical',"
                "worker_epoch=1,lease_until=clock_timestamp()+interval '2 seconds' "
                "WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        )

    blocker = database.connect()
    transaction = blocker.begin()
    blocker.execute(
        text(
            "SELECT principal_id FROM campaign_members "
            "WHERE campaign_id=:campaign AND principal_id=:principal FOR UPDATE"
        ),
        {"campaign": CAMPAIGN_A, "principal": PRINCIPAL_A},
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                _commit_resolution_from_database,
                database,
                _resolution_bundle(accepted.turn_id),
            )
            waiting = False
            for _ in range(100):
                with database.connect() as observer:
                    waiting = bool(
                        observer.scalar(
                            text(
                                "SELECT EXISTS("
                                "SELECT 1 FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND pid<>pg_backend_pid() "
                                "AND wait_event_type='Lock' "
                                "AND query LIKE '%campaign_members%'"
                                ")"
                            )
                        )
                    )
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting, "commit transaction did not reach the authorization lock"
            time.sleep(2.1)
            transaction.commit()
            with pytest.raises(RuntimeError, match="lease"):
                future.result(timeout=10)
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.scalar(text("SELECT count(*) FROM events")) == 0


def test_commit_resolution_returns_existing_commit_without_duplicate_writes(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)
    bundle = _resolution_bundle(accepted.turn_id)

    assert _commit_resolution_from_database(database, bundle) == 0
    assert _commit_resolution_from_database(database, bundle) == 0

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 1
        assert connection.scalar(text("SELECT count(*) FROM events")) == len(
            _event_types(bundle)
        )


def test_narration_lease_rejects_old_owner_after_recovery(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    source = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET resolution_status='committed',route='narrative',"
                "committed_state_version=0,committed_at=now() WHERE id=:turn"
            ),
            {"turn": source.turn_id},
        )
    first = _acquire_narration_lease_from_database(database, source.turn_id)
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET narration_lease_until=now()-interval '1 second' "
                "WHERE id=:turn"
            ),
            {"turn": source.turn_id},
        )
    second = _acquire_narration_lease_from_database(database, source.turn_id)

    assert second.worker_epoch == first.worker_epoch + 1
    assert not _save_narration_from_database(
        database,
        source.turn_id,
        (),
        worker_epoch=first.worker_epoch,
    )
    assert _save_narration_from_database(
        database,
        source.turn_id,
        (),
        worker_epoch=second.worker_epoch,
    )


def test_narration_save_is_atomic_and_appends_event(database: Engine) -> None:
    event_id = uuid4()
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    source = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET resolution_status='committed',route='mechanical',"
                "committed_state_version=0,committed_at=now() WHERE id=:turn"
            ),
            {"turn": source.turn_id},
        )
    lease = _acquire_narration_lease_from_database(database, source.turn_id)
    saved = _save_narration_from_database(
        database,
        source.turn_id,
        (ChoiceDraft(UUID(CHOICE_C), 1, "周囲を見回す"),),
        worker_epoch=lease.worker_epoch,
        event_id=event_id,
    )

    assert saved
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT narration_status,narration,narration_lease_until "
                "FROM turns WHERE id=:turn"
            ),
            {"turn": source.turn_id},
        ).one() == ("completed", "遅れて届いた描写", None)
        assert connection.scalar(
            text("SELECT count(*) FROM turn_choices WHERE source_turn_id=:turn"),
            {"turn": source.turn_id},
        ) == 1
        event_row = connection.execute(
            text(
                "SELECT id,sequence,state_version,type,payload FROM events "
                "WHERE turn_id=:turn"
            ),
            {"turn": source.turn_id},
        ).mappings().one()
        assert event_row["id"] == event_id
        assert event_row["sequence"] == 1
        assert event_row["state_version"] == 0
        assert event_row["type"] == "GMNarrationGenerated"
        assert event_row["payload"] == {
            "fallback": False,
            "narration": "遅れて届いた描写",
        }
        assert connection.scalar(
            text("SELECT event_sequence FROM campaigns WHERE id=:campaign"),
            {"campaign": CAMPAIGN_A},
        ) == 1

    assert not _save_narration_from_database(
        database,
        source.turn_id,
        (),
        worker_epoch=lease.worker_epoch,
    )


def test_not_applied_narration_choices_use_current_campaign_version(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    source = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE campaigns SET state_version=2 WHERE id=:campaign"
            ),
            {"campaign": CAMPAIGN_A},
        )
        connection.execute(
            text(
                "UPDATE turns SET resolution_status='not_applied',route='mechanical' "
                "WHERE id=:turn"
            ),
            {"turn": source.turn_id},
        )
    lease = _acquire_narration_lease_from_database(database, source.turn_id)

    assert _save_narration_from_database(
        database,
        source.turn_id,
        (ChoiceDraft(UUID(CHOICE_C), 1, "言い換える"),),
        worker_epoch=lease.worker_epoch,
    )
    with database.connect() as connection:
        assert connection.scalar(
            text("SELECT state_version FROM turn_choices WHERE id=:choice"),
            {"choice": CHOICE_C},
        ) == 2
        assert connection.scalar(
            text("SELECT state_version FROM events WHERE turn_id=:turn"),
            {"turn": source.turn_id},
        ) == 2


def test_narration_save_rolls_back_status_choices_and_event(database: Engine) -> None:
    with database.begin() as connection:
        _seed_members_entities_and_scene(connection)

    source = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET resolution_status='committed',route='mechanical',"
                "committed_state_version=0,committed_at=now() WHERE id=:turn"
            ),
            {"turn": source.turn_id},
        )
    lease = _acquire_narration_lease_from_database(database, source.turn_id)

    async def fail_save() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            try:
                await PostgresNarrationRepository(session).save_conditionally(
                    UUID(CAMPAIGN_A),
                    source.turn_id,
                    lease.worker_epoch,
                    "保存されない描写",
                    (ChoiceDraft(UUID(CHOICE_C), 1, ""),),
                )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    with pytest.raises(IntegrityError), asyncio.Runner(
        loop_factory=asyncio.SelectorEventLoop
    ) as runner:
        runner.run(fail_save())

    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT narration_status,narration FROM turns WHERE id=:turn"
            ),
            {"turn": source.turn_id},
        ).one() == ("generating", None)
        assert connection.scalar(
            text("SELECT count(*) FROM turn_choices WHERE source_turn_id=:turn"),
            {"turn": source.turn_id},
        ) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM events WHERE turn_id=:turn"),
            {"turn": source.turn_id},
        ) == 0
        assert connection.scalar(
            text("SELECT event_sequence FROM campaigns WHERE id=:campaign"),
            {"campaign": CAMPAIGN_A},
        ) == 0


@pytest.mark.parametrize(
    ("roll", "outcome", "total", "narration_text", "choice_label"),
    [
        (
            10,
            "success",
            12,
            "注意深く見回すと、床に新しい足跡が見つかった。",
            "足跡を追う",
        ),
        (
            1,
            "failure",
            3,
            "注意深く見回したが、新しい痕跡は見つからなかった。",
            "別の場所を調べる",
        ),
    ],
)
def test_fake_llm_skill_check_round_trip_reopens_turn_acceptance(
    database: Engine,
    roll: int,
    outcome: str,
    total: int,
    narration_text: str,
    choice_label: str,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal',"
                "'床に新しい足跡が残っている。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            assert lower <= roll <= upper
            return roll

    async def round_trip() -> tuple[
        dict[str, Any], int, dict[str, Any], tuple[str, str]
    ]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            authorization = PostgresAuthorizationPolicy(factory)

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            turn_service = TurnService(
                authorization,
                unit_of_work_factory,
                RuntimePolicy(3, 1, 3),
            )
            query_service = TurnQueryService(authorization, unit_of_work_factory)
            principal = AuthenticatedPrincipal(
                principal_id=UUID(PRINCIPAL_A),
                issuer="integration-test",
                subject="player-a",
                authenticated_at=datetime.now(UTC),
                auth_context=frozenset(),
            )

            async def authenticate() -> AuthenticatedPrincipal:
                return principal

            app = create_app(
                turn_service=turn_service,
                turn_query_service=query_service,
                principal_provider=authenticate,
            )
            intent_transport = ScriptedFakeTransport(
                [
                    {
                        "kind": "action_plan",
                        "actions": [
                            {
                                "kind": "skill_check",
                                "skill_ref": "perception",
                                "objective": "周囲の痕跡を見つける",
                                "target_ref": None,
                            }
                        ],
                    }
                ]
            )
            narration_transport = ScriptedFakeTransport(
                [
                    {
                        "narration": narration_text,
                        "choices": [{"label": choice_label}],
                    }
                ]
            )
            resolution_worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                intent_transport,
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
                action_id_factory=lambda: UUID(ACTION_A),
                rng_source="seeded_test",
            )
            narration_worker = NarrationWorker(
                unit_of_work_factory,
                narration_transport,
                WorkerPhasePolicy(60, 3, 120, "fake-narration"),
                choice_id_factory=lambda: UUID(CHOICE_A),
            )

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                accepted = await client.post(
                    f"/campaigns/{CAMPAIGN_A}/turns",
                    json={
                        "request_id": REQUEST_A,
                        "expected_state_version": 0,
                        "actor_id": ACTOR_A,
                        "content": {
                            "kind": "text",
                            "text": "周囲を注意深く観察する",
                        },
                    },
                )
                assert accepted.status_code == 202
                turn_id = UUID(accepted.json()["turn_id"])

                assert await resolution_worker.run_once()
                assert not await resolution_worker.run_once()
                assert await narration_worker.run_once()
                assert not await narration_worker.run_once()

                completed = await client.get(
                    f"/campaigns/{CAMPAIGN_A}/turns/{turn_id}"
                )
                campaign_state = await client.get(f"/campaigns/{CAMPAIGN_A}/state")
                replay = await client.post(
                    f"/campaigns/{CAMPAIGN_A}/turns",
                    json={
                        "request_id": REQUEST_A,
                        "expected_state_version": 0,
                        "actor_id": ACTOR_A,
                        "content": {
                            "kind": "text",
                            "text": "周囲を注意深く観察する",
                        },
                    },
                )
                next_turn = await client.post(
                    f"/campaigns/{CAMPAIGN_A}/turns",
                    json={
                        "request_id": REQUEST_B,
                        "expected_state_version": 0,
                        "actor_id": ACTOR_A,
                        "content": {"kind": "choice", "choice_id": CHOICE_A},
                    },
                )
            assert completed.status_code == 200
            assert campaign_state.status_code == 200
            assert campaign_state.json() == {
                "state_version": completed.json()["committed_state_version"],
                "latest_turn": completed.json(),
            }
            assert replay.status_code == 202
            assert replay.json() == completed.json()
            assert intent_transport.calls[0].purpose == "intent"
            assert narration_transport.calls[0].purpose == "result_narration"
            assert "discriminator" in intent_transport.calls[0].output_schema
            return (
                completed.json(),
                next_turn.status_code,
                next_turn.json(),
                (
                    intent_transport.calls[0].input_data,
                    narration_transport.calls[0].input_data,
                ),
            )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        completed, next_status, next_body, llm_inputs = runner.run(round_trip())

    assert completed["resolution_status"] == "committed"
    assert completed["narration_status"] == "completed"
    assert completed["narration"] == narration_text
    assert completed["choices"] == [{"id": CHOICE_A, "label": choice_label}]
    result = completed["action_results"][0]["result"]
    assert result["outcome"] == outcome
    assert result["dice"][0]["total"] == total
    assert next_status == 202
    assert next_body["resolution_status"] == "pending"
    assert CAMPAIGN_A not in llm_inputs[0]
    assert SCENE_A not in llm_inputs[0]
    assert ACTOR_A not in llm_inputs[0]
    clue = "床に新しい足跡が残っている。"
    assert (clue in llm_inputs[1]) is (outcome == "success")

    with database.connect() as connection:
        assert connection.scalar(
            text("SELECT llm_call_count FROM turns WHERE request_id=:request"),
            {"request": REQUEST_A},
        ) == 2
        assert list(
            connection.execute(
                text(
                    "SELECT type FROM events WHERE turn_id=("
                    "SELECT id FROM turns WHERE request_id=:request"
                    ") ORDER BY sequence"
                ),
                {"request": REQUEST_A},
            ).scalars()
        ) == ["DiceRolled", "ActionResolved", "GMNarrationGenerated"]


def test_resolution_context_uses_only_recent_completed_public_history(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'secret_check','perception','normal',"
                "'SECRET_SUCCESS_DESCRIPTION'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )
        connection.execute(
            text(
                "INSERT INTO turns("
                "id,campaign_id,scene_id,request_id,created_by,actor_id,input_payload,"
                "request_hash,input_kind,input_text,expected_state_version,"
                "committed_state_version,route,resolution_status,committed_at,"
                "narration_status,narration,narration_input,resolution_failure_code,created_at"
                ") VALUES("
                ":turn_a,:campaign,:scene,:request_a,:principal,:actor,'{}',"
                "decode(repeat('00',32),'hex'),'text','床を調べる',0,0,'mechanical',"
                "'committed',now(),'completed','古い足跡を見つけた。',"
                "'{\"private\":\"SECRET_NARRATION_INPUT\"}',"
                "'SECRET_FAILURE_CODE',now()-interval '2 minutes'"
                "),("
                ":turn_b,:campaign,:scene,:request_b,:principal,:actor,'{}',"
                "decode(repeat('01',32),'hex'),'text','扉を調べる',0,NULL,'mechanical',"
                "'not_applied',NULL,'completed','左右どちらの扉を調べますか?',"
                "NULL,'SECRET_CLARIFICATION_FAILURE',now()-interval '1 minute'"
                ")"
            ),
            {
                "turn_a": TURN_A,
                "turn_b": TURN_B,
                "campaign": CAMPAIGN_A,
                "scene": SCENE_A,
                "request_a": REQUEST_A,
                "request_b": REQUEST_B,
                "principal": PRINCIPAL_A,
                "actor": ACTOR_A,
            },
        )
        connection.execute(
            text(
                "INSERT INTO actions("
                "id,campaign_id,turn_id,ordinal,actor_id,kind,command,result,"
                "result_kind,ruleset_version"
                ") VALUES("
                ":action,:campaign,:turn,1,:actor,'skill_check',"
                "'{\"private\":\"SECRET_COMMAND\"}',"
                "'{\"kind\":\"applied\",\"outcome\":\"success\","
                "\"facts\":[\"足跡の向きは北だ。\"],\"dice\":[],"
                "\"state_changes\":[]}',"
                "'applied','mvp_v1'"
                ")"
            ),
            {
                "action": ACTION_A,
                "campaign": CAMPAIGN_A,
                "turn": TURN_A,
                "actor": ACTOR_A,
            },
        )

    async def resolve_answer() -> dict[str, Any]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            await _accept_turn(factory, _player_turn("左の扉です", REQUEST_C))

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            transport = ScriptedFakeTransport(
                [
                    {
                        "kind": "narrative",
                        "narration": "左の扉へ向かった。",
                        "choices": [],
                    }
                ]
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(MagicMock())),
                WorkerPhasePolicy(60, 3, 120, "fake-narrative"),
                recent_messages_limit=5,
            )
            assert await worker.run_once()
            assert transport.request_count == 1
            return json.loads(transport.calls[0].input_data)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        llm_input = runner.run(resolve_answer())

    assert llm_input["player_text"] == "左の扉です"
    assert [message["source"] for message in llm_input["recent_messages"]] == [
        "recent_player",
        "recent_action_result",
        "recent_gm",
        "recent_player",
        "recent_gm",
    ]
    assert [message["content"] for message in llm_input["recent_messages"]] == [
        "床を調べる",
        '{"dice":[],"facts":["足跡の向きは北だ。"],"kind":"applied",'
        '"outcome":"success"}',
        "古い足跡を見つけた。",
        "扉を調べる",
        "左右どちらの扉を調べますか?",
    ]
    serialized = json.dumps(llm_input, ensure_ascii=False)
    assert "SECRET_" not in serialized


def test_ungrounded_result_narration_retries_then_falls_back(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            assert lower <= 10 <= upper
            return 10

    async def run_workers() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("999の判定結果で周囲を注意深く観察する")
            )
            resolution = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [
                        {
                            "kind": "action_plan",
                            "actions": [
                                {
                                    "kind": "skill_check",
                                    "skill_ref": "perception",
                                    "objective": "足跡を見つける",
                                    "target_ref": None,
                                }
                            ],
                        }
                    ]
                ),
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
            )
            assert await resolution.run_once(accepted.turn_id)
            transport = ScriptedFakeTransport(
                [
                    {"narration": "判定結果は999だった。", "choices": []},
                    {"narration": "判定結果は999だった。", "choices": []},
                ]
            )
            narration = NarrationWorker(
                unit_of_work_factory,
                transport,
                WorkerPhasePolicy(60, 3, 120, "fake-narration"),
            )
            assert await narration.run_once(accepted.turn_id)
            assert await narration.run_once(accepted.turn_id)
            return transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count = runner.run(run_workers())

    assert request_count == 2
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason,"
                "llm_call_count,narration_attempt_count FROM turns "
                "WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("committed", "fallback", "INVALID_OUTPUT", 3, 2)


def test_incapacitated_actor_is_not_retried(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "UPDATE mvp_characters SET current_hp=0 "
                "WHERE campaign_id=:campaign AND entity_id=:actor"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def resolve() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            transport = ScriptedFakeTransport(
                [
                    {
                        "kind": "action_plan",
                        "actions": [
                            {
                                "kind": "skill_check",
                                "skill_ref": "perception",
                                "objective": "足跡を見つける",
                                "target_ref": None,
                            }
                        ],
                    }
                ]
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await worker.run_once(accepted.turn_id)
            return transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count = runner.run(resolve())

    assert request_count == 1
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,llm_call_count,"
                "resolution_attempt_count FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("not_applied", "completed", 1, 1)
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0


def test_unregistered_skill_check_is_not_retried(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def resolve() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            transport = ScriptedFakeTransport(
                [
                    {
                        "kind": "action_plan",
                        "actions": [
                            {
                                "kind": "skill_check",
                                "skill_ref": "perception",
                                "objective": "足跡を見つける",
                                "target_ref": None,
                            }
                        ],
                    }
                ]
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await worker.run_once(accepted.turn_id)
            return transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count = runner.run(resolve())

    assert request_count == 1
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,llm_call_count,"
                "resolution_attempt_count FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("not_applied", "completed", 1, 1)
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0


def test_atomic_not_applied_needs_no_narration_recovery(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(
        database, _player_turn("周囲を注意深く観察する")
    )
    lease = _acquire_lease_from_database(database, accepted.turn_id)

    async def recover() -> tuple[int, str]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            async with factory() as session:
                finalized = await PostgresTurnRepository(session).finalize_not_applied(
                    accepted.turn_id,
                    lease.turn.worker_epoch,
                    "clarification required",
                )
                assert finalized
                await session.commit()

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            transport = ScriptedFakeTransport([])
            worker = NarrationWorker(
                unit_of_work_factory,
                transport,
                WorkerPhasePolicy(60, 3, 120, "fake-narration"),
            )
            assert not await worker.run_once(accepted.turn_id)
            next_turn = await _accept_turn(
                factory,
                _player_turn(
                    "今日は静かだね",
                    request_id=REQUEST_B,
                ),
            )
            return transport.request_count, next_turn.resolution_status

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count, next_status = runner.run(recover())

    assert request_count == 0
    assert next_status == "pending"
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason,narration "
                "FROM turns WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        ).one() == (
            "not_applied",
            "completed",
            None,
            "clarification required",
        )
        assert connection.scalar(
            text(
                "SELECT count(*) FROM events "
                "WHERE turn_id=:turn AND type='GMNarrationGenerated'"
            ),
            {"turn": accepted.turn_id},
        ) == 1


def test_narration_timeout_survives_worker_replacement_and_falls_back(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal',"
                "'床に新しい足跡が残っている。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            assert lower <= 10 <= upper
            return 10

    async def run_workers() -> tuple[object, object]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            resolution = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [
                        {
                            "kind": "action_plan",
                            "actions": [
                                {
                                    "kind": "skill_check",
                                    "skill_ref": "perception",
                                    "objective": "周囲の痕跡を見つける",
                                    "target_ref": None,
                                }
                            ],
                        }
                    ]
                ),
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
                action_id_factory=lambda: UUID(ACTION_A),
                rng_source="seeded_test",
            )
            assert await resolution.run_once(accepted.turn_id)

            first_worker = NarrationWorker(
                unit_of_work_factory,
                ScriptedFakeTransport([TimeoutError("first worker stopped")]),
                WorkerPhasePolicy(60, 3, 120, "fake-narration"),
            )
            assert await first_worker.run_once(accepted.turn_id)
            async with factory() as session:
                first_deadline = await session.scalar(
                    text("SELECT narration_deadline FROM turns WHERE id=:turn"),
                    {"turn": accepted.turn_id},
                )

            replacement_worker = NarrationWorker(
                unit_of_work_factory,
                ScriptedFakeTransport([TimeoutError("replacement also stopped")]),
                WorkerPhasePolicy(60, 3, 120, "fake-narration"),
            )
            assert await replacement_worker.run_once(accepted.turn_id)
            async with factory() as session:
                final_deadline = await session.scalar(
                    text("SELECT narration_deadline FROM turns WHERE id=:turn"),
                    {"turn": accepted.turn_id},
                )
            return first_deadline, final_deadline

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        first_deadline, final_deadline = runner.run(run_workers())

    assert final_deadline == first_deadline
    with database.connect() as connection:
        turn = connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason,"
                "llm_call_count,narration_attempt_count,narration "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one()
        assert turn == (
            "committed",
            "fallback",
            "MODEL_TIMEOUT",
            3,
            2,
            "判定結果は保存されましたが、描写を生成できませんでした。",
        )
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 1
        assert list(
            connection.execute(
                text("SELECT type FROM events ORDER BY sequence")
            ).scalars()
        ) == ["DiceRolled", "ActionResolved", "GMNarrationGenerated"]

    reopened = _accept_from_database(database, _player_turn(request_id=REQUEST_B))
    assert reopened.resolution_status == "pending"


def test_fake_clarification_finishes_not_applied_without_game_writes(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def resolve() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(factory, _player_turn("調べる"))
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [
                        {
                            "kind": "clarification_required",
                            "question": "何を注意深く調べますか?",
                        }
                    ]
                ),
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
                rng_source="seeded_test",
            )
            assert await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(resolve())

    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,narration,llm_call_count "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == (
            "not_applied",
            "completed",
            "何を注意深く調べますか?",
            1,
        )
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert list(connection.execute(text("SELECT type FROM events")).scalars()) == [
            "GMNarrationGenerated"
        ]

    reopened = _accept_from_database(database, _player_turn(request_id=REQUEST_B))
    assert reopened.resolution_status == "pending"


def test_invalid_intent_retries_are_bounded_and_end_in_fallback(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def exhaust() -> list[object]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [
                        {"kind": "action_plan", "actions": "invalid"},
                        {"kind": "action_plan", "actions": "invalid"},
                        {"kind": "action_plan", "actions": "invalid"},
                    ]
                ),
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
                rng_source="seeded_test",
            )
            deadlines: list[object] = []
            for _ in range(3):
                assert await worker.run_once(accepted.turn_id)
                async with factory() as session:
                    deadlines.append(
                        await session.scalar(
                            text(
                                "SELECT resolution_deadline FROM turns WHERE id=:turn"
                            ),
                            {"turn": accepted.turn_id},
                        )
                    )
            return deadlines

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        deadlines = runner.run(exhaust())

    assert deadlines[0] == deadlines[1] == deadlines[2]
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason,"
                "llm_call_count,resolution_attempt_count "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("failed", "fallback", "INVALID_OUTPUT", 3, 3)
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert list(connection.execute(text("SELECT type FROM events")).scalars()) == [
            "GMNarrationGenerated"
        ]

    reopened = _accept_from_database(database, _player_turn(request_id=REQUEST_B))
    assert reopened.resolution_status == "pending"


def test_invalid_narrative_output_consumes_the_single_call_budget(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def resolve() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(factory, _player_turn("今日は静かだね"))
            transport = ScriptedFakeTransport(
                [{"kind": "narrative", "narration": 123, "choices": []}]
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-narrative"),
            )
            assert await worker.run_once(accepted.turn_id)
            assert not await worker.run_once(accepted.turn_id)
            return transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count = runner.run(resolve())

    assert request_count == 1
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason,"
                "llm_call_count,resolution_attempt_count FROM turns "
                "WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("failed", "fallback", "INVALID_OUTPUT", 1, 1)
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0


def test_provider_refusal_uses_public_recovery_reason(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def resolve() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(factory, _player_turn("今日は静かだね"))
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport([ProviderRefusalError("refused")]),
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-narrative"),
            )
            assert await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(resolve())

    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason,llm_call_count "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("failed", "fallback", "MODEL_REFUSAL", 1)


def test_narrative_route_commits_zero_actions_in_one_llm_call(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def resolve() -> tuple[int, int]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(factory, _player_turn("今日は静かだね"))
            transport = ScriptedFakeTransport(
                [
                    {
                        "kind": "narrative",
                        "narration": "静かな風が練習場を通り抜ける。",
                        "choices": [{"label": "少し休む"}],
                    }
                ]
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
                choice_id_factory=lambda: UUID(CHOICE_A),
            )
            assert await worker.run_once(accepted.turn_id)
            return transport.request_count, accepted.turn_id.int

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count, _ = runner.run(resolve())

    assert request_count == 1
    with database.connect() as connection:
        turn = connection.execute(
            text(
                "SELECT route,initial_route,routing_rule_version,routing_reason_codes,"
                "resolution_status,narration_status,committed_state_version,narration,"
                "llm_call_count FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one()
        assert turn == (
            "narrative",
            "narrative",
            "mvp_v1",
            ["narrative_or_fallback"],
            "committed",
            "completed",
            0,
            "静かな風が練習場を通り抜ける。",
            1,
        )
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert list(connection.execute(text("SELECT type FROM events")).scalars()) == [
            "GMNarrationGenerated"
        ]
        assert connection.scalar(text("SELECT label FROM turn_choices")) == "少し休む"

    reopened = _accept_from_database(database, _player_turn(request_id=REQUEST_B))
    assert reopened.resolution_status == "pending"


def test_narrative_escalation_reuses_first_call_as_mechanical_intent(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'inspect_device','perception','normal',"
                "'装置には小さな起動印が刻まれている。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            assert lower <= 10 <= upper
            return 10

    async def resolve() -> tuple[int, int]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(factory, _player_turn("謎の装置を作動させる"))
            resolution_transport = ScriptedFakeTransport(
                [
                    {
                        "kind": "resolution_required",
                        "actions": [
                            {
                                "kind": "skill_check",
                                "skill_ref": "perception",
                                "objective": "装置の起動方法を見抜く",
                                "target_ref": None,
                            }
                        ],
                    }
                ]
            )
            resolution = SkillCheckResolutionWorker(
                unit_of_work_factory,
                resolution_transport,
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
                action_id_factory=lambda: UUID(ACTION_A),
                rng_source="seeded_test",
            )
            assert await resolution.run_once(accepted.turn_id)

            narration_transport = ScriptedFakeTransport(
                [{"narration": "起動印の意味を読み取った。", "choices": []}]
            )
            narration = NarrationWorker(
                unit_of_work_factory,
                narration_transport,
                WorkerPhasePolicy(60, 3, 120, "fake-narration"),
            )
            assert await narration.run_once(accepted.turn_id)
            return resolution_transport.request_count, narration_transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        resolution_calls, narration_calls = runner.run(resolve())

    assert (resolution_calls, narration_calls) == (1, 1)
    with database.connect() as connection:
        turn = connection.execute(
            text(
                "SELECT route,initial_route,routing_reason_codes,resolution_status,"
                "narration_status,llm_call_count FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one()
        assert turn == (
            "mechanical",
            "narrative",
            ["narrative_or_fallback"],
            "committed",
            "completed",
            2,
        )
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 1


def test_exhausted_resolution_lease_is_terminalized_without_another_llm_call(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE turns SET resolution_status='resolving',route='mechanical',"
                "worker_epoch=1,lease_until=now()-interval '1 second',"
                "resolution_attempt_count=3,resolution_started_at=now()-interval '2 minutes',"
                "resolution_deadline=now()-interval '1 second' WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        )

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def finalize() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            transport = ScriptedFakeTransport([])
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await worker.run_once(accepted.turn_id)
            return transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count = runner.run(finalize())

    assert request_count == 0
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason "
                "FROM turns WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        ).one() == ("failed", "fallback", "UNKNOWN")


def test_exhausted_narration_lease_falls_back_without_another_llm_call(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            return 10

    async def finalize() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            resolution = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [
                        {
                            "kind": "action_plan",
                            "actions": [
                                {
                                    "kind": "skill_check",
                                    "skill_ref": "perception",
                                    "objective": "足跡を見つける",
                                    "target_ref": None,
                                }
                            ],
                        }
                    ]
                ),
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await resolution.run_once(accepted.turn_id)
            async with factory() as session:
                await session.execute(
                    text(
                        "UPDATE turns SET narration_status='generating',"
                        "narration_worker_epoch=1,"
                        "narration_lease_until=now()-interval '1 second',"
                        "narration_attempt_count=3,"
                        "narration_started_at=now()-interval '2 minutes',"
                        "narration_deadline=now()-interval '1 second' WHERE id=:turn"
                    ),
                    {"turn": accepted.turn_id},
                )
                await session.commit()
            transport = ScriptedFakeTransport([])
            narration = NarrationWorker(
                unit_of_work_factory,
                transport,
                WorkerPhasePolicy(60, 3, 120, "fake-narration"),
            )
            assert await narration.run_once(accepted.turn_id)
            return transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count = runner.run(finalize())

    assert request_count == 0
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason,llm_call_count "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("committed", "fallback", "UNKNOWN", 1)


def test_lost_commit_response_is_requeried_without_reapplying_action(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            return 10

    async def resolve() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            commits = 0

            class LostResponseUnitOfWork(PostgresUnitOfWork):
                async def commit(self) -> None:
                    nonlocal commits
                    await super().commit()
                    commits += 1
                    if commits == 5:
                        raise ConnectionError("commit response was lost")

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return LostResponseUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [
                        {
                            "kind": "action_plan",
                            "actions": [
                                {
                                    "kind": "skill_check",
                                    "skill_ref": "perception",
                                    "objective": "足跡を見つける",
                                    "target_ref": None,
                                }
                            ],
                        }
                    ]
                ),
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
                action_id_factory=lambda: UUID(ACTION_A),
            )
            assert await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(resolve())

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 1
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,llm_call_count "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("committed", "pending", 1)


def test_state_change_between_snapshot_and_commit_becomes_not_applied(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            return 10

    class StateChangingTransport:
        async def request(
            self,
            model_id: str,
            purpose: str,
            instruction: str,
            input_data: str,
            output_schema: dict[str, object],
        ) -> object:
            with database.begin() as connection:
                connection.execute(
                    text("UPDATE campaigns SET state_version=1 WHERE id=:campaign"),
                    {"campaign": CAMPAIGN_A},
                )
            return {
                "kind": "action_plan",
                "actions": [
                    {
                        "kind": "skill_check",
                        "skill_ref": "perception",
                        "objective": "足跡を見つける",
                        "target_ref": None,
                    }
                ],
            }

    async def resolve() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                StateChangingTransport(),
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(resolve())

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,narration,llm_call_count "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == (
            "not_applied",
            "completed",
            "状況が更新されたため判定を確定しませんでした。もう一度入力してください。",
            1,
        )


def test_state_change_before_worker_snapshot_does_not_reroll(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def resolve() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            async with factory() as session:
                await session.execute(
                    text("UPDATE campaigns SET state_version=1 WHERE id=:campaign"),
                    {"campaign": CAMPAIGN_A},
                )
                await session.commit()
            transport = ScriptedFakeTransport(
                [
                    {
                        "kind": "action_plan",
                        "actions": [
                            {
                                "kind": "skill_check",
                                "skill_ref": "perception",
                                "objective": "足跡を見つける",
                                "target_ref": None,
                            }
                        ],
                    }
                ]
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await worker.run_once(accepted.turn_id)
            return transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count = runner.run(resolve())

    assert request_count == 0
    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,narration,llm_call_count "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == (
            "not_applied",
            "completed",
            "状況が更新されたため判定を開始しませんでした。もう一度入力してください。",
            0,
        )


def test_transient_provider_failure_retries_with_persistent_budget(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            return 10

    async def resolve() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            transport = ScriptedFakeTransport(
                [
                    ConnectionError("temporary provider failure"),
                    {
                        "kind": "action_plan",
                        "actions": [
                            {
                                "kind": "skill_check",
                                "skill_ref": "perception",
                                "objective": "足跡を見つける",
                                "target_ref": None,
                            }
                        ],
                    },
                ]
            )
            first = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await first.run_once(accepted.turn_id)
            replacement = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await replacement.run_once(accepted.turn_id)
            return transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count = runner.run(resolve())

    assert request_count == 2
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,llm_call_count,"
                "resolution_attempt_count FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("committed", "pending", 2, 2)
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 1


@pytest.mark.parametrize("revocation", ["membership", "controller"])
def test_worker_rechecks_actor_authorization_before_llm(
    database: Engine,
    revocation: str,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(
        database, _player_turn("周囲を注意深く観察する")
    )
    with database.begin() as connection:
        if revocation == "membership":
            connection.execute(
                text(
                    "UPDATE campaign_members SET active=false "
                    "WHERE campaign_id=:campaign AND principal_id=:principal"
                ),
                {"campaign": CAMPAIGN_A, "principal": PRINCIPAL_A},
            )
        else:
            connection.execute(
                text("UPDATE entities SET controller_id=NULL WHERE id=:actor"),
                {"actor": ACTOR_A},
            )

    class UnusedRandom:
        def randint(self, lower: int, upper: int) -> int:
            raise AssertionError(f"dice must not be used: {lower}-{upper}")

    async def resolve() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            transport = ScriptedFakeTransport([])
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(UnusedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await worker.run_once(accepted.turn_id)
            return transport.request_count

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        request_count = runner.run(resolve())

    assert request_count == 0
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,llm_call_count "
                "FROM turns WHERE id=:turn"
            ),
            {"turn": accepted.turn_id},
        ).one() == ("not_applied", "completed", 0)
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0


def test_commit_rechecks_actor_authorization_after_llm(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            return 10

    class RevokingTransport:
        async def request(
            self,
            model_id: str,
            purpose: str,
            instruction: str,
            input_data: str,
            output_schema: dict[str, object],
        ) -> object:
            with database.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE campaign_members SET active=false "
                        "WHERE campaign_id=:campaign AND principal_id=:principal"
                    ),
                    {"campaign": CAMPAIGN_A, "principal": PRINCIPAL_A},
                )
            return {
                "kind": "action_plan",
                "actions": [
                    {
                        "kind": "skill_check",
                        "skill_ref": "perception",
                        "objective": "足跡を見つける",
                        "target_ref": None,
                    }
                ],
            }

    async def resolve() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                RevokingTransport(),
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            assert await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(resolve())

    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,llm_call_count "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("not_applied", "completed", 1)
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0


def test_pre_resolution_context_excludes_success_only_scene_description(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'hidden_tracks','perception','normal',"
                "'判定に成功すると新しい足跡を発見する。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    async def resolve() -> str:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(factory, _player_turn("今日は静かだね"))
            transport = ScriptedFakeTransport(
                [{"kind": "narrative", "narration": "静かな時間が流れる。", "choices": []}]
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(MagicMock())),
                WorkerPhasePolicy(60, 3, 120, "fake-narrative"),
            )
            assert await worker.run_once(accepted.turn_id)
            return transport.calls[0].input_data

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        input_data = runner.run(resolve())

    payload = json.loads(input_data)
    assert "足跡" not in payload["scene_view"]["content"]


def test_clarification_is_saved_atomically_before_narration_worker_can_claim(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)

    async def resolve_with_competing_narrator() -> tuple[bool, bool]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            raced = False

            def normal_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            class RacingUnitOfWork(PostgresUnitOfWork):
                async def commit(self) -> None:
                    nonlocal raced
                    await super().commit()
                    if raced:
                        return
                    async with factory() as session:
                        state = (
                            await session.execute(
                                text(
                                    "SELECT resolution_status,narration_status "
                                    "FROM turns WHERE request_id=:request"
                                ),
                                {"request": REQUEST_A},
                            )
                        ).one_or_none()
                    if state == ("not_applied", "pending"):
                        raced = True
                        narrator = NarrationWorker(
                            normal_factory,
                            ScriptedFakeTransport([]),
                            WorkerPhasePolicy(60, 3, 120, "fake-narration"),
                        )
                        assert await narrator.run_once()

            def racing_factory() -> PostgresUnitOfWork:
                return RacingUnitOfWork(factory)

            accepted = await _accept_turn(factory, _player_turn("調べる"))
            worker = SkillCheckResolutionWorker(
                racing_factory,
                ScriptedFakeTransport(
                    [
                        {
                            "kind": "clarification_required",
                            "question": "何を注意深く調べますか?",
                        }
                    ]
                ),
                MvpV1Ruleset(DiceEngine(MagicMock())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
            )
            resolved = await worker.run_once(accepted.turn_id)
            return resolved, raced

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        resolved, raced = runner.run(resolve_with_competing_narrator())

    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,narration,recovery_reason "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == (
            "not_applied",
            "completed",
            "何を注意深く調べますか?",
            None,
        )
        assert connection.scalar(
            text(
                "SELECT count(*) FROM events "
                "WHERE turn_id=(SELECT id FROM turns WHERE request_id=:request) "
                "AND type='GMNarrationGenerated'"
            ),
            {"request": REQUEST_A},
        ) == 1
    assert resolved
    assert not raced


@pytest.mark.parametrize(
    ("player_text", "response"),
    [
        (
            "周囲を注意深く観察する",
            {
                "kind": "action_plan",
                "actions": [
                    {
                        "kind": "skill_check",
                        "skill_ref": "perception",
                        "objective": "足跡を見つける",
                        "target_ref": None,
                    }
                ],
            },
        ),
        (
            "今日は静かだね",
            {"kind": "narrative", "narration": "静かな時間が流れる。", "choices": []},
        ),
    ],
)
def test_resolution_result_after_phase_deadline_becomes_fallback(
    database: Engine,
    player_text: str,
    response: dict[str, object],
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            assert lower <= 10 <= upper
            return 10

    class ExpiringTransport:
        async def request(
            self,
            model_id: str,
            purpose: str,
            instruction: str,
            input_data: str,
            output_schema: dict[str, object],
        ) -> object:
            with database.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE turns SET "
                        "resolution_deadline=clock_timestamp()-interval '1 second',"
                        "lease_until=clock_timestamp()+interval '30 seconds' "
                        "WHERE request_id=:request"
                    ),
                    {"request": REQUEST_A},
                )
            return response

    async def resolve() -> bool:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(factory, _player_turn(player_text))
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ExpiringTransport(),
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-turn"),
            )
            return await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        resolved = runner.run(resolve())

    assert resolved
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("failed", "fallback", "UNKNOWN")
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0


def test_narration_result_after_phase_deadline_becomes_fallback(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "INSERT INTO mvp_skill_modifiers("
                "campaign_id,character_id,skill_ref,modifier"
                ") VALUES(:campaign,:actor,'perception',2)"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "INSERT INTO mvp_scene_skill_checks("
                "campaign_id,scene_id,check_ref,skill_ref,difficulty,public_description"
                ") VALUES("
                ":campaign,:scene,'observe_room','perception','normal','足跡がある。'"
                ")"
            ),
            {"campaign": CAMPAIGN_A, "scene": SCENE_A},
        )

    class FixedRandom:
        def randint(self, lower: int, upper: int) -> int:
            assert lower <= 10 <= upper
            return 10

    class ExpiringNarrationTransport:
        async def request(
            self,
            model_id: str,
            purpose: str,
            instruction: str,
            input_data: str,
            output_schema: dict[str, object],
        ) -> object:
            with database.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE turns SET "
                        "narration_deadline=clock_timestamp()-interval '1 second',"
                        "narration_lease_until=clock_timestamp()+interval '30 seconds' "
                        "WHERE request_id=:request"
                    ),
                    {"request": REQUEST_A},
                )
            return {"narration": "期限後の描写", "choices": []}

    async def run_workers() -> bool:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            accepted = await _accept_turn(
                factory, _player_turn("周囲を注意深く観察する")
            )
            resolution = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [
                        {
                            "kind": "action_plan",
                            "actions": [
                                {
                                    "kind": "skill_check",
                                    "skill_ref": "perception",
                                    "objective": "足跡を見つける",
                                    "target_ref": None,
                                }
                            ],
                        }
                    ]
                ),
                MvpV1Ruleset(DiceEngine(FixedRandom())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
            )
            assert await resolution.run_once(accepted.turn_id)
            narrator = NarrationWorker(
                unit_of_work_factory,
                ExpiringNarrationTransport(),
                WorkerPhasePolicy(60, 3, 120, "fake-narration"),
            )
            return await narrator.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        narrated = runner.run(run_workers())

    assert narrated
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,narration_status,recovery_reason,narration "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == (
            "committed",
            "fallback",
            "UNKNOWN",
            "判定結果は保存されましたが、描写を生成できませんでした。",
        )


@pytest.mark.parametrize(
    ("player_text", "intent", "rolls", "expected_hp", "expected_quantity", "events"),
    [
        (
            "鉄の剣でゴブリンを攻撃する",
            {"kind": "attack", "target_ref": "goblin", "weapon_ref": "iron_sword"},
            [10, 4],
            (5, 6),
            2,
            ["DiceRolled", "DiceRolled", "DamageApplied", "ActionResolved"],
        ),
        (
            "回復ポーションを飲む",
            {"kind": "use_item", "item_ref": "healing_potion", "target_ref": None},
            [2],
            (9, 10),
            1,
            ["DiceRolled", "HealingApplied", "ItemConsumed", "ActionResolved"],
        ),
    ],
)
def test_worker_commits_attack_and_healing_item_from_registered_refs(
    database: Engine,
    player_text: str,
    intent: dict[str, object],
    rolls: list[int],
    expected_hp: tuple[int, int],
    expected_quantity: int,
    events: list[str],
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "UPDATE mvp_characters SET current_hp=5 "
                "WHERE campaign_id=:campaign AND entity_id=:actor"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )

    class FixedSequence:
        def __init__(self) -> None:
            self.values = iter(rolls)

        def randint(self, lower: int, upper: int) -> int:
            value = next(self.values)
            assert lower <= value <= upper
            return value

    async def resolve() -> dict[str, Any]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            accepted = await _accept_turn(factory, _player_turn(player_text))

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            transport = ScriptedFakeTransport(
                [{"kind": "action_plan", "actions": [intent]}]
            )
            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                transport,
                MvpV1Ruleset(DiceEngine(FixedSequence())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
                rng_source="seeded_test",
            )
            assert await worker.run_once(accepted.turn_id)
            return json.loads(transport.calls[0].input_data)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        llm_input = runner.run(resolve())

    assert set(llm_input["supported_action_types"]) == {
        "attack",
        "use_item",
    }
    assert {entity["ref"] for entity in llm_input["allowed_entity_refs"]} == {
        "goblin",
        "healing_potion",
        "hero",
        "iron_sword",
    }
    with database.connect() as connection:
        hp = connection.execute(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id IN (:actor,:target) "
                "ORDER BY entity_id"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A, "target": ACTOR_C},
        ).scalars().all()
        assert tuple(hp) == expected_hp
        assert connection.scalar(
            text(
                "SELECT quantity FROM mvp_inventory "
                "WHERE campaign_id=:campaign AND owner_id=:actor AND item_id=:item"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A, "item": ITEM_A},
        ) == expected_quantity
        assert connection.scalar(
            text("SELECT state_version FROM campaigns WHERE id=:campaign"),
            {"campaign": CAMPAIGN_A},
        ) == 1
        assert list(
            connection.execute(
                text("SELECT type FROM events ORDER BY sequence")
            ).scalars()
        ) == events


@pytest.mark.parametrize(
    (
        "initial_hp",
        "quantity",
        "roll",
        "reason",
        "expected_hp",
        "expected_quantity",
        "follow_up_attack",
    ),
    [
        (9, 2, 6, "rule_precondition", 10, 1, True),
        (5, 1, 2, "resource_unavailable", 9, 0, False),
    ],
)
def test_worker_keeps_first_heal_when_second_use_becomes_not_applicable(
    database: Engine,
    initial_hp: int,
    quantity: int,
    roll: int,
    reason: str,
    expected_hp: int,
    expected_quantity: int,
    follow_up_attack: bool,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "UPDATE mvp_characters SET current_hp=:hp "
                "WHERE campaign_id=:campaign AND entity_id=:actor"
            ),
            {"hp": initial_hp, "campaign": CAMPAIGN_A, "actor": ACTOR_A},
        )
        connection.execute(
            text(
                "UPDATE mvp_inventory SET quantity=:quantity "
                "WHERE campaign_id=:campaign AND owner_id=:actor AND item_id=:item"
            ),
            {
                "quantity": quantity,
                "campaign": CAMPAIGN_A,
                "actor": ACTOR_A,
                "item": ITEM_A,
            },
        )

    class FixedHeal:
        def __init__(self) -> None:
            self.calls = 0
            self.values = iter([roll, 10, 4] if follow_up_attack else [roll])

        def randint(self, lower: int, upper: int) -> int:
            self.calls += 1
            value = next(self.values)
            assert lower <= value <= upper
            return value

    random = FixedHeal()
    item = {"kind": "use_item", "item_ref": "healing_potion", "target_ref": None}
    intents = [item, item]
    if follow_up_attack:
        intents.append(
            {"kind": "attack", "target_ref": "goblin", "weapon_ref": "iron_sword"}
        )

    async def resolve() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            accepted = await _accept_turn(factory, _player_turn("回復ポーションを二回使う"))

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport([{"kind": "action_plan", "actions": intents}]),
                MvpV1Ruleset(DiceEngine(random)),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
                rng_source="seeded_test",
            )
            assert await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(resolve())

    assert random.calls == (3 if follow_up_attack else 1)
    with database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT resolution_status,committed_state_version "
                "FROM turns WHERE request_id=:request"
            ),
            {"request": REQUEST_A},
        ).one() == ("committed", 1)
        assert connection.scalar(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:actor"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
        ) == expected_hp
        assert connection.scalar(
            text(
                "SELECT quantity FROM mvp_inventory "
                "WHERE campaign_id=:campaign AND owner_id=:actor AND item_id=:item"
            ),
            {"campaign": CAMPAIGN_A, "actor": ACTOR_A, "item": ITEM_A},
        ) == expected_quantity
        action_rows = list(
            connection.execute(
                text(
                    "SELECT result_kind,result->>'reason' FROM actions ORDER BY ordinal"
                )
            )
        )
        expected_actions = [("applied", None), ("not_applicable", reason)]
        if follow_up_attack:
            expected_actions.append(("applied", None))
        assert action_rows == expected_actions
        event_types = list(
            connection.execute(text("SELECT type FROM events ORDER BY sequence")).scalars()
        )
        expected_events = [
            "DiceRolled",
            "HealingApplied",
            "ItemConsumed",
            "ActionResolved",
            "ActionResolved",
        ]
        if follow_up_attack:
            expected_events.extend(
                ["DiceRolled", "DiceRolled", "DamageApplied", "ActionResolved"]
            )
        assert event_types == expected_events


def test_worker_attack_miss_keeps_hp_and_state_version(database: Engine) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "UPDATE mvp_characters SET defense=30 "
                "WHERE campaign_id=:campaign AND entity_id=:target"
            ),
            {"campaign": CAMPAIGN_A, "target": ACTOR_C},
        )

    class FixedMiss:
        def randint(self, lower: int, upper: int) -> int:
            assert lower <= 1 <= upper
            return 1

    async def resolve() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            accepted = await _accept_turn(factory, _player_turn("ゴブリンを攻撃する"))

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [
                        {
                            "kind": "action_plan",
                            "actions": [
                                {
                                    "kind": "attack",
                                    "target_ref": "goblin",
                                    "weapon_ref": "iron_sword",
                                }
                            ],
                        }
                    ]
                ),
                MvpV1Ruleset(DiceEngine(FixedMiss())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
                rng_source="seeded_test",
            )
            assert await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(resolve())

    with database.connect() as connection:
        action = connection.execute(
            text("SELECT result FROM actions")
        ).scalar_one()
        assert action["outcome"] == "failure"
        assert len(action["dice"]) == 1
        assert connection.scalar(
            text("SELECT state_version FROM campaigns WHERE id=:campaign"),
            {"campaign": CAMPAIGN_A},
        ) == 0
        assert connection.scalar(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:target"
            ),
            {"campaign": CAMPAIGN_A, "target": ACTOR_C},
        ) == 10
        assert list(
            connection.execute(text("SELECT type FROM events ORDER BY sequence")).scalars()
        ) == ["DiceRolled", "ActionResolved"]


def test_later_attack_on_target_reduced_to_zero_is_not_applicable(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        connection.execute(
            text(
                "UPDATE mvp_characters SET current_hp=3,defense=1 "
                "WHERE campaign_id=:campaign AND entity_id=:target"
            ),
            {"campaign": CAMPAIGN_A, "target": ACTOR_C},
        )

    class FixedSequence:
        def __init__(self) -> None:
            self.values = iter([10, 4])

        def randint(self, lower: int, upper: int) -> int:
            value = next(self.values)
            assert lower <= value <= upper
            return value

    attack = {
        "kind": "attack",
        "target_ref": "goblin",
        "weapon_ref": "iron_sword",
    }

    async def resolve() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            accepted = await _accept_turn(factory, _player_turn("二回攻撃する"))

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [{"kind": "action_plan", "actions": [attack, attack]}]
                ),
                MvpV1Ruleset(DiceEngine(FixedSequence())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
                rng_source="seeded_test",
            )
            assert await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(resolve())

    with database.connect() as connection:
        assert list(
            connection.execute(
                text("SELECT result_kind FROM actions ORDER BY ordinal")
            ).scalars()
        ) == ["applied", "not_applicable"]
        assert connection.scalar(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:target"
            ),
            {"campaign": CAMPAIGN_A, "target": ACTOR_C},
        ) == 0
        assert list(
            connection.execute(text("SELECT type FROM events ORDER BY sequence")).scalars()
        ) == [
            "DiceRolled",
            "DiceRolled",
            "DamageApplied",
            "ActionResolved",
            "ActionResolved",
        ]


@pytest.mark.parametrize(
    ("intent", "mutation"),
    [
        (
            {"kind": "attack", "target_ref": "goblin", "weapon_ref": "healing_potion"},
            "none",
        ),
        (
            {
                "kind": "use_item",
                "item_ref": "healing_potion",
                "target_ref": "goblin",
            },
            "injure",
        ),
        (
            {"kind": "attack", "target_ref": "goblin", "weapon_ref": None},
            "incapacitate",
        ),
        (
            {"kind": "attack", "target_ref": "goblin", "weapon_ref": None},
            "incapacitate_target",
        ),
        (
            {"kind": "use_item", "item_ref": "healing_potion", "target_ref": None},
            "empty_inventory",
        ),
        (
            {"kind": "use_item", "item_ref": "healing_potion", "target_ref": None},
            "none",
        ),
    ],
)
def test_worker_rejects_illegal_attack_and_item_intents_without_game_writes(
    database: Engine,
    intent: dict[str, object],
    mutation: str,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
        if mutation == "injure":
            connection.execute(
                text(
                    "UPDATE mvp_characters SET current_hp=5 "
                    "WHERE campaign_id=:campaign AND entity_id=:actor"
                ),
                {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
            )
        elif mutation == "incapacitate":
            connection.execute(
                text(
                    "UPDATE mvp_characters SET current_hp=0 "
                    "WHERE campaign_id=:campaign AND entity_id=:actor"
                ),
                {"campaign": CAMPAIGN_A, "actor": ACTOR_A},
            )
        elif mutation == "incapacitate_target":
            connection.execute(
                text(
                    "UPDATE mvp_characters SET current_hp=0 "
                    "WHERE campaign_id=:campaign AND entity_id=:target"
                ),
                {"campaign": CAMPAIGN_A, "target": ACTOR_C},
            )
        elif mutation == "empty_inventory":
            connection.execute(
                text(
                    "UPDATE mvp_inventory SET quantity=0 "
                    "WHERE campaign_id=:campaign AND owner_id=:actor AND item_id=:item"
                ),
                {"campaign": CAMPAIGN_A, "actor": ACTOR_A, "item": ITEM_A},
            )

    async def resolve() -> None:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            player_text = (
                "攻撃する" if intent["kind"] == "attack" else "回復ポーションを使う"
            )
            accepted = await _accept_turn(factory, _player_turn(player_text))

            def unit_of_work_factory() -> PostgresUnitOfWork:
                return PostgresUnitOfWork(factory)

            worker = SkillCheckResolutionWorker(
                unit_of_work_factory,
                ScriptedFakeTransport(
                    [{"kind": "action_plan", "actions": [intent]}]
                ),
                MvpV1Ruleset(DiceEngine(MagicMock())),
                WorkerPhasePolicy(60, 3, 120, "fake-intent"),
            )
            assert await worker.run_once(accepted.turn_id)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(resolve())

    with database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM actions")) == 0
        assert connection.scalar(
            text("SELECT state_version FROM campaigns WHERE id=:campaign"),
            {"campaign": CAMPAIGN_A},
        ) == 0
        assert connection.scalar(
            text("SELECT resolution_status FROM turns")
        ) == "not_applied"


def test_public_event_query_projects_turns_in_sequence_after_cursor(
    database: Engine,
) -> None:
    with database.begin() as connection:
        _seed_resolution_state(connection)
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)
    _commit_resolution_from_database(database, _resolution_bundle(accepted.turn_id))
    assert _save_narration_from_database(database, accepted.turn_id, ())

    async def fetch() -> tuple[tuple[object, ...], tuple[object, ...]]:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            repository = PostgresPublicEventRepository(session)
            visible = await repository.list_after(UUID(CAMPAIGN_A), 1, limit=10)
            other_campaign = await repository.list_after(UUID(CAMPAIGN_B), 0, limit=10)
            return visible, other_campaign

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        visible, other_campaign = runner.run(fetch())

    assert [event.id for event in visible] == [2, 3]
    assert all(event.type == "turn.updated" for event in visible)
    assert all(event.payload.turn.turn_id == accepted.turn_id for event in visible)
    assert all(event.payload.turn.narration_status == "completed" for event in visible)
    assert "rng" not in json.dumps(
        [event.model_dump(mode="json") for event in visible]
    )
    assert other_campaign == ()


def test_development_fixture_is_idempotent(database: Engine) -> None:
    from ai_rpg.runtime import seed_development_fixture

    url = database.url.render_as_string(hide_password=False)
    first = seed_development_fixture(url)
    second = seed_development_fixture(url)

    assert second == first
    with database.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM campaigns WHERE id=:id"),
            {"id": first.campaign_id},
        ) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM campaign_members "
                "WHERE campaign_id=:campaign AND principal_id=:principal AND active"
            ),
            {"campaign": first.campaign_id, "principal": first.principal_id},
        ) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:actor"
            ),
            {"campaign": first.campaign_id, "actor": first.actor_id},
        ) == 1
        assert connection.execute(
            text(
                "SELECT modifier FROM mvp_skill_modifiers "
                "WHERE campaign_id=:campaign AND character_id=:actor "
                "AND skill_ref='perception'"
            ),
            {"campaign": first.campaign_id, "actor": first.actor_id},
        ).scalar_one() == 2
        assert connection.execute(
            text(
                "SELECT difficulty,public_description FROM mvp_scene_skill_checks "
                "WHERE campaign_id=:campaign AND scene_id=:scene "
                "AND check_ref='observe_room'"
            ),
            {"campaign": first.campaign_id, "scene": first.scene_id},
        ).one() == ("normal", "床に新しい足跡が残っている。")


def test_independent_cli_processes_complete_fake_round_trip(database: Engine) -> None:
    from ai_rpg.runtime import DEVELOPMENT_FIXTURE

    url = database.url.render_as_string(hide_password=False)
    env = {**os.environ, "AIRPG_DATABASE_URL": url, "PYTHONUNBUFFERED": "1"}
    seed = subprocess.run(
        [sys.executable, "-m", "ai_rpg.cli", "seed-dev"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(seed.stdout)["campaign_id"] == str(DEVELOPMENT_FIXTURE.campaign_id)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ai_rpg.cli",
            "api",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--dev-principal",
            str(DEVELOPMENT_FIXTURE.principal_id),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while True:
            if api.poll() is not None:
                output = api.stdout.read() if api.stdout is not None else ""
                pytest.fail(f"API process exited before readiness: {output}")
            try:
                with Client(base_url=base_url) as client:
                    if client.get("/health").status_code == 200:
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("API process did not become ready")
            time.sleep(0.05)

        request_id = uuid4()
        with Client(base_url=base_url) as client:
            accepted = client.post(
                f"/campaigns/{DEVELOPMENT_FIXTURE.campaign_id}/turns",
                json={
                    "request_id": str(request_id),
                    "expected_state_version": 0,
                    "actor_id": str(DEVELOPMENT_FIXTURE.actor_id),
                    "content": {"kind": "text", "text": "周囲を注意深く観察する"},
                },
            )
        assert accepted.status_code == 202
        turn_id = accepted.json()["turn_id"]

        commands = [
            ("resolution-worker", True),
            ("resolution-worker", False),
            ("narration-worker", True),
            ("narration-worker", False),
        ]
        for command, expected_processed in commands:
            completed = subprocess.run(
                [sys.executable, "-m", "ai_rpg.cli", command, "--fake", "--once"],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            assert json.loads(completed.stdout)["processed"] is expected_processed

        with Client(base_url=base_url) as client:
            response = client.get(
                f"/campaigns/{DEVELOPMENT_FIXTURE.campaign_id}/turns/{turn_id}"
            )
        assert response.status_code == 200
        assert response.json()["resolution_status"] == "committed"
        assert response.json()["narration_status"] == "completed"
        assert response.json()["action_results"][0]["result"]["outcome"] == "success"
        with database.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM actions WHERE turn_id=:turn"),
                {"turn": turn_id},
            ) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM events WHERE turn_id=:turn"),
                {"turn": turn_id},
            ) == 3
    finally:
        api.terminate()
        try:
            api.wait(timeout=5)
        except subprocess.TimeoutExpired:
            api.kill()
            api.wait(timeout=5)
