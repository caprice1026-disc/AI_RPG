"""単一設定境界のUnit Test。"""

import pytest
from pydantic import ValidationError

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


def test_tiers_inherit_common_gemini_model() -> None:
    settings = Settings()

    assert settings.llm_model == "google:gemini-3.5-flash"
    assert settings.fast_model == "google:gemini-3.5-flash"
    assert settings.quality_model == "google:gemini-3.5-flash"
    assert settings.background_model == "google:gemini-3.5-flash"


def test_common_model_and_explicit_tier_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRPG_LLM_MODEL", "google:gemini-custom")
    monkeypatch.setenv("AIRPG_QUALITY_MODEL", "openai:gpt-5.4")
    settings = Settings(fast_model="gpt-5-mini")

    assert settings.fast_model == "openai-responses:gpt-5-mini"
    assert settings.quality_model == "openai-responses:gpt-5.4"
    assert settings.background_model == "google:gemini-custom"


@pytest.mark.parametrize(
    "model", ["gpt-5-mini", "openai:gpt-5-mini", "openai-responses:gpt-5-mini"]
)
def test_openai_aliases_resolve_before_workers_use_them(model: str) -> None:
    settings = Settings(llm_model=model)

    assert settings.llm_model == "openai-responses:gpt-5-mini"
    assert settings.fast_model == "openai-responses:gpt-5-mini"
    assert settings.quality_model == "openai-responses:gpt-5-mini"
    assert settings.background_model == "openai-responses:gpt-5-mini"


@pytest.mark.parametrize("model", ["anthropic:claude", "gateway:google:model", "google:", "", " "])
def test_invalid_provider_or_empty_model_fails_without_exposing_keys(model: str) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(llm_model=model, gemini_api_key="private-test-key")

    assert "private-test-key" not in str(error.value)
    assert "private-test-key" not in repr(error.value)


@pytest.mark.parametrize(
    ("field", "alias", "preferred"),
    [
        ("gemini_api_key", "GEMINI_API_KEY", "AIRPG_GEMINI_API_KEY"),
        ("openai_api_key", "OPENAI_API_KEY", "AIRPG_OPENAI_API_KEY"),
    ],
)
def test_key_aliases_remain_secrets_and_airpg_takes_precedence(
    monkeypatch: pytest.MonkeyPatch, field: str, alias: str, preferred: str,
) -> None:
    monkeypatch.setenv(alias, "alias-secret")
    settings = Settings()
    assert getattr(settings, field).get_secret_value() == "alias-secret"
    assert "alias-secret" not in repr(settings)
    monkeypatch.setenv(preferred, "preferred-secret")
    settings = Settings()
    assert getattr(settings, field).get_secret_value() == "preferred-secret"
    assert "preferred-secret" not in repr(settings)


def test_environment_overrides_runtime_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """環境変数が型検証された設定へ反映される。"""

    monkeypatch.setenv("AIRPG_MAX_ACTIONS_PER_TURN", "5")
    assert Settings().max_actions_per_turn == 5


def test_invalid_action_limit_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """許容範囲外の設定では起動用modelを生成できない。"""

    monkeypatch.setenv("AIRPG_MAX_ACTIONS_PER_TURN", "11")
    with pytest.raises(ValidationError):
        Settings()


def test_worker_retry_defaults_are_bounded() -> None:
    settings = Settings()

    assert settings.resolution_max_attempts == 3
    assert settings.narration_max_attempts == 3
    assert settings.resolution_deadline_seconds == 120
    assert settings.narration_deadline_seconds == 120


def test_provider_timeout_must_fit_inside_worker_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIRPG_WORKER_LEASE_SECONDS", "60")
    monkeypatch.setenv("AIRPG_LLM_TIMEOUT_SECONDS", "60")

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(("value", "expected"), [("0", 0), ("100", 100)])
def test_recent_message_limit_accepts_configured_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: int,
) -> None:
    monkeypatch.setenv("AIRPG_RECENT_MESSAGES_LIMIT", value)

    assert Settings().recent_messages_limit == expected


@pytest.mark.parametrize("value", ["-1", "101"])
def test_recent_message_limit_rejects_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("AIRPG_RECENT_MESSAGES_LIMIT", value)

    with pytest.raises(ValidationError):
        Settings()


def test_openai_api_key_is_read_as_a_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIRPG_OPENAI_API_KEY", "test-secret")

    settings = Settings()

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings)


def test_oidc_settings_parse_explicit_asymmetric_algorithms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIRPG_AUTH_ISSUER", "https://idp.example.com/")
    monkeypatch.setenv("AIRPG_AUTH_AUDIENCE", "ai-rpg-api")
    monkeypatch.setenv("AIRPG_AUTH_ALLOWED_ALGORITHMS", "RS256,ES256")

    settings = Settings()

    assert settings.require_oidc() == (
        "https://idp.example.com/",
        "ai-rpg-api",
        ("RS256", "ES256"),
    )


def test_oidc_is_optional_for_non_api_processes() -> None:
    settings = Settings(auth_issuer=None, auth_audience=None)

    with pytest.raises(ValueError, match="AIRPG_AUTH_ISSUER"):
        settings.require_oidc()


@pytest.mark.parametrize("algorithm", ["HS256", "none", ""])
def test_oidc_rejects_unsafe_algorithm_allowlist(algorithm: str) -> None:
    with pytest.raises(ValidationError):
        Settings(auth_allowed_algorithms=algorithm)


@pytest.mark.parametrize("values", [
    {"auth_client_id": "browser"},
    {"auth_app_origin": "https://game.example"},
    {"auth_client_id": "browser", "auth_app_origin": "https://game.example/path"},
    {"auth_issuer": "https://user:password@idp.example"},
    {"auth_allow_insecure_loopback": True, "auth_issuer": "http://idp.example"},
    {"auth_client_id": "browser", "auth_app_origin": "http://game.example",
     "auth_allow_insecure_loopback": True},
])
def test_browser_auth_rejects_partial_or_unsafe_configuration(values: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(**values)


def test_browser_auth_only_allows_explicit_loopback_http() -> None:
    settings = Settings(
        auth_issuer="http://127.0.0.1:8180/realms/airpg",
        auth_client_id="browser", auth_app_origin="http://127.0.0.1:8035/",
        auth_allow_insecure_loopback=True,
    )
    assert settings.auth_app_origin == "http://127.0.0.1:8035"
    assert settings.browser_auth_enabled


def test_browser_auth_is_opt_in() -> None:
    assert not Settings().browser_auth_enabled


@pytest.mark.parametrize(("origin", "canonical"), [
    ("HTTPS://GAME.EXAMPLE:443/", "https://game.example"),
    ("https://game.example:8443", "https://game.example:8443"),
    ("http://LOCALHOST:80", "http://localhost"),
    ("http://[::1]:8035", "http://[::1]:8035"),
    ("https://xn--fa-hia.example", "https://xn--fa-hia.example"),
])
def test_browser_origin_matches_browser_serialization(origin, canonical):
    settings = Settings(auth_client_id="web", auth_app_origin=origin,
                        auth_allow_insecure_loopback=True)
    assert settings.auth_app_origin == canonical


@pytest.mark.parametrize("origin", ["https://faß.example", "https://例え.example"])
def test_browser_origin_requires_explicit_ascii_hostname(origin):
    with pytest.raises(ValidationError, match="ASCII"):
        Settings(auth_client_id="web", auth_app_origin=origin)
