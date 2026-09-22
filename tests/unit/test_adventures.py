"""Adventure boundary validation and authenticated catalog."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from ai_rpg.api import create_app
from ai_rpg.application import AuthenticatedPrincipal
from ai_rpg.cli import _parser


async def authenticated() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        principal_id=UUID(int=21),
        issuer="test",
        subject="private-subject",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset(),
    )


@pytest.mark.asyncio
async def test_adventure_endpoints_require_authentication() -> None:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        for path in ("/adventures/catalog", "/adventures", f"/campaigns/{uuid4()}/history"):
            assert (await c.get(path)).status_code == 401
        response = await c.post(
            "/adventures",
            json={
                "request_id": str(uuid4()),
                "scenario_ref": "ruined_chapel",
                "scenario_version": 1,
                "preset_ref": "scout",
                "player_name": "Hero",
            },
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_catalog_exposes_only_public_scenario_and_preset_fields() -> None:
    app = create_app(principal_provider=authenticated)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/adventures/catalog")
    assert response.status_code == 200
    data = response.json()
    assert data["scenarios"] == [
        {
            "scenario_ref": "ruined_chapel",
            "scenario_version": 2,
            "title": "廃礼拝堂の聖印",
            "objective": "廃礼拝堂の銀の聖印を回収し、村の共同庫へ持ち帰る",
        }
    ]
    assert {p["preset_ref"] for p in data["presets"]} == {"scout", "guardian"}
    assert all(set(p) == {"preset_ref", "name", "description", "max_hp"} for p in data["presets"])
    assert "private-subject" not in response.text


@pytest.mark.parametrize("name", ["", " ", "a" * 41, "hero\nadmin", "hero\x00"])
def test_invalid_player_name_is_rejected(name: str) -> None:
    from ai_rpg.contracts.adventures import CreateAdventureRequest

    with pytest.raises(ValidationError):
        CreateAdventureRequest(
            request_id=uuid4(),
            scenario_ref="ruined_chapel",
            scenario_version=1,
            preset_ref="scout",
            player_name=name,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"preset_ref": "wizard"},
        {"scenario_ref": "unknown"},
        {"scenario_version": 999},
        {"principal_id": str(uuid4())},
        {"current_hp": 999},
        {"player_name": " "},
    ],
)
async def test_invalid_start_is_rejected_before_database(change: dict[str, object]) -> None:
    app = create_app(principal_provider=authenticated)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/adventures",
            json={
                "request_id": str(uuid4()),
                "scenario_ref": "ruined_chapel",
                "scenario_version": 1,
                "preset_ref": "scout",
                "player_name": "Hero",
                **change,
            },
        )
    assert response.status_code == 422


def test_development_principal_flag_is_optional_but_never_enabled_by_default() -> None:
    parser = _parser()
    assert parser.parse_args(["api"]).dev_principal is None
    assert parser.parse_args(["api", "--dev-principal"]).dev_principal == UUID(
        "10000000-0000-0000-0000-000000000021"
    )
    principal = uuid4()
    assert parser.parse_args(["api", "--dev-principal", str(principal)]).dev_principal == principal
