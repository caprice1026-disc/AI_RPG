"""Model selection, SDK retry limits and client ownership without network access."""

import asyncio
from collections.abc import Mapping
from importlib import import_module

import httpx2
import pytest
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel

from ai_rpg.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in (
        "AIRPG_LLM_MODEL", "AIRPG_FAST_MODEL", "AIRPG_QUALITY_MODEL",
        "AIRPG_BACKGROUND_MODEL", "AIRPG_GEMINI_API_KEY", "GEMINI_API_KEY",
        "GOOGLE_API_KEY", "AIRPG_OPENAI_API_KEY", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _capture_models(monkeypatch: pytest.MonkeyPatch) -> dict[str, Model]:
    factory = import_module("ai_rpg.llm.models")
    original = factory.PydanticAILLM
    captured: dict[str, Model] = {}

    def build(models: Mapping[str, Model]) -> object:
        captured.update(models)
        return original(models)

    monkeypatch.setattr(factory, "PydanticAILLM", build)
    return captured


@pytest.mark.asyncio
async def test_fake_needs_no_clients_or_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = import_module("ai_rpg.llm.models")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("fake mode must not construct HTTP clients")

    monkeypatch.setattr(factory.httpx2, "AsyncClient", forbidden)
    monkeypatch.setattr(factory.httpx2, "Client", forbidden)
    async with factory.build_language_models(Settings(), fake=True) as llm:
        assert type(llm).__name__ == "DevelopmentFakeLLM"
        assert callable(llm.generate_narrative)
        assert callable(llm.extract_intent)
        assert callable(llm.narrate_result)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["google:gemini-3.5-flash", "gpt-5-mini"])
@pytest.mark.parametrize("key", [None, "", "   ", "test\nkey", "test\x00key"])
async def test_selected_provider_needs_valid_key_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, model: str, key: str | None,
) -> None:
    factory = import_module("ai_rpg.llm.models")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid settings must fail before creating clients")

    monkeypatch.setattr(factory.httpx2, "AsyncClient", forbidden)
    settings = Settings(llm_model=model, gemini_api_key=key, openai_api_key=key)
    with pytest.raises(ValueError, match=r"AIRPG_.*API_KEY"):
        async with factory.build_language_models(settings, fake=False):
            pytest.fail("invalid credentials accepted")


@pytest.mark.asyncio
async def test_unused_common_provider_does_not_require_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = import_module("ai_rpg.llm.models")
    captured = _capture_models(monkeypatch)
    settings = Settings(
        fast_model="gpt-5-mini", quality_model="gpt-5-mini", background_model="gpt-5-mini",
        openai_api_key="openai-test",
    )
    async with factory.build_language_models(settings, fake=False):
        assert list(captured) == ["openai-responses:gpt-5-mini"]


@pytest.mark.asyncio
async def test_all_selected_keys_validated_before_any_client_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = import_module("ai_rpg.llm.models")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("a later missing provider key must not leave partially opened clients")

    monkeypatch.setattr(factory.httpx2, "AsyncClient", forbidden)
    settings = Settings(quality_model="gpt-5.4", gemini_api_key="google-test")
    with pytest.raises(ValueError, match="AIRPG_OPENAI_API_KEY"):
        async with factory.build_language_models(settings, fake=False):
            pytest.fail("missing OpenAI credentials accepted")


@pytest.mark.asyncio
async def test_tier_mapping_has_canonical_ids_and_clients_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = import_module("ai_rpg.llm.models")
    captured = _capture_models(monkeypatch)
    settings = Settings(
        fast_model="google:gemini-3.5-flash", quality_model="openai:gpt-5.4",
        background_model="gpt-5-mini", gemini_api_key="google-test", openai_api_key="openai-test",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.invalid/")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://untrusted.invalid/")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")

    async with factory.build_language_models(settings, fake=False):
        assert set(captured) == {
            "google:gemini-3.5-flash", "openai-responses:gpt-5.4", "openai-responses:gpt-5-mini",
        }
        google = captured["google:gemini-3.5-flash"]
        openai = captured["openai-responses:gpt-5.4"]
        assert isinstance(google, GoogleModel)
        assert isinstance(openai, OpenAIResponsesModel)
        assert google.model_name == "gemini-3.5-flash"
        assert openai.model_name == "gpt-5.4"
        google_client = google.client._api_client
        openai_client = openai.client
        assert google_client.vertexai is False
        assert google_client._http_options.base_url == "https://generativelanguage.googleapis.com/"
        assert str(openai_client.base_url) == "https://api.openai.com/v1/"
        assert google_client._http_options.retry_options.attempts == 1
        assert openai_client.max_retries == 0
        assert google_client._async_httpx_client.trust_env is False
        assert google_client._httpx_client.trust_env is False
        assert openai_client._client.trust_env is False
        assert not openai_client.is_closed()

    assert google_client._async_httpx_client.is_closed
    assert google_client._httpx_client.is_closed
    assert openai_client.is_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["cancel", "body", "adapter_setup", "provider_setup", "google_setup"]
)
async def test_clients_close_on_cancellation_and_partial_setup_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    factory = import_module("ai_rpg.llm.models")
    clients: list[httpx2.AsyncClient] = []
    sync_clients: list[httpx2.Client] = []
    real_client = httpx2.AsyncClient
    real_sync_client = httpx2.Client

    class TrackedSyncClient(real_sync_client):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            sync_clients.append(self)

    class TrackedHTTPClient(real_client):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            clients.append(self)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("setup failed with google-test and openai-test credentials")

    monkeypatch.setattr(factory.httpx2, "AsyncClient", TrackedHTTPClient)
    monkeypatch.setattr(factory.httpx2, "Client", TrackedSyncClient)
    if failure == "adapter_setup":
        monkeypatch.setattr(factory, "PydanticAILLM", fail)
    if failure == "provider_setup":
        monkeypatch.setattr(factory, "OpenAIProvider", fail)
    if failure == "google_setup":
        monkeypatch.setattr(factory, "GoogleClient", fail)

    entered = asyncio.Event()

    async def run() -> None:
        settings = Settings(
            quality_model="gpt-5.4", gemini_api_key="google-test", openai_api_key="openai-test",
        )
        async with factory.build_language_models(settings, fake=False):
            entered.set()
            if failure == "cancel":
                await asyncio.Event().wait()
            raise RuntimeError("body failed")

    task = asyncio.create_task(run())
    if failure == "cancel":
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
    error_type = (
        asyncio.CancelledError if failure == "cancel"
        else ValueError if failure.endswith("setup") else RuntimeError
    )
    with pytest.raises(error_type) as error:
        await task
    assert "google-test" not in str(error.value)
    assert "openai-test" not in str(error.value)
    assert clients
    assert all(client.is_closed for client in clients)
    assert sync_clients
    assert all(client.is_closed for client in sync_clients)
