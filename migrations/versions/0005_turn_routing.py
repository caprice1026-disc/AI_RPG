"""Turn routerの初期判断と根拠を永続化する。"""

from alembic import op

revision = "0005_turn_routing"
down_revision = "0004_scene_skill_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
ALTER TABLE turns
    ADD COLUMN initial_route text CHECK(initial_route IN ('narrative','mechanical')),
    ADD COLUMN routing_rule_version text CHECK(
        routing_rule_version IS NULL OR length(routing_rule_version) BETWEEN 1 AND 100
    ),
    ADD COLUMN routing_reason_codes jsonb CHECK(
        routing_reason_codes IS NULL OR jsonb_typeof(routing_reason_codes)='array'
    ),
    ADD CONSTRAINT routing_metadata_complete CHECK(
        (initial_route IS NULL)
        = (routing_rule_version IS NULL AND routing_reason_codes IS NULL)
    );
"""
    )


def downgrade() -> None:
    op.execute(
        r"""
ALTER TABLE turns
    DROP CONSTRAINT routing_metadata_complete,
    DROP COLUMN routing_reason_codes,
    DROP COLUMN routing_rule_version,
    DROP COLUMN initial_route;
"""
    )
