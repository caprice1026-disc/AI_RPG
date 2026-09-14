"""PostgreSQLイベントログを公開ストリームへ投影するadapter。"""

from collections.abc import AsyncIterator, Callable
from uuid import UUID


class PostgresEventSource:
    """認可済みイベントの取得関数をストリームportへ適合させる。"""

    def __init__(self, fetch: Callable[[UUID, int], AsyncIterator[str]]) -> None:
        self._fetch = fetch

    def subscribe(self, campaign_id: UUID, after: int) -> AsyncIterator[str]:
        """cursorより後の公開イベントだけを購読する。"""

        return self._fetch(campaign_id, after)
