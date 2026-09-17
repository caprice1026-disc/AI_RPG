"""永続予約を通る構造化出力adapterとFake transport。"""

from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from ai_rpg.llm import (
    CallBudgetExceeded,
    ScriptedFakeTransport,
    StructuredOutputAdapter,
    StructuredRequest,
)


@pytest.mark.asyncio
async def test_adapter_reserves_before_transport_and_validates_output() -> None:
    order: list[str] = []

    async def reserve() -> bool:
        order.append("reserve")
        return True

    transport = ScriptedFakeTransport([{"value": 7}])
    adapter = StructuredOutputAdapter(transport, reserve)

    result = await adapter.generate(
        StructuredRequest(
            model_id="fake",
            purpose="intent",
            system_instruction="test",
            input_data="{}",
            output_adapter=TypeAdapter(dict[str, int]),
        )
    )

    assert result == {"value": 7}
    assert order == ["reserve"]
    assert transport.request_count == 1
    assert transport.calls[0].purpose == "intent"
    assert transport.calls[0].output_schema["type"] == "object"


@pytest.mark.asyncio
async def test_adapter_does_not_call_transport_when_db_reservation_fails() -> None:
    reserve = AsyncMock(return_value=False)
    transport = ScriptedFakeTransport([{"value": 7}])

    with pytest.raises(CallBudgetExceeded):
        await StructuredOutputAdapter(transport, reserve).generate(
            StructuredRequest(
                model_id="fake",
                purpose="intent",
                system_instruction="test",
                input_data="{}",
                output_adapter=TypeAdapter(dict[str, int]),
            )
        )

    assert transport.request_count == 0


@pytest.mark.asyncio
async def test_invalid_fake_output_still_consumes_one_reserved_call() -> None:
    reserve = AsyncMock(return_value=True)
    transport = ScriptedFakeTransport([{"value": "invalid"}])

    with pytest.raises(ValidationError):
        await StructuredOutputAdapter(transport, reserve).generate(
            StructuredRequest(
                model_id="fake",
                purpose="intent",
                system_instruction="test",
                input_data="{}",
                output_adapter=TypeAdapter(dict[str, int]),
            )
        )

    reserve.assert_awaited_once()
    assert transport.request_count == 1


@pytest.mark.asyncio
async def test_fake_transport_can_script_timeout() -> None:
    transport = ScriptedFakeTransport([TimeoutError("scripted timeout")])

    with pytest.raises(TimeoutError, match="scripted timeout"):
        await transport.request("fake", "intent", "test", "{}", {"type": "object"})

    assert transport.request_count == 1


@pytest.mark.asyncio
async def test_fake_transport_can_script_transient_failure() -> None:
    transport = ScriptedFakeTransport([ConnectionError("provider unavailable")])

    with pytest.raises(ConnectionError, match="provider unavailable"):
        await transport.request(
            "fake", "intent", "test", "{}", {"type": "object"}
        )

    assert transport.request_count == 1
