"""Scenario narration keeps newly acquired public facts when the scene changes."""

from unittest.mock import Mock
from uuid import UUID

import pytest

from ai_rpg.application.ports import ScenarioRunSnapshot, ScenarioSceneSnapshot
from ai_rpg.application.ports.llm import ResolutionLLM
from ai_rpg.application.ports.repositories import ScenarioProgressUpdate
from ai_rpg.application.scenarios import ScenarioOutcome, ScenarioProgressor
from ai_rpg.application.workers import SkillCheckResolutionWorker, WorkerPhasePolicy
from ai_rpg.engine import DiceEngine, MvpV1Ruleset, SeededRandomSource
from ai_rpg.scenarios import BUILTIN_SCENARIOS, ScenarioCatalog, ScenarioDefinition


def scenario_run(scene_ref: str, flags: tuple[str, ...] = ()) -> ScenarioRunSnapshot:
    return ScenarioRunSnapshot(
        campaign_id=UUID(int=100),
        scenario_ref="ruined_chapel",
        scenario_version=2,
        status="active",
        ending_ref=None,
        scenes=tuple(
            ScenarioSceneSnapshot(
                id=UUID(int=scene.sequence),
                sequence=scene.sequence,
                status="active" if scene.scene_ref == scene_ref else "planned",
            )
            for scene in BUILTIN_SCENARIOS.get("ruined_chapel", 2).scenes
        ),
        flags=frozenset(flags),
    )


def worker_for(progressor: ScenarioProgressor) -> SkillCheckResolutionWorker:
    return SkillCheckResolutionWorker(
        unit_of_work_factory=Mock(),
        llm=Mock(spec=ResolutionLLM),
        ruleset=MvpV1Ruleset(DiceEngine(SeededRandomSource(0))),
        policy=WorkerPhasePolicy(60, 3, 120, "unused"),
        scenario_progressor=progressor,
    )


@pytest.mark.parametrize(
    ("scene_ref", "action_ref", "outcome", "flags", "source", "description", "new_flags"),
    [
        (
            "archive", "read_archive", "failure", (), "scenario_scene",
            "祭壇前の通路: 狭い通路の先に半開きの扉がある。",
            ("archive_fragment_read", "costly"),
        ),
        (
            "archive", "read_archive", "success", (), "scenario_scene",
            "祭壇前の通路: 狭い通路の先に半開きの扉がある。",
            ("relic_history_read",),
        ),
        (
            "archive", "read_archive", "failure", ("costly",), "scenario_scene",
            "祭壇前の通路: 狭い通路の先に半開きの扉がある。",
            ("archive_fragment_read",),
        ),
        (
            "passage", "listen_at_door", "success", (), "scenario_facts", "",
            ("listened_at_door", "guard_words_heard"),
        ),
        (
            "passage", "listen_at_door", "failure", (), "scenario_facts", "",
            ("listened_at_door",),
        ),
        (
            "entrance", "enter_chapel", "neutral", ("quest_accepted",), "scenario_scene",
            "長椅子の広間: 傾いた長椅子が行く手を塞いでいる。", (),
        ),
        (
            "entrance", "leave_entrance", "neutral", (), "scenario_ending",
            "撤退: 銀の聖印の回収を断念し、礼拝堂から引き返した。", (),
        ),
    ],
    ids=[
        "failed-archive-advances-with-clue-and-cost",
        "successful-archive-advances-with-history",
        "already-known-cost-is-not-repeated",
        "facts-only-success",
        "facts-only-failure",
        "scene-without-new-facts",
        "ending-without-new-facts",
    ],
)
def test_public_state_keeps_description_and_only_newly_acquired_facts(
    scene_ref: str,
    action_ref: str,
    outcome: ScenarioOutcome,
    flags: tuple[str, ...],
    source: str,
    description: str,
    new_flags: tuple[str, ...],
) -> None:
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    run = scenario_run(scene_ref, flags)
    binding = progressor.bind_registered_action(run, action_ref)
    update = progressor.progress_for(run, binding, outcome, None)
    assert update is not None
    assert update.add_flags == new_flags

    fragment = worker_for(progressor)._scenario_public_state_after(run, update)

    assert fragment.source == source
    assert fragment.trust_level == "trusted"
    assert fragment.access_scope == "public"
    assert fragment.content.startswith(description)
    definition = BUILTIN_SCENARIOS.get("ruined_chapel", 2)
    for flag in definition.flags:
        assert fragment.content.count(flag.public_fact) == int(flag.flag_ref in new_flags)
        assert flag.flag_ref not in fragment.content
    for scene in definition.scenes:
        for note in scene.npc_notes:
            assert note not in fragment.content
    if source == "scenario_facts":
        assert fragment.content == " / ".join(
            flag.public_fact for flag in definition.flags if flag.flag_ref in new_flags
        )
    assert "現在の場面で行動を終えた。" not in fragment.content


def test_ending_with_new_facts_keeps_ending_and_public_fact() -> None:
    # Built-in endings currently add no flags; exercise a validated v2 effect
    # that does, through the real progressor rather than fabricating its update.
    payload = BUILTIN_SCENARIOS.get("ruined_chapel", 2).model_dump(mode="json")
    payload["scenes"][-1]["actions"][-1]["success"]["add_flags"] = ["costly"]
    definition = ScenarioDefinition.model_validate(payload)
    progressor = ScenarioProgressor(ScenarioCatalog([definition]))
    run = scenario_run("return", ("relic_recovered",))
    binding = progressor.bind_registered_action(run, "return_relic")
    update = progressor.progress_for(run, binding, "neutral", None)
    assert update is not None
    assert update.ending_ref == "recovered"
    assert update.add_flags == ("costly",)

    fragment = worker_for(progressor)._scenario_public_state_after(run, update)

    assert fragment.source == "scenario_ending"
    assert fragment.content.startswith("聖印の帰還: 銀の聖印を回収し、村の共同庫へ無事に届けた。")
    assert "礼拝堂で足止めされ、予定より時間を費やした。" in fragment.content
    assert "costly" not in fragment.content
    assert "relic_recovered" not in fragment.content


def test_no_transition_or_public_facts_keeps_fallback() -> None:
    run = scenario_run("passage")
    update = ScenarioProgressUpdate(
        from_scene_id=UUID(int=4), to_scene_id=None, add_flags=(), ending_ref=None,
    )

    fragment = worker_for(ScenarioProgressor(BUILTIN_SCENARIOS))._scenario_public_state_after(
        run, update,
    )

    assert fragment.source == "scenario_facts"
    assert fragment.content == "現在の場面で行動を終えた。"
