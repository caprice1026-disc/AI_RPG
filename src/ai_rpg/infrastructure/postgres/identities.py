"""OIDC identityと内部principalの不変な対応を保存するadapter。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.infrastructure.postgres.models import (
    PrincipalIdentityModel,
    PrincipalModel,
)


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    issuer: str
    subject: str
    principal_id: UUID
    created_at: datetime
    disabled_at: datetime | None


class IdentityRegistrationConflict(Exception):
    pass


class IdentityNotFound(Exception):
    pass


class PostgresIdentityStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def resolve(self, issuer: str, subject: str) -> UUID | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(PrincipalIdentityModel.principal_id).where(
                    PrincipalIdentityModel.issuer == issuer,
                    PrincipalIdentityModel.subject == subject,
                    PrincipalIdentityModel.disabled_at.is_(None),
                )
            )
            return result.scalar_one_or_none()

    async def register(
        self,
        issuer: str,
        subject: str,
        principal_id: UUID | None = None,
    ) -> IdentityRecord:
        async with self._sessions.begin() as session:
            identity = await self._find(session, issuer, subject)
            if identity is not None:
                return self._registered(identity, principal_id)

            generated_principal = principal_id is None
            selected_principal_id = principal_id or uuid4()
            principal_result = await session.execute(
                insert(PrincipalModel)
                .values(id=selected_principal_id)
                .on_conflict_do_nothing()
                .returning(PrincipalModel.id)
            )
            identity_result = await session.execute(
                insert(PrincipalIdentityModel)
                .values(
                    issuer=issuer,
                    subject=subject,
                    principal_id=selected_principal_id,
                )
                .on_conflict_do_nothing()
                .returning(PrincipalIdentityModel.principal_id)
            )
            if (
                generated_principal
                and principal_result.scalar_one_or_none() is not None
                and identity_result.scalar_one_or_none() is None
            ):
                await session.execute(
                    delete(PrincipalModel).where(
                        PrincipalModel.id == selected_principal_id
                    )
                )

            identity = await self._find(session, issuer, subject)
            assert identity is not None
            return self._registered(identity, principal_id)

    async def disable(self, issuer: str, subject: str) -> IdentityRecord:
        async with self._sessions.begin() as session:
            identity = await self._find(session, issuer, subject, for_update=True)
            if identity is None:
                raise IdentityNotFound
            if identity.disabled_at is None:
                disabled_at = await session.scalar(
                    select(
                        func.greatest(
                            func.current_timestamp(),
                            identity.created_at,
                        )
                    )
                )
                assert disabled_at is not None
                identity.disabled_at = disabled_at
            return self._record(identity)

    @staticmethod
    async def _find(
        session: AsyncSession,
        issuer: str,
        subject: str,
        *,
        for_update: bool = False,
    ) -> PrincipalIdentityModel | None:
        statement = select(PrincipalIdentityModel).where(
            PrincipalIdentityModel.issuer == issuer,
            PrincipalIdentityModel.subject == subject,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await session.execute(statement)
        return result.scalar_one_or_none()

    @classmethod
    def _registered(
        cls,
        identity: PrincipalIdentityModel,
        requested_principal_id: UUID | None,
    ) -> IdentityRecord:
        record = cls._record(identity)
        if record.disabled_at is not None or (
            requested_principal_id is not None
            and record.principal_id != requested_principal_id
        ):
            raise IdentityRegistrationConflict
        return record

    @staticmethod
    def _record(identity: PrincipalIdentityModel) -> IdentityRecord:
        return IdentityRecord(
            issuer=identity.issuer,
            subject=identity.subject,
            principal_id=identity.principal_id,
            created_at=identity.created_at,
            disabled_at=identity.disabled_at,
        )
