"""Persist deterministic registered action input without changing old turns."""

import sqlalchemy as sa
from alembic import op

revision = "0012_registered_action_input"
down_revision = "0011_adventure_starts"
branch_labels = None
depends_on = None


def _drop_input_checks() -> None:
    # The original content constraint was unnamed; discover only input-kind checks.
    op.execute("""
DO $$ DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
        WHERE conrelid='turns'::regclass AND contype='c'
          AND pg_get_constraintdef(oid) LIKE '%input_kind%'
    LOOP EXECUTE format('ALTER TABLE turns DROP CONSTRAINT %I', c.conname); END LOOP;
END $$;
""")


def upgrade() -> None:
    op.add_column("turns", sa.Column("selected_action_ref", sa.Text(), nullable=True))
    _drop_input_checks()
    op.create_check_constraint(
        "turns_input_kind_check", "turns", "input_kind IN ('text','choice','scenario_action')"
    )
    op.create_check_constraint(
        "turns_input_content_check",
        "turns",
        """
        (input_kind='text' AND input_text IS NOT NULL AND length(input_text) BETWEEN 1 AND 8000
         AND selected_choice_id IS NULL AND selected_action_ref IS NULL) OR
        (input_kind='choice' AND input_text IS NULL AND selected_choice_id IS NOT NULL
         AND selected_action_ref IS NULL) OR
        (input_kind='scenario_action' AND input_text IS NOT NULL AND length(input_text) BETWEEN 1 AND 8000
         AND selected_choice_id IS NULL AND selected_action_ref IS NOT NULL
         AND length(selected_action_ref) BETWEEN 1 AND 120)
    """,
    )


def downgrade() -> None:
    op.execute("""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM turns WHERE input_kind='scenario_action') THEN
        RAISE EXCEPTION 'cannot downgrade: registered action turns still exist';
    END IF;
END $$;
""")
    _drop_input_checks()
    op.drop_column("turns", "selected_action_ref")
    op.create_check_constraint("turns_input_kind_check", "turns", "input_kind IN ('text','choice')")
    op.create_check_constraint(
        "turns_input_content_check",
        "turns",
        """
        (input_kind='text' AND input_text IS NOT NULL AND length(input_text) BETWEEN 1 AND 8000
         AND selected_choice_id IS NULL) OR
        (input_kind='choice' AND input_text IS NULL AND selected_choice_id IS NOT NULL)
    """,
    )
