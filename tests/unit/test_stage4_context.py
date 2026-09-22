"""Public model context and repeatable fail-forward flags."""

import json
from dataclasses import replace
from uuid import uuid4

from ai_rpg.application.scenarios import ScenarioProgressor
from ai_rpg.application.workers import SkillCheckResolutionWorker, WorkerPhasePolicy
from ai_rpg.engine import DiceEngine, MvpV1Ruleset
from ai_rpg.llm import DevelopmentFakeLLM
from ai_rpg.scenarios import BUILTIN_SCENARIOS

from .test_combat import Rolls, combat_case


def test_context_contains_public_actions_npc_notes_and_own_hp_inventory_only():
    work, snapshot = combat_case(hp=6)
    potion, other_item, other_owner = uuid4(), uuid4(), uuid4()
    snapshot = replace(
        snapshot,
        entities=(
            *({**row, "label": row["ref"]} for row in snapshot.entities),
            {
                "id": potion,
                "ref": "healing_potion",
                "label": "回復ポーション",
                "kind": "item",
                "archived_at": None,
            },
            {
                "id": other_item,
                "ref": "secret_item",
                "label": "秘密",
                "kind": "item",
                "archived_at": None,
            },
        ),
        inventory=(
            {"owner_id": work.actor_id, "item_id": potion, "quantity": 2, "equipped": False},
            {"owner_id": other_owner, "item_id": other_item, "quantity": 1, "equipped": False},
        ),
    )
    worker = SkillCheckResolutionWorker(
        lambda: None,
        DevelopmentFakeLLM(),
        MvpV1Ruleset(DiceEngine(Rolls([]))),
        WorkerPhasePolicy(60, 3, 120, "fake"),
        scenario_progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
    )
    context = worker._mechanical_input(work, snapshot)
    pc = json.loads(context.pc_view.content)
    assert (pc["current_hp"], pc["max_hp"]) == (6, 10)
    assert pc["inventory"] == [
        {"item_ref": "healing_potion", "name": "回復ポーション", "quantity": 2, "equipped": False}
    ]
    scene = json.loads(context.scene_view.content)
    assert scene["npc_notes"]
    action = next(a for a in scene["available_actions"] if a["action_ref"] == "negotiate_guard")
    assert action["kind"] == "skill_check" and action["skill_ref"] == "persuasion"
    serialized = context.model_dump_json()
    for hidden in (
        "secret_item",
        "combat_started",
        "defeat_ending_ref",
        "required_flags",
        str(work.actor_id),
        str(other_owner),
    ):
        assert hidden not in serialized


def test_repeated_cost_flag_is_not_inserted_twice():
    _work, snapshot = combat_case()
    run = snapshot.scenario_run
    run = replace(
        run,
        flags=frozenset({"costly"}),
        scenes=tuple(
            replace(s, status="active" if s.sequence == 3 else "closed") for s in run.scenes
        ),
    )
    progressor = ScenarioProgressor(BUILTIN_SCENARIOS)
    binding = progressor.bind_registered_action(run, "read_archive")
    update = progressor.progress_for(run, binding, "failure", None)
    assert update.add_flags == ("archive_fragment_read",)
