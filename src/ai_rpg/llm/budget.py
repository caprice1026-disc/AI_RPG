"""物理的なprovider request回数を制限する。"""


class CallBudgetExceeded(Exception):
    """予約できるLLM呼び出しが残っていないことを表す。"""


class CallBudget:
    """provider request送信直前に消費する呼び出し予算。"""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("呼び出し上限は1以上である必要があります")
        self._limit = limit
        self._reserved = 0

    @property
    def remaining(self) -> int:
        """予約可能な残り回数を返す。"""

        return self._limit - self._reserved

    def reserve(self) -> None:
        """requestを一回予約し、上限超過なら送信前に拒否する。"""

        if self.remaining == 0:
            raise CallBudgetExceeded("LLM呼び出し予算を使い切りました")
        self._reserved += 1
