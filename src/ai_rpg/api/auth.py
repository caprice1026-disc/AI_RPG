"""OIDC DiscoveryとBearer JWT検証のHTTP境界。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from fastapi import Header, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from ai_rpg.application import AuthenticatedPrincipal
from ai_rpg.config import Settings
from ai_rpg.infrastructure.database import create_session_factory
from ai_rpg.infrastructure.postgres import PostgresIdentityStore

logger = logging.getLogger(__name__)


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


IdentityResolver = Callable[[str, str], Awaitable[UUID | None]]


class OidcBearerAuthenticator:
    def __init__(
        self,
        verifier: OidcJwtVerifier,
        resolve_identity: IdentityResolver,
    ) -> None:
        self._verifier = verifier
        self._resolve_identity = resolve_identity

    async def __call__(
        self,
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthenticatedPrincipal:
        try:
            token = _bearer_token(authorization)
        except HTTPException:
            logger.warning("oidc_credential_rejected")
            raise
        validated = await self._verify_or_http_error(token)
        try:
            principal_id = await self._resolve_identity(
                validated.issuer,
                validated.subject,
            )
        except SQLAlchemyError as error:
            logger.warning("oidc_identity_store_unavailable")
            raise _authentication_unavailable() from error
        if principal_id is None:
            logger.warning("oidc_identity_unavailable")
            raise _unauthenticated()
        logger.info(
            "oidc_authentication_succeeded issuer=%s principal_id=%s",
            validated.issuer,
            principal_id,
        )
        return AuthenticatedPrincipal(
            principal_id=principal_id,
            issuer=validated.issuer,
            subject=validated.subject,
            authenticated_at=validated.authenticated_at,
            auth_context=frozenset(),
            credential_expires_at=validated.expires_at,
        )

    async def _verify_or_http_error(self, token: str) -> ValidatedOidcIdentity:
        try:
            return await self._verifier.verify(token)
        except InvalidCredentialError as error:
            logger.warning("oidc_credential_rejected")
            raise _unauthenticated() from error
        except AuthenticationUnavailableError as error:
            logger.warning("oidc_provider_unavailable")
            raise _authentication_unavailable() from error


def _bearer_token(authorization: str | None) -> str:
    parts = authorization.split() if authorization is not None else []
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        raise _unauthenticated()
    return parts[1]


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHENTICATED"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authentication_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "AUTHENTICATION_UNAVAILABLE"},
    )


async def build_oidc_authenticator(settings: Settings) -> OidcBearerAuthenticator:
    issuer, audience, algorithms = settings.require_oidc()
    async with httpx.AsyncClient(timeout=5.0) as client:
        configuration = await discover_oidc(issuer, client)
    jwks = jwt.PyJWKClient(
        configuration.jwks_uri,
        lifespan=300,
        timeout=5,
        cooldown_duration=30,
    )
    verifier = OidcJwtVerifier(
        issuer=issuer,
        audience=audience,
        algorithms=algorithms,
        jwks=jwks,
    )
    store = PostgresIdentityStore(create_session_factory(settings.database_url))
    return OidcBearerAuthenticator(verifier, store.resolve)
