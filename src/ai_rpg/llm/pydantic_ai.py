"""一回の要求に制限した用途別Pydantic AI Agent。"""

from collections.abc import Mapping
from json import JSONDecodeError
from typing import Any, cast

import httpx2
from openai import APIResponseValidationError, APITimeoutError
from pydantic import BaseModel, ValidationError, create_model
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from pydantic_ai.usage import UsageLimits

from ai_rpg.application.ports.llm import (
    IntentResult,
    NarrativeResult,
    ProviderHTTPError,
    ProviderOutputError,
    ProviderRefusalError,
)
from ai_rpg.contracts.common import Contract
from ai_rpg.contracts.context import MechanicalInput, NarrativeInput
from ai_rpg.contracts.llm_decisions import make_decision_output_types
from ai_rpg.contracts.responses import MechanicalNarrationDraft, MechanicalNarrationInput
from ai_rpg.llm.prompts import (
    INTENT_INSTRUCTIONS,
    NARRATION_INSTRUCTIONS,
    NARRATIVE_INSTRUCTIONS,
)


class _AgentOutput(Contract):
    result: object


class PydanticAILLM:
    """ApplicationによるDB予約後に呼ぶ。SDK clientの寿命はruntimeが管理する。"""

    def __init__(self, models: Mapping[str, Model]) -> None:
        self._models = dict(models)

    async def generate_narrative(
        self, context: NarrativeInput, *, model_id: str
    ) -> NarrativeResult:
        output, _ = make_decision_output_types(context.output_limits.max_actions)
        return cast(
            NarrativeResult,
            await self._run(model_id, "narrative", NARRATIVE_INSTRUCTIONS, context, output),
        )

    async def extract_intent(self, context: MechanicalInput, *, model_id: str) -> IntentResult:
        _, output = make_decision_output_types(context.output_limits.max_actions)
        return cast(
            IntentResult, await self._run(model_id, "intent", INTENT_INSTRUCTIONS, context, output)
        )

    async def narrate_result(
        self, context: MechanicalNarrationInput, *, model_id: str
    ) -> MechanicalNarrationDraft:
        return cast(
            MechanicalNarrationDraft,
            await self._run(
                model_id,
                "result_narration",
                NARRATION_INSTRUCTIONS,
                context,
                MechanicalNarrationDraft,
            ),
        )

    async def _run(
        self,
        model_id: str,
        purpose: str,
        instructions: str,
        context: BaseModel,
        output_type: Any,
    ) -> object:
        # Typed object envelope supports providers that reject a root union.
        # Schema adaptation itself belongs to Pydantic AI, not handwritten JSON rewrites.
        envelope = create_model(
            f"{purpose}_output", __base__=_AgentOutput, result=(output_type, ...)
        )
        agent = Agent(
            self._models[model_id],
            output_type=NativeOutput(envelope, strict=True),
            instructions=instructions,
            retries=0,
            model_settings=OpenAIResponsesModelSettings(openai_store=False, max_tokens=4096),
        )
        agent.instrument = False
        try:
            result = await agent.run(
                context.model_dump_json(), usage_limits=UsageLimits(request_limit=1)
            )
        except ContentFilterError:
            raise ProviderRefusalError("model refused output") from None
        except ModelHTTPError as error:
            raise ProviderHTTPError(error.status_code) from None
        except (httpx2.TimeoutException, APITimeoutError):
            raise TimeoutError("model request timed out") from None
        except ModelAPIError as error:
            if isinstance(error.__cause__, (httpx2.TimeoutException, APITimeoutError)):
                raise TimeoutError("model request timed out") from None
            raise ConnectionError("model request failed") from None
        except httpx2.RequestError:
            raise ConnectionError("model request failed") from None
        except (UnexpectedModelBehavior, UsageLimitExceeded, ValidationError):
            raise ProviderOutputError("invalid model output") from None
        except (JSONDecodeError, APIResponseValidationError, TypeError, AttributeError):
            # SDKs can fail while decoding a malformed HTTP 200 envelope,
            # before Pydantic AI constructs a ModelResponse.
            raise ProviderOutputError("invalid provider response") from None

        response = result.response
        if response.finish_reason == "content_filter":
            raise ProviderRefusalError("model refused output")
        if response.finish_reason != "stop":
            raise ProviderOutputError("incomplete model output")
        return result.output.result
