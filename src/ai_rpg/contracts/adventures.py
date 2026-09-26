"""Public adventure catalog, start, list and history contracts."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from ai_rpg.contracts.common import Contract, InputText, NonNegativeInt, PositiveInt, Ref, ShortText
from ai_rpg.contracts.responses import TurnResponse


class AbilityScores(Contract):
    strength: int = Field(strict=True, ge=0, le=3)
    agility: int = Field(strict=True, ge=0, le=3)
    insight: int = Field(strict=True, ge=0, le=3)
    presence: int = Field(strict=True, ge=0, le=3)


class AbilityAllocation(AbilityScores):
    @model_validator(mode="after")
    def two_points(self) -> Self:
        values = (self.strength, self.agility, self.insight, self.presence)
        if sum(values) != 2 or any(value > 2 for value in values):
            raise ValueError("Ability allocation must spend exactly two points")
        return self


class CharacterCreationSummary(Contract):
    abilities: list[Literal["strength", "agility", "insight", "presence"]]
    points: Literal[2]
    specialties: list[
        Literal["athletics", "acrobatics", "perception", "stealth", "persuasion"]
    ]


class ScenarioSummary(Contract):
    scenario_ref: Ref
    scenario_version: PositiveInt
    title: ShortText
    objective: ShortText
    character_creation: CharacterCreationSummary | None = None


class PresetSummary(Contract):
    preset_ref: Ref
    name: ShortText
    description: ShortText
    max_hp: PositiveInt
    base_abilities: AbilityScores | None = None


class AdventureCatalogResponse(Contract):
    scenarios: list[ScenarioSummary]
    presets: list[PresetSummary]


class CreateAdventureRequest(Contract):
    request_id: UUID
    scenario_ref: Ref
    scenario_version: PositiveInt
    preset_ref: Ref
    player_name: Annotated[str, Field(min_length=1, max_length=40)]
    ability_points: AbilityAllocation | None = None
    specialty_skill: Literal[
        "athletics", "acrobatics", "perception", "stealth", "persuasion"
    ] | None = None

    @model_validator(mode="after")
    def paired_creation_choices(self) -> Self:
        if (self.ability_points is None) != (self.specialty_skill is None):
            raise ValueError("Ability points and specialty must be selected together")
        return self

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
