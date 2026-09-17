"""Sceneにruleset検証済みの技能判定条件を登録する。"""

from alembic import op

revision = "0004_scene_skill_checks"
down_revision = "0003_turn_worker_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
CREATE TABLE mvp_scene_skill_checks (
    campaign_id uuid NOT NULL,
    scene_id uuid NOT NULL,
    check_ref text NOT NULL CHECK(check_ref ~ '^[a-z][a-z0-9_]{0,63}$'),
    skill_ref text NOT NULL CHECK(skill_ref IN ('athletics','acrobatics','perception','stealth','persuasion')),
    difficulty text NOT NULL CHECK(difficulty IN ('easy','normal','hard')),
    target_id uuid,
    public_description text NOT NULL CHECK(length(public_description) BETWEEN 1 AND 2000),
    PRIMARY KEY(campaign_id,scene_id,check_ref),
    FOREIGN KEY(campaign_id,scene_id) REFERENCES scenes(campaign_id,id),
    FOREIGN KEY(campaign_id,target_id) REFERENCES entities(campaign_id,id)
);
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE mvp_scene_skill_checks")
