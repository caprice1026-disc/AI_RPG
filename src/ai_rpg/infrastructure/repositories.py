"""PostgreSQL Repositoryの基底実装。"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class PostgresEventRepository:
    """追記専用イベントログを操作するRepository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_sequence(self, campaign_id: UUID) -> int:
        """Campaign行をロックして次の単調増加sequenceを確保する。"""

        statement = text(
            "UPDATE campaigns SET event_sequence = event_sequence + 1 "
            "WHERE id = :campaign_id RETURNING event_sequence"
        )
        result = await self._session.execute(statement, {"campaign_id": campaign_id})
        sequence = result.scalar_one()
        return int(sequence)
