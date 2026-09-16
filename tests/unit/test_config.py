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
