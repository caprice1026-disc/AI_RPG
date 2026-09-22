"""Single-use browser logins and revocable sessions backed by PostgreSQL."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.infrastructure.postgres.models import (
    BrowserSessionModel,
    LoginAttemptModel,
    PrincipalIdentityModel,
)


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    nonce_digest: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class BrowserSession:
    principal_id: UUID
    issuer: str
    subject: str
    authenticated_at: datetime
    expires_at: datetime
    csrf_token: str


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class PostgresBrowserSessionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create_login(
        self,
        *,
        state: str,
        binding: str,
        nonce: str,
        code_verifier: str,
        expires_at: datetime,
    ) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                delete(LoginAttemptModel).where(
                    LoginAttemptModel.expires_at <= func.current_timestamp()
                )
            )
            session.add(
                LoginAttemptModel(
                    state_digest=_digest(state),
                    binding_digest=_digest(binding),
                    nonce_digest=_digest(nonce),
                    code_verifier=code_verifier,
                    expires_at=expires_at,
                )
            )

    async def consume_login(
        self, *, state: str, binding: str, now: datetime
    ) -> LoginAttempt | None:
        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(LoginAttemptModel)
                .where(
                    LoginAttemptModel.state_digest == _digest(state),
                    LoginAttemptModel.binding_digest == _digest(binding),
                    LoginAttemptModel.expires_at > now,
                )
                .returning(LoginAttemptModel.nonce_digest, LoginAttemptModel.code_verifier)
            )
            row = result.one_or_none()
            if row is None:
                return None
            return LoginAttempt(nonce_digest=row.nonce_digest, code_verifier=row.code_verifier)

    async def create_session(
        self,
        *,
        issuer: str,
        subject: str,
        token: str,
        csrf_token: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> UUID | None:
        async with self._sessions.begin() as session:
            await session.execute(
                delete(BrowserSessionModel).where(
                    BrowserSessionModel.expires_at <= func.current_timestamp()
                )
            )
            identity = await session.scalar(
                select(PrincipalIdentityModel)
                .where(
                    PrincipalIdentityModel.issuer == issuer,
                    PrincipalIdentityModel.subject == subject,
                    PrincipalIdentityModel.disabled_at.is_(None),
                )
                .with_for_update()
            )
            if identity is None:
                return None
            session.add(
                BrowserSessionModel(
                    token_digest=_digest(token),
                    identity_id=identity.identity_id,
                    csrf_token=csrf_token,
                    created_at=created_at,
                    expires_at=expires_at,
                )
            )
            return identity.principal_id

    async def resolve_session(self, *, token: str, now: datetime) -> BrowserSession | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(
                    PrincipalIdentityModel.principal_id,
                    PrincipalIdentityModel.issuer,
                    PrincipalIdentityModel.subject,
                    BrowserSessionModel.created_at,
                    BrowserSessionModel.expires_at,
                    BrowserSessionModel.csrf_token,
                )
                .join(
                    PrincipalIdentityModel,
                    PrincipalIdentityModel.identity_id == BrowserSessionModel.identity_id,
                )
                .where(
                    BrowserSessionModel.token_digest == _digest(token),
                    BrowserSessionModel.expires_at > now,
                    PrincipalIdentityModel.disabled_at.is_(None),
                )
            )
            row = result.one_or_none()
            if row is None:
                return None
            return BrowserSession(
                principal_id=row.principal_id,
                issuer=row.issuer,
                subject=row.subject,
                authenticated_at=row.created_at,
                expires_at=row.expires_at,
                csrf_token=row.csrf_token,
            )

    async def delete_session(self, token: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                delete(BrowserSessionModel).where(
                    BrowserSessionModel.token_digest == _digest(token)
                )
            )
