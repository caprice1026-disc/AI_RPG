"""モデルrouting、構造化出力adapter、呼び出し予算。"""

from ai_rpg.llm.budget import CallBudget, CallBudgetExceeded
from ai_rpg.llm.fake import DevelopmentFakeTransport, ScriptedFakeTransport
from ai_rpg.llm.openai import OpenAIResponsesTransport
from ai_rpg.llm.routing import ModelRouter, ModelTier
from ai_rpg.llm.structured import (
    ProviderError,
    ProviderHTTPError,
    ProviderOutputError,
    ProviderRefusalError,
    StructuredOutputAdapter,
    StructuredRequest,
)

__all__ = [
    "CallBudget",
    "CallBudgetExceeded",
    "DevelopmentFakeTransport",
    "ModelRouter",
    "ModelTier",
    "OpenAIResponsesTransport",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderOutputError",
    "ProviderRefusalError",
    "ScriptedFakeTransport",
    "StructuredOutputAdapter",
    "StructuredRequest",
]
