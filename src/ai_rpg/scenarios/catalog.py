"""組込みScenario catalog。"""

from collections.abc import Iterable, Mapping
from importlib.resources import files
from types import MappingProxyType

from ai_rpg.scenarios.models import ScenarioDefinition


class ScenarioCatalog:
    def __init__(self, scenarios: Iterable[ScenarioDefinition]) -> None:
        by_ref: dict[tuple[str, int], ScenarioDefinition] = {}
        for scenario in scenarios:
            key = (scenario.scenario_ref, scenario.version)
            if key in by_ref:
                raise ValueError(f"Scenario参照とversionが重複しています: {key}")
            by_ref[key] = scenario
        self._scenarios: Mapping[tuple[str, int], ScenarioDefinition] = MappingProxyType(by_ref)

    def get(self, scenario_ref: str, version: int) -> ScenarioDefinition:
        return self._scenarios[(scenario_ref, version)]


def _load_builtin(filename: str) -> ScenarioDefinition:
    payload = files("ai_rpg.scenarios").joinpath(filename).read_text(encoding="utf-8")
    return ScenarioDefinition.model_validate_json(payload)


BUILTIN_SCENARIOS = ScenarioCatalog(
    [
        _load_builtin("ruined_chapel.json"),
        _load_builtin("ruined_chapel_v2.json"),
        _load_builtin("ruined_chapel_v3.json"),
    ]
)
