"""Safe maintenance entrypoint and bounded retention settings."""

import asyncio

import pytest
from pydantic import ValidationError

from ai_rpg.cli import _parser
from ai_rpg.config import Settings
from ai_rpg.infrastructure.postgres.maintenance import prune_draft_history


def test_pruning_requires_explicit_apply() -> None:
    args = _parser().parse_args(["prune-draft-history"])
    assert args.apply is False
    assert args.retention_days is None
    assert args.batch_size is None
    explicit = _parser().parse_args(
        [
            "prune-draft-history",
            "--apply",
            "--retention-days",
            "90",
            "--batch-size",
            "25",
        ]
    )
    assert explicit.apply is True
    assert explicit.retention_days == 90
    assert explicit.batch_size == 25


@pytest.mark.parametrize(
    "field,value",
    [
        ("draft_history_retention_days", 0),
        ("draft_history_retention_days", 3651),
        ("draft_history_prune_batch_size", 0),
        ("draft_history_prune_batch_size", 1001),
    ],
)
def test_retention_configuration_rejects_unbounded_values(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "options",
    [
        {"retention_days": 0},
        {"retention_days": 3651},
        {"retention_days": True},
        {"batch_size": 0},
        {"batch_size": 1001},
        {"apply": "yes"},
    ],
)
def test_invalid_overrides_fail_before_opening_a_session(options) -> None:
    with pytest.raises(ValueError):
        asyncio.run(prune_draft_history(None, **options))
