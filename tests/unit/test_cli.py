"""管理CLIとproduction API起動分岐の契約。"""

import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from ai_rpg import cli
from ai_rpg.api import OidcDiscoveryError
from ai_rpg.config import Settings
from ai_rpg.infrastructure.postgres import (
    IdentityNotFound,
    IdentityRecord,
    IdentityRegistrationConflict,
)

ISSUER = "https://idp.example.com/"


def _settings(*, auth_issuer: str | None = ISSUER) -> Settings:
    return Settings(database_url="postgresql+psycopg://test/test", auth_issuer=auth_issuer)


def _record(principal_id: UUID, *, disabled: bool = False) -> IdentityRecord:
    created_at = datetime(2026, 9, 19, tzinfo=UTC)
    return IdentityRecord(
        issuer=ISSUER,
        subject="player-1",
        principal_id=principal_id,
        created_at=created_at,
        disabled_at=created_at if disabled else None,
    )


def _patch_auth_store(
    monkeypatch: pytest.MonkeyPatch,
    store: MagicMock,
    settings: Settings | None = None,
) -> tuple[MagicMock, object]:
    sessions = object()
    session_factory = MagicMock(return_value=sessions)
    store_factory = MagicMock(return_value=store)
    monkeypatch.setattr(cli, "get_settings", lambda: settings or _settings())
    monkeypatch.setattr(cli, "_configure_event_loop", lambda: None)
    monkeypatch.setattr(cli, "create_session_factory", session_factory, raising=False)
    monkeypatch.setattr(cli, "PostgresIdentityStore", store_factory, raising=False)
    return store_factory, sessions


def _patch_api_runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    create_app = MagicMock(return_value=object())
    uvicorn_run = MagicMock()
    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "_configure_event_loop", lambda: None)
    monkeypatch.setattr(cli, "create_app", create_app)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=uvicorn_run))
    return create_app, uvicorn_run


def test_auth_register_parser_accepts_optional_principal_id() -> None:
    principal_id = uuid4()

    args = cli._parser().parse_args(
        [
            "auth",
            "register",
            "--subject",
            "player-1",
            "--principal-id",
            str(principal_id),
        ]
    )

    assert args.command == "auth"
    assert args.auth_command == "register"
    assert args.subject == "player-1"
    assert args.principal_id == principal_id


def test_auth_register_prints_safe_json_and_uses_configured_issuer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    principal_id = uuid4()
    store = MagicMock()
    store.register = AsyncMock(return_value=_record(principal_id))
    store_factory, sessions = _patch_auth_store(monkeypatch, store)

    cli.main(
        [
            "auth",
            "register",
            "--subject",
            "player-1",
            "--principal-id",
            str(principal_id),
        ]
    )

    assert json.loads(capsys.readouterr().out) == {
        "issuer": ISSUER,
        "subject": "player-1",
        "principal_id": str(principal_id),
        "status": "active",
    }
    store_factory.assert_called_once_with(sessions)
    store.register.assert_awaited_once_with(ISSUER, "player-1", principal_id)


def test_auth_disable_prints_safe_json_and_strips_subject(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    principal_id = uuid4()
    store = MagicMock()
    store.disable = AsyncMock(return_value=_record(principal_id, disabled=True))
    _patch_auth_store(monkeypatch, store)

    cli.main(["auth", "disable", "--subject", "  player-1  "])

    assert json.loads(capsys.readouterr().out) == {
        "issuer": ISSUER,
        "subject": "player-1",
        "principal_id": str(principal_id),
        "status": "disabled",
    }
    store.disable.assert_awaited_once_with(ISSUER, "player-1")


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (
            ["auth", "register", "--subject", "player-1"],
            IdentityRegistrationConflict("identity registration conflict"),
        ),
        (
            ["auth", "disable", "--subject", "player-1"],
            IdentityNotFound("identity not found"),
        ),
    ],
)
def test_auth_store_errors_are_cli_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    error: Exception,
) -> None:
    store = MagicMock()
    store.register = AsyncMock(side_effect=error)
    store.disable = AsyncMock(side_effect=error)
    _patch_auth_store(monkeypatch, store)

    with pytest.raises(SystemExit, match="2"):
        cli.main(argv)

    assert str(error) in capsys.readouterr().err


@pytest.mark.parametrize(
    ("settings", "subject", "message"),
    [
        (_settings(auth_issuer=None), "player-1", "AIRPG_AUTH_ISSUER"),
        (_settings(), "   ", "subjectは空にできません"),
    ],
)
def test_auth_rejects_invalid_configuration_before_opening_store(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    subject: str,
    message: str,
) -> None:
    store = MagicMock()
    store_factory, _ = _patch_auth_store(monkeypatch, store, settings)

    with pytest.raises(SystemExit, match="2"):
        cli.main(["auth", "register", "--subject", subject])

    assert message in capsys.readouterr().err
    store_factory.assert_not_called()


def test_production_api_awaits_oidc_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    create_app, uvicorn_run = _patch_api_runtime(monkeypatch)
    provider = object()
    builder = AsyncMock(return_value=provider)
    monkeypatch.setattr(cli, "build_oidc_authenticator", builder, raising=False)

    cli.main(["api"])

    builder.assert_awaited_once()
    create_app.assert_called_once_with(principal_provider=provider)
    uvicorn_run.assert_called_once()


def test_development_api_does_not_build_oidc_authenticator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_app, uvicorn_run = _patch_api_runtime(monkeypatch)
    builder = AsyncMock()
    monkeypatch.setattr(cli, "build_oidc_authenticator", builder, raising=False)
    principal_id = uuid4()

    cli.main(["api", "--dev-principal", str(principal_id)])

    builder.assert_not_awaited()
    provider = create_app.call_args.kwargs["principal_provider"]
    assert provider is not None
    uvicorn_run.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [ValueError("invalid oidc settings"), OidcDiscoveryError("discovery failed")],
)
def test_production_api_authentication_setup_errors_are_cli_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    create_app, uvicorn_run = _patch_api_runtime(monkeypatch)
    builder = AsyncMock(side_effect=error)
    monkeypatch.setattr(cli, "build_oidc_authenticator", builder, raising=False)

    with pytest.raises(SystemExit, match="2"):
        cli.main(["api"])

    assert str(error) in capsys.readouterr().err
    create_app.assert_not_called()
    uvicorn_run.assert_not_called()
