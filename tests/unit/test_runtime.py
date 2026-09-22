"""独立process向け開発runtimeの最小契約。"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from ai_rpg import cli
from ai_rpg.application import RuleBasedTurnRouter, ScenarioProgressor
from ai_rpg.config import Settings
from ai_rpg.contracts.context import ContextFragment, MechanicalInput, NarrativeInput, OutputLimits
from ai_rpg.contracts.responses import MechanicalNarrationInput
from ai_rpg.llm.fake import DevelopmentFakeLLM
from ai_rpg.runtime import (
    DEVELOPMENT_FIXTURE,
    build_language_models,
    build_narration_worker,
    build_resolution_worker,
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.mark.parametrize("command", ["resolution-worker", "narration-worker"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "setup_failure"])
def test_worker_cli_owns_models_in_one_loop_and_always_closes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    outcome: str,
) -> None:
    settings = Settings(_env_file=None, openai_api_key=None)
    events: list[tuple[str, asyncio.AbstractEventLoop]] = []
    llm = object()

    @asynccontextmanager
    async def models(settings_arg: Settings, *, fake: bool) -> AsyncIterator[object]:
        assert settings_arg is settings
        assert fake
        events.append(("open", asyncio.get_running_loop()))
        try:
            yield llm
        finally:
            events.append(("close", asyncio.get_running_loop()))

    class Worker:
        async def run_once(self, turn_id: object = None) -> bool:
            events.append(("run", asyncio.get_running_loop()))
            if outcome == "failure":
                raise RuntimeError("worker failure")
            if outcome == "cancel":
                raise asyncio.CancelledError
            return True

    def build(settings_arg: Settings, model_arg: object, **kwargs: object) -> Worker:
        assert settings_arg is settings
        assert model_arg is llm
        assert kwargs == ({"deterministic": True} if command == "resolution-worker" else {})
        events.append(("build", asyncio.get_running_loop()))
        if outcome == "setup_failure":
            raise RuntimeError("worker setup failure")
        return Worker()

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_configure_event_loop", lambda: None)
    monkeypatch.setattr(cli, "build_language_models", models, raising=False)
    monkeypatch.setattr(cli, "build_resolution_worker", build)
    monkeypatch.setattr(cli, "build_narration_worker", build)

    if outcome == "success":
        cli.main([command, "--fake", "--once"])
        assert json.loads(capsys.readouterr().out) == {"processed": True}
    else:
        error_type = asyncio.CancelledError if outcome == "cancel" else RuntimeError
        with pytest.raises(error_type):
            cli.main([command, "--fake", "--once"])

    assert [event for event, _ in events] == (
        ["open", "build", "close"] if outcome == "setup_failure"
        else ["open", "build", "run", "close"]
    )
    assert len({loop for _, loop in events}) == 1
    assert events[0][1].is_closed()


def test_development_fixture_exposes_all_scenario_scenes() -> None:
    fixture = DEVELOPMENT_FIXTURE

    assert fixture.scene_id == fixture.entrance_scene_id
    assert len(
        {
            fixture.entrance_scene_id,
            fixture.hall_scene_id,
            fixture.sanctum_scene_id,
        }
    ) == 3


@pytest.mark.asyncio
async def test_development_fake_supports_each_worker_port() -> None:
    fragment = ContextFragment(
        source="scene", trust_level="trusted", access_scope="public", content="quiet room",
    )
    narrative_input = NarrativeInput(
        player_text="look around", scene_view=fragment, pc_view=fragment,
        recent_messages=[], allowed_entity_refs=[], output_limits=OutputLimits(),
    )
    mechanical_input = MechanicalInput(
        **narrative_input.model_dump(), supported_action_types=["skill_check"],
        supported_skill_refs=["perception"],
    )
    narration_input = MechanicalNarrationInput(
        player_text="look around", committed_state_version=1, resolved_actions=[],
        public_state_after=[], allowed_entity_refs=[], output_limits=OutputLimits(),
    )
    async with build_language_models(Settings(), fake=True) as llm:
        intent = await llm.extract_intent(mechanical_input, model_id="fake")
        narrative = await llm.generate_narrative(narrative_input, model_id="fake")
        result = await llm.narrate_result(narration_input, model_id="fake")

    assert intent.model_dump() == {
        "kind": "action_plan",
        "actions": [
            {
                "kind": "skill_check",
                "skill_ref": "perception",
                "objective": "周囲の痕跡を見つける",
                "target_ref": None,
            }
        ],
    }
    assert narrative.model_dump() == {
        "kind": "narrative",
        "narration": "静かな時間が流れている。",
        "choices": [],
    }
    assert result.model_dump() == {
        "narration": "判定結果が確定した。",
        "choices": [],
    }


@pytest.mark.parametrize(
    "player_text",
    [
        "礼拝堂に入る",
        "広間を調べる",
        "守衛と交渉する",
        "隠密する",
        "撤退する",
    ],
)
def test_scenario_state_changing_verbs_route_to_mechanical(player_text: str) -> None:
    assert RuleBasedTurnRouter().decide(player_text).route == "mechanical"


def test_default_resolution_worker_receives_scenario_progressor() -> None:
    llm = DevelopmentFakeLLM()
    settings = Settings(fast_model="gpt-5-mini", quality_model="google:gemini-3.5-flash")
    worker = build_resolution_worker(
        settings, llm, deterministic=True
    )

    assert isinstance(worker._scenario_progressor, ScenarioProgressor)
    assert worker._llm is llm
    assert worker._policy.model_id == "openai-responses:gpt-5-mini"
    narrator = build_narration_worker(settings, llm)
    assert narrator._narrator is llm
    assert narrator._policy.model_id == "google:gemini-3.5-flash"
