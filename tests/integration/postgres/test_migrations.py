"""実PostgreSQLに対するmigration制約テスト。"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
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

from ai_rpg.application import IdempotencyConflictError, TurnInProgressError
from ai_rpg.contracts import PlayerTurnInput, TurnResponse
from ai_rpg.infrastructure.postgres.repositories import PostgresTurnRepository

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
TURN_A = "00000000-0000-0000-0000-000000000041"
ACTION_A = "00000000-0000-0000-0000-000000000051"
EVENT_A = "00000000-0000-0000-0000-000000000061"
REQUEST_A = "00000000-0000-0000-0000-000000000071"
REQUEST_B = "00000000-0000-0000-0000-000000000072"
CHOICE_A = "00000000-0000-0000-0000-000000000081"
CHOICE_B = "00000000-0000-0000-0000-000000000082"


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
    text_value: str = "進む", request_id: str = REQUEST_A
) -> PlayerTurnInput:
    return PlayerTurnInput.model_validate(
        {
            "request_id": request_id,
            "expected_state_version": 0,
            "actor_id": ACTOR_A,
            "content": {"kind": "text", "text": text_value},
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
    factory: async_sessionmaker[AsyncSession], turn: PlayerTurnInput
) -> TurnResponse:
    async with factory() as session:
        response = await PostgresTurnRepository(session).add(
            UUID(CAMPAIGN_A), UUID(PRINCIPAL_A), turn, 3
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


def _accept_from_database(database: Engine, turn: PlayerTurnInput) -> TurnResponse:
    async def accept() -> TurnResponse:
        url = database.url.render_as_string(hide_password=False)
        async with _postgres_sessions(url) as factory:
            return await _accept_turn(factory, turn)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(accept())


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
        if "select id from campaigns" in normalized and "for update" in normalized:
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
