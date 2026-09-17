"""provider固有SDKを隔離する構造化出力adapter境界。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar

from pydantic import TypeAdapter

from ai_rpg.llm.budget import CallBudgetExceeded

OutputT = TypeVar("OutputT")
LLMPurpose: TypeAlias = Literal["intent", "narrative", "result_narration"]


@dataclass(frozen=True, slots=True)
class StructuredRequest(Generic[OutputT]):
    """モデルへ渡すデータと期待する出力契約。"""

    model_id: str
    purpose: LLMPurpose
    system_instruction: str
    input_data: str
    output_adapter: TypeAdapter[OutputT]


class ProviderTransport(Protocol):
    """provider SDKを包む最小transport port。"""

    async def request(
        self,
        model_id: str,
        purpose: LLMPurpose,
        instruction: str,
        input_data: str,
        output_schema: dict[str, object],
    ) -> object:
        """自動retryを行わず一回の物理requestを送る。"""


class StructuredOutputAdapter:
    """呼び出し予算を予約してprovider出力をPydanticで検証する。"""

    def __init__(
        self,
        transport: ProviderTransport,
        reserve_call: Callable[[], Awaitable[bool]],
    ) -> None:
        self._transport = transport
        self._reserve_call = reserve_call

    async def generate(self, request: StructuredRequest[OutputT]) -> OutputT:
        """送信直前に予算を消費し、未検証出力を外へ漏らさない。"""

        if not await self._reserve_call():
            raise CallBudgetExceeded("LLM呼び出し予算を使い切りました")
        raw = await self._transport.request(
            request.model_id,
            request.purpose,
            request.system_instruction,
            request.input_data,
            request.output_adapter.json_schema(),
        )
        return request.output_adapter.validate_python(raw)
