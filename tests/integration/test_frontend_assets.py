"""Python-only installations serve the bundled Vue application and its assets."""

import re

import pytest
from httpx import ASGITransport, AsyncClient

from ai_rpg.api import create_app


@pytest.mark.asyncio
async def test_vue_entry_and_hashed_assets_are_served_without_node() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test",
    ) as client:
        entry = await client.get("/")
        assert entry.status_code == 200
        assert entry.headers["content-type"].startswith("text/html")
        assert entry.headers["cache-control"] == "no-store"
        assert 'id="app"' in entry.text
        assert 'type="module"' in entry.text
        assets = re.findall(r'(?:src|href)="(/static/vue/assets/[^\"]+)"', entry.text)
        assert any(path.endswith(".js") for path in assets)
        assert any(path.endswith(".css") for path in assets)
        for path in assets:
            response = await client.get(path)
            assert response.status_code == 200
            expected = "javascript" if path.endswith(".js") else "text/css"
            assert expected in response.headers["content-type"]
            assert response.headers["x-content-type-options"] == "nosniff"
        art = await client.get("/static/vue/art/ruined-chapel.png")
        assert art.status_code == 200
        assert art.headers["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_vue_static_route_does_not_expose_source_or_parent_directories() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test",
    ) as client:
        for path in (
            "/static/vue/%2e%2e/%2e%2e/config.py",
            "/static/vue/src/App.vue",
            "/static/vue/missing.js",
            "/static/play-state.js",
        ):
            assert (await client.get(path)).status_code == 404
