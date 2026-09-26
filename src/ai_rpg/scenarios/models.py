"""型付きScenario定義。"""

from typing import Annotated, Literal, Self, TypeAlias

from pydantic import Field, model_validator

from ai_rpg.contracts.common import Contract, NonNegativeInt, PositiveInt, Ref, ShortText

SUPPORTED_SKILL_REFS = frozenset({"perception", "persuasion", "stealth"})


class ScenarioEndingOverride(Contract):
    requires_flags: tuple[Ref, ...] = Field(min_length=1)
    ending_ref: Ref


class ScenarioEffect(Contract):
    next_scene_ref: Ref | None = None
    ending_ref: Ref | None = None
    add_flags: tuple[Ref, ...] = ()
    overrides: tuple[ScenarioEndingOverride, ...] = ()


class ScenarioActionConditions(Contract):
    required_flags: tuple[Ref, ...] = ()
    disabled_flags: tuple[Ref, ...] = ()


class DirectScenarioAction(ScenarioActionConditions):
    action_ref: Ref
    label: ShortText
    public_fact: ShortText
    kind: Literal["scenario_action"]
    success: ScenarioEffect


class SkillScenarioAction(ScenarioActionConditions):
    action_ref: Ref
    label: ShortText
    kind: Literal["skill_check"]
    check_ref: Ref
    skill_ref: Ref
    success: ScenarioEffect
    failure: ScenarioEffect


class AttackScenarioAction(ScenarioActionConditions):
    action_ref: Ref
    label: ShortText
    kind: Literal["attack"]
    target_ref: Ref
    defeated: ScenarioEffect


ScenarioActionDefinition: TypeAlias = Annotated[
    DirectScenarioAction | SkillScenarioAction | AttackScenarioAction,
    Field(discriminator="kind"),
]


class ScenarioCombatDefinition(Contract):
    enemy_ref: Ref
    started_flag: Ref
    defeat_ending_ref: Ref
    damage_expression: Literal["1d4", "1d6"]
    damage_bonus: NonNegativeInt = 0


class SceneDefinition(Contract):
    scene_ref: Ref
    sequence: PositiveInt
    title: ShortText
    description: ShortText
    actions: tuple[ScenarioActionDefinition, ...]
    npc_notes: tuple[ShortText, ...] = ()
    combat: ScenarioCombatDefinition | None = None
    open_flags: tuple[Ref, ...] = ()
    open_destinations: tuple[Ref, ...] = ()


class ScenarioFlagDefinition(Contract):
    flag_ref: Ref
    public_fact: ShortText


class RewardRule(Contract):
    tier: Literal["full", "reduced", "none"]
    description: ShortText


class EndingDefinition(Contract):
    ending_ref: Ref
    title: ShortText
    summary: ShortText
    required_flags: tuple[Ref, ...] = ()
    reward: RewardRule | None = None


class ProtectedFact(Contract):
    fact_ref: Ref
    kind: Literal["location", "motive", "rule"]
    statement: ShortText
    scene_ref: Ref | None = None


class BoundedWorld(Contract):
    region_name: ShortText
    boundary: ShortText
    goal_scene_ref: Ref
    goal_flag_ref: Ref
    outside_ending_ref: Ref
    protected_facts: tuple[ProtectedFact, ...] = Field(min_length=1)
    protected_terms: tuple[ShortText, ...] = ()


class ScenarioDefinition(Contract):
    scenario_ref: Ref
    version: PositiveInt
    title: ShortText
    objective: ShortText
    scenes: tuple[SceneDefinition, ...]
    flags: tuple[ScenarioFlagDefinition, ...]
    endings: tuple[EndingDefinition, ...]
    ruleset_ref: Ref = "mvp_v1"
    world: BoundedWorld | None = None

    @model_validator(mode="after")
    def valid_graph(self) -> Self:
        scene_refs = [scene.scene_ref for scene in self.scenes]
        sequences = [scene.sequence for scene in self.scenes]
        action_refs = [action.action_ref for scene in self.scenes for action in scene.actions]
        flag_refs = [flag.flag_ref for flag in self.flags]
        ending_refs = [ending.ending_ref for ending in self.endings]

        for label, refs in (
            ("Scene", scene_refs),
            ("Scene sequence", sequences),
            ("Action", action_refs),
            ("Flag", flag_refs),
            ("Ending", ending_refs),
        ):
            if len(refs) != len(set(refs)):
                raise ValueError(f"{label}参照は一意である必要があります")
        if 1 not in sequences:
            raise ValueError("初期Sceneにはsequence 1が必要です")

        known_scenes = set(scene_refs)
        known_flags = set(flag_refs)
        known_endings = set(ending_refs)
        if self.world is not None:
            world = self.world
            if world.goal_scene_ref not in known_scenes:
                raise ValueError("world goal scene references an unknown Scene")
            if world.goal_flag_ref not in known_flags:
                raise ValueError("world goal flag references an unknown Flag")
            if world.outside_ending_ref not in known_endings:
                raise ValueError("world outside ending references an unknown Ending")
            fact_refs = [fact.fact_ref for fact in world.protected_facts]
            if len(fact_refs) != len(set(fact_refs)):
                raise ValueError("protected fact references must be unique")
            if any(fact.scene_ref is not None and fact.scene_ref not in known_scenes
                   for fact in world.protected_facts):
                raise ValueError("protected fact references an unknown Scene")
            if any(ending.reward is None for ending in self.endings):
                raise ValueError("bounded scenario endings need a reward rule")
        for ending in self.endings:
            if not set(ending.required_flags) <= known_flags:
                raise ValueError("Ending references an unknown Flag")
        for scene in self.scenes:
            if not set(scene.open_flags) <= known_flags:
                raise ValueError("Scene open_flags references an unknown Flag")
            if not set(scene.open_destinations) <= known_scenes:
                raise ValueError("Scene open_destinations references an unknown Scene")
            if scene.combat is not None:
                combat = scene.combat
                if not any(
                    isinstance(action, AttackScenarioAction)
                    and action.target_ref == combat.enemy_ref
                    for action in scene.actions
                ):
                    raise ValueError("Combat enemy must match an attack target in the same scene")
                if combat.started_flag not in known_flags:
                    raise ValueError("Combat started_flag references an unknown flag")
                if combat.defeat_ending_ref not in known_endings:
                    raise ValueError("Combat defeat_ending_ref references an unknown ending")
            for action in scene.actions:
                required_flags = set(action.required_flags)
                disabled_flags = set(action.disabled_flags)
                if not (required_flags | disabled_flags) <= known_flags:
                    raise ValueError("Action条件が存在しないFlagを参照しています")
                if required_flags & disabled_flags:
                    raise ValueError("Actionの必須Flagと無効化Flagは重複できません")

                effects: tuple[ScenarioEffect, ...]
                if isinstance(action, SkillScenarioAction):
                    if action.skill_ref not in SUPPORTED_SKILL_REFS:
                        raise ValueError(f"未対応の技能参照です: {action.skill_ref}")
                    effects = (action.success, action.failure)
                elif isinstance(action, DirectScenarioAction):
                    effects = (action.success,)
                else:
                    effects = (action.defeated,)

                for effect in effects:
                    if effect.next_scene_ref is not None and effect.ending_ref is not None:
                        raise ValueError("Scene遷移とEndingは同時に指定できません")
                    if (
                        effect.next_scene_ref is not None
                        and effect.next_scene_ref not in known_scenes
                    ):
                        raise ValueError(f"存在しないScene参照です: {effect.next_scene_ref}")
                    if effect.ending_ref is not None and effect.ending_ref not in known_endings:
                        raise ValueError(f"存在しないEnding参照です: {effect.ending_ref}")
                    if not set(effect.add_flags) <= known_flags:
                        raise ValueError("存在しないFlag参照です")
                    if effect.overrides and effect.ending_ref is None:
                        raise ValueError("Ending overrideにはbase Endingが必要です")
                    for override in effect.overrides:
                        if not set(override.requires_flags) <= known_flags:
                            raise ValueError("Ending overrideが存在しないFlagを参照しています")
                        if override.ending_ref not in known_endings:
                            raise ValueError("Ending overrideが存在しないEndingを参照しています")

        return self
