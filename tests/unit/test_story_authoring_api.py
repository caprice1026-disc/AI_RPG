"""Router boundary tests independent of authentication providers and PostgreSQL."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ai_rpg.api.stories import create_stories_router
from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.ports.adventures import InvalidAdventureError
from ai_rpg.application.ports.repositories import AuthorizationError, IdempotencyConflictError
from ai_rpg.application.stories import StoryError, StoryService, templates
from ai_rpg.contracts.stories import (
    AuthoringDraft,
    CreateStoryPlaytestRequest,
    StoryDraftResponse,
    StoryPlaytestVersionResponse,
)


@pytest.mark.asyncio
async def test_playtest_cannot_bypass_validation_admission() -> None:
    store = MagicMock()
    store.replay = AsyncMock(return_value=None)
    store.cached_validation = AsyncMock(side_effect=StoryError("usage_limit_exceeded", 429))
    store.get_draft = AsyncMock(return_value=StoryDraftResponse(
        story_id=uuid4(), revision=1, draft=templates().templates[0].initial_draft,
        updated_at=datetime.now(UTC),
    ))
    store.create_playtest_version = AsyncMock()
    principal = AuthenticatedPrincipal(uuid4(), "test", "test", datetime.now(UTC), frozenset())
    with pytest.raises(StoryError, match="usage_limit_exceeded"):
        await StoryService(store).create_playtest_version(
            principal, uuid4(), CreateStoryPlaytestRequest(request_id=uuid4(), expected_revision=1),
        )
    store.create_playtest_version.assert_not_awaited()


def test_router_uses_injected_auth_and_serializes_cas_conflicts() -> None:
    principal = AuthenticatedPrincipal(
        principal_id=uuid4(),
        issuer="test",
        subject="test",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset(),
    )

    async def authenticated() -> AuthenticatedPrincipal:
        return principal

    service = MagicMock(spec=StoryService)
    service.templates.return_value = templates()
    service.save = AsyncMock(side_effect=StoryError("revision_conflict"))
    service.create = AsyncMock(
        return_value=StoryDraftResponse(
            story_id=uuid4(),
            revision=1,
            draft=AuthoringDraft(),
            updated_at=datetime.now(UTC),
        )
    )
    app = FastAPI()
    app.include_router(create_stories_router(principal_provider=authenticated, service=service))
    with TestClient(app) as client:
        assert len(client.get("/stories/templates").json()["templates"]) >= 2
        created = client.post("/stories", json={"request_id": str(uuid4()), "draft": {}})
        assert created.status_code == 201
        assert service.create.call_args.args[0] == principal
        conflict = client.put(
            f"/stories/{uuid4()}/draft",
            json={
                "request_id": str(uuid4()),
                "expected_revision": 1,
                "draft": {},
            },
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": {"code": "revision_conflict"}}
        invalid = client.put(
            f"/stories/{uuid4()}/draft",
            json={
                "request_id": str(uuid4()),
                "expected_revision": 0,
                "draft": {},
            },
        )
        assert invalid.status_code == 422


def test_auth_dependency_guards_even_templates_and_does_not_call_service() -> None:
    async def unauthorized() -> AuthenticatedPrincipal:
        raise HTTPException(status_code=401)

    service = MagicMock(spec=StoryService)
    app = FastAPI()
    app.include_router(create_stories_router(principal_provider=unauthorized, service=service))
    with TestClient(app) as client:
        assert client.get("/stories/templates").status_code == 401
        assert client.get(f"/stories/{uuid4()}/draft").status_code == 401
    service.templates.assert_not_called()
    service.get_draft.assert_not_called()


def test_playtest_without_hook_returns_version_and_never_claims_completion() -> None:
    principal = AuthenticatedPrincipal(
        principal_id=uuid4(),
        issuer="test",
        subject="test",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset(),
    )

    async def authenticated() -> AuthenticatedPrincipal:
        return principal

    version = StoryPlaytestVersionResponse(
        story_id=uuid4(), story_version_id=uuid4(), content_hash="a" * 64, draft_revision=1
    )
    service = MagicMock(spec=StoryService)
    service.create_playtest_version = AsyncMock(return_value=version)
    app = FastAPI()
    app.include_router(create_stories_router(principal_provider=authenticated, service=service))
    with TestClient(app) as client:
        response = client.post(
            f"/stories/{version.story_id}/playtests",
            json={
                "request_id": str(uuid4()),
                "expected_revision": 1,
            },
        )
    assert response.status_code == 201
    assert response.json() == version.model_dump(mode="json")
    assert "author_playtest_acknowledged" not in response.json()


@pytest.mark.parametrize(
    "error,status",
    [
        (InvalidAdventureError("allocation required"), 422),
        (AuthorizationError("forbidden"), 403),
        (IdempotencyConflictError("different request"), 409),
    ],
)
def test_playtest_hook_maps_existing_application_errors(error: Exception, status: int) -> None:
    principal = AuthenticatedPrincipal(
        principal_id=uuid4(),
        issuer="test",
        subject="test",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset(),
    )

    async def authenticated() -> AuthenticatedPrincipal:
        return principal

    version = StoryPlaytestVersionResponse(
        story_id=uuid4(), story_version_id=uuid4(), content_hash="a" * 64, draft_revision=1
    )
    service = MagicMock(spec=StoryService)
    service.create_playtest_version = AsyncMock(return_value=version)
    app = FastAPI()
    app.include_router(
        create_stories_router(
            principal_provider=authenticated,
            service=service,
            playtest_creator=AsyncMock(side_effect=error),
        )
    )
    with TestClient(app) as client:
        response = client.post(
            f"/stories/{version.story_id}/playtests",
            json={
                "request_id": str(uuid4()),
                "expected_revision": 1,
            },
        )
    assert response.status_code == status
