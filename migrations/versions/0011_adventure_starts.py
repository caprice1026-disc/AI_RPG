"""Persist atomic, principal-scoped adventure start requests."""

from alembic import op

revision = "0011_adventure_starts"
down_revision = "0010_scenario_action_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE adventure_start_requests (
    principal_id uuid NOT NULL,
    request_id uuid NOT NULL,
    input_payload jsonb NOT NULL CHECK (jsonb_typeof(input_payload)='object'),
    campaign_id uuid NOT NULL UNIQUE REFERENCES campaigns(id) DEFERRABLE INITIALLY DEFERRED,
    actor_id uuid NOT NULL,
    PRIMARY KEY (principal_id, request_id),
    FOREIGN KEY (campaign_id, actor_id) REFERENCES entities(campaign_id,id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX turns_history ON turns(campaign_id, created_at, id);
""")


def downgrade() -> None:
    op.execute("DROP INDEX turns_history; DROP TABLE adventure_start_requests;")
