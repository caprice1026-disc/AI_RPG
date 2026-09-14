"""mvp_v1のCampaign単位Canonical状態。"""

from alembic import op

revision = "0002_mvp_v1_canonical"
down_revision = "0001_initial_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
CREATE TABLE mvp_characters (campaign_id uuid NOT NULL, entity_id uuid NOT NULL, current_hp integer NOT NULL, max_hp integer NOT NULL, defense integer NOT NULL CHECK(defense>=0), attack_bonus integer NOT NULL DEFAULT 0, PRIMARY KEY(campaign_id,entity_id), FOREIGN KEY(campaign_id,entity_id) REFERENCES entities(campaign_id,id), CHECK(max_hp>=1), CHECK(current_hp BETWEEN 0 AND max_hp));
CREATE TABLE mvp_skill_modifiers (campaign_id uuid NOT NULL, character_id uuid NOT NULL, skill_ref text NOT NULL CHECK(skill_ref IN ('athletics','acrobatics','perception','stealth','persuasion')), modifier integer NOT NULL, PRIMARY KEY(campaign_id,character_id,skill_ref), FOREIGN KEY(campaign_id,character_id) REFERENCES mvp_characters(campaign_id,entity_id));
CREATE TABLE mvp_weapons (campaign_id uuid NOT NULL, entity_id uuid NOT NULL, damage_expression text NOT NULL CHECK(damage_expression ~ '^[0-9]+d[0-9]+([+-][0-9]+)?$'), damage_bonus integer NOT NULL DEFAULT 0, PRIMARY KEY(campaign_id,entity_id), FOREIGN KEY(campaign_id,entity_id) REFERENCES entities(campaign_id,id));
CREATE TABLE mvp_inventory (campaign_id uuid NOT NULL, owner_id uuid NOT NULL, item_id uuid NOT NULL, quantity integer NOT NULL CHECK(quantity>=0), equipped boolean NOT NULL DEFAULT false, PRIMARY KEY(campaign_id,owner_id,item_id), FOREIGN KEY(campaign_id,owner_id) REFERENCES mvp_characters(campaign_id,entity_id), FOREIGN KEY(campaign_id,item_id) REFERENCES entities(campaign_id,id));
CREATE UNIQUE INDEX one_equipped_weapon ON mvp_inventory(campaign_id,owner_id) WHERE equipped;
""")


def downgrade() -> None:
    op.execute(
        "DROP TABLE mvp_inventory; DROP TABLE mvp_weapons; DROP TABLE mvp_skill_modifiers; DROP TABLE mvp_characters;"
    )
