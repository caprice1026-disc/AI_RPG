"""Authenticated adventure catalog, creation and resume use cases."""

from uuid import UUID

from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.ports.adventures import (
    AdventureStore,
    CharacterPreset,
    InvalidAdventureError,
    InvalidHistoryCursorError,
)
from ai_rpg.contracts.adventures import (
    AdventureCatalogResponse,
    AdventureHistoryResponse,
    AdventureListResponse,
    CreateAdventureRequest,
    CreateAdventureResponse,
    PresetSummary,
    ScenarioSummary,
)
from ai_rpg.scenarios import BUILTIN_SCENARIOS

PRESETS = (
    CharacterPreset(
        PresetSummary(
            preset_ref="scout", name="斥候", description="探索と隠密に長けた冒険者。", max_hp=10
        ),
        defense=12,
        attack_bonus=2,
        perception=3,
        persuasion=1,
        stealth=3,
    ),
    CharacterPreset(
        PresetSummary(
            preset_ref="guardian", name="守護者", description="打たれ強い前衛の冒険者。", max_hp=14
        ),
        defense=14,
        attack_bonus=3,
        perception=1,
        persuasion=2,
        stealth=0,
    ),
)


class AdventureService:
    def __init__(self, store: AdventureStore) -> None:
        self._store = store

    def catalog(self) -> AdventureCatalogResponse:
        scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 2)
        return AdventureCatalogResponse(
            scenarios=[
                ScenarioSummary(
                    scenario_ref=scenario.scenario_ref,
                    scenario_version=scenario.version,
                    title=scenario.title,
                    objective=scenario.objective,
                )
            ],
            presets=[preset.summary for preset in PRESETS],
        )

    async def create(
        self,
        principal: AuthenticatedPrincipal,
        request: CreateAdventureRequest,
    ) -> CreateAdventureResponse:
        try:
            scenario = BUILTIN_SCENARIOS.get(request.scenario_ref, request.scenario_version)
            preset = next(p for p in PRESETS if p.summary.preset_ref == request.preset_ref)
        except (KeyError, StopIteration) as error:
            raise InvalidAdventureError("Unknown scenario or preset") from error
        return await self._store.create(principal.principal_id, request, scenario, preset)

    async def list_owned(self, principal: AuthenticatedPrincipal) -> AdventureListResponse:
        return await self._store.list_owned(principal.principal_id)

    async def history(
        self,
        principal: AuthenticatedPrincipal,
        campaign_id: UUID,
        limit: int = 50,
        before_turn_id: UUID | None = None,
    ) -> AdventureHistoryResponse:
        if not 1 <= limit <= 100:
            raise InvalidHistoryCursorError("History limit must be between 1 and 100")
        return await self._store.history(principal.principal_id, campaign_id, limit, before_turn_id)
