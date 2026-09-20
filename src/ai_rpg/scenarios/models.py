"""型付きScenario定義。"""

from typing import Annotated, Literal, Self, TypeAlias

from pydantic import Field, model_validator

from ai_rpg.contracts.common import Contract, PositiveInt, Ref, ShortText

SUPPORTED_SKILL_REFS = frozenset({"perception", "persuasion", "stealth"})


class ScenarioEffect(Contract):
    next_scene_ref: Ref | None = None
    ending_ref: Ref | None = None
    add_flags: list[Ref] = Field(default_factory=list)


class DirectScenarioAction(Contract):
    action_ref: Ref
    label: ShortText
    public_fact: ShortText
    kind: Literal["scenario_action"]
    success: ScenarioEffect


class SkillScenarioAction(Contract):
    action_ref: Ref
    label: ShortText
    kind: Literal["skill_check"]
    check_ref: Ref
    skill_ref: Ref
    success: ScenarioEffect
    failure: ScenarioEffect


class AttackScenarioAction(Contract):
    action_ref: Ref
    label: ShortText
    kind: Literal["attack"]
    target_ref: Ref
    defeated: ScenarioEffect


ScenarioActionDefinition: TypeAlias = Annotated[
    DirectScenarioAction | SkillScenarioAction | AttackScenarioAction,
    Field(discriminator="kind"),
]


class SceneDefinition(Contract):
    scene_ref: Ref
    sequence: PositiveInt
    title: ShortText
    description: ShortText
    actions: list[ScenarioActionDefinition]


class ScenarioFlagDefinition(Contract):
    flag_ref: Ref
    public_fact: ShortText


class EndingDefinition(Contract):
    ending_ref: Ref
    title: ShortText
    summary: ShortText


class ScenarioDefinition(Contract):
    scenario_ref: Ref
    version: PositiveInt
    title: ShortText
    objective: ShortText
    scenes: list[SceneDefinition]
    flags: list[ScenarioFlagDefinition]
    endings: list[EndingDefinition]

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
        for scene in self.scenes:
            for action in scene.actions:
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

        return self
