"""Scenario runtime progressを保存する。"""

from alembic import op

revision = "0009_scenario_progress"
down_revision = "0008_scene_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
CREATE TABLE mvp_scenario_runs (
    campaign_id uuid PRIMARY KEY REFERENCES campaigns(id),
    scenario_ref text NOT NULL,
    scenario_version integer NOT NULL,
    status text NOT NULL,
    ending_ref text,
    CHECK (scenario_ref ~ '^[a-z][a-z0-9_]{0,63}$'),
    CHECK (scenario_version > 0),
    CHECK (status IN ('active','completed')),
    CHECK ((status='completed') = (ending_ref IS NOT NULL)),
    CHECK (ending_ref IS NULL OR ending_ref ~ '^[a-z][a-z0-9_]{0,63}$')
);

CREATE TABLE mvp_scenario_flags (
    campaign_id uuid NOT NULL REFERENCES mvp_scenario_runs(campaign_id),
    flag_ref text NOT NULL,
    PRIMARY KEY (campaign_id,flag_ref),
    CHECK (flag_ref ~ '^[a-z][a-z0-9_]{0,63}$')
);
"""
    )


def downgrade() -> None:
    op.execute(
        """
DROP TABLE mvp_scenario_flags;
DROP TABLE mvp_scenario_runs;
"""
    )
