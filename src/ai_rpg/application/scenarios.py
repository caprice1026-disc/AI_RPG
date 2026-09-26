"""Scenario定義と保存済みrunから進行を評価する純粋Application component。"""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from ai_rpg.application.ports import ScenarioRunSnapshot, ScenarioSceneSnapshot
from ai_rpg.application.ports.repositories import ScenarioFact, ScenarioProgressUpdate
from ai_rpg.contracts.llm_decisions import OpenActionIntent
from ai_rpg.scenarios import (
    AttackScenarioAction,
    DirectScenarioAction,
    ScenarioCatalog,
    ScenarioDefinition,
    ScenarioEffect,
    SceneDefinition,
    SkillScenarioAction,
)


class ScenarioStateError(ValueError):
    """保存済みScenario状態が登録済み定義と一致しない。"""


class ScenarioActionUnavailableError(ValueError):
    """登録済みScenario行動のflag条件を満たしていない。"""


@dataclass(frozen=True, slots=True)
class ScenarioPublicAction:
    action_ref: str
    label: str
    kind: str
    skill_ref: str | None = None
    target_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioPublicFact:
    fact_ref: str
    kind: str
    public_text: str
    scene_ref: str


@dataclass(frozen=True, slots=True)
class ScenarioPublicContext:
    scene_title: str
    scene_description: str
    objective: str
    discovered_facts: tuple[str, ...]
    available_actions: tuple[tuple[str, str], ...]
    action_details: tuple[ScenarioPublicAction, ...] = ()
    npc_notes: tuple[str, ...] = ()
    generated_facts: tuple[ScenarioPublicFact, ...] = ()


ScenarioActionBinding: TypeAlias = DirectScenarioAction | SkillScenarioAction | AttackScenarioAction
ScenarioOutcome: TypeAlias = Literal["success", "failure", "neutral"]


def _action_is_available(action: ScenarioActionBinding, flags: frozenset[str]) -> bool:
    return set(action.required_flags) <= flags and flags.isdisjoint(action.disabled_flags)


class ScenarioProgressor:
    def __init__(self, catalog: ScenarioCatalog) -> None:
        self._catalog = catalog

    def definition_for(self, snapshot: ScenarioRunSnapshot) -> ScenarioDefinition:
        try:
            return self._catalog.get(snapshot.scenario_ref, snapshot.scenario_version)
        except KeyError as error:
            raise ScenarioStateError("Scenario定義と保存versionが一致しません") from error

    def bind_registered_action(
        self, snapshot: ScenarioRunSnapshot, action_ref: str
    ) -> ScenarioActionBinding:
        _definition, scene, _active = self._current_scene(snapshot)
        for action in scene.actions:
            if action.action_ref == action_ref and _action_is_available(action, snapshot.flags):
                return action
        raise ScenarioActionUnavailableError("現在実行できる登録済み行動ではありません")

    def scene_for(self, snapshot: ScenarioRunSnapshot) -> SceneDefinition:
        return self._current_scene(snapshot)[1]

    def public_context_for(self, snapshot: ScenarioRunSnapshot) -> ScenarioPublicContext:
        definition, scene, _active = self._current_scene(snapshot)
        scene_ref_by_id = {
            runtime.id: defined.scene_ref
            for runtime in snapshot.scenes
            for defined in definition.scenes
            if runtime.sequence == defined.sequence
        }
        return ScenarioPublicContext(
            scene_title=scene.title,
            scene_description=scene.description,
            objective=definition.objective,
            discovered_facts=(
                tuple(flag.public_fact for flag in definition.flags
                      if flag.flag_ref in snapshot.flags)
                + tuple(fact.public_text for fact in snapshot.facts)
            ),
            available_actions=tuple(
                (action.action_ref, action.label)
                for action in scene.actions
                if _action_is_available(action, snapshot.flags)
            ),
            action_details=tuple(
                ScenarioPublicAction(
                    action.action_ref,
                    action.label,
                    action.kind,
                    action.skill_ref if isinstance(action, SkillScenarioAction) else None,
                    action.target_ref if isinstance(action, AttackScenarioAction) else None,
                )
                for action in scene.actions
                if _action_is_available(action, snapshot.flags)
            ),
            npc_notes=scene.npc_notes,
            generated_facts=tuple(
                ScenarioPublicFact(
                    fact.fact_ref, fact.kind, fact.public_text,
                    scene_ref_by_id[fact.scene_id],
                )
                for fact in snapshot.facts
            ),
        )

    def bind_scenario_action(
        self, snapshot: ScenarioRunSnapshot, action_ref: str
    ) -> DirectScenarioAction | None:
        _definition, scene, _active = self._current_scene(snapshot)
        matches = tuple(
            action
            for action in scene.actions
            if isinstance(action, DirectScenarioAction) and action.action_ref == action_ref
        )
        if len(matches) != 1:
            return None
        if not _action_is_available(matches[0], snapshot.flags):
            raise ScenarioActionUnavailableError("Scenario行動のflag条件を満たしていません")
        return matches[0]

    def bind_skill_check(
        self, snapshot: ScenarioRunSnapshot, skill_ref: str
    ) -> SkillScenarioAction | None:
        _definition, scene, _active = self._current_scene(snapshot)
        matches = tuple(
            action
            for action in scene.actions
            if isinstance(action, SkillScenarioAction) and action.skill_ref == skill_ref
        )
        if len(matches) != 1:
            return None
        if not _action_is_available(matches[0], snapshot.flags):
            raise ScenarioActionUnavailableError("Scenario行動のflag条件を満たしていません")
        return matches[0]

    def bind_attack(
        self, snapshot: ScenarioRunSnapshot, target_ref: str
    ) -> AttackScenarioAction | None:
        _definition, scene, _active = self._current_scene(snapshot)
        matches = tuple(
            action
            for action in scene.actions
            if isinstance(action, AttackScenarioAction) and action.target_ref == target_ref
        )
        if len(matches) != 1:
            return None
        if not _action_is_available(matches[0], snapshot.flags):
            raise ScenarioActionUnavailableError("Scenario行動のflag条件を満たしていません")
        return matches[0]

    def progress_for(
        self,
        snapshot: ScenarioRunSnapshot,
        binding: ScenarioActionBinding,
        outcome: ScenarioOutcome,
        target_hp_after: int | None,
    ) -> ScenarioProgressUpdate | None:
        definition, scene, active = self._current_scene(snapshot)
        if not any(action is binding for action in scene.actions):
            raise ScenarioStateError("Action bindingが現在Sceneと一致しません")

        effect: ScenarioEffect
        if isinstance(binding, DirectScenarioAction):
            if outcome == "failure":
                return None
            effect = binding.success
        elif isinstance(binding, SkillScenarioAction):
            if outcome == "neutral":
                return None
            effect = binding.success if outcome == "success" else binding.failure
        else:
            if target_hp_after != 0:
                return None
            effect = binding.defeated

        ending_ref = effect.ending_ref
        for override in effect.overrides:
            if set(override.requires_flags) <= snapshot.flags:
                ending_ref = override.ending_ref
                break

        to_scene_id = None
        if effect.next_scene_ref is not None:
            target = next(
                candidate
                for candidate in definition.scenes
                if candidate.scene_ref == effect.next_scene_ref
            )
            to_scene_id = next(
                runtime.id for runtime in snapshot.scenes if runtime.sequence == target.sequence
            )

        return ScenarioProgressUpdate(
            from_scene_id=active.id,
            to_scene_id=to_scene_id,
            add_flags=tuple(flag for flag in effect.add_flags if flag not in snapshot.flags),
            ending_ref=ending_ref,
            elapsed_actions=int(definition.ruleset_ref == "mvp_v2"),
        )

    def progress_open(
        self, snapshot: ScenarioRunSnapshot, intent: OpenActionIntent,
        outcome: Literal["success", "failure"],
    ) -> ScenarioProgressUpdate:
        definition, scene, active = self._current_scene(snapshot)
        if definition.world is None or definition.ruleset_ref != "mvp_v2":
            raise ScenarioActionUnavailableError("This scenario does not accept open actions")
        if intent.target_fact_ref is not None:
            target_fact = next(
                (fact for fact in snapshot.facts if fact.fact_ref == intent.target_fact_ref), None
            )
            if target_fact is None or (
                target_fact.kind in {"place", "person"} and target_fact.scene_id != active.id
            ):
                raise ScenarioActionUnavailableError("Target fact is not available here")
        effect = intent.success if outcome == "success" else intent.failure
        if effect is None:
            raise ScenarioActionUnavailableError("The proposed action has no failure effect")
        flags = set(effect.add_flags)
        if not flags <= set(scene.open_flags):
            raise ScenarioActionUnavailableError("Proposed flags are not allowed here")
        if (
            definition.world.goal_flag_ref in flags
            and scene.scene_ref != definition.world.goal_scene_ref
        ):
            raise ScenarioActionUnavailableError("The goal object is not at this location")
        resulting_flags = snapshot.flags | flags
        if effect.ending_ref is not None:
            if effect.ending_ref in {
                candidate.combat.defeat_ending_ref
                for candidate in definition.scenes if candidate.combat is not None
            }:
                raise ScenarioActionUnavailableError("Defeat is resolved by the combat rules")
            ending = next(
                (value for value in definition.endings if value.ending_ref == effect.ending_ref),
                None,
            )
            if ending is None or not set(ending.required_flags) <= resulting_flags:
                raise ScenarioActionUnavailableError("Ending conditions are not met")
        if not 0 <= snapshot.alert_level + effect.alert_delta <= 5:
            raise ScenarioActionUnavailableError("Alert would exceed its bounds")
        to_scene_id = None
        if effect.next_scene_ref is not None:
            target = next(
                (value for value in definition.scenes if value.scene_ref == effect.next_scene_ref),
                None,
            )
            if target is None:
                raise ScenarioActionUnavailableError("Destination is outside the scenario")
            if target.scene_ref not in scene.open_destinations:
                raise ScenarioActionUnavailableError("Destination is not reachable from here")
            to_scene_id = next(value.id for value in snapshot.scenes
                               if value.sequence == target.sequence)
            if to_scene_id == active.id:
                to_scene_id = None
        # A generated fact is a local observation, never a replacement for an authored core fact.
        proposed_refs = [fact.fact_ref for fact in effect.facts]
        if len(proposed_refs) != len(set(proposed_refs)):
            raise ScenarioActionUnavailableError("Duplicate generated fact references")
        for fact in effect.facts:
            if fact.fact_ref in {item.fact_ref for item in definition.world.protected_facts}:
                raise ScenarioActionUnavailableError("Generated fact reference is protected")
            if any(term.casefold() in fact.public_text.casefold()
                   for term in definition.world.protected_terms):
                raise ScenarioActionUnavailableError("A proposed fact mentions protected lore")
            previous = next((value for value in snapshot.facts
                             if value.fact_ref == fact.fact_ref), None)
            if previous is not None and (
                previous.kind != fact.kind or previous.public_text != fact.public_text
                or previous.scene_id != active.id
            ):
                raise ScenarioActionUnavailableError("Generated fact reference conflicts")
        return ScenarioProgressUpdate(
            from_scene_id=active.id, to_scene_id=to_scene_id,
            add_flags=tuple(flag for flag in effect.add_flags if flag not in snapshot.flags),
            ending_ref=effect.ending_ref, elapsed_actions=1,
            alert_delta=effect.alert_delta,
            facts=tuple(ScenarioFact(fact.fact_ref, fact.kind, fact.public_text, active.id)
                        for fact in effect.facts
                        if all(previous.fact_ref != fact.fact_ref for previous in snapshot.facts)),
        )

    def _current_scene(
        self, snapshot: ScenarioRunSnapshot
    ) -> tuple[ScenarioDefinition, SceneDefinition, ScenarioSceneSnapshot]:
        definition = self.definition_for(snapshot)
        if snapshot.status != "active" or snapshot.ending_ref is not None:
            raise ScenarioStateError("Scenario runがactive状態ではありません")

        definition_sequences = {scene.sequence for scene in definition.scenes}
        runtime_sequences = {scene.sequence for scene in snapshot.scenes}
        if (
            len(runtime_sequences) != len(snapshot.scenes)
            or runtime_sequences != definition_sequences
        ):
            raise ScenarioStateError("Scenario定義とruntime Scene sequenceが一致しません")

        active_scenes = tuple(scene for scene in snapshot.scenes if scene.status == "active")
        if len(active_scenes) != 1:
            raise ScenarioStateError("active Sceneを一意に解決できません")
        active = active_scenes[0]
        scene = next(
            (scene for scene in definition.scenes if scene.sequence == active.sequence),
            None,
        )
        if scene is None:
            raise ScenarioStateError("active Scene sequenceがScenario定義と一致しません")
        return definition, scene, active
