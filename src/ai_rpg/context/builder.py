"""追加可能なProviderからContextを構築する。"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ai_rpg.contracts import ContextFragment


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """Providerへ渡す参照範囲。"""

    campaign_id: UUID
    actor_id: UUID


class ContextProvider(Protocol):
    """一種類のContextを取得するplugin境界。"""

    async def provide(self, request: ContextRequest) -> ContextFragment | None:
        """利用可能かつ認可済みの断片だけを返す。"""


class ContextBuilder:
    """Providerを登録順に実行して断片を組み立てる。"""

    def __init__(self, providers: tuple[ContextProvider, ...]) -> None:
        self._providers = providers

    async def build(self, request: ContextRequest) -> tuple[ContextFragment, ...]:
        """欠落した任意断片を除外してContextを返す。"""

        fragments = [await provider.provide(request) for provider in self._providers]
        return tuple(fragment for fragment in fragments if fragment is not None)
