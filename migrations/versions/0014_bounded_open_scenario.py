"""Store v3 character builds, bounded facts, pressure and risk proposals."""

from alembic import op

revision = "0014_bounded_open_scenario"
down_revision = "0013_browser_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
ALTER TABLE mvp_scenario_runs
    ADD COLUMN elapsed_actions integer NOT NULL DEFAULT 0,
    ADD COLUMN alert_level integer NOT NULL DEFAULT 0,
    ADD CONSTRAINT scenario_elapsed_nonnegative CHECK (elapsed_actions >= 0),
    ADD CONSTRAINT scenario_alert_bounded CHECK (alert_level BETWEEN 0 AND 5);

CREATE TABLE mvp_character_abilities (
    campaign_id uuid NOT NULL,
    character_id uuid NOT NULL,
    strength integer NOT NULL CHECK (strength BETWEEN 0 AND 3),
    agility integer NOT NULL CHECK (agility BETWEEN 0 AND 3),
    insight integer NOT NULL CHECK (insight BETWEEN 0 AND 3),
    presence integer NOT NULL CHECK (presence BETWEEN 0 AND 3),
    specialty_skill text NOT NULL CHECK (
        specialty_skill IN ('athletics','acrobatics','perception','stealth','persuasion')),
    PRIMARY KEY (campaign_id, character_id),
    FOREIGN KEY (campaign_id, character_id)
        REFERENCES mvp_characters(campaign_id, entity_id)
);

CREATE TABLE mvp_scenario_facts (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES mvp_scenario_runs(campaign_id),
    fact_ref text NOT NULL CHECK (fact_ref ~ '^[a-z][a-z0-9_]{0,63}$'),
    scene_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('place','person','clue','route')),
    public_text text NOT NULL CHECK (length(public_text) BETWEEN 1 AND 500),
    created_by_turn_id uuid NOT NULL,
    UNIQUE (campaign_id, id),
    UNIQUE (campaign_id, fact_ref),
    FOREIGN KEY (campaign_id, scene_id) REFERENCES scenes(campaign_id, id),
    FOREIGN KEY (campaign_id, created_by_turn_id) REFERENCES turns(campaign_id, id)
);
CREATE INDEX scenario_facts_location ON mvp_scenario_facts(campaign_id, scene_id);

CREATE TABLE mvp_action_proposals (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES mvp_scenario_runs(campaign_id),
    actor_id uuid NOT NULL,
    source_turn_id uuid NOT NULL UNIQUE,
    state_version bigint NOT NULL CHECK (state_version >= 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    risk_text text NOT NULL CHECK (length(risk_text) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (campaign_id, actor_id) REFERENCES mvp_characters(campaign_id, entity_id),
    FOREIGN KEY (campaign_id, source_turn_id) REFERENCES turns(campaign_id, id)
);
""")
    op.drop_constraint("actions_kind_check", "actions", type_="check")
    op.create_check_constraint(
        "actions_kind_check", "actions",
        "kind IN ('attack','skill_check','use_item','scenario_action','open_action')",
    )
    op.drop_constraint("turns_input_kind_check", "turns", type_="check")
    op.drop_constraint("turns_input_content_check", "turns", type_="check")
    op.create_check_constraint(
        "turns_input_kind_check", "turns",
        "input_kind IN ('text','choice','scenario_action','confirm_action')",
    )
    op.create_check_constraint(
        "turns_input_content_check", "turns",
        """(input_kind='text' AND input_text IS NOT NULL AND length(input_text) BETWEEN 1 AND 8000
            AND selected_choice_id IS NULL AND selected_action_ref IS NULL) OR
           (input_kind='choice' AND input_text IS NULL AND selected_choice_id IS NOT NULL
            AND selected_action_ref IS NULL) OR
           (input_kind='scenario_action' AND input_text IS NOT NULL AND length(input_text) BETWEEN 1 AND 8000
            AND selected_choice_id IS NULL AND selected_action_ref IS NOT NULL
            AND length(selected_action_ref) BETWEEN 1 AND 120) OR
           (input_kind='confirm_action' AND input_text IS NOT NULL AND length(input_text) BETWEEN 1 AND 8000
            AND selected_choice_id IS NULL AND selected_action_ref IS NOT NULL
            AND length(selected_action_ref)=36)""",
    )


def downgrade() -> None:
    op.execute("""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM mvp_character_abilities)
       OR EXISTS (SELECT 1 FROM mvp_scenario_facts)
       OR EXISTS (SELECT 1 FROM mvp_action_proposals)
       OR EXISTS (SELECT 1 FROM actions WHERE kind='open_action')
       OR EXISTS (SELECT 1 FROM turns WHERE input_kind='confirm_action')
       OR EXISTS (SELECT 1 FROM mvp_scenario_runs
                  WHERE elapsed_actions <> 0 OR alert_level <> 0) THEN
        RAISE EXCEPTION 'cannot downgrade: bounded scenario state still exists';
    END IF;
END $$;
""")
    op.drop_constraint("actions_kind_check", "actions", type_="check")
    op.create_check_constraint(
        "actions_kind_check", "actions",
        "kind IN ('attack','skill_check','use_item','scenario_action')",
    )
    op.drop_constraint("turns_input_kind_check", "turns", type_="check")
    op.drop_constraint("turns_input_content_check", "turns", type_="check")
    op.create_check_constraint(
        "turns_input_kind_check", "turns", "input_kind IN ('text','choice','scenario_action')",
    )
    op.create_check_constraint(
        "turns_input_content_check", "turns",
        """(input_kind='text' AND input_text IS NOT NULL AND length(input_text) BETWEEN 1 AND 8000
            AND selected_choice_id IS NULL AND selected_action_ref IS NULL) OR
           (input_kind='choice' AND input_text IS NULL AND selected_choice_id IS NOT NULL
            AND selected_action_ref IS NULL) OR
           (input_kind='scenario_action' AND input_text IS NOT NULL AND length(input_text) BETWEEN 1 AND 8000
            AND selected_choice_id IS NULL AND selected_action_ref IS NOT NULL
            AND length(selected_action_ref) BETWEEN 1 AND 120)""",
    )
    op.execute("""
DROP TABLE mvp_action_proposals;
DROP TABLE mvp_scenario_facts;
DROP TABLE mvp_character_abilities;
ALTER TABLE mvp_scenario_runs
    DROP CONSTRAINT scenario_elapsed_nonnegative,
    DROP CONSTRAINT scenario_alert_bounded,
    DROP COLUMN elapsed_actions,
    DROP COLUMN alert_level;
""")
