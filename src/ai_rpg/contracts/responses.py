"""クライアントへ公開するTurnレスポンス契約。"""

from typing import Literal, Self, TypeAlias
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from ai_rpg.contracts.common import (
    Contract,
    InputText,
    NarrationText,
    NonNegativeInt,
    Ref,
    ShortText,
)
from ai_rpg.contracts.context import ContextFragment, EntityRef, OutputLimits
from ai_rpg.contracts.llm_decisions import ChoiceDraft
from ai_rpg.domain.results import ResolvedAction, ResolvedEnemyReaction

RecoveryReason: TypeAlias = Literal[
    "MODEL_TIMEOUT",
    "INVALID_OUTPUT",
    "MODEL_REFUSAL",
    "INJECTION_DETECTED",
    "CONTEXT_CONFLICT",
    "UNKNOWN",
]


class MechanicalNarrationInput(Contract):
    """確定結果から描写だけを生成させるLLM入力。"""

    player_text: InputText
    committed_state_version: NonNegativeInt
    resolved_actions: list[ResolvedAction]
    enemy_reactions: list[ResolvedEnemyReaction] = Field(default_factory=list)
    public_state_after: list[ContextFragment]
    allowed_entity_refs: list[EntityRef]
    output_limits: OutputLimits


class MechanicalNarrationDraft(Contract):
    narration: NarrationText
    choices: list[ChoiceDraft] = Field(max_length=5)


class TurnRecovery(Contract):
    fallback: StrictBool
    reason: RecoveryReason | None

    @model_validator(mode="after")
    def consistent_reason(self) -> Self:
        if self.fallback != (self.reason is not None):
            raise ValueError("fallbackとreasonは一致する必要があります")
        return self


class Choice(Contract):
    id: UUID
    label: ShortText


class RiskPreview(Contract):
    proposal_id: UUID
    risk_text: ShortText


ResolutionStatus: TypeAlias = Literal["pending", "resolving", "committed", "not_applied", "failed"]
NarrationStatus: TypeAlias = Literal["pending", "generating", "completed", "fallback"]


class TurnResponse(Contract):
    turn_id: UUID
    route: Literal["narrative", "mechanical"] | None
    resolution_status: ResolutionStatus
    narration_status: NarrationStatus
    committed_state_version: NonNegativeInt | None
    narration: NarrationText | None
    choices: list[Choice]
    action_results: list[ResolvedAction]
    enemy_reactions: list[ResolvedEnemyReaction] = Field(default_factory=list)
    recovery: TurnRecovery
    risk_preview: RiskPreview | None = None

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if (self.resolution_status == "committed") != (self.committed_state_version is not None):
            raise ValueError("committed状態とstate versionは一致する必要があります")
        if self.resolution_status == "committed" and self.route is None:
            raise ValueError("確定済みTurnにはrouteが必要です")
        if (self.action_results or self.enemy_reactions) and (
            self.route != "mechanical" or self.resolution_status != "committed"
        ):
            raise ValueError("Action結果には確定済みMechanical Turnが必要です")
        done = self.narration_status in ("completed", "fallback")
        if done != (self.narration is not None):
            raise ValueError("終端の描写状態だけがnarrationを持てます")
        if self.choices and not done:
            raise ValueError("選択肢には終端の描写状態が必要です")
        if (self.narration_status == "fallback") != self.recovery.fallback:
            raise ValueError("fallback状態とrecoveryは一致する必要があります")
        return self


class AdventureScene(Contract):
    scene_ref: Ref
    title: ShortText
    description: ShortText


class AdventureAction(Contract):
    action_ref: Ref
    label: ShortText


class AdventureEnding(Contract):
    ending_ref: Ref
    title: ShortText
    summary: ShortText
    reward: ShortText | None = None


class AdventureCombatState(Contract):
    enemy_ref: Ref
    enemy_name: ShortText
    current_hp: NonNegativeInt
    max_hp: NonNegativeInt
    active: StrictBool


class AdventureFact(Contract):
    fact_ref: Ref
    kind: Literal["place", "person", "clue", "route"]
    public_text: ShortText
    scene_ref: Ref


class AdventureState(Contract):
    scenario_ref: Ref
    title: ShortText
    objective: ShortText
    status: Literal["active", "completed"]
    current_scene: AdventureScene | None
    discovered_facts: list[ShortText]
    generated_facts: list[AdventureFact] = Field(default_factory=list)
    available_actions: list[AdventureAction]
    ending: AdventureEnding | None
    combat: AdventureCombatState | None = None
    elapsed_actions: NonNegativeInt | None = None
    alert_level: int | None = Field(default=None, ge=0, le=5)


class AbilityDisplay(Contract):
    strength: int = Field(ge=0, le=3)
    agility: int = Field(ge=0, le=3)
    insight: int = Field(ge=0, le=3)
    presence: int = Field(ge=0, le=3)


class InventoryItem(Contract):
    item_id: UUID
    item_ref: Ref | None = None
    name: ShortText
    quantity: NonNegativeInt
    equipped: StrictBool


class PlayerState(Contract):
    actor_id: UUID
    name: ShortText
    current_hp: NonNegativeInt
    max_hp: NonNegativeInt
    inventory: list[InventoryItem] = Field(default_factory=list)
    abilities: AbilityDisplay | None = None
    specialty_skill: Ref | None = None


class CampaignStateResponse(Contract):
    """プレイ再開に必要なCampaignの正本versionと最新Turn。"""

    state_version: NonNegativeInt
    latest_turn: TurnResponse | None
    adventure: AdventureState | None = None
    player: PlayerState | None = None
