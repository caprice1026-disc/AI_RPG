"""Infrastructure限定ORM mappingのschema coverage。"""

from ai_rpg.infrastructure.postgres.models import Base, TurnModel


def test_metadata_contains_contract_tables_and_worker_control_columns() -> None:
    assert {
        "campaigns",
        "campaign_members",
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
    } <= set(Base.metadata.tables)

    assert {
        "llm_call_budget",
        "resolution_attempt_count",
        "resolution_deadline",
        "narration_worker_epoch",
        "narration_lease_until",
        "narration_attempt_count",
        "narration_deadline",
    } <= set(TurnModel.__table__.columns.keys())


def test_metadata_preserves_open_turn_partial_unique_index() -> None:
    index = next(index for index in TurnModel.__table__.indexes if index.name == "one_open_turn")

    assert index.unique
    assert "narration_status" in str(index.dialect_options["postgresql"]["where"])
