"""SSE transportのbackpressure境界。"""

import asyncio

import pytest

from ai_rpg.api.app import _TimedStreamingResponse


@pytest.mark.asyncio
async def test_slow_sse_subscriber_is_disconnected_after_send_timeout() -> None:
    async def body():  # type: ignore[no-untyped-def]
        yield "event: turn.updated\ndata: {}\n\n"

    sent: list[str] = []

    async def slow_send(message: dict[str, object]) -> None:
        sent.append(str(message["type"]))
        if message.get("more_body") is True:
            await asyncio.sleep(1)

    response = _TimedStreamingResponse(body(), media_type="text/event-stream")
    response.send_timeout_seconds = 0.001

    await response.stream_response(slow_send)  # type: ignore[arg-type]

    assert sent == ["http.response.start", "http.response.body"]
