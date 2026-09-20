"""型付きScenario定義と組込みcatalog。"""

from ai_rpg.scenarios.catalog import BUILTIN_SCENARIOS, ScenarioCatalog
from ai_rpg.scenarios.models import (
    AttackScenarioAction,
    DirectScenarioAction,
    EndingDefinition,
    ScenarioActionDefinition,
    ScenarioDefinition,
    ScenarioEffect,
    ScenarioEndingOverride,
    ScenarioFlagDefinition,
    SceneDefinition,
    SkillScenarioAction,
)

__all__ = [
    "BUILTIN_SCENARIOS",
    "AttackScenarioAction",
    "DirectScenarioAction",
    "EndingDefinition",
    "ScenarioActionDefinition",
    "ScenarioCatalog",
    "ScenarioDefinition",
    "ScenarioEffect",
    "ScenarioEndingOverride",
    "ScenarioFlagDefinition",
    "SceneDefinition",
    "SkillScenarioAction",
]
