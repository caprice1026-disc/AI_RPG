"""OIDC DiscoveryとBearer JWT検証のHTTP境界。"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import jwt


@dataclass(frozen=True, slots=True)
class OidcConfiguration:
    issuer: str
    jwks_uri: str


class OidcDiscoveryError(RuntimeError):
    """OIDC Discoveryを安全に利用できない。"""


class InvalidCredentialError(Exception):
    """Bearer credentialを検証できない。"""


class AuthenticationUnavailableError(RuntimeError):
    """外部認証基盤を一時的に利用できない。"""


@dataclass(frozen=True, slots=True)
class ValidatedOidcIdentity:
    issuer: str
    subject: str
    authenticated_at: datetime
    expires_at: datetime


async def discover_oidc(
    issuer: str,
    client: httpx.AsyncClient,
) -> OidcConfiguration:
    """設定Issuerに完全一致するHTTPS JWKS endpointを取得する。"""

    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise OidcDiscoveryError("OIDC Discoveryを取得できません") from error
    if not isinstance(payload, dict) or payload.get("issuer") != issuer:
        raise OidcDiscoveryError("OIDC Discoveryのissuerが一致しません")
    jwks_uri = payload.get("jwks_uri")
    if not isinstance(jwks_uri, str):
        raise OidcDiscoveryError("OIDC Discoveryのjwks_uriが不正です")
    try:
        parsed = urlsplit(jwks_uri)
    except ValueError as error:
        raise OidcDiscoveryError("OIDC Discoveryのjwks_uriが不正です") from error
    if parsed.scheme != "https" or not parsed.netloc:
        raise OidcDiscoveryError("OIDC Discoveryのjwks_uriが不正です")
    return OidcConfiguration(issuer=issuer, jwks_uri=jwks_uri)


class OidcJwtVerifier:
    """固定Issuer・Audience・algorithmでBearer JWTを検証する。"""

    def __init__(
        self,
        issuer: str,
        audience: str,
        algorithms: tuple[str, ...],
        jwks: jwt.PyJWKClient,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._algorithms = algorithms
        self._jwks = jwks

    async def verify(self, token: str) -> ValidatedOidcIdentity:
        authenticated_at = datetime.now(UTC)
        try:
            signing_key = await asyncio.to_thread(
                self._jwks.get_signing_key_from_jwt,
                token,
            )
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=30,
                options={"require": ["iss", "aud", "sub", "exp"]},
            )
        except jwt.PyJWKClientConnectionError as error:
            raise AuthenticationUnavailableError("JWKSを取得できません") from error
        except (
            jwt.InvalidTokenError,
            jwt.PyJWKClientError,
            TypeError,
            OverflowError,
        ) as error:
            raise InvalidCredentialError("Bearer tokenが不正です") from error

        subject = claims.get("sub")
        expiration = claims.get("exp")
        if not isinstance(subject, str) or not subject:
            raise InvalidCredentialError("Bearer tokenが不正です")
        if isinstance(expiration, bool) or not isinstance(expiration, (int, float)):
            raise InvalidCredentialError("Bearer tokenが不正です")
        try:
            expires_at = datetime.fromtimestamp(expiration, UTC)
        except (OSError, OverflowError, ValueError) as error:
            raise InvalidCredentialError("Bearer tokenが不正です") from error
        if expires_at <= authenticated_at:
            raise InvalidCredentialError("Bearer tokenが不正です")
        return ValidatedOidcIdentity(
            issuer=self._issuer,
            subject=subject,
            authenticated_at=authenticated_at,
            expires_at=expires_at,
        )
