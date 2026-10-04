"""Resolve authored initialization without scenario-name dispatch."""

from ai_rpg.scenarios.models import ScenarioDefinition, ScenarioInitialization


def initialization_for(definition: ScenarioDefinition) -> ScenarioInitialization:
    if definition.initialization is None:
        raise ValueError("Executable scenarios require explicit initialization")
    return definition.initialization


def item_effect_ref(definition: ScenarioDefinition, item_ref: str) -> str | None:
    """Return a ruleset effect, never infer one from an item's name."""
    return next(
        (item.effect_ref for item in initialization_for(definition).items
         if item.ref == item_ref),
        None,
    )
