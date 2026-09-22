"""Browser OIDC flow through real ASGI routes with an HTTP provider boundary."""

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request

from ai_rpg.api.app import create_app
from ai_rpg.api.auth import OidcConfiguration, OidcJwtVerifier
from ai_rpg.api.browser_auth import BrowserAuthenticator
from ai_rpg.config import Settings

ISSUER = "https://identity.example"
ORIGIN = "https://game.example"


class MemorySessions:
    """Only the DB boundary is replaced; flow/JWT/cookies stay real."""

    def __init__(self):
        self.logins = {}
        self.sessions = {}
        self.principal_id = uuid4()
        self.registered = True
        self.disabled = False

    async def create_login(self, **values):
        self.logins[values["state"]] = values

    async def consume_login(self, *, state, binding, now):
        row = self.logins.get(state)
        if not row or row["binding"] != binding or row["expires_at"] <= now:
            return None
        del self.logins[state]
        return SimpleNamespace(
            nonce_digest=hashlib.sha256(row["nonce"].encode()).hexdigest(),
            code_verifier=row["code_verifier"],
        )

    async def create_session(self, **values):
        if not self.registered or self.disabled:
            return None
        self.sessions[values["token"]] = SimpleNamespace(
            principal_id=self.principal_id, issuer=values["issuer"], subject=values["subject"],
            authenticated_at=values["created_at"], expires_at=values["expires_at"],
            csrf_token=values["csrf_token"],
        )
        return self.principal_id

    async def resolve_session(self, *, token, now):
        row = self.sessions.get(token)
        return row if row and row.expires_at > now and not self.disabled else None

    async def delete_session(self, token):
        self.sessions.pop(token, None)


@pytest.fixture
def setup_auth():
    store = MemorySessions()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key()))
    verifier = OidcJwtVerifier(ISSUER, "web-client", ("RS256",), jwks)
    provider_requests = []
    provider = {"status": 200, "nonce_override": None}

    def token_response(request):
        provider_requests.append(request)
        values = parse_qs(request.content.decode())
        assert values["redirect_uri"] == [ORIGIN + "/auth/callback"]
        assert values["code_verifier"] == [provider["verifier"]]
        claims = {
            "iss": ISSUER, "aud": "web-client", "sub": "private-subject",
            "iat": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(minutes=10),
            "nonce": provider["nonce_override"] or provider["nonce"],
        }
        return httpx.Response(provider["status"], json={"id_token": jwt.encode(
            claims, key, algorithm="RS256",
        )})

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(token_response))
    bearer = AsyncMock(side_effect=Exception("Bearer must not be called"))
    settings = Settings(_env_file=None, auth_issuer=ISSUER, auth_audience="api",
                        auth_client_id="web-client", auth_app_origin=ORIGIN)
    auth = BrowserAuthenticator(
        settings, OidcConfiguration(ISSUER, ISSUER + "/keys", ISSUER + "/authorize",
                                    ISSUER + "/token", ("none",)),
        store, verifier, bearer, client=outbound,
    )
    app = create_app(principal_provider=auth, browser_auth=auth)
    return SimpleNamespace(auth=auth, store=store, app=app, provider=provider,
                           requests=provider_requests, bearer=bearer, client=outbound)


async def begin(client, setup):
    response = await client.get("/auth/login")
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid"]
    state = query["state"][0]
    setup.provider["nonce"] = query["nonce"][0]
    setup.provider["verifier"] = setup.store.logins[state]["code_verifier"]
    return state


async def login(client, setup):
    state = await begin(client, setup)
    response = await client.get("/auth/callback", params={"state": state, "code": "private-code"})
    return response, state


@pytest.mark.asyncio
async def test_browser_login_session_and_logout_are_cookie_bound(setup_auth):
    setup = setup_auth
    async with setup.client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url=ORIGIN,
    ) as client:
        response, state = await login(client, setup)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" in response.headers["set-cookie"]
        session = await client.get("/auth/session")
        assert session.status_code == 200
        body = session.json()
        assert body["principal_id"] == str(setup.store.principal_id)
        assert body["mode"] == "session"
        assert "private-subject" not in session.text
        assert "private-code" not in session.text
        assert session.headers["cache-control"] == "no-store"
        retry = await client.get("/auth/callback", params={"state": state, "code": "private-code"})
        assert "login_error=" in retry.headers["location"]
        assert len(setup.requests) == 1
        logout = await client.post("/auth/logout", headers={
            "Origin": ORIGIN, "X-CSRF-Token": body["csrf_token"],
        })
        assert logout.status_code == 204
        assert not setup.store.sessions
        assert (await client.get("/auth/session")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"},
                                      {"Origin": ORIGIN, "X-CSRF-Token": "wrong"}])
async def test_cookie_mutation_requires_origin_and_csrf(setup_auth, headers):
    setup = setup_auth
    async with setup.client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url=ORIGIN,
    ) as client:
        await login(client, setup)
        response = await client.post("/adventures", json={}, headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "CSRF_REJECTED"
        assert (await client.post("/auth/logout", headers=headers)).status_code == 403
        assert setup.store.sessions


@pytest.mark.asyncio
async def test_callback_cannot_cross_browser_cookie_binding(setup_auth):
    setup = setup_auth
    async with setup.client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url=ORIGIN,
    ) as client:
        state = await begin(client, setup)
        client.cookies.clear()
        response = await client.get("/auth/callback", params={"state": state, "code": "code"})
        assert "login_error=" in response.headers["location"]
        assert not setup.requests
        assert not setup.store.sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["nonce", "unregistered", "unavailable"])
async def test_login_failures_do_not_issue_a_session(setup_auth, failure):
    setup = setup_auth
    if failure == "nonce":
        setup.provider["nonce_override"] = "wrong"
    elif failure == "unregistered":
        setup.store.registered = False
    else:
        setup.provider["status"] = 503
    async with setup.client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url=ORIGIN,
    ) as client:
        response, _ = await login(client, setup)
        assert "login_error=" in response.headers["location"]
        assert "private" not in response.headers["location"]
        assert not setup.store.sessions


@pytest.mark.asyncio
async def test_identity_disable_and_expiry_invalidate_existing_cookie(setup_auth):
    setup = setup_auth
    async with setup.client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url=ORIGIN,
    ) as client:
        await login(client, setup)
        setup.store.disabled = True
        assert (await client.get("/auth/session")).status_code == 401
        setup.store.disabled = False
        for session in setup.store.sessions.values():
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert (await client.get("/auth/session")).status_code == 401


@pytest.mark.asyncio
async def test_invalid_bearer_does_not_fall_back_to_valid_cookie(setup_auth):
    setup = setup_auth
    setup.bearer.side_effect = HTTPException(401, detail={"code": "UNAUTHENTICATED"})
    async with setup.client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url=ORIGIN,
    ) as client:
        await login(client, setup)
        response = await client.get("/auth/session", headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
        assert (await client.get("/auth/session")).status_code == 200


@pytest.mark.asyncio
async def test_stream_guard_observes_logout_and_identity_disable(setup_auth):
    setup = setup_auth
    async with setup.client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url=ORIGIN,
    ) as client:
        await login(client, setup)
        request = Request({"type": "http", "method": "GET", "path": "/events", "headers": [
            (b"cookie", client.build_request("GET", "/").headers["cookie"].encode()),
        ]})
        await setup.auth(request)
        assert await setup.auth.stream_is_valid(request)
        setup.store.disabled = True
        assert not await setup.auth.stream_is_valid(request)
        setup.store.disabled = False
        setup.store.sessions.clear()
        assert not await setup.auth.stream_is_valid(request)


@pytest.mark.asyncio
async def test_login_attempt_expiry_prevents_token_exchange(setup_auth):
    setup = setup_auth
    async with setup.client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app), base_url=ORIGIN,
    ) as client:
        state = await begin(client, setup)
        setup.store.logins[state]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
        response = await client.get("/auth/callback", params={"state": state, "code": "code"})
        assert response.headers["location"] == "/?login_error=LOGIN_REJECTED"
        assert not setup.requests
