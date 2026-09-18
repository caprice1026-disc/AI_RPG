"""単一設定境界のUnit Test。"""

import pytest
from pydantic import ValidationError

from ai_rpg.config import Settings


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
