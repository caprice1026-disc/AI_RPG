"""Scenario actionを保存可能にする。"""

from alembic import op

revision = "0010_scenario_action_kind"
down_revision = "0009_scenario_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("actions_kind_check", "actions", type_="check")
    op.create_check_constraint(
        "actions_kind_check",
        "actions",
        "kind IN ('attack','skill_check','use_item','scenario_action')",
    )


def downgrade() -> None:
    op.execute(
        """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM actions WHERE kind='scenario_action') THEN
        RAISE EXCEPTION 'cannot downgrade: scenario_action rows still exist';
    END IF;
END $$;
"""
    )
    op.drop_constraint("actions_kind_check", "actions", type_="check")
    op.create_check_constraint(
        "actions_kind_check",
        "actions",
        "kind IN ('attack','skill_check','use_item')",
    )
