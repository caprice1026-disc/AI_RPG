"""Body limits do not trust Content-Length or partial chunks."""

from unittest.mock import AsyncMock

import pytest

from ai_rpg.api.request_limits import RequestSizeLimit


@pytest.mark.asyncio
async def test_chunked_limit_rejects_before_json_application():
    app, send = AsyncMock(), AsyncMock()
    receive = AsyncMock(side_effect=[
        {"type": "http.request", "body": b"123456", "more_body": True},
        {"type": "http.request", "body": b"78901", "more_body": False},
    ])
    await RequestSizeLimit(app, max_bytes=10)({"type": "http", "method": "POST"}, receive, send)
    app.assert_not_called()
    assert send.call_args_list[0].args[0]["status"] == 413


@pytest.mark.asyncio
async def test_allowed_chunks_are_replayed_exactly_once():
    receive = AsyncMock(side_effect=[
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456", "more_body": False},
        {"type": "http.disconnect"},
    ])
    async def app(scope, receiver, send):
        assert (await receiver())["body"] == b"123456"
        assert (await receiver())["type"] == "http.disconnect"
    await RequestSizeLimit(app, max_bytes=10)(
        {"type": "http", "method": "PUT"}, receive, AsyncMock(),
    )
