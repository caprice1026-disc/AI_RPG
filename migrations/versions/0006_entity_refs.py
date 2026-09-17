"""LLMへ公開する安定したEntity参照名を登録する。"""

from alembic import op

revision = "0006_entity_refs"
down_revision = "0005_turn_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
ALTER TABLE entities
    ADD COLUMN ref text,
    ADD COLUMN label text,
    ADD CONSTRAINT entity_ref_pair CHECK((ref IS NULL)=(label IS NULL)),
    ADD CONSTRAINT entity_ref_format CHECK(
        ref IS NULL OR ref ~ '^[a-z][a-z0-9_]{0,63}$'
    ),
    ADD CONSTRAINT entity_label_length CHECK(
        label IS NULL OR length(label) BETWEEN 1 AND 500
    );
CREATE UNIQUE INDEX entities_campaign_ref
    ON entities(campaign_id,ref) WHERE ref IS NOT NULL;
"""
    )


def downgrade() -> None:
    op.execute(
        r"""
DROP INDEX entities_campaign_ref;
ALTER TABLE entities
    DROP CONSTRAINT entity_label_length,
    DROP CONSTRAINT entity_ref_format,
    DROP CONSTRAINT entity_ref_pair,
    DROP COLUMN label,
    DROP COLUMN ref;
"""
    )
