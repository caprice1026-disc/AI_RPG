"""HTTP adapterのIntegration Test。"""

import pytest
from httpx import ASGITransport, AsyncClient

from ai_rpg.api import create_app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    """application factoryが応答可能なASGI appを返す。"""

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
