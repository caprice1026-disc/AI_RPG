"""Canonical Scene-Entity relationを保存する。"""

from alembic import op

revision = "0008_scene_entities"
down_revision = "0007_oidc_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE mvp_scene_entities (
    campaign_id uuid NOT NULL,
    scene_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    is_public boolean NOT NULL DEFAULT true,
    is_attack_reachable boolean NOT NULL DEFAULT false,
    PRIMARY KEY (campaign_id,scene_id,entity_id),
    FOREIGN KEY (campaign_id,scene_id) REFERENCES scenes(campaign_id,id),
    FOREIGN KEY (campaign_id,entity_id) REFERENCES entities(campaign_id,id),
    CONSTRAINT scene_entity_reachable_is_public
      CHECK (NOT is_attack_reachable OR is_public)
);
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE mvp_scene_entities")
