"""request hashの決定性を検証する。"""

from uuid import UUID

from ai_rpg.contracts import PlayerTurnInput
from ai_rpg.infrastructure.postgres import request_hash


def test_request_hash_is_deterministic_and_scoped() -> None:
    campaign = UUID(int=1)
    scene = UUID(int=2)
    principal = UUID(int=3)
    turn = PlayerTurnInput.model_validate(
        {
            "request_id": str(UUID(int=4)),
            "expected_state_version": 0,
            "actor_id": str(UUID(int=5)),
            "content": {"kind": "text", "text": "  Open Door  "},
        }
    )
    first = request_hash(1, campaign, scene, principal, turn)
    assert len(first) == 32
    assert first == request_hash(1, campaign, scene, principal, turn)
    assert first != request_hash(2, campaign, scene, principal, turn)
