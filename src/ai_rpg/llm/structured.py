"""provider固有SDKを隔離する構造化出力adapter境界。"""

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import TypeAdapter

from ai_rpg.llm.budget import CallBudget

OutputT = TypeVar("OutputT")


@dataclass(frozen=True, slots=True)
class StructuredRequest(Generic[OutputT]):
    """モデルへ渡すデータと期待する出力契約。"""

    model_id: str
    system_instruction: str
    input_data: str
    output_adapter: TypeAdapter[OutputT]


class ProviderTransport(Protocol):
    """provider SDKを包む最小transport port。"""

    async def request(self, model_id: str, instruction: str, input_data: str) -> object:
        """自動retryを行わず一回の物理requestを送る。"""


class StructuredOutputAdapter:
    """呼び出し予算を予約してprovider出力をPydanticで検証する。"""

    def __init__(self, transport: ProviderTransport, budget: CallBudget) -> None:
        self._transport = transport
        self._budget = budget

    async def generate(self, request: StructuredRequest[OutputT]) -> OutputT:
        """送信直前に予算を消費し、未検証出力を外へ漏らさない。"""

        self._budget.reserve()
        raw = await self._transport.request(
            request.model_id, request.system_instruction, request.input_data
        )
        return request.output_adapter.validate_python(raw)
