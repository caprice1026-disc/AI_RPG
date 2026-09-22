"""Browser authentication persistence against the exclusive real PostgreSQL DB."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import test_migrations
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_migrations import _postgres_sessions, _run_alembic

from ai_rpg.infrastructure.postgres.browser_sessions import PostgresBrowserSessionStore
from ai_rpg.infrastructure.postgres.identities import PostgresIdentityStore
from ai_rpg.infrastructure.postgres.models import Base

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("AIRPG_TEST_DATABASE_URL"), reason="Test DB required"),
]
ISSUER = "https://idp.example.test/"
SUBJECT = "private-player-subject"
database = test_migrations.database
empty_database_url = test_migrations.empty_database_url


@asynccontextmanager
async def stores(
    database: Engine,
) -> AsyncIterator[
    tuple[PostgresBrowserSessionStore, PostgresIdentityStore, async_sessionmaker[AsyncSession]]
]:
    async with _postgres_sessions(database.url.render_as_string(hide_password=False)) as factory:
        yield PostgresBrowserSessionStore(factory), PostgresIdentityStore(factory), factory


def test_browser_schema_has_private_identity_reference_and_expiry_indexes(database: Engine) -> None:
    schema = inspect(database)
    assert {"login_attempts", "browser_sessions"} <= set(schema.get_table_names())
    expected = {
        "login_attempts": {
            "state_digest",
            "binding_digest",
            "nonce_digest",
            "code_verifier",
            "expires_at",
        },
        "browser_sessions": {
            "token_digest",
            "identity_id",
            "csrf_token",
            "created_at",
            "expires_at",
        },
    }
    for table, columns in expected.items():
        actual = schema.get_columns(table)
        assert {column["name"] for column in actual} == columns
        assert all(not column["nullable"] for column in actual)
        assert any(index["column_names"] == ["expires_at"] for index in schema.get_indexes(table))
        assert set(Base.metadata.tables[table].columns.keys()) == columns
    assert schema.get_pk_constraint("principal_identities")["constrained_columns"] == [
        "issuer",
        "subject",
    ]
    assert any(
        constraint["column_names"] == ["identity_id"]
        for constraint in schema.get_unique_constraints("principal_identities")
    )
    foreign_key = schema.get_foreign_keys("browser_sessions")[0]
    assert foreign_key["constrained_columns"] == ["identity_id"]
    assert foreign_key["referred_table"] == "principal_identities"
    assert foreign_key["referred_columns"] == ["identity_id"]


def test_migration_backfills_legacy_identities_and_round_trips(empty_database_url: str) -> None:
    _run_alembic(empty_database_url, "upgrade", "0012_registered_action_input")
    engine = create_engine(empty_database_url)
    principal_id = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO principals(id) VALUES (:id)"), {"id": principal_id}
            )
            connection.execute(
                text(
                    "INSERT INTO principal_identities(issuer,subject,principal_id) VALUES (:issuer,:subject,:id)"
                ),
                [
                    {"issuer": ISSUER, "subject": subject, "id": principal_id}
                    for subject in ("legacy", "disabled")
                ],
            )
            connection.execute(
                text("UPDATE principal_identities SET disabled_at=now() WHERE subject='disabled'")
            )
            before = connection.execute(
                text("SELECT * FROM principal_identities ORDER BY subject")
            ).all()
        _run_alembic(empty_database_url, "upgrade", "head")
        with engine.connect() as connection:
            ids = (
                connection.execute(text("SELECT identity_id FROM principal_identities"))
                .scalars()
                .all()
            )
            assert len(ids) == len(set(ids)) == 2
            assert all(isinstance(identity_id, UUID) for identity_id in ids)
        _run_alembic(empty_database_url, "downgrade", "0012_registered_action_input")
        assert "browser_sessions" not in inspect(engine).get_table_names()
        assert "login_attempts" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT * FROM principal_identities ORDER BY subject")
                ).all()
                == before
            )
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(
                text("UPDATE principal_identities SET subject='changed' WHERE subject='legacy'")
            )
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE principal_identities SET disabled_at=now() WHERE subject='legacy'")
            )
        _run_alembic(empty_database_url, "upgrade", "head")
        assert "identity_id" in {
            column["name"] for column in inspect(engine).get_columns("principal_identities")
        }
    finally:
        engine.dispose()


def test_login_hashes_secrets_wrong_binding_preserves_attempt_and_consumes_once(
    database: Engine,
) -> None:
    async def exercise() -> None:
        async with stores(database) as (store, _, factory):
            now = datetime.now(UTC)
            await store.create_login(
                state="state",
                binding="binding",
                nonce="nonce",
                code_verifier="pkce-verifier",
                expires_at=now + timedelta(minutes=5),
            )
            async with factory() as session:
                row = (await session.execute(text("SELECT * FROM login_attempts"))).mappings().one()
                assert (
                    row["state_digest"]
                    == "4ba69735ca53765ed6a709edb56c6ea236b7193a3b29a6b390c346f0f4340e4e"
                )
                assert (
                    row["binding_digest"]
                    == "80f70afeef3caa57646fd20afb95be9c3f2c03d38e091906de31838813dcc22e"
                )
                assert (
                    row["nonce_digest"]
                    == "78377b525757b494427f89014f97d79928f3938d14eb51e20fb5dec9834eb304"
                )
                assert not {"state", "binding", "nonce"}.intersection(row.values())
            assert await store.consume_login(state="state", binding="wrong", now=now) is None
            assert await store.consume_login(state="unknown", binding="binding", now=now) is None
            attempt = await store.consume_login(state="state", binding="binding", now=now)
            assert attempt is not None
            assert attempt.nonce_digest == row["nonce_digest"]
            assert attempt.code_verifier == "pkce-verifier"
            with pytest.raises(FrozenInstanceError):
                attempt.code_verifier = "changed"
            assert await store.consume_login(state="state", binding="binding", now=now) is None
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM login_attempts")) == 0

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_concurrent_login_consumers_have_exactly_one_winner(database: Engine) -> None:
    async def exercise() -> None:
        async with stores(database) as (store, _, _):
            now = datetime.now(UTC)
            await store.create_login(
                state="concurrent",
                binding="binding",
                nonce="nonce",
                code_verifier="verifier",
                expires_at=now + timedelta(minutes=5),
            )
            barrier = asyncio.Barrier(8)

            async def consume() -> object:
                await barrier.wait()
                return await store.consume_login(state="concurrent", binding="binding", now=now)

            results = await asyncio.wait_for(
                asyncio.gather(*(consume() for _ in range(8))), timeout=10
            )
            assert sum(result is not None for result in results) == 1

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_login_expiry_boundary_and_creation_cleanup(database: Engine) -> None:
    async def exercise() -> None:
        async with stores(database) as (store, _, factory):
            now = datetime.now(UTC)
            expiry = now + timedelta(minutes=5)
            await store.create_login(
                state="boundary",
                binding="binding",
                nonce="nonce",
                code_verifier="verifier",
                expires_at=expiry,
            )
            assert (
                await store.consume_login(state="boundary", binding="binding", now=expiry) is None
            )
            assert (
                await store.consume_login(
                    state="boundary", binding="binding", now=expiry + timedelta(microseconds=1)
                )
                is None
            )
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE login_attempts SET expires_at=now()-interval '1 second'")
                )
            await store.create_login(
                state="fresh",
                binding="binding",
                nonce="nonce",
                code_verifier="fresh",
                expires_at=expiry,
            )
            async with factory() as session:
                assert (
                    await session.execute(text("SELECT code_verifier FROM login_attempts"))
                ).scalars().all() == ["fresh"]
            assert (
                await store.consume_login(
                    state="fresh", binding="binding", now=expiry - timedelta(microseconds=1)
                )
                is not None
            )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_session_round_trip_hashes_token_and_logout_is_idempotent(database: Engine) -> None:
    async def exercise() -> None:
        async with stores(database) as (store, identities, factory):
            identity = await identities.register(ISSUER, SUBJECT)
            assert await identities.register(ISSUER, SUBJECT) == identity
            now = datetime.now(UTC)
            expiry = now + timedelta(hours=8)
            assert (
                await store.create_session(
                    issuer=ISSUER,
                    subject=SUBJECT,
                    token="token",
                    csrf_token="csrf",
                    created_at=now,
                    expires_at=expiry,
                )
                == identity.principal_id
            )
            async with factory() as session:
                row = (
                    (await session.execute(text("SELECT * FROM browser_sessions"))).mappings().one()
                )
                assert (
                    row["token_digest"]
                    == "3c469e9d6c5875d37a43f353d4f88e61fcf812c66eee3457465a40b0da4153e0"
                )
                assert not {"token", SUBJECT}.intersection(row.values())
                assert row["identity_id"] == await session.scalar(
                    text("SELECT identity_id FROM principal_identities")
                )
            resolved = await store.resolve_session(token="token", now=now)
            assert resolved is not None
            assert (resolved.principal_id, resolved.issuer, resolved.subject) == (
                identity.principal_id,
                ISSUER,
                SUBJECT,
            )
            assert (resolved.authenticated_at, resolved.expires_at, resolved.csrf_token) == (
                now,
                expiry,
                "csrf",
            )
            with pytest.raises(FrozenInstanceError):
                resolved.csrf_token = "changed"
            assert await store.resolve_session(token="unknown", now=now) is None
            assert await store.resolve_session(token=row["token_digest"], now=now) is None
            await store.delete_session("unknown")
            assert await store.resolve_session(token="token", now=now) == resolved
            await store.delete_session("token")
            await store.delete_session("token")
            assert await store.resolve_session(token="token", now=now) is None
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM browser_sessions")) == 0

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_unregistered_wrong_issuer_and_disabled_identities_cannot_create_sessions(
    database: Engine,
) -> None:
    async def exercise() -> None:
        async with stores(database) as (store, identities, factory):
            await identities.register(ISSUER, SUBJECT)
            await identities.disable(ISSUER, SUBJECT)
            now = datetime.now(UTC)
            for issuer, subject in (
                (ISSUER, "unregistered"),
                ("https://other.example/", SUBJECT),
                (ISSUER, SUBJECT),
            ):
                assert (
                    await store.create_session(
                        issuer=issuer,
                        subject=subject,
                        token="token",
                        csrf_token="csrf",
                        created_at=now,
                        expires_at=now + timedelta(hours=8),
                    )
                    is None
                )
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM browser_sessions")) == 0
                assert await session.scalar(text("SELECT count(*) FROM principal_identities")) == 1

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_identity_disablement_revokes_all_existing_sessions(database: Engine) -> None:
    async def exercise() -> None:
        async with stores(database) as (store, identities, _):
            identity = await identities.register(ISSUER, SUBJECT)
            await identities.register(ISSUER, "other-identity", identity.principal_id)
            now = datetime.now(UTC)
            for token, subject in (
                ("first", SUBJECT),
                ("second", SUBJECT),
                ("other", "other-identity"),
            ):
                await store.create_session(
                    issuer=ISSUER,
                    subject=subject,
                    token=token,
                    csrf_token="csrf",
                    created_at=now,
                    expires_at=now + timedelta(hours=8),
                )
            await identities.disable(ISSUER, SUBJECT)
            assert await store.resolve_session(token="first", now=now) is None
            assert await store.resolve_session(token="second", now=now) is None
            assert await store.resolve_session(token="other", now=now) is not None

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_session_expiry_is_absolute_and_creation_cleans_expired_rows(database: Engine) -> None:
    async def exercise() -> None:
        async with stores(database) as (store, identities, factory):
            await identities.register(ISSUER, SUBJECT)
            now = datetime.now(UTC)
            expiry = now + timedelta(hours=8)
            await store.create_session(
                issuer=ISSUER,
                subject=SUBJECT,
                token="token",
                csrf_token="csrf",
                created_at=now,
                expires_at=expiry,
            )
            assert (
                await store.resolve_session(token="token", now=expiry - timedelta(microseconds=1))
                is not None
            )
            assert await store.resolve_session(token="token", now=expiry) is None
            assert (
                await store.resolve_session(token="token", now=expiry + timedelta(microseconds=1))
                is None
            )
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE browser_sessions SET created_at=now()-interval '2 hours', expires_at=now()-interval '1 hour'"
                    )
                )
            await store.create_session(
                issuer=ISSUER,
                subject=SUBJECT,
                token="fresh",
                csrf_token="fresh-csrf",
                created_at=now,
                expires_at=expiry,
            )
            async with factory() as session:
                assert (
                    await session.execute(text("SELECT csrf_token FROM browser_sessions"))
                ).scalars().all() == ["fresh-csrf"]

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


@pytest.mark.parametrize("lifetime", [timedelta(0), timedelta(seconds=-1)])
def test_database_rejects_session_expiry_not_after_creation(
    database: Engine, lifetime: timedelta
) -> None:
    async def exercise() -> None:
        async with stores(database) as (store, identities, factory):
            await identities.register(ISSUER, SUBJECT)
            now = datetime.now(UTC)
            with pytest.raises(IntegrityError):
                await store.create_session(
                    issuer=ISSUER,
                    subject=SUBJECT,
                    token="token",
                    csrf_token="csrf",
                    created_at=now,
                    expires_at=now + lifetime,
                )
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM browser_sessions")) == 0

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_identity_id_cannot_change_during_disablement(database: Engine) -> None:
    async def exercise() -> None:
        async with stores(database) as (_, identities, factory):
            await identities.register(ISSUER, SUBJECT)
            with pytest.raises(DBAPIError):
                async with factory.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE principal_identities SET identity_id=gen_random_uuid(), disabled_at=now()"
                        )
                    )
            assert await identities.resolve(ISSUER, SUBJECT) is not None
            await identities.disable(ISSUER, SUBJECT)
            with pytest.raises(DBAPIError):
                async with factory.begin() as session:
                    await session.execute(text("UPDATE principal_identities SET disabled_at=NULL"))

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())


def test_session_creation_waits_for_identity_disablement_to_commit(database: Engine) -> None:
    async def exercise() -> None:
        async with stores(database) as (store, identities, factory):
            await identities.register(ISSUER, SUBJECT)
            now = datetime.now(UTC)
            async with factory() as blocker:
                await blocker.execute(
                    text(
                        "UPDATE principal_identities SET disabled_at=now() WHERE issuer=:issuer AND subject=:subject"
                    ),
                    {"issuer": ISSUER, "subject": SUBJECT},
                )
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                creating = asyncio.create_task(
                    store.create_session(
                        issuer=ISSUER,
                        subject=SUBJECT,
                        token="token",
                        csrf_token="csrf",
                        created_at=now,
                        expires_at=now + timedelta(hours=8),
                    )
                )
                try:
                    async with asyncio.timeout(10), factory() as observer:
                        while not creating.done():
                            waiting = await observer.scalar(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))"
                                ),
                                {"pid": blocker_pid},
                            )
                            if waiting:
                                break
                            await asyncio.sleep(0.01)
                        assert not creating.done(), (
                            "Session creation bypassed the identity row lock"
                        )
                    await blocker.commit()
                    assert await asyncio.wait_for(creating, timeout=10) is None
                finally:
                    await blocker.rollback()
                    if not creating.done():
                        creating.cancel()
                    await asyncio.gather(creating, return_exceptions=True)
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM browser_sessions")) == 0

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())
