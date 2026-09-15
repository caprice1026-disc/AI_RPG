"""実PostgreSQLに対するmigration制約テスト。"""

import os
import subprocess
import sys
from collections.abc import Generator, Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

URL = os.getenv("AIRPG_TEST_DATABASE_URL")
ROOT = Path(__file__).parents[3]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="AIRPG_TEST_DATABASE_URLが未設定です"),
]

CAMPAIGN_A = "00000000-0000-0000-0000-000000000001"
CAMPAIGN_B = "00000000-0000-0000-0000-000000000002"
SCENE_A = "00000000-0000-0000-0000-000000000011"
PRINCIPAL_A = "00000000-0000-0000-0000-000000000021"
PRINCIPAL_B = "00000000-0000-0000-0000-000000000022"
ACTOR_A = "00000000-0000-0000-0000-000000000031"
ACTOR_B = "00000000-0000-0000-0000-000000000032"
TURN_A = "00000000-0000-0000-0000-000000000041"
ACTION_A = "00000000-0000-0000-0000-000000000051"
EVENT_A = "00000000-0000-0000-0000-000000000061"


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
