"""A living scene enemy reacts once after an applied player turn."""

from dataclasses import replace
from uuid import uuid4

from ai_rpg.application.combat import resolve_enemy_reaction
from ai_rpg.application.ports.repositories import (
    ActionRecord,
    CanonicalSnapshot,
    ResolutionWorkItem,
    ScenarioProgressUpdate,
    ScenarioRunSnapshot,
    ScenarioSceneSnapshot,
)
from ai_rpg.application.scenarios import ScenarioProgressor
from ai_rpg.domain.commands import AttackCommand, UseItemCommand
from ai_rpg.domain.events import RNGMetadata
from ai_rpg.domain.models import CharacterState
from ai_rpg.domain.results import NotApplicableResult
from ai_rpg.engine import DiceEngine, MvpV1Ruleset
from ai_rpg.scenarios import BUILTIN_SCENARIOS


class Rolls:
    def __init__(self, values):
        self.values = iter(values)

    def randint(self, minimum, maximum):
        value = next(self.values)
        assert minimum <= value <= maximum
        return value


def combat_case(*, active=False, hp=10, enemy_hp=10):
    definition = BUILTIN_SCENARIOS.get("ruined_chapel", 2)
    scenes = tuple(
        ScenarioSceneSnapshot(
            uuid4(), s.sequence, "active" if s.scene_ref == "sanctum" else "closed"
        )
        for s in definition.scenes
    )
    scene = next(s for s in scenes if s.status == "active")
    campaign, turn, actor, enemy = (uuid4() for _ in range(4))
    work = ResolutionWorkItem(
        turn, campaign, scene.id, uuid4(), actor, True, 1, 3, 0, "攻撃する", (), "mechanical"
    )
    snapshot = CanonicalSnapshot(
        campaign,
        0,
        (
            {"entity_id": actor, "current_hp": hp, "max_hp": 10, "defense": 12, "attack_bonus": 2},
            {
                "entity_id": enemy,
                "current_hp": enemy_hp,
                "max_hp": 10,
                "defense": 11,
                "attack_bonus": 1,
            },
        ),
        (),
        (),
        (),
        (),
        (
            {"id": actor, "ref": "hero", "kind": "pc", "archived_at": None},
            {"id": enemy, "ref": "goblin", "kind": "npc", "archived_at": None},
        ),
        ({"entity_id": enemy, "is_public": True, "is_attack_reachable": True},),
        ScenarioRunSnapshot(
            campaign,
            "ruined_chapel",
            2,
            "active",
            None,
            scenes,
            frozenset({"combat_started"}) if active else frozenset(),
        ),
    )
    return work, snapshot


def player_attack(work, snapshot, rolls):
    actor, enemy = [
        CharacterState(id=r["entity_id"], **{k: v for k, v in r.items() if k != "entity_id"})
        for r in snapshot.characters
    ]
    command = AttackCommand(
        kind="attack",
        action_id=uuid4(),
        campaign_id=work.campaign_id,
        turn_id=work.turn_id,
        actor_id=work.actor_id,
        ordinal=1,
        target_id=enemy.id,
        weapon_id=None,
        attack_bonus=2,
        damage_expression="1d6",
        damage_bonus=0,
    )
    result = MvpV1Ruleset(DiceEngine(Rolls(rolls))).resolve_attack(command, actor, enemy)
    rng = tuple(
        RNGMetadata(source="seeded_test", implementation_version="mvp_v1", draw_index=i)
        for i in range(len(result.dice))
    )
    return ActionRecord(command, result, rng)


def react(work, snapshot, records, rolls=(), update=None):
    return resolve_enemy_reaction(
        work,
        snapshot,
        records,
        update,
        ruleset=MvpV1Ruleset(DiceEngine(Rolls(rolls))),
        progressor=ScenarioProgressor(BUILTIN_SCENARIOS),
        reaction_id_factory=uuid4,
        rng_source="seeded_test",
        rng_implementation_version="mvp_v1",
    )


def test_attack_miss_starts_combat_and_enemy_can_hit():
    work, snapshot = combat_case()
    record = player_attack(work, snapshot, [1])
    result = react(work, snapshot, (record,), [20, 4])
    assert len(result.reactions) == 1
    reaction = result.reactions[0]
    assert reaction.command.actor_id == snapshot.characters[1]["entity_id"]
    assert reaction.command.target_id == work.actor_id
    assert reaction.result.state_changes[0].hp_after == 6
    assert [r.draw_index for r in reaction.rng] == [1, 2]
    assert result.scenario_update.add_flags == ("combat_started",)


def test_killing_enemy_or_retreat_suppresses_counterattack():
    work, snapshot = combat_case(active=True, enemy_hp=1)
    killed = player_attack(work, snapshot, [20, 1])
    assert not react(work, snapshot, (killed,)).reactions
    missed = player_attack(work, snapshot, [1])
    update = ScenarioProgressUpdate(work.scene_id, None, (), "retreated")
    assert not react(work, snapshot, (missed,), update=update).reactions
    update = replace(update, to_scene_id=uuid4(), ending_ref=None)
    assert not react(work, snapshot, (missed,), update=update).reactions


def test_counterattack_at_zero_hp_completes_defeat():
    work, snapshot = combat_case(active=True, hp=2)
    record = player_attack(work, snapshot, [1])
    result = react(work, snapshot, (record,), [20, 4])
    assert result.reactions[0].result.state_changes[0].hp_after == 0
    assert result.scenario_update.ending_ref == "defeated"
    assert result.scenario_update.add_flags == ()


def test_heal_then_counterattack_uses_healed_hp():
    work, snapshot = combat_case(active=True, hp=2)
    actor = CharacterState(id=work.actor_id, current_hp=2, max_hp=10, defense=12, attack_bonus=2)
    command = UseItemCommand(
        kind="use_item",
        action_id=uuid4(),
        campaign_id=work.campaign_id,
        turn_id=work.turn_id,
        actor_id=work.actor_id,
        ordinal=1,
        item_id=uuid4(),
        target_id=work.actor_id,
        effect_ref="healing_potion",
    )
    healed = MvpV1Ruleset(DiceEngine(Rolls([4]))).resolve_use_item(
        command, actor, actor, quantity=1
    )
    record = ActionRecord(
        command,
        healed,
        (RNGMetadata(source="seeded_test", implementation_version="mvp_v1", draw_index=0),),
    )
    result = react(work, snapshot, (record,), [20, 3])
    damage = result.reactions[0].result.state_changes[0]
    assert (damage.hp_before, damage.hp_after) == (8, 5)


def test_not_applied_or_noncombat_scene_never_rolls_enemy_dice():
    work, snapshot = combat_case(active=True)
    record = player_attack(work, snapshot, [1])
    invalid = replace(
        record,
        result=NotApplicableResult(kind="not_applicable", reason="rule_precondition"),
        rng=(),
    )
    assert not react(work, snapshot, (invalid,)).reactions
    assert not react(work, replace(snapshot, scenario_run=None), (record,)).reactions


def test_enemy_miss_persists_reaction_without_hp_change():
    work, snapshot = combat_case(active=True)
    record = player_attack(work, snapshot, [1])
    result = react(work, snapshot, (record,), [1])
    assert result.reactions[0].result.outcome == "failure"
    assert result.reactions[0].result.state_changes == []
    assert result.scenario_update is None
