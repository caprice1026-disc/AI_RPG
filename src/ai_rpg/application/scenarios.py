"""Scenario定義と保存済みrunから進行を評価する純粋Application component。"""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from ai_rpg.application.ports import ScenarioRunSnapshot, ScenarioSceneSnapshot
from ai_rpg.application.ports.repositories import (
    ScenarioProgressUpdate,
)
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


@dataclass(frozen=True, slots=True)
class ScenarioPublicContext:
    scene_title: str
    scene_description: str
    objective: str
    discovered_facts: tuple[str, ...]
    available_actions: tuple[tuple[str, str], ...]


ScenarioActionBinding: TypeAlias = (
    DirectScenarioAction | SkillScenarioAction | AttackScenarioAction
)
ScenarioOutcome: TypeAlias = Literal["success", "failure", "neutral"]


class ScenarioProgressor:
    def __init__(self, catalog: ScenarioCatalog) -> None:
        self._catalog = catalog

    def definition_for(self, snapshot: ScenarioRunSnapshot) -> ScenarioDefinition:
        try:
            return self._catalog.get(snapshot.scenario_ref, snapshot.scenario_version)
        except KeyError as error:
            raise ScenarioStateError("Scenario定義と保存versionが一致しません") from error

    def public_context_for(self, snapshot: ScenarioRunSnapshot) -> ScenarioPublicContext:
        definition, scene, _active = self._current_scene(snapshot)
        return ScenarioPublicContext(
            scene_title=scene.title,
            scene_description=scene.description,
            objective=definition.objective,
            discovered_facts=tuple(
                flag.public_fact
                for flag in definition.flags
                if flag.flag_ref in snapshot.flags
            ),
            available_actions=tuple(
                (action.action_ref, action.label) for action in scene.actions
            ),
        )

    def bind_scenario_action(
        self, snapshot: ScenarioRunSnapshot, action_ref: str
    ) -> DirectScenarioAction | None:
        _definition, scene, _active = self._current_scene(snapshot)
        matches = tuple(
            action
            for action in scene.actions
            if isinstance(action, DirectScenarioAction)
            and action.action_ref == action_ref
        )
        return matches[0] if len(matches) == 1 else None

    def bind_skill_check(
        self, snapshot: ScenarioRunSnapshot, skill_ref: str
    ) -> SkillScenarioAction | None:
        _definition, scene, _active = self._current_scene(snapshot)
        matches = tuple(
            action
            for action in scene.actions
            if isinstance(action, SkillScenarioAction) and action.skill_ref == skill_ref
        )
        return matches[0] if len(matches) == 1 else None

    def bind_attack(
        self, snapshot: ScenarioRunSnapshot, target_ref: str
    ) -> AttackScenarioAction | None:
        _definition, scene, _active = self._current_scene(snapshot)
        matches = tuple(
            action
            for action in scene.actions
            if isinstance(action, AttackScenarioAction)
            and action.target_ref == target_ref
        )
        return matches[0] if len(matches) == 1 else None

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
                runtime.id
                for runtime in snapshot.scenes
                if runtime.sequence == target.sequence
            )

        return ScenarioProgressUpdate(
            from_scene_id=active.id,
            to_scene_id=to_scene_id,
            add_flags=effect.add_flags,
            ending_ref=ending_ref,
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
