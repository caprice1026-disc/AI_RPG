"""構造化出力adapterの下で使うscript式Fake transport。"""

from collections.abc import Sequence
from dataclasses import dataclass

from ai_rpg.llm.structured import LLMPurpose


@dataclass(frozen=True, slots=True)
class FakeCall:
    model_id: str
    purpose: LLMPurpose
    instruction: str
    input_data: str
    output_schema: dict[str, object]


class ScriptedFakeTransport:
    """応答または例外を指定順に一度ずつ返す。"""

    def __init__(self, script: Sequence[object]) -> None:
        self._script = list(script)
        self.request_count = 0
        self.calls: list[FakeCall] = []

    async def request(
        self,
        model_id: str,
        purpose: LLMPurpose,
        instruction: str,
        input_data: str,
        output_schema: dict[str, object],
    ) -> object:
        if self.request_count >= len(self._script):
            raise RuntimeError("Fake LLMのscriptを使い切りました")
        self.calls.append(
            FakeCall(model_id, purpose, instruction, input_data, output_schema)
        )
        outcome = self._script[self.request_count]
        self.request_count += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
