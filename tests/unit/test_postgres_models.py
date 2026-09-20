"""Infrastructure限定ORM mappingのschema coverage。"""

from uuid import UUID

from sqlalchemy import CheckConstraint

from ai_rpg.application.ports import CanonicalSnapshot
from ai_rpg.infrastructure.postgres import models
from ai_rpg.infrastructure.postgres.models import (
    Base,
    EntityModel,
    PrincipalIdentityModel,
    TurnModel,
)


def test_metadata_contains_contract_tables_and_worker_control_columns() -> None:
    assert {
        "campaigns",
        "campaign_members",
        "principals",
        "principal_identities",
        "entities",
        "scenes",
        "turns",
        "turn_choices",
        "actions",
        "events",
        "mvp_characters",
        "mvp_skill_modifiers",
        "mvp_weapons",
        "mvp_inventory",
        "mvp_scene_skill_checks",
        "mvp_scene_entities",
    } <= set(Base.metadata.tables)

    assert {
        "llm_call_budget",
        "initial_route",
        "routing_rule_version",
        "routing_reason_codes",
        "resolution_attempt_count",
        "resolution_deadline",
        "narration_worker_epoch",
        "narration_lease_until",
        "narration_attempt_count",
        "narration_deadline",
    } <= set(TurnModel.__table__.columns.keys())
    assert {"ref", "label"} <= set(EntityModel.__table__.columns.keys())
    assert {
        "issuer",
        "subject",
        "principal_id",
        "created_at",
        "disabled_at",
    } == set(PrincipalIdentityModel.__table__.columns.keys())


def test_metadata_preserves_open_turn_partial_unique_index() -> None:
    index = next(index for index in TurnModel.__table__.indexes if index.name == "one_open_turn")

    assert index.unique
    assert "narration_status" in str(index.dialect_options["postgresql"]["where"])


def test_scene_entity_metadata_matches_scope_constraints() -> None:
    assert hasattr(models, "MvpSceneEntityModel")
    table = models.MvpSceneEntityModel.__table__
    assert set(table.columns.keys()) == {
        "campaign_id",
        "scene_id",
        "entity_id",
        "is_public",
        "is_attack_reachable",
    }
    assert list(table.primary_key.columns.keys()) == ["campaign_id", "scene_id", "entity_id"]
    assert all(not column.nullable for column in table.columns)
    assert str(table.c.is_public.server_default.arg) == "true"
    assert str(table.c.is_attack_reachable.server_default.arg) == "false"
    assert {
        (tuple(fk.column_keys), tuple(element.target_fullname for element in fk.elements))
        for fk in table.foreign_key_constraints
    } == {
        (("campaign_id", "scene_id"), ("scenes.campaign_id", "scenes.id")),
        (("campaign_id", "entity_id"), ("entities.campaign_id", "entities.id")),
    }
    check = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "scene_entity_reachable_is_public"
    )
    assert str(check.sqltext) == "NOT is_attack_reachable OR is_public"


def test_scenario_progress_metadata_matches_constraints() -> None:
    run = models.MvpScenarioRunModel.__table__
    flags = models.MvpScenarioFlagModel.__table__

    assert list(run.primary_key.columns.keys()) == ["campaign_id"]
    assert {"scenario_ref", "scenario_version", "status", "ending_ref"} <= set(run.c.keys())
    assert list(flags.primary_key.columns.keys()) == ["campaign_id", "flag_ref"]


def test_canonical_snapshot_defaults_scene_entities_for_existing_fixtures() -> None:
    snapshot = CanonicalSnapshot(
        campaign_id=UUID(int=1),
        state_version=0,
        characters=(),
        skills=(),
        equipment=(),
        inventory=(),
        skill_checks=(),
        entities=(),
    )
    assert snapshot.scene_entities == ()
    assert snapshot.scenario_run is None
