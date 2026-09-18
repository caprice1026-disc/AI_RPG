"""OIDC DiscoveryとBearer JWT検証のUnit Test。"""

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import jwt
import jwt.api_jwk as jwt_api_jwk
import jwt.jwk_set_cache as jwt_jwk_set_cache
import jwt.jwks_client as jwt_jwks_client
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_rpg.api.auth import (
    AuthenticationUnavailableError,
    InvalidCredentialError,
    OidcDiscoveryError,
    OidcJwtVerifier,
    discover_oidc,
)

ISSUER = "https://idp.example.com/"
AUDIENCE = "ai-rpg-api"


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
    client = jwt.PyJWKClient(
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
