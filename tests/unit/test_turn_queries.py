"""認可付きTurn公開DTO取得ユースケース。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ai_rpg.application import (
    AuthenticatedPrincipal,
    AuthorizationError,
    AuthorizationPolicy,
    TurnNotFoundError,
    TurnQueryService,
)
from ai_rpg.application.ports import ScenarioRunSnapshot, ScenarioSceneSnapshot
from ai_rpg.contracts import CampaignStateResponse, TurnResponse
from ai_rpg.scenarios import BUILTIN_SCENARIOS


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        principal_id=uuid4(),
        issuer="test",
        subject="player",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset(),
    )


def _response() -> TurnResponse:
    return TurnResponse.model_validate(
        {
            "turn_id": uuid4(),
            "route": None,
            "resolution_status": "pending",
            "narration_status": "pending",
            "committed_state_version": None,
            "narration": None,
            "choices": [],
            "action_results": [],
            "recovery": {"fallback": False, "reason": None},
        }
    )


@pytest.mark.asyncio
async def test_get_returns_repository_public_projection() -> None:
    principal = _principal()
    campaign_id = uuid4()
    response = _response()
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = True
    unit_of_work = AsyncMock()
    unit_of_work.__aenter__.return_value = unit_of_work
    unit_of_work.turns = AsyncMock()
    unit_of_work.turns.get_response.return_value = response
    service = TurnQueryService(
        authorization, MagicMock(return_value=unit_of_work), BUILTIN_SCENARIOS
    )

    assert await service.get(principal, campaign_id, response.turn_id) == response
    unit_of_work.turns.get_response.assert_awaited_once_with(campaign_id, response.turn_id)
    unit_of_work.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_rejects_unauthorized_campaign_before_repository() -> None:
    principal = _principal()
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = False
    unit_of_work_factory = MagicMock()
    service = TurnQueryService(authorization, unit_of_work_factory, BUILTIN_SCENARIOS)

    with pytest.raises(AuthorizationError):
        await service.get(principal, uuid4(), uuid4())

    unit_of_work_factory.assert_not_called()


@pytest.mark.asyncio
async def test_get_reports_missing_turn_without_leaking_internal_rows() -> None:
    principal = _principal()
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = True
    unit_of_work = AsyncMock()
    unit_of_work.__aenter__.return_value = unit_of_work
    unit_of_work.turns = AsyncMock()
    unit_of_work.turns.get_response.return_value = None
    service = TurnQueryService(
        authorization, MagicMock(return_value=unit_of_work), BUILTIN_SCENARIOS
    )

    with pytest.raises(TurnNotFoundError):
        await service.get(principal, uuid4(), uuid4())

    unit_of_work.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_campaign_state_returns_version_and_latest_turn() -> None:
    principal = _principal()
    campaign_id = uuid4()
    state = CampaignStateResponse(state_version=4, latest_turn=_response())
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = True
    unit_of_work = AsyncMock()
    unit_of_work.__aenter__.return_value = unit_of_work
    unit_of_work.turns = AsyncMock()
    unit_of_work.turns.get_campaign_state.return_value = state
    unit_of_work.scenarios = AsyncMock()
    unit_of_work.scenarios.snapshot.return_value = None
    service = TurnQueryService(
        authorization, MagicMock(return_value=unit_of_work), BUILTIN_SCENARIOS
    )

    result = await service.get_campaign_state(principal, campaign_id)

    assert result == state
    assert result.adventure is None
    unit_of_work.turns.get_campaign_state.assert_awaited_once_with(campaign_id)
    unit_of_work.scenarios.snapshot.assert_awaited_once_with(campaign_id)
    unit_of_work.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_campaign_state_projects_public_adventure() -> None:
    principal = _principal()
    campaign_id = uuid4()
    entrance_id, hall_id, sanctum_id = uuid4(), uuid4(), uuid4()
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = True
    unit_of_work = AsyncMock()
    unit_of_work.__aenter__.return_value = unit_of_work
    unit_of_work.turns = AsyncMock()
    unit_of_work.turns.get_campaign_state.return_value = CampaignStateResponse(
        state_version=0, latest_turn=None
    )
    unit_of_work.scenarios = AsyncMock()
    unit_of_work.scenarios.snapshot.return_value = ScenarioRunSnapshot(
        campaign_id=campaign_id,
        scenario_ref="ruined_chapel",
        scenario_version=1,
        status="active",
        ending_ref=None,
        scenes=(
            ScenarioSceneSnapshot(id=entrance_id, sequence=1, status="active"),
            ScenarioSceneSnapshot(id=hall_id, sequence=2, status="planned"),
            ScenarioSceneSnapshot(id=sanctum_id, sequence=3, status="planned"),
        ),
        flags=frozenset(),
    )
    service = TurnQueryService(
        authorization, MagicMock(return_value=unit_of_work), BUILTIN_SCENARIOS
    )

    state = await service.get_campaign_state(principal, campaign_id)

    assert state.adventure is not None
    assert state.adventure.objective == "廃礼拝堂の奥から銀の聖印を回収する"
    assert state.adventure.current_scene is not None
    assert state.adventure.current_scene.scene_ref == "entrance"
    assert [action.action_ref for action in state.adventure.available_actions] == [
        "enter_chapel"
    ]


@pytest.mark.asyncio
async def test_get_campaign_state_projects_public_ending() -> None:
    principal = _principal()
    campaign_id = uuid4()
    authorization = AsyncMock(spec=AuthorizationPolicy)
    authorization.can_access_campaign.return_value = True
    unit_of_work = AsyncMock()
    unit_of_work.__aenter__.return_value = unit_of_work
    unit_of_work.turns = AsyncMock()
    unit_of_work.turns.get_campaign_state.return_value = CampaignStateResponse(
        state_version=3, latest_turn=None
    )
    unit_of_work.scenarios = AsyncMock()
    unit_of_work.scenarios.snapshot.return_value = ScenarioRunSnapshot(
        campaign_id=campaign_id,
        scenario_ref="ruined_chapel",
        scenario_version=1,
        status="completed",
        ending_ref="recovered",
        scenes=tuple(
            ScenarioSceneSnapshot(id=uuid4(), sequence=sequence, status="closed")
            for sequence in (1, 2, 3)
        ),
        flags=frozenset({"clue_found"}),
    )
    service = TurnQueryService(
        authorization, MagicMock(return_value=unit_of_work), BUILTIN_SCENARIOS
    )

    state = await service.get_campaign_state(principal, campaign_id)

    assert state.adventure is not None
    assert state.adventure.current_scene is None
    assert state.adventure.available_actions == []
    assert state.adventure.discovered_facts == [
        "広間で聖印へ続く手掛かりを見つけた。"
    ]
    assert state.adventure.ending is not None
    assert state.adventure.ending.ending_ref == "recovered"
