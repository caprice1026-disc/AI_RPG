"""Campaign membershipを参照するPostgreSQL認可adapter。"""

from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.infrastructure.postgres.models import CampaignMemberModel


class PostgresAuthorizationPolicy:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def can_access_campaign(self, principal_id: UUID, campaign_id: UUID) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                select(
                    exists().where(
                        CampaignMemberModel.campaign_id == campaign_id,
                        CampaignMemberModel.principal_id == principal_id,
                        CampaignMemberModel.active.is_(True),
                    )
                )
            )
            return bool(result.scalar_one())
