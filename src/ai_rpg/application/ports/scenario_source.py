"""Resolve immutable story versions before entering pure game logic."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ai_rpg.contracts.adventures import CreateAdventureRequest, ScenarioSummary
from ai_rpg.scenarios.models import ScenarioDefinition


@dataclass(frozen=True, slots=True)
class ResolvedScenario:
    version_id: UUID
    definition: ScenarioDefinition


class ScenarioSource(Protocol):
    async def for_start(
        self, principal_id: UUID, request: CreateAdventureRequest,
    ) -> ResolvedScenario: ...

    async def catalog(self, principal_id: UUID) -> list[ScenarioSummary]: ...
