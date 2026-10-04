"""型付きScenario定義。"""

from typing import Annotated, Literal, Self, TypeAlias

from pydantic import Field, model_validator

from ai_rpg.contracts.common import Contract, PositiveInt, Ref, ShortText

SUPPORTED_SKILL_REFS = frozenset({"perception", "persuasion", "stealth"})
Visibility: TypeAlias = Literal["public", "secret", "author_only"]
Capability: TypeAlias = Literal[
    "skill_checks", "combat", "item_use", "open_actions", "protected_facts"
]


class InitialCharacter(Contract):
    ref: Ref
    label: ShortText
    kind: Literal["npc", "monster"] = "npc"
    max_hp: int = Field(default=1, strict=True, ge=1, le=1000)
    defense: int = Field(default=0, strict=True, ge=0, le=30)
    attack_bonus: int = Field(default=0, strict=True, ge=-20, le=20)


class InitialWeapon(Contract):
    damage_expression: Literal["1d4", "1d6"]
    damage_bonus: int = Field(default=0, strict=True, ge=0, le=20)


class InitialItem(Contract):
    ref: Ref
    label: ShortText
    effect_ref: Literal["healing_potion"] | None = None
    weapon: InitialWeapon | None = None
    owner_ref: Ref | None = None
    quantity: int = Field(default=1, strict=True, ge=1, le=99)
    equipped: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def valid_equipment(self) -> Self:
        if self.weapon is not None and self.effect_ref is not None:
            raise ValueError("An item cannot be both a weapon and a consumable")
        if self.equipped and (self.weapon is None or self.owner_ref is None):
            raise ValueError("Equipped items need a weapon and an owner")
        if self.owner_ref is None and self.quantity != 1:
            raise ValueError("Unowned items represent one entity, not an inventory stack")
        return self


class InitialPlacement(Contract):
    entity_ref: Ref
    scene_ref: Ref
    visibility: Visibility = "public"
    attackable: bool = Field(default=False, strict=True)


class ScenarioInitialization(Contract):
    characters: tuple[InitialCharacter, ...] = Field(default=(), max_length=100)
    items: tuple[InitialItem, ...] = Field(default=(), max_length=100)
    placements: tuple[InitialPlacement, ...] = Field(default=(), max_length=500)


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
    check_ref: Literal["easy", "normal", "hard"]
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
    damage_bonus: int = Field(default=0, strict=True, ge=0, le=20)


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
    kind: Literal["location", "motive", "rule", "existence"]
    statement: ShortText
    scene_ref: Ref | None = None
    visibility: Visibility = "secret"
    reveal_flag_ref: Ref | None = None
    entity_ref: Ref | None = None
    acquired_flag_ref: Ref | None = None


class BoundedWorld(Contract):
    region_name: ShortText
    boundary: ShortText
    goal_scene_ref: Ref
    goal_flag_ref: Ref
    outside_ending_ref: Ref
    protected_facts: tuple[ProtectedFact, ...] = Field(min_length=1)
    protected_terms: tuple[ShortText, ...] = ()
    impossible_destinations: tuple[ShortText, ...] = ()


class ScenarioDefinition(Contract):
    schema_version: Literal[1, 2] = 1
    scenario_ref: Ref
    version: PositiveInt
    title: ShortText
    objective: ShortText
    scenes: tuple[SceneDefinition, ...]
    flags: tuple[ScenarioFlagDefinition, ...]
    endings: tuple[EndingDefinition, ...]
    ruleset_ref: Literal["mvp_v1", "mvp_v2"] = "mvp_v1"
    world: BoundedWorld | None = None
    required_capabilities: tuple[Capability, ...] = ()
    initialization: ScenarioInitialization | None = None

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

        self._validate_initialization(known_scenes, known_flags)
        return self

    def _validate_initialization(self, scenes: set[str], flags: set[str]) -> None:
        initial = self.initialization
        if initial is None:
            if self.schema_version == 2:
                raise ValueError("Schema 2 requires explicit initialization (empty is allowed)")
            return  # Historical definitions remain readable; not executable without initialization.
        characters = {value.ref: value for value in initial.characters}
        items = {value.ref: value for value in initial.items}
        refs = [value.ref for value in initial.characters] + [value.ref for value in initial.items]
        if len(refs) != len(set(refs)) or "hero" in refs:
            raise ValueError("Initialization entity refs must be unique; hero is reserved")
        equipped_owners = [value.owner_ref for value in initial.items if value.equipped]
        if len(equipped_owners) != len(set(equipped_owners)):
            raise ValueError("Only one weapon may be equipped per owner")
        for item in initial.items:
            if item.owner_ref is not None and item.owner_ref not in {"hero", *characters}:
                raise ValueError("Item owner references an unknown character")
        placements = {(value.entity_ref, value.scene_ref): value for value in initial.placements}
        if len(placements) != len(initial.placements):
            raise ValueError("Duplicate entity placement")
        for placement in initial.placements:
            if placement.entity_ref not in refs or placement.scene_ref not in scenes:
                raise ValueError("Placement references an unknown entity or Scene")
            if placement.attackable and (
                placement.visibility != "public" or placement.entity_ref not in characters
            ):
                raise ValueError("Attackable placements must be public characters")
            if placement.entity_ref in items and items[placement.entity_ref].owner_ref is not None:
                raise ValueError("An owned item cannot also be placed in a Scene")
        needed: set[str] = set()
        if any(item.effect_ref is not None for item in initial.items):
            needed.add("item_use")
        if self.world is not None and self.ruleset_ref == "mvp_v2":
            needed.add("open_actions")
        for scene in self.scenes:
            skills = [action.skill_ref for action in scene.actions
                      if isinstance(action, SkillScenarioAction)]
            targets = [action.target_ref for action in scene.actions
                       if isinstance(action, AttackScenarioAction)]
            if len(skills) != len(set(skills)) or len(targets) != len(set(targets)):
                raise ValueError("Scene skill and attack bindings must be unambiguous")
            if skills:
                needed.add("skill_checks")
            for target in targets:
                needed.add("combat")
                target_placement = placements.get((target, scene.scene_ref))
                if target_placement is None or not target_placement.attackable:
                    raise ValueError("Attack target needs an attackable placement in its Scene")
        if self.world is not None:
            for fact in self.world.protected_facts:
                if (self.schema_version == 2 and fact.kind in {"location", "existence"}
                        and fact.entity_ref is None):
                    raise ValueError("Structural protected facts require an entity reference")
                if fact.reveal_flag_ref is not None and fact.reveal_flag_ref not in flags:
                    raise ValueError("Protected fact reveal references an unknown Flag")
                if fact.acquired_flag_ref is not None and fact.acquired_flag_ref not in flags:
                    raise ValueError("Protected fact acquisition references an unknown Flag")
                if fact.visibility == "author_only" and fact.reveal_flag_ref is not None:
                    raise ValueError("Author-only facts cannot be revealed")
                if fact.entity_ref is not None:
                    needed.add("protected_facts")
                    if fact.entity_ref not in refs:
                        raise ValueError("Protected fact references an unknown entity")
                    if fact.kind in {"location", "existence"} and (
                        fact.scene_ref is None
                        or (fact.entity_ref, fact.scene_ref) not in placements
                    ):
                        raise ValueError("Protected entity must be placed in its declared Scene")
                    if fact.kind == "location" and any(
                        placement.entity_ref == fact.entity_ref
                        and placement.scene_ref != fact.scene_ref
                        for placement in initial.placements
                    ):
                        raise ValueError("Protected item location contradicts another placement")
                if fact.acquired_flag_ref is not None and (
                    fact.kind != "location" or fact.entity_ref not in items
                ):
                    raise ValueError("Acquisition facts must describe a placed item location")
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("Duplicate capability requirement")
        if "open_actions" in self.required_capabilities and (
            self.ruleset_ref != "mvp_v2" or self.world is None
        ):
            raise ValueError("Open actions require mvp_v2 and a bounded world")
        if self.schema_version == 2 and not needed <= set(self.required_capabilities):
            missing = sorted(needed - set(self.required_capabilities))
            raise ValueError(f"Missing capability requirements: {missing}")
