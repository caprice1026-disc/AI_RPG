"""Immutable story versions, revisioned drafts and authoring evidence."""

from alembic import op

revision = "0015_story_authoring"
down_revision = "0014_bounded_open_scenario"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE stories (
    id uuid PRIMARY KEY,
    owner_principal_id uuid REFERENCES principals(id),
    visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','unlisted','public')),
    lifecycle text NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active','withdrawn','blocked','archived')),
    current_release_id uuid,
    current_release_kind text NOT NULL DEFAULT 'release' CHECK (current_release_kind='release'),
    builtin_ref text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((owner_principal_id IS NULL) = (builtin_ref IS NOT NULL))
);
CREATE INDEX ix_stories_owner_principal_id ON stories(owner_principal_id);
CREATE TABLE story_drafts (
    story_id uuid PRIMARY KEY REFERENCES stories(id),
    revision bigint NOT NULL CHECK (revision>0),
    authoring_schema_version integer NOT NULL CHECK (authoring_schema_version=1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    template_id text,
    template_version integer,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE story_draft_revisions (
    story_id uuid NOT NULL REFERENCES stories(id),
    revision bigint NOT NULL CHECK (revision>0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    actor_id uuid NOT NULL REFERENCES principals(id),
    source text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (story_id, revision)
);
CREATE TABLE story_versions (
    id uuid PRIMARY KEY,
    story_id uuid NOT NULL REFERENCES stories(id),
    kind text NOT NULL CHECK (kind IN ('release','playtest')),
    release_number integer CHECK (release_number>0),
    source_draft_revision bigint,
    schema_version integer NOT NULL CHECK (schema_version IN (1,2)),
    ruleset_ref text NOT NULL,
    ruleset_version text NOT NULL,
    capability_requirements jsonb NOT NULL CHECK (jsonb_typeof(capability_requirements)='array'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    public_metadata jsonb NOT NULL CHECK (jsonb_typeof(public_metadata)='object'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind='release') = (release_number IS NOT NULL)),
    UNIQUE (story_id, release_number),
    UNIQUE (story_id, id, kind),
    FOREIGN KEY (story_id, source_draft_revision) REFERENCES story_draft_revisions(story_id, revision)
);
ALTER TABLE stories ADD CONSTRAINT stories_current_release_fk
    FOREIGN KEY (id, current_release_id, current_release_kind)
    REFERENCES story_versions(story_id, id, kind);
ALTER TABLE mvp_scenario_runs ADD COLUMN story_version_id uuid REFERENCES story_versions(id);
CREATE INDEX scenario_runs_story_version ON mvp_scenario_runs(story_version_id);
CREATE TABLE builtin_scenario_versions (
    scenario_ref text NOT NULL,
    scenario_version integer NOT NULL CHECK (scenario_version>0),
    story_version_id uuid NOT NULL UNIQUE REFERENCES story_versions(id),
    PRIMARY KEY (scenario_ref, scenario_version)
);
CREATE TABLE story_validation_reports (
    id uuid PRIMARY KEY,
    story_id uuid NOT NULL REFERENCES stories(id),
    draft_revision bigint NOT NULL,
    content_hash text,
    draft_hash text NOT NULL,
    validator_version text NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (story_id, draft_revision) REFERENCES story_draft_revisions(story_id, revision)
);
CREATE TABLE story_playtest_records (
    campaign_id uuid PRIMARY KEY REFERENCES mvp_scenario_runs(campaign_id),
    story_version_id uuid NOT NULL REFERENCES story_versions(id),
    actor_id uuid NOT NULL REFERENCES principals(id),
    debug_modified boolean NOT NULL DEFAULT false,
    author_acknowledged boolean NOT NULL DEFAULT false,
    result text,
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE story_requests (
    principal_id uuid NOT NULL REFERENCES principals(id),
    request_id uuid NOT NULL,
    operation text NOT NULL,
    story_id uuid NOT NULL REFERENCES stories(id),
    input_hash text NOT NULL,
    response jsonb NOT NULL CHECK (jsonb_typeof(response)='object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, request_id)
);
CREATE FUNCTION story_reject_snapshot_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'story snapshot is immutable' USING ERRCODE = '23514';
END $$;
CREATE TRIGGER story_versions_immutable BEFORE UPDATE OR DELETE ON story_versions
    FOR EACH ROW EXECUTE FUNCTION story_reject_snapshot_change();
CREATE TRIGGER story_validation_reports_immutable BEFORE UPDATE OR DELETE ON story_validation_reports
    FOR EACH ROW EXECUTE FUNCTION story_reject_snapshot_change();
CREATE TRIGGER story_revisions_immutable BEFORE UPDATE ON story_draft_revisions
    FOR EACH ROW EXECUTE FUNCTION story_reject_snapshot_change();
""")


def downgrade() -> None:
    op.execute("""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM stories)
       OR EXISTS (SELECT 1 FROM mvp_scenario_runs WHERE story_version_id IS NOT NULL) THEN
        RAISE EXCEPTION 'cannot downgrade: story authoring data still exists';
    END IF;
END $$;
DROP TABLE story_requests;
DROP TABLE story_playtest_records;
DROP TABLE story_validation_reports;
DROP TABLE builtin_scenario_versions;
ALTER TABLE mvp_scenario_runs DROP COLUMN story_version_id;
ALTER TABLE stories DROP CONSTRAINT stories_current_release_fk;
DROP TABLE story_versions;
DROP TABLE story_draft_revisions;
DROP TABLE story_drafts;
DROP TABLE stories;
DROP FUNCTION story_reject_snapshot_change();
""")
