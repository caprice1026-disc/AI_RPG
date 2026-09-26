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
    AbilityScores,
    AdventureCatalogResponse,
    AdventureHistoryResponse,
    AdventureListResponse,
    CharacterCreationSummary,
    CreateAdventureRequest,
    CreateAdventureResponse,
    PresetSummary,
    ScenarioSummary,
)
from ai_rpg.scenarios import BUILTIN_SCENARIOS

PRESETS = (
    CharacterPreset(
        PresetSummary(
            preset_ref="scout", name="斥候", description="探索と隠密に長けた冒険者。", max_hp=10,
            base_abilities=AbilityScores(strength=0, agility=2, insight=1, presence=0),
        ),
        defense=12,
        attack_bonus=2,
        perception=3,
        persuasion=1,
        stealth=3,
    ),
    CharacterPreset(
        PresetSummary(
            preset_ref="guardian", name="守護者", description="打たれ強い前衛の冒険者。", max_hp=14,
            base_abilities=AbilityScores(strength=2, agility=0, insight=0, presence=1),
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
        scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 3)
        return AdventureCatalogResponse(
            scenarios=[
                ScenarioSummary(
                    scenario_ref=scenario.scenario_ref,
                    scenario_version=scenario.version,
                    title=scenario.title,
                    objective=scenario.objective,
                    character_creation=CharacterCreationSummary(
                        abilities=["strength", "agility", "insight", "presence"],
                        points=2,
                        specialties=[
                            "athletics", "acrobatics", "perception", "stealth", "persuasion"
                        ],
                    ),
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
        if scenario.ruleset_ref == "mvp_v2":
            if request.ability_points is None or request.specialty_skill is None:
                raise InvalidAdventureError("Ability points and specialty are required")
            assert preset.summary.base_abilities is not None
            base = preset.summary.base_abilities
            points = request.ability_points
            if any(
                getattr(base, ability) + getattr(points, ability) > 3
                for ability in ("strength", "agility", "insight", "presence")
            ):
                raise InvalidAdventureError("Ability score exceeds the allowed maximum")
        elif request.ability_points is not None or request.specialty_skill is not None:
            raise InvalidAdventureError("This scenario does not use ability allocation")
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
