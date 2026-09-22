"""Explicit providers and event-loop-scoped SDK clients for game workers."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx2
from google.genai import Client as GoogleClient
from google.genai.types import HttpOptions, HttpRetryOptions
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from ai_rpg.application.ports.llm import GameLLM
from ai_rpg.config import Settings
from ai_rpg.llm.fake import DevelopmentFakeLLM
from ai_rpg.llm.pydantic_ai import PydanticAILLM


@asynccontextmanager
async def build_language_models(settings: Settings, *, fake: bool) -> AsyncIterator[GameLLM]:
    """Keep every client alive for the worker, including cleanup after failed setup."""

    if fake:
        yield DevelopmentFakeLLM()
        return

    model_ids = tuple(dict.fromkeys((
        settings.fast_model, settings.quality_model, settings.background_model,
    )))
    selected = {model_id.split(":", 1)[0] for model_id in model_ids}
    keys: dict[str, str] = {}
    for provider, secret, name in (
        ("google", settings.gemini_api_key, "AIRPG_GEMINI_API_KEY or GEMINI_API_KEY"),
        ("openai-responses", settings.openai_api_key, "AIRPG_OPENAI_API_KEY or OPENAI_API_KEY"),
    ):
        if provider in selected:
            key = secret.get_secret_value() if secret is not None else ""
            if not key or any(not 33 <= ord(char) <= 126 for char in key):
                raise ValueError(f"{name} requires a nonempty printable API key for real workers")
            keys[provider] = key

    async with AsyncExitStack() as stack:
        try:
            http = await stack.enter_async_context(
                httpx2.AsyncClient(trust_env=False, timeout=settings.llm_timeout_seconds)
            )
            models: dict[str, Model] = {}
            if "google" in selected:
                # genai also constructs a synchronous client; own both from the outset.
                sync_http = stack.enter_context(
                    httpx2.Client(trust_env=False, timeout=settings.llm_timeout_seconds)
                )
                google = GoogleClient(
                    vertexai=False,
                    api_key=keys["google"],
                    http_options=HttpOptions(
                        base_url="https://generativelanguage.googleapis.com/",
                        httpx_client=sync_http,
                        httpx_async_client=http,
                        client_args={"trust_env": False, "verify": True},
                        async_client_args={"trust_env": False, "verify": True},
                        timeout=settings.llm_timeout_seconds * 1000,
                        retry_options=HttpRetryOptions(attempts=1),
                    ),
                )
                stack.callback(google.close)
                stack.push_async_callback(google.aio.aclose)
                google_provider = GoogleProvider(client=google)
                models.update({
                    model_id: GoogleModel(model_id.split(":", 1)[1], provider=google_provider)
                    for model_id in model_ids if model_id.startswith("google:")
                })
            if "openai-responses" in selected:
                openai = AsyncOpenAI(
                    api_key=keys["openai-responses"],
                    base_url="https://api.openai.com/v1",
                    http_client=http,
                    timeout=settings.llm_timeout_seconds,
                    max_retries=0,
                )
                stack.push_async_callback(openai.close)
                openai_provider = OpenAIProvider(openai_client=openai)
                models.update({
                    model_id: OpenAIResponsesModel(
                        model_id.split(":", 1)[1], provider=openai_provider,
                    )
                    for model_id in model_ids if model_id.startswith("openai-responses:")
                })
            llm = PydanticAILLM(models)
        except Exception:
            # SDK validation errors may include constructor arguments containing secrets.
            raise ValueError("LLM client initialization failed") from None
        yield llm
