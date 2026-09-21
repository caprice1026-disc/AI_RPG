"""Atomic adventure storage boundary and typed server-owned PC presets."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ai_rpg.contracts.adventures import (
    AdventureHistoryResponse,
    AdventureListResponse,
    CreateAdventureRequest,
    CreateAdventureResponse,
    PresetSummary,
)
from ai_rpg.scenarios import ScenarioDefinition


class InvalidAdventureError(ValueError):
    code = "INVALID_ADVENTURE"


class InvalidHistoryCursorError(ValueError):
    code = "INVALID_HISTORY_CURSOR"


@dataclass(frozen=True, slots=True)
class CharacterPreset:
    summary: PresetSummary
    defense: int
    attack_bonus: int
    perception: int
    persuasion: int
    stealth: int


class AdventureStore(Protocol):
    async def create(
        self,
        principal_id: UUID,
        request: CreateAdventureRequest,
        scenario: ScenarioDefinition,
        preset: CharacterPreset,
    ) -> CreateAdventureResponse: ...

    async def list_owned(self, principal_id: UUID) -> AdventureListResponse: ...

    async def history(
        self,
        principal_id: UUID,
        campaign_id: UUID,
        limit: int,
        before_turn_id: UUID | None,
    ) -> AdventureHistoryResponse: ...
