"""Public participation, bounded usage and operator audit."""

from alembic import op

revision = "0017_community"
down_revision = "0016_story_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE usage_reservations (
    principal_id uuid NOT NULL, purpose text NOT NULL, request_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(principal_id,purpose,request_key)
);
CREATE INDEX ix_usage_reservations_created_at ON usage_reservations(created_at);
CREATE TABLE service_controls (id integer PRIMARY KEY CHECK(id=1), usage_paused boolean NOT NULL);
INSERT INTO service_controls VALUES(1,false);
CREATE TABLE profiles (principal_id uuid PRIMARY KEY, display_name text NOT NULL
    CHECK(length(display_name) BETWEEN 1 AND 40));
CREATE TABLE story_reports (
    id uuid PRIMARY KEY, story_id uuid NOT NULL REFERENCES stories(id), reporter_id uuid NOT NULL,
    request_id uuid NOT NULL, reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 2000),
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(reporter_id,request_id)
);
CREATE TABLE moderation_audit (
    id uuid PRIMARY KEY, actor_id uuid NOT NULL, request_id uuid NOT NULL,
    story_id uuid REFERENCES stories(id), operation text NOT NULL, reason text NOT NULL,
    previous_value text NOT NULL, next_value text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(actor_id,request_id)
);
CREATE TRIGGER moderation_audit_immutable BEFORE UPDATE OR DELETE ON moderation_audit
    FOR EACH ROW EXECUTE FUNCTION story_reject_snapshot_change();
ALTER TABLE login_attempts ADD COLUMN registration_requested boolean NOT NULL DEFAULT false;
""")


def downgrade() -> None:
    op.execute("""
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM profiles) OR EXISTS(SELECT 1 FROM story_reports)
       OR EXISTS(SELECT 1 FROM moderation_audit) OR EXISTS(SELECT 1 FROM usage_reservations) THEN
        RAISE EXCEPTION 'cannot downgrade: community data still exists';
    END IF;
END $$;
ALTER TABLE login_attempts DROP COLUMN registration_requested;
DROP TABLE moderation_audit,story_reports,profiles,service_controls,usage_reservations;
""")
