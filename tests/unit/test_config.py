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
