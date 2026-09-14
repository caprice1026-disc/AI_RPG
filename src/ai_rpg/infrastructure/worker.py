"""API processから独立して動作するasync worker。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress


class Worker:
    """leaseで取得したjobを停止要求まで順次処理する。"""

    def __init__(self, poll: Callable[[], Awaitable[bool]], poll_interval: float = 1.0) -> None:
        self._poll = poll
        self._poll_interval = poll_interval
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """現在の処理後にloopを終了するよう要求する。"""

        self._stopping.set()

    async def run(self) -> None:
        """通知を正本にせず定期pollでjobを回収する。"""

        while not self._stopping.is_set():
            processed = await self._poll()
            if not processed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
