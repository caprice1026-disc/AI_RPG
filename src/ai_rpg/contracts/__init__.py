"""外部入力、LLM入出力、公開DTOの契約。"""

from ai_rpg.contracts.context import ContextFragment, EntityRef, OutputLimits
from ai_rpg.contracts.events import PublicEvent, PublicTurnEventPayload
from ai_rpg.contracts.llm_decisions import (
    ActionIntent,
    AttackIntent,
    MechanicalDecision,
    NarrativeDecision,
    ScenarioActionIntent,
    make_decision_types,
)
from ai_rpg.contracts.player_turn import ChoiceInput, PlayerTurnInput, TextInput
from ai_rpg.contracts.responses import (
    AdventureAction,
    AdventureEnding,
    AdventureScene,
    AdventureState,
    CampaignStateResponse,
    TurnRecovery,
    TurnResponse,
)

__all__ = [
    "ActionIntent",
    "AdventureAction",
    "AdventureEnding",
    "AdventureScene",
    "AdventureState",
    "AttackIntent",
    "CampaignStateResponse",
    "ChoiceInput",
    "ContextFragment",
    "EntityRef",
    "MechanicalDecision",
    "NarrativeDecision",
    "OutputLimits",
    "PlayerTurnInput",
    "PublicEvent",
    "PublicTurnEventPayload",
    "ScenarioActionIntent",
    "TextInput",
    "TurnRecovery",
    "TurnResponse",
    "make_decision_types",
]
