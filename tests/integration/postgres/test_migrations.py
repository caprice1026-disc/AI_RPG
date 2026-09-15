"""実PostgreSQLに対するmigration制約テスト。"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, RowMapping, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_rpg.application import (
    AuthorizationError,
    ChoiceNotAvailableError,
    IdempotencyConflictError,
    InvalidCommitBundleError,
    StateVersionConflictError,
    TurnInProgressError,
)
from ai_rpg.application.ports import ActionRecord, ChoiceDraft, CommitBundle, EventRecord, Lease
from ai_rpg.contracts import PlayerTurnInput, TurnResponse
from ai_rpg.infrastructure.postgres.repositories import (
    PostgresNarrationRepository,
    PostgresTurnRepository,
)

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


def _save_narration_from_database(
    database: Engine,
    turn_id: UUID,
    choices: tuple[ChoiceDraft, ...],
) -> bool:
    async def save() -> bool:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            saved = await PostgresNarrationRepository(session).save_conditionally(
                UUID(CAMPAIGN_A),
                turn_id,
                0,
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
            "INSERT INTO entities(id,campaign_id,kind) "
            "VALUES(:actor,:campaign,'npc')"
        ),
        {"actor": ACTOR_C, "campaign": CAMPAIGN_A},
    )
    connection.execute(
        text(
            "INSERT INTO mvp_characters("
            "campaign_id,entity_id,current_hp,max_hp,defense,attack_bonus"
            ") VALUES(:campaign,:actor,10,10,12,2),(:campaign,:target,10,10,11,1)"
        ),
        {"campaign": CAMPAIGN_A, "actor": ACTOR_A, "target": ACTOR_C},
    )


def _resolution_action(turn_id: UUID, ordinal: int) -> ActionRecord:
    action_id = UUID(int=1000 + ordinal)
    roll = {
        "expression": "1d20+2",
        "rolls": [10],
        "modifier": 2,
        "total": 12,
    }
    result = {
        "kind": "applied",
        "outcome": "success",
        "facts": ["技能判定はsuccess(合計12)"],
        "dice": [roll],
        "damage": [],
    }
    return ActionRecord(
        id=action_id,
        ordinal=ordinal,
        actor_id=UUID(ACTOR_A),
        kind="skill_check",
        command={
            "action_id": action_id,
            "campaign_id": UUID(CAMPAIGN_A),
            "turn_id": turn_id,
            "actor_id": UUID(ACTOR_A),
            "ordinal": ordinal,
            "kind": "skill_check",
            "skill_ref": "perception",
            "objective": "扉を調べる",
            "target_id": None,
        },
        result=result,
        result_kind="applied",
    )


def _attack_action(turn_id: UUID) -> ActionRecord:
    action_id = UUID(int=1101)
    result = {
        "kind": "applied",
        "outcome": "success",
        "facts": ["攻撃が命中し3ダメージを与えた"],
        "dice": [
            {"expression": "1d20+2", "rolls": [12], "modifier": 2, "total": 14},
            {"expression": "1d6", "rolls": [3], "modifier": 0, "total": 3},
        ],
        "damage": [
            {
                "target_id": UUID(ACTOR_C),
                "amount": 3,
                "hp_before": 10,
                "hp_after": 7,
            }
        ],
    }
    return ActionRecord(
        id=action_id,
        ordinal=1,
        actor_id=UUID(ACTOR_A),
        kind="attack",
        command={
            "action_id": action_id,
            "campaign_id": UUID(CAMPAIGN_A),
            "turn_id": turn_id,
            "actor_id": UUID(ACTOR_A),
            "ordinal": 1,
            "kind": "attack",
            "target_id": UUID(ACTOR_C),
            "weapon_id": None,
        },
        result=result,
        result_kind="applied",
        target_id=UUID(ACTOR_C),
    )


def _resolution_events(action: ActionRecord) -> tuple[EventRecord, ...]:
    rolls = action.result["dice"]
    damage = action.result["damage"]
    assert isinstance(rolls, list)
    assert isinstance(damage, list)
    event_id = 2000 + action.ordinal * 100
    events = [
        EventRecord(
            id=UUID(int=event_id + index),
            event_type="DiceRolled",
            action_id=action.id,
            payload={
                "roll": roll,
                "rng": {
                    "source": "seeded_test",
                    "implementation_version": "mvp_v1",
                    "draw_index": index,
                },
            },
        )
        for index, roll in enumerate(rolls)
    ]
    events.extend(
        EventRecord(
            id=UUID(int=event_id + len(rolls) + index),
            event_type="DamageApplied",
            action_id=action.id,
            payload=damage_fact,
        )
        for index, damage_fact in enumerate(damage)
    )
    events.append(
        EventRecord(
            id=UUID(int=event_id + len(rolls) + len(damage)),
            event_type="ActionResolved",
            action_id=action.id,
            payload={"result": action.result},
        )
    )
    return tuple(events)


def _resolution_bundle(
    turn_id: UUID,
    *,
    first_ordinal: int = 1,
    action_count: int = 1,
    scene_id: str = SCENE_A,
    narration_version: int = 0,
    include_narration_results: bool = True,
    invalid_event_type: bool = False,
    canonical_updates: tuple[tuple[UUID, int, int], ...] = (),
    attack: bool = False,
) -> CommitBundle:
    actions = (
        (_attack_action(turn_id),)
        if attack
        else tuple(
            _resolution_action(turn_id, ordinal)
            for ordinal in range(first_ordinal, first_ordinal + action_count)
        )
    )
    events = tuple(event for action in actions for event in _resolution_events(action))
    if invalid_event_type:
        events = (replace(events[0], event_type="UnregisteredEvent"), *events[1:])
    resolved_actions = [
        {
            "action_id": action.id,
            "ordinal": action.ordinal,
            "result": action.result,
        }
        for action in actions
    ]
    return CommitBundle(
        campaign_id=UUID(CAMPAIGN_A),
        scene_id=UUID(scene_id),
        turn_id=turn_id,
        worker_epoch=1,
        base_state_version=0,
        canonical_changed=bool(canonical_updates),
        canonical_updates=canonical_updates,
        actions=actions,
        events=events,
        narration_input={
            "player_text": "扉を調べる",
            "committed_state_version": narration_version,
            "resolved_actions": resolved_actions if include_narration_results else [],
            "public_state_after": [],
            "allowed_entity_refs": [],
            "output_limits": {"max_actions": 3, "max_choices": 5},
        },
    )


def _acquire_lease_from_database(database: Engine, turn_id: UUID) -> Lease:
    async def acquire() -> Lease:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            lease = await PostgresTurnRepository(session).acquire_lease(
                turn_id,
                lease_seconds=60,
            )
            await session.commit()
            assert lease is not None
            return lease

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(acquire())


def _commit_resolution_from_database(database: Engine, bundle: CommitBundle) -> int:
    async def commit() -> int:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory, factory() as session:
            try:
                version = await PostgresTurnRepository(session).commit_resolution(bundle)
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
        assert {"campaigns", "events", "mvp_inventory"} <= _public_tables(engine)
    finally:
        engine.dispose()

    _run_alembic(empty_database_url, "downgrade", "base")
    engine = create_engine(empty_database_url)
    try:
        assert _public_tables(engine) == {"alembic_version"}
        assert _public_functions(engine) == set()
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
    ),
    [
        (CAMPAIGN_B, SCENE_B, PRINCIPAL_B, ACTOR_B, "completed"),
        (CAMPAIGN_A, SCENE_B, PRINCIPAL_A, ACTOR_A, "completed"),
        (CAMPAIGN_A, SCENE_A, PRINCIPAL_A, ACTOR_A, "pending"),
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

    with pytest.raises(ChoiceNotAvailableError) as caught:
        _accept_from_database(
            database,
            _player_turn(choice_id=CHOICE_C),
        )

    assert caught.value.code == "CHOICE_NOT_AVAILABLE"
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
        "unregistered-event",
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
    elif invalid_bundle == "unregistered-event":
        options["invalid_event_type"] = True
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
        "flag-mismatch",
        "unchanged",
        "missing-character",
        "invalid-hp",
        "duplicate-character",
        "unmatched-result",
        "non-bool-flag",
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

    target_id = UUID(ACTOR_C)
    hp = 7
    if invalid_update == "unchanged":
        hp = 10
    elif invalid_update == "missing-character":
        target_id = UUID(int=99999)
    elif invalid_update == "invalid-hp":
        hp = -1
    bundle = _resolution_bundle(
        accepted.turn_id,
        canonical_updates=((target_id, hp, 10),),
        narration_version=1,
        attack=invalid_update != "unmatched-result",
    )
    if invalid_update == "flag-mismatch":
        narration_input = {**bundle.narration_input, "committed_state_version": 0}
        bundle = replace(bundle, canonical_changed=False, narration_input=narration_input)
    elif invalid_update == "duplicate-character":
        bundle = replace(
            bundle,
            canonical_updates=((*bundle.canonical_updates, bundle.canonical_updates[0])),
        )
    elif invalid_update == "non-bool-flag":
        bundle = replace(bundle, canonical_changed=1)

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
    canonical_updates = ((UUID(ACTOR_C), 7, 10),) if state_changed else ()
    bundle = _resolution_bundle(
        accepted.turn_id,
        canonical_updates=canonical_updates,
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
        ) == (version, len(bundle.events))
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
            (index, version, event.event_type)
            for index, event in enumerate(bundle.events, start=1)
        ]
        assert connection.scalar(
            text(
                "SELECT current_hp FROM mvp_characters "
                "WHERE campaign_id=:campaign AND entity_id=:target"
            ),
            {"campaign": CAMPAIGN_A, "target": ACTOR_C},
        ) == (7 if state_changed else 10)


@pytest.mark.parametrize(
    "mismatch",
    ["command-parent", "result-kind", "event-payload", "event-order"],
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
        action = replace(
            bundle.actions[0],
            command={**bundle.actions[0].command, "turn_id": UUID(int=9999)},
        )
        bundle = replace(bundle, actions=(action,))
    elif mismatch == "result-kind":
        action = replace(bundle.actions[0], result_kind="not_applicable")
        bundle = replace(bundle, actions=(action,))
    elif mismatch == "event-payload":
        payload = dict(bundle.events[0].payload)
        payload["roll"] = {**payload["roll"], "total": 11}
        bundle = replace(bundle, events=(replace(bundle.events[0], payload=payload), *bundle.events[1:]))
    elif mismatch == "event-order":
        bundle = replace(bundle, events=tuple(reversed(bundle.events)))

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
    accepted = _accept_from_database(database, _player_turn())
    _acquire_lease_from_database(database, accepted.turn_id)
    bundle = _resolution_bundle(
        accepted.turn_id,
        canonical_updates=((UUID(ACTOR_C), 7, 10),),
        narration_version=1,
        attack=True,
    )
    conflicting_event_id = bundle.events[-1].id
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
        _commit_resolution_from_database(database, bundle)

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
            {"campaign": CAMPAIGN_A, "target": ACTOR_C},
        ) == 10


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

    with pytest.raises(RuntimeError, match="worker leaseまたはCanonical version"):
        _commit_resolution_from_database(database, bundle)

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
        assert connection.scalar(text("SELECT count(*) FROM events")) == len(bundle.events)


def test_late_narration_does_not_add_choices_after_later_turn(database: Engine) -> None:
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
    _accept_from_database(database, _player_turn(request_id=REQUEST_B))

    saved = _save_narration_from_database(
        database,
        source.turn_id,
        (ChoiceDraft(UUID(CHOICE_C), 1, "遅い選択肢"),),
    )

    assert saved
    with database.connect() as connection:
        assert connection.scalar(
            text("SELECT narration_status FROM turns WHERE id=:turn"),
            {"turn": source.turn_id},
        ) == "completed"
        assert connection.scalar(
            text("SELECT count(*) FROM turn_choices WHERE source_turn_id=:turn"),
            {"turn": source.turn_id},
        ) == 0
