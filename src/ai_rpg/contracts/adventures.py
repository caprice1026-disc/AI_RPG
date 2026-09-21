"""Public adventure catalog, start, list and history contracts."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from ai_rpg.contracts.common import Contract, InputText, NonNegativeInt, PositiveInt, Ref, ShortText
from ai_rpg.contracts.responses import TurnResponse


class ScenarioSummary(Contract):
    scenario_ref: Ref
    scenario_version: PositiveInt
    title: ShortText
    objective: ShortText


class PresetSummary(Contract):
    preset_ref: Ref
    name: ShortText
    description: ShortText
    max_hp: PositiveInt


class AdventureCatalogResponse(Contract):
    scenarios: list[ScenarioSummary]
    presets: list[PresetSummary]


class CreateAdventureRequest(Contract):
    request_id: UUID
    scenario_ref: Ref
    scenario_version: PositiveInt
    preset_ref: Ref
    player_name: Annotated[str, Field(min_length=1, max_length=40)]

    @field_validator("player_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip() or any(not char.isprintable() for char in value):
            raise ValueError("Player name must contain printable non-whitespace text")
        # Preserve exact input for idempotency, including surrounding spaces.
        return value


class CreateAdventureResponse(Contract):
    campaign_id: UUID
    actor_id: UUID


class AdventureSummary(Contract):
    campaign_id: UUID
    actor_id: UUID
    player_name: ShortText
    scenario_ref: Ref
    scenario_version: PositiveInt
    title: ShortText
    status: Literal["active", "completed"]
    state_version: NonNegativeInt
    created_at: datetime


class AdventureListResponse(Contract):
    adventures: list[AdventureSummary]


class HistoryItem(Contract):
    created_at: datetime
    player_input: InputText
    turn: TurnResponse


class AdventureHistoryResponse(Contract):
    items: list[HistoryItem]
    next_before_turn_id: UUID | None
