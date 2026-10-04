"""Display refs are drawn only from this turn's frozen public narration input."""

from uuid import uuid4

from ai_rpg.infrastructure.postgres.repositories import PostgresTurnRepository


def test_public_turn_labels_do_not_expose_unmentioned_entities_or_private_context():
    row = {
        "id": uuid4(), "route": "mechanical", "resolution_status": "committed",
        "narration_status": "completed", "committed_state_version": 1,
        "narration": "@custom_journal を持ち帰った。", "replay_choices": [],
        "replay_actions": [], "recovery_reason": None, "risk_proposal_id": None,
        "narration_input": {
            "allowed_entity_refs": [
                {"ref": "custom_journal", "label": "航海日誌", "entity_kind": "object"},
                {"ref": "not_mentioned", "label": "別の道具", "entity_kind": "item"},
            ],
            "public_state_after": [{"content": "not a display label"}],
        },
    }
    response = PostgresTurnRepository._to_response(row)
    assert response.entity_labels == {"custom_journal": "航海日誌"}
    assert "not_mentioned" not in response.model_dump_json()
    row["narration_input"] = None
    assert PostgresTurnRepository._to_response(row).entity_labels == {}
