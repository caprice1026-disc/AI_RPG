"""用途別LLM境界。SDKの型と呼出予算を公開しない。"""

from typing import Protocol, TypeAlias

from ai_rpg.contracts.context import MechanicalInput, NarrativeInput
from ai_rpg.contracts.llm_decisions import (
    ActionPlan,
    ClarificationRequired,
    NarrativeDraft,
    ResolutionRequired,
)
from ai_rpg.contracts.responses import MechanicalNarrationDraft, MechanicalNarrationInput

NarrativeResult: TypeAlias = NarrativeDraft | ResolutionRequired | ClarificationRequired
IntentResult: TypeAlias = ActionPlan | ClarificationRequired


class ProviderError(RuntimeError):
    """本文や秘密値を含まない、分類済みのモデル失敗。"""


class ProviderHTTPError(ProviderError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"provider HTTP {status_code}")


class ProviderOutputError(ProviderError):
    """空、不正、未完了のモデル出力。"""


class ProviderRefusalError(ProviderError):
    """モデルの拒否または安全フィルター。"""


class NarrativeGenerator(Protocol):
    async def generate_narrative(
        self, context: NarrativeInput, *, model_id: str
    ) -> NarrativeResult: ...


class IntentExtractor(Protocol):
    async def extract_intent(
        self, context: MechanicalInput, *, model_id: str
    ) -> IntentResult: ...


class ResultNarrator(Protocol):
    async def narrate_result(
        self, context: MechanicalNarrationInput, *, model_id: str
    ) -> MechanicalNarrationDraft: ...


class ResolutionLLM(NarrativeGenerator, IntentExtractor, Protocol):
    """解決workerに必要な2用途。"""


class GameLLM(ResolutionLLM, ResultNarrator, Protocol):
    """runtimeが構成する用途別LLM一式。"""
