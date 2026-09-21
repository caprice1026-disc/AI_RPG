"""API、worker、開発fixtureを別processで起動するCLI。"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from ai_rpg.api import OidcDiscoveryError, build_oidc_authenticator, create_app
from ai_rpg.application import AuthenticatedPrincipal
from ai_rpg.config import Settings, get_settings
from ai_rpg.infrastructure.database import create_session_factory
from ai_rpg.infrastructure.postgres import (
    IdentityNotFound,
    IdentityRegistrationConflict,
    PostgresIdentityStore,
)
from ai_rpg.runtime import (
    build_narration_worker,
    build_provider_transport,
    build_resolution_worker,
    migrate_database,
    run_worker,
    seed_development_fixture,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-rpg")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed-dev")

    api = commands.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--dev-principal", type=UUID)

    auth = commands.add_parser("auth")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)

    register = auth_commands.add_parser("register")
    register.add_argument("--subject", required=True)
    register.add_argument("--principal-id", type=UUID)

    disable = auth_commands.add_parser("disable")
    disable.add_argument("--subject", required=True)

    for name in ("resolution-worker", "narration-worker"):
        worker = commands.add_parser(name)
        worker.add_argument("--fake", action="store_true")
        worker.add_argument("--once", action="store_true")
    return parser


def _development_principal(
    principal_id: UUID,
) -> Callable[[], Awaitable[AuthenticatedPrincipal]]:
    async def provide() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            principal_id=principal_id,
            issuer="development",
            subject=str(principal_id),
            authenticated_at=datetime.now(UTC),
            auth_context=frozenset({"development"}),
        )

    return provide


def _configure_event_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def _run_auth_command(
    args: argparse.Namespace,
    settings: Settings,
) -> dict[str, object]:
    if settings.auth_issuer is None:
        raise ValueError("AIRPG_AUTH_ISSUER is required for identity administration")
    subject = args.subject.strip()
    if not subject:
        raise ValueError("subjectは空にできません")
    store = PostgresIdentityStore(create_session_factory(settings.database_url))
    if args.auth_command == "register":
        record = await store.register(settings.auth_issuer, subject, args.principal_id)
        status_value = "active"
    else:
        record = await store.disable(settings.auth_issuer, subject)
        status_value = "disabled"
    return {
        "issuer": record.issuer,
        "subject": record.subject,
        "principal_id": str(record.principal_id),
        "status": status_value,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    _configure_event_loop()

    if args.command == "seed-dev":
        migrate_database(settings.database_url)
        fixture = seed_development_fixture(settings.database_url)
        print(
            json.dumps(
                {
                    "campaign_id": str(fixture.campaign_id),
                    "scene_id": str(fixture.scene_id),
                    "entrance_scene_id": str(fixture.entrance_scene_id),
                    "hall_scene_id": str(fixture.hall_scene_id),
                    "sanctum_scene_id": str(fixture.sanctum_scene_id),
                    "principal_id": str(fixture.principal_id),
                    "actor_id": str(fixture.actor_id),
                }
            )
        )
        return

    if args.command == "auth":
        try:
            result = asyncio.run(_run_auth_command(args, settings))
        except SQLAlchemyError:
            parser.error("identity database operation failed")
        except (ValueError, IdentityNotFound, IdentityRegistrationConflict) as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False))
        return

    if args.command == "api":
        import uvicorn

        if args.dev_principal is not None:
            app = create_app(principal_provider=_development_principal(args.dev_principal))
        else:
            try:
                principal_provider = asyncio.run(build_oidc_authenticator(settings))
            except (ValueError, OidcDiscoveryError) as error:
                parser.error(str(error))
            app = create_app(principal_provider=principal_provider)
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            loop="ai_rpg.runtime:selector_event_loop" if sys.platform == "win32" else "auto",
        )
        return

    try:
        transport = build_provider_transport(settings, fake=args.fake)
    except ValueError as error:
        parser.error(str(error))
    worker = (
        build_resolution_worker(settings, transport, deterministic=args.fake)
        if args.command == "resolution-worker"
        else build_narration_worker(settings, transport)
    )
    processed = asyncio.run(
        run_worker(
            worker,
            once=args.once,
            poll_seconds=1.0,
        )
    )
    if args.once:
        print(json.dumps({"processed": processed}))


if __name__ == "__main__":
    main()
