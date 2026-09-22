"""OIDC code flowとDB-backed Cookie認証。Provider tokenは保存しない。"""

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from ai_rpg.api.auth import (
    AuthenticationUnavailableError,
    InvalidCredentialError,
    OidcBearerAuthenticator,
    OidcConfiguration,
    OidcDiscoveryError,
    OidcJwtVerifier,
    _authentication_unavailable,
    _ProviderJWKClient,
    _unauthenticated,
    build_oidc_authenticator,
    discover_oidc,
)
from ai_rpg.application import AuthenticatedPrincipal
from ai_rpg.config import Settings
from ai_rpg.infrastructure.database import create_session_factory

if TYPE_CHECKING:
    from ai_rpg.infrastructure.postgres.browser_sessions import (
        BrowserSession,
        PostgresBrowserSessionStore,
    )

_SESSION_SECONDS = 8 * 60 * 60


class BrowserAuthenticator:
    """Bearerを優先し、Cookieの更新要求にOrigin/CSRF照合を追加する。"""

    def __init__(
        self, settings: Settings, configuration: OidcConfiguration,
        store: "PostgresBrowserSessionStore", verifier: OidcJwtVerifier,
        bearer: OidcBearerAuthenticator, *, client: httpx.AsyncClient | None = None,
    ) -> None:
        assert settings.auth_client_id and settings.auth_app_origin
        assert configuration.authorization_endpoint and configuration.token_endpoint
        self._settings, self._configuration = settings, configuration
        self._store, self._verifier, self._bearer = store, verifier, bearer
        self._client = client or httpx.AsyncClient(timeout=10.0, follow_redirects=False)
        self._origin = settings.auth_app_origin
        self._redirect_uri = self._origin + "/auth/callback"
        self._secure = urlsplit(self._origin).scheme == "https"
        self._cookie = "__Host-airpg_session" if self._secure else "airpg_session_dev"
        self._binding_cookie = "__Host-airpg_login" if self._secure else "airpg_login_dev"
        methods = configuration.token_endpoint_auth_methods
        if settings.auth_client_secret is None:
            self._client_auth = "none"
        elif "client_secret_basic" in methods:
            self._client_auth = "client_secret_basic"
        else:
            self._client_auth = "client_secret_post"
        if self._client_auth not in methods:
            raise OidcDiscoveryError("OIDC client認証方式を利用できません")

    async def close(self) -> None:
        await self._client.aclose()

    async def __call__(
        self, request: Request, authorization: Annotated[str | None, Header()] = None,
    ) -> AuthenticatedPrincipal:
        if authorization is not None:
            request.state.auth_mode = "bearer"
            return await self._bearer(authorization)
        session = await self._session(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
                request.headers.get("origin") != self._origin
                or not hmac.compare_digest(
                    request.headers.get("x-csrf-token", "").encode(), session.csrf_token.encode(),
                )
        ):
            raise HTTPException(403, detail={"code": "CSRF_REJECTED"})
        request.state.auth_mode = "session"
        request.state.browser_session = session
        return AuthenticatedPrincipal(
            principal_id=session.principal_id, issuer=session.issuer, subject=session.subject,
            authenticated_at=session.authenticated_at, auth_context=frozenset({"browser_session"}),
            credential_expires_at=session.expires_at,
        )

    async def _session(self, request: Request) -> "BrowserSession":
        token = request.cookies.get(self._cookie, "")
        if not token or len(token) > 256:
            raise _unauthenticated()
        try:
            session = await self._store.resolve_session(token=token, now=datetime.now(UTC))
        except SQLAlchemyError as error:
            raise _authentication_unavailable() from error
        if session is None:
            raise _unauthenticated()
        return session

    async def stream_is_valid(self, request: Request) -> bool:
        """SSE開始後のlogoutとidentity無効化を各poll前に確認する。"""
        if getattr(request.state, "auth_mode", None) != "session":
            return True
        try:
            await self._session(request)
        except HTTPException:
            return False
        return True

    def _set_cookie(self, response: Response, name: str, value: str, lifetime: int) -> None:
        response.set_cookie(
            name, value, max_age=lifetime, secure=self._secure,
            httponly=True, samesite="lax", path="/",
        )

    def _clear_cookie(self, response: Response, name: str) -> None:
        response.delete_cookie(name, secure=self._secure, httponly=True, samesite="lax", path="/")

    async def _exchange_code(self, code: str, verifier: str) -> str:
        data = {
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "redirect_uri": self._redirect_uri, "client_id": self._settings.auth_client_id or "",
        }
        basic: httpx.Auth = httpx.Auth()
        if self._settings.auth_client_secret is not None:
            secret = self._settings.auth_client_secret.get_secret_value()
            if self._client_auth == "client_secret_basic":
                basic = httpx.BasicAuth(quote_plus(data["client_id"]), quote_plus(secret))
            else:
                data["client_secret"] = secret
        assert self._configuration.token_endpoint
        try:
            response = await self._client.post(
                self._configuration.token_endpoint, data=data, auth=basic,
                timeout=10.0, follow_redirects=False,
            )
            if response.status_code >= 500 or response.status_code == 429:
                raise AuthenticationUnavailableError("OIDC token endpoint unavailable")
            response.raise_for_status()
            payload = response.json()
        except httpx.TransportError as error:
            raise AuthenticationUnavailableError("OIDC token endpoint unavailable") from error
        except (httpx.HTTPStatusError, ValueError) as error:
            raise InvalidCredentialError("OIDC code exchange rejected") from error
        token = payload.get("id_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token or len(token) > 32768:
            raise InvalidCredentialError("OIDC ID token missing")
        return token

    async def _callback(self, request: Request) -> RedirectResponse:
        error_code = "LOGIN_REJECTED"
        try:
            params = request.query_params
            state, code = params.get("state", ""), params.get("code", "")
            binding = request.cookies.get(self._binding_cookie, "")
            if (
                not state or not binding or len(state) > 256 or len(binding) > 256
                or len(params.getlist("state")) != 1 or len(params.getlist("code")) != 1
                or not code or len(code) > 4096 or "error" in params
            ):
                raise InvalidCredentialError("OIDC callback rejected")
            attempt = await self._store.consume_login(
                state=state, binding=binding, now=datetime.now(UTC),
            )
            if attempt is None:
                raise InvalidCredentialError("OIDC login attempt expired")
            token = await self._exchange_code(code, attempt.code_verifier)
            identity = await self._verifier.verify(token, nonce_digest=attempt.nonce_digest)
            session_token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            now = datetime.now(UTC)
            principal_id = await self._store.create_session(
                issuer=identity.issuer, subject=identity.subject, token=session_token,
                csrf_token=csrf_token, created_at=now,
                expires_at=now + timedelta(seconds=_SESSION_SECONDS),
            )
            if principal_id is None:
                error_code = "IDENTITY_NOT_REGISTERED"
                raise InvalidCredentialError("OIDC identity unavailable")
            old_token = request.cookies.get(self._cookie)
            if old_token:
                await self._store.delete_session(old_token)
            response = RedirectResponse("/", status_code=303)
            self._set_cookie(response, self._cookie, session_token, _SESSION_SECONDS)
        except (AuthenticationUnavailableError, SQLAlchemyError):
            response = RedirectResponse("/?login_error=AUTHENTICATION_UNAVAILABLE", status_code=303)
        except InvalidCredentialError:
            response = RedirectResponse(f"/?login_error={error_code}", status_code=303)
        self._clear_cookie(response, self._binding_cookie)
        return response

    def mount(self, app: FastAPI) -> None:
        @app.get("/auth/login", include_in_schema=False)
        async def login() -> RedirectResponse:
            state, binding, nonce = (secrets.token_urlsafe(32) for _ in range(3))
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            try:
                await self._store.create_login(
                    state=state, binding=binding, nonce=nonce, code_verifier=verifier,
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            except SQLAlchemyError as error:
                raise _authentication_unavailable() from error
            query = urlencode({
                "response_type": "code", "scope": "openid",
                "client_id": self._settings.auth_client_id, "redirect_uri": self._redirect_uri,
                "state": state, "nonce": nonce, "code_challenge_method": "S256",
                "code_challenge": challenge.rstrip(b"=").decode(),
            })
            response = RedirectResponse(
                f"{self._configuration.authorization_endpoint}?{query}", status_code=303,
            )
            self._set_cookie(response, self._binding_cookie, binding, 300)
            return response

        app.add_api_route(
            "/auth/callback", self._callback, methods=["GET"], include_in_schema=False,
        )

        @app.post("/auth/logout", status_code=204, include_in_schema=False)
        async def logout(
            request: Request, principal: Annotated[AuthenticatedPrincipal, Depends(self)],
        ) -> Response:
            if getattr(request.state, "auth_mode", None) != "session":
                raise HTTPException(400, detail={"code": "BROWSER_SESSION_REQUIRED"})
            try:
                await self._store.delete_session(request.cookies[self._cookie])
            except SQLAlchemyError as error:
                raise _authentication_unavailable() from error
            response = Response(status_code=204)
            self._clear_cookie(response, self._cookie)
            return response


async def build_browser_authenticator(settings: Settings) -> BrowserAuthenticator:
    from ai_rpg.infrastructure.postgres.browser_sessions import PostgresBrowserSessionStore

    issuer, _, algorithms = settings.require_oidc()
    if not settings.browser_auth_enabled:
        raise ValueError("browser OIDC settings required")
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        configuration = await discover_oidc(
            issuer, client, browser=True, allow_loopback=settings.auth_allow_insecure_loopback,
        )
    jwks = _ProviderJWKClient(configuration.jwks_uri, lifespan=300, timeout=5, cooldown_duration=30)
    return BrowserAuthenticator(
        settings, configuration, PostgresBrowserSessionStore(
            create_session_factory(settings.database_url),
        ), OidcJwtVerifier(issuer, settings.auth_client_id or "", algorithms, jwks),
        await build_oidc_authenticator(settings),
    )
