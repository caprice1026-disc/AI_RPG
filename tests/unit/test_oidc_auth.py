"""OIDC DiscoveryとBearer JWT検証のUnit Test。"""

import logging
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import jwt
import jwt.api_jwk as jwt_api_jwk
import jwt.jwk_set_cache as jwt_jwk_set_cache
import jwt.jwks_client as jwt_jwks_client
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.exc import SQLAlchemyError

import ai_rpg.api as api_module
import ai_rpg.api.auth as auth_module
from ai_rpg.api.auth import (
    AuthenticationUnavailableError,
    InvalidCredentialError,
    OidcDiscoveryError,
    OidcJwtVerifier,
    discover_oidc,
)

ISSUER = "https://idp.example.com/"
AUDIENCE = "ai-rpg-api"
PRINCIPAL_ID = UUID("00000000-0000-0000-0000-000000000021")


@dataclass
class _MonotonicClock:
    value: float = 1_000.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def jwks_clock(monkeypatch: pytest.MonkeyPatch) -> _MonotonicClock:
    clock = _MonotonicClock()
    monkeypatch.setattr(jwt_api_jwk, "time", clock)
    monkeypatch.setattr(jwt_jwk_set_cache, "time", clock)
    monkeypatch.setattr(jwt_jwks_client, "time", clock)
    return clock


@pytest.mark.asyncio
async def test_discovery_requires_exact_issuer_and_https_jwks() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://idp.example.com/.well-known/openid-configuration"
        )
        return httpx.Response(
            200,
            json={
                "issuer": "https://idp.example.com/",
                "jwks_uri": "https://idp.example.com/keys",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        configuration = await discover_oidc("https://idp.example.com/", client)

    assert configuration.issuer == "https://idp.example.com/"
    assert configuration.jwks_uri == "https://idp.example.com/keys"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "issuer": "https://other.example.com/",
            "jwks_uri": "https://idp.example.com/keys",
        },
        {
            "issuer": "https://idp.example.com/",
            "jwks_uri": "http://idp.example.com/keys",
        },
        {
            "issuer": "https://idp.example.com/",
            "jwks_uri": "https://[",
        },
        [],
        {},
        {"issuer": "https://idp.example.com/"},
    ],
    ids=[
        "different-issuer",
        "non-https-jwks",
        "malformed-jwks",
        "non-object",
        "missing-fields",
        "missing-jwks-uri",
    ],
)
async def test_discovery_rejects_invalid_metadata(payload: object) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OidcDiscoveryError) as error:
            await discover_oidc("https://idp.example.com/", client)

    assert "player-1" not in str(error.value)


@pytest.mark.asyncio
async def test_discovery_rejects_malformed_json_without_exposing_body() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="private-response-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OidcDiscoveryError) as error:
            await discover_oidc("https://idp.example.com/", client)

    assert "private-response-body" not in str(error.value)


@pytest.mark.asyncio
async def test_discovery_maps_http_errors_without_exposing_body() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="private-response-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OidcDiscoveryError) as error:
            await discover_oidc("https://idp.example.com/", client)

    assert "private-response-body" not in str(error.value)


@pytest.mark.asyncio
async def test_discovery_maps_timeout_without_exposing_request_data() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private-timeout-detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OidcDiscoveryError) as error:
            await discover_oidc("https://idp.example.com/", client)

    assert "private-timeout-detail" not in str(error.value)


@pytest.fixture
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, object]:
    jwk: dict[str, object] = jwt.algorithms.RSAAlgorithm.to_jwk(
        key.public_key(), as_dict=True
    )
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


def _jwks_client(
    *payloads: dict[str, object],
) -> tuple[jwt.PyJWKClient, Mock]:
    client = auth_module._ProviderJWKClient(
        "https://idp.example.com/keys",
        lifespan=300,
        timeout=5,
        cooldown_duration=30,
    )
    call_index = 0

    def fetch_data() -> dict[str, object]:
        nonlocal call_index
        payload = payloads[min(call_index, len(payloads) - 1)]
        call_index += 1
        if client.jwk_set_cache is not None:
            client.jwk_set_cache.put(payload)
        client._last_successful_fetch = jwt_jwks_client.time.monotonic()
        return payload

    fetch = Mock(side_effect=fetch_data)
    client.fetch_data = fetch
    return client, fetch


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "player-1",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "nbf": now - timedelta(seconds=1),
    }
    claims.update(overrides)
    return claims


def _token(
    key: rsa.RSAPrivateKey,
    claims: dict[str, object],
    *,
    kid: str = "key-1",
    algorithm: str = "RS256",
) -> str:
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def _verifier(client: jwt.PyJWKClient) -> OidcJwtVerifier:
    return OidcJwtVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=("RS256",),
        jwks=client,
    )


@pytest.mark.asyncio
async def test_verifier_returns_only_validated_identity_fields(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    client, _ = _jwks_client({"keys": [_public_jwk(signing_key, "key-1")]})
    token = _token(signing_key, _claims())
    started_at = datetime.now(UTC)

    identity = await _verifier(client).verify(token)

    assert identity.issuer == ISSUER
    assert identity.subject == "player-1"
    assert identity.expires_at > identity.authenticated_at >= started_at
    assert {field.name for field in fields(identity)} == {
        "issuer",
        "subject",
        "authenticated_at",
        "expires_at",
    }
    assert not hasattr(identity, "email")


@pytest.mark.asyncio
async def test_verifier_rejects_bad_signature(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client, _ = _jwks_client({"keys": [_public_jwk(signing_key, "key-1")]})
    token = _token(other_key, _claims())

    with pytest.raises(InvalidCredentialError) as error:
        await _verifier(client).verify(token)

    assert token not in str(error.value)
    assert "player-1" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "exp"])
async def test_verifier_requires_oidc_claims(
    signing_key: rsa.RSAPrivateKey,
    claim: str,
) -> None:
    claims = _claims()
    del claims[claim]
    client, _ = _jwks_client({"keys": [_public_jwk(signing_key, "key-1")]})
    token = _token(signing_key, claims)

    with pytest.raises(InvalidCredentialError):
        await _verifier(client).verify(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "overrides"),
    [
        ("wrong-issuer", {"iss": "https://other.example.com/"}),
        ("wrong-audience", {"aud": "other-api"}),
        ("expired-within-leeway", {"exp": datetime.now(UTC) - timedelta(seconds=1)}),
        ("future-nbf", {"nbf": datetime.now(UTC) + timedelta(minutes=5)}),
        ("invalid-iat", {"iat": "not-a-number"}),
        ("empty-subject", {"sub": ""}),
        ("boolean-expiry", {"exp": True}),
        ("string-expiry", {"exp": "4102444800"}),
    ],
)
async def test_verifier_rejects_invalid_claims(
    signing_key: rsa.RSAPrivateKey,
    name: str,
    overrides: dict[str, object],
) -> None:
    del name
    client, _ = _jwks_client({"keys": [_public_jwk(signing_key, "key-1")]})
    token = _token(signing_key, _claims(**overrides))

    with pytest.raises(InvalidCredentialError) as error:
        await _verifier(client).verify(token)

    assert token not in str(error.value)
    assert "player-1" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("exp", ["private-exp-value"]),
        ("exp", {"private-exp-value": 1}),
        ("exp", float("inf")),
        ("exp", float("nan")),
        ("iat", None),
        ("iat", float("inf")),
        ("iat", float("nan")),
        ("nbf", None),
        ("nbf", float("inf")),
        ("nbf", float("nan")),
    ],
    ids=[
        "exp-list",
        "exp-object",
        "exp-infinity",
        "exp-nan",
        "iat-null",
        "iat-infinity",
        "iat-nan",
        "nbf-null",
        "nbf-infinity",
        "nbf-nan",
    ],
)
async def test_verifier_maps_malformed_numeric_dates_without_exposing_claims(
    signing_key: rsa.RSAPrivateKey,
    claim: str,
    value: object,
) -> None:
    client, _ = _jwks_client({"keys": [_public_jwk(signing_key, "key-1")]})
    token = _token(signing_key, _claims(**{claim: value}))

    with pytest.raises(InvalidCredentialError) as error:
        await _verifier(client).verify(token)

    assert str(error.value) == "Bearer tokenが不正です"
    assert token not in str(error.value)
    assert "private-exp-value" not in str(error.value)


@pytest.mark.asyncio
async def test_verifier_rejects_disallowed_algorithm(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    client, _ = _jwks_client({"keys": [_public_jwk(signing_key, "key-1")]})
    token = _token(signing_key, _claims(), algorithm="RS384")

    with pytest.raises(InvalidCredentialError):
        await _verifier(client).verify(token)


@pytest.mark.asyncio
async def test_verifier_maps_jwks_connection_failure_without_details(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    client = jwt.PyJWKClient("https://idp.example.com/keys")
    client.fetch_data = Mock(
        side_effect=jwt.PyJWKClientConnectionError("private-provider-detail")
    )
    token = _token(signing_key, _claims())

    with pytest.raises(AuthenticationUnavailableError) as error:
        await _verifier(client).verify(token)

    assert "private-provider-detail" not in str(error.value)
    assert token not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_response",
    [
        JSONDecodeError("private-provider-body", "private-provider-body", 0),
        [],
        {"keys": []},
        {"keys": [None]},
        {"keys": [{"kid": "private-kid", "kty": "private-kty"}]},
    ],
    ids=[
        "malformed-json",
        "non-object",
        "empty-keys",
        "non-object-key",
        "unusable-keys",
    ],
)
async def test_authenticator_maps_invalid_jwks_document_to_unavailable(
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    provider_response: object,
) -> None:
    settings = Mock()
    settings.require_oidc.return_value = (ISSUER, AUDIENCE, ("RS256",))
    settings.database_url = "postgresql+psycopg://airpg@database/airpg"
    discovery = AsyncMock(
        return_value=auth_module.OidcConfiguration(
            issuer=ISSUER,
            jwks_uri="https://idp.example.com/keys",
        )
    )
    resolver = AsyncMock(return_value=PRINCIPAL_ID)
    store = Mock(resolve=resolver)

    def fetch_data(_: jwt.PyJWKClient) -> object:
        if isinstance(provider_response, Exception):
            raise provider_response
        return provider_response

    monkeypatch.setattr(auth_module, "discover_oidc", discovery)
    monkeypatch.setattr(auth_module.jwt.PyJWKClient, "fetch_data", fetch_data)
    monkeypatch.setattr(auth_module, "create_session_factory", Mock())
    monkeypatch.setattr(auth_module, "PostgresIdentityStore", Mock(return_value=store))
    authenticator = await auth_module.build_oidc_authenticator(settings)
    token = _token(signing_key, _claims())

    with pytest.raises(auth_module.HTTPException) as error:
        await authenticator(f"Bearer {token}")

    assert error.value.status_code == 503
    assert error.value.detail == {"code": "AUTHENTICATION_UNAVAILABLE"}
    assert "private-provider-body" not in str(error.value)
    assert "private-kid" not in str(error.value)
    assert token not in str(error.value)
    assert "player-1" not in str(error.value)
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_verifier_refreshes_unknown_kid_only_after_cooldown(
    signing_key: rsa.RSAPrivateKey,
    jwks_clock: _MonotonicClock,
) -> None:
    jwks = {"keys": [_public_jwk(signing_key, "key-1")]}
    client, fetch = _jwks_client(jwks, jwks)
    token = _token(signing_key, _claims(), kid="unknown-key")

    with pytest.raises(InvalidCredentialError):
        await _verifier(client).verify(token)

    assert fetch.call_count == 1

    jwks_clock.advance(30)

    with pytest.raises(InvalidCredentialError):
        await _verifier(client).verify(token)

    assert fetch.call_count == 2


@pytest.mark.asyncio
async def test_verifier_reuses_cached_jwks_until_lifespan_expires(
    signing_key: rsa.RSAPrivateKey,
    jwks_clock: _MonotonicClock,
) -> None:
    jwks = {"keys": [_public_jwk(signing_key, "key-1")]}
    client, fetch = _jwks_client(jwks, jwks)
    verifier = _verifier(client)
    token = _token(signing_key, _claims())

    first = await verifier.verify(token)
    jwks_clock.advance(300)
    second = await verifier.verify(token)

    assert first.subject == second.subject == "player-1"
    assert fetch.call_count == 1

    jwks_clock.advance(0.001)
    third = await verifier.verify(token)

    assert third.subject == "player-1"
    assert fetch.call_count == 2


@pytest.mark.asyncio
async def test_verifier_accepts_rotated_key_after_jwks_refresh(
    signing_key: rsa.RSAPrivateKey,
    jwks_clock: _MonotonicClock,
) -> None:
    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    initial_jwks = {"keys": [_public_jwk(signing_key, "key-1")]}
    rotated_jwks = {
        "keys": [
            _public_jwk(signing_key, "key-1"),
            _public_jwk(rotated_key, "key-2"),
        ]
    }
    client, fetch = _jwks_client(initial_jwks, rotated_jwks)
    verifier = _verifier(client)

    await verifier.verify(_token(signing_key, _claims()))
    rotated_token = _token(rotated_key, _claims(), kid="key-2")

    with pytest.raises(InvalidCredentialError):
        await verifier.verify(rotated_token)

    assert fetch.call_count == 1

    jwks_clock.advance(30)
    identity = await verifier.verify(rotated_token)

    assert identity.subject == "player-1"
    assert fetch.call_count == 2


@pytest.mark.asyncio
async def test_verifier_does_not_log_token_or_claims(
    signing_key: rsa.RSAPrivateKey,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _ = _jwks_client({"keys": [_public_jwk(signing_key, "key-1")]})
    token = _token(signing_key, _claims())

    await _verifier(client).verify(token)

    assert token not in caplog.text
    assert "player-1" not in caplog.text


def _validated_identity() -> auth_module.ValidatedOidcIdentity:
    authenticated_at = datetime(2026, 9, 18, tzinfo=UTC)
    return auth_module.ValidatedOidcIdentity(
        issuer=ISSUER,
        subject="private-subject",
        authenticated_at=authenticated_at,
        expires_at=authenticated_at + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_bearer_authenticator_maps_validated_identity_to_principal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    validated = _validated_identity()
    verifier = AsyncMock(spec=OidcJwtVerifier)
    verifier.verify.return_value = validated
    resolver = AsyncMock(return_value=PRINCIPAL_ID)
    authenticator = auth_module.OidcBearerAuthenticator(verifier, resolver)
    caplog.set_level(logging.INFO)

    principal = await authenticator("bEaReR signed-token")

    assert principal.principal_id == PRINCIPAL_ID
    assert principal.auth_context == frozenset()
    assert principal.credential_expires_at == validated.expires_at
    resolver.assert_awaited_once_with(validated.issuer, validated.subject)
    assert "oidc_authentication_succeeded" in caplog.text
    assert ISSUER in caplog.text
    assert str(PRINCIPAL_ID) in caplog.text
    assert "signed-token" not in caplog.text
    assert validated.subject not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorization", "invalid_credential", "resolved_principal", "event"),
    [
        (None, False, PRINCIPAL_ID, "oidc_credential_rejected"),
        ("Bearer", False, PRINCIPAL_ID, "oidc_credential_rejected"),
        ("Basic signed-token", False, PRINCIPAL_ID, "oidc_credential_rejected"),
        (
            "Bearer signed-token extra",
            False,
            PRINCIPAL_ID,
            "oidc_credential_rejected",
        ),
        ("Bearer signed-token", True, PRINCIPAL_ID, "oidc_credential_rejected"),
        ("Bearer signed-token", False, None, "oidc_identity_unavailable"),
        ("Bearer signed-token", False, None, "oidc_identity_unavailable"),
    ],
    ids=[
        "missing-header",
        "empty-token",
        "basic-auth",
        "extra-part",
        "invalid-token",
        "unknown-identity",
        "disabled-identity",
    ],
)
async def test_bearer_authenticator_returns_uniform_unauthenticated_error(
    authorization: str | None,
    invalid_credential: bool,
    resolved_principal: UUID | None,
    event: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    validated = _validated_identity()
    verifier = AsyncMock(spec=OidcJwtVerifier)
    verifier.verify.return_value = validated
    if invalid_credential:
        verifier.verify.side_effect = InvalidCredentialError("private-token-detail")
    resolver = AsyncMock(return_value=resolved_principal)
    authenticator = auth_module.OidcBearerAuthenticator(verifier, resolver)

    with pytest.raises(auth_module.HTTPException) as error:
        await authenticator(authorization)

    assert error.value.status_code == 401
    assert error.value.detail == {"code": "UNAUTHENTICATED"}
    assert error.value.headers == {"WWW-Authenticate": "Bearer"}
    assert event in caplog.text
    assert "signed-token" not in caplog.text
    assert validated.subject not in caplog.text
    assert "private-token-detail" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verifier_error", "resolver_error", "event"),
    [
        (
            AuthenticationUnavailableError("private-provider-detail"),
            None,
            "oidc_provider_unavailable",
        ),
        (
            None,
            SQLAlchemyError("private-database-detail"),
            "oidc_identity_store_unavailable",
        ),
    ],
    ids=["provider-unavailable", "identity-store-unavailable"],
)
async def test_bearer_authenticator_returns_authentication_unavailable(
    verifier_error: Exception | None,
    resolver_error: Exception | None,
    event: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    validated = _validated_identity()
    verifier = AsyncMock(spec=OidcJwtVerifier)
    verifier.verify.return_value = validated
    if verifier_error is not None:
        verifier.verify.side_effect = verifier_error
    resolver = AsyncMock(return_value=PRINCIPAL_ID)
    if resolver_error is not None:
        resolver.side_effect = resolver_error
    authenticator = auth_module.OidcBearerAuthenticator(verifier, resolver)

    with pytest.raises(auth_module.HTTPException) as error:
        await authenticator("Bearer signed-token")

    assert error.value.status_code == 503
    assert error.value.detail == {"code": "AUTHENTICATION_UNAVAILABLE"}
    assert event in caplog.text
    assert "signed-token" not in caplog.text
    assert validated.subject not in caplog.text
    assert "private-provider-detail" not in caplog.text
    assert "private-database-detail" not in caplog.text


@pytest.mark.asyncio
async def test_build_oidc_authenticator_wires_validated_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Mock()
    settings.require_oidc.return_value = (ISSUER, AUDIENCE, ("RS256",))
    settings.database_url = "postgresql+psycopg://airpg@database/airpg"

    client = Mock()
    client_context = AsyncMock()
    client_context.__aenter__.return_value = client
    async_client_factory = Mock(return_value=client_context)
    discovery = AsyncMock(
        return_value=auth_module.OidcConfiguration(
            issuer=ISSUER,
            jwks_uri="https://idp.example.com/keys",
        )
    )
    jwks = Mock()
    jwks_factory = Mock(return_value=jwks)
    sessions = Mock()
    session_factory_builder = Mock(return_value=sessions)
    store = Mock()
    store.resolve = AsyncMock(return_value=PRINCIPAL_ID)
    store_factory = Mock(return_value=store)
    verifier = AsyncMock(spec=OidcJwtVerifier)
    verifier.verify.return_value = _validated_identity()
    verifier_factory = Mock(return_value=verifier)

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", async_client_factory)
    monkeypatch.setattr(auth_module, "discover_oidc", discovery)
    monkeypatch.setattr(auth_module, "_ProviderJWKClient", jwks_factory)
    monkeypatch.setattr(auth_module, "create_session_factory", session_factory_builder)
    monkeypatch.setattr(auth_module, "PostgresIdentityStore", store_factory)
    monkeypatch.setattr(auth_module, "OidcJwtVerifier", verifier_factory)

    authenticator = await auth_module.build_oidc_authenticator(settings)
    principal = await authenticator("Bearer signed-token")

    settings.require_oidc.assert_called_once_with()
    async_client_factory.assert_called_once_with(timeout=5.0)
    discovery.assert_awaited_once_with(ISSUER, client)
    jwks_factory.assert_called_once_with(
        "https://idp.example.com/keys",
        lifespan=300,
        timeout=5,
        cooldown_duration=30,
    )
    session_factory_builder.assert_called_once_with(settings.database_url)
    store_factory.assert_called_once_with(sessions)
    verifier_factory.assert_called_once_with(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=("RS256",),
        jwks=jwks,
    )
    assert principal.principal_id == PRINCIPAL_ID
    assert api_module.build_oidc_authenticator is auth_module.build_oidc_authenticator
    assert api_module.OidcDiscoveryError is auth_module.OidcDiscoveryError
