"""API、worker、開発fixtureを別processで起動するCLI。"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from ai_rpg.api import create_app
from ai_rpg.application import AuthenticatedPrincipal
from ai_rpg.config import get_settings
from ai_rpg.llm import DevelopmentFakeTransport
from ai_rpg.runtime import (
    build_narration_worker,
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
                    "principal_id": str(fixture.principal_id),
                    "actor_id": str(fixture.actor_id),
                }
            )
        )
        return

    if args.command == "api":
        import uvicorn

        app = (
            create_app(principal_provider=_development_principal(args.dev_principal))
            if args.dev_principal is not None
            else create_app()
        )
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            loop="ai_rpg.runtime:selector_event_loop" if sys.platform == "win32" else "auto",
        )
        return

    if not args.fake:
        parser.error("worker起動には現在 --fake が必要です")
    transport = DevelopmentFakeTransport()
    worker = (
        build_resolution_worker(settings, transport, deterministic=True)
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
