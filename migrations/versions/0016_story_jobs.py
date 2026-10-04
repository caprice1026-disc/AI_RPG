"""Durable authoring jobs, fenced attempt reservations and owner-only proposals."""

from alembic import op

revision = "0016_story_jobs"
down_revision = "0015_story_authoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE authoring_jobs (
    id uuid PRIMARY KEY,
    story_id uuid NOT NULL REFERENCES stories(id),
    owner_principal_id uuid NOT NULL REFERENCES principals(id),
    request_id uuid NOT NULL,
    input_hash text NOT NULL,
    base_revision bigint NOT NULL,
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    snapshot_hash text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('check','fill','outline','concretize')),
    instructions text NOT NULL,
    approved_outline jsonb,
    model_id text NOT NULL,
    limits jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts>=0),
    lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch>=0),
    lease_until timestamptz,
    physical_requests integer NOT NULL DEFAULT 0 CHECK (physical_requests BETWEEN 0 AND 1),
    input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens>=0),
    output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens>=0),
    usage_complete boolean NOT NULL DEFAULT false,
    actual_model text,
    error_code text,
    outline jsonb,
    outline_revision integer NOT NULL DEFAULT 0,
    approved_outline_revision integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(owner_principal_id,request_id),
    UNIQUE(id,story_id,base_revision),
    FOREIGN KEY (story_id,base_revision) REFERENCES story_draft_revisions(story_id,revision),
    CHECK ((state='running') = (lease_until IS NOT NULL)),
    CHECK (approved_outline_revision IS NULL OR approved_outline_revision=outline_revision)
);
CREATE INDEX ix_authoring_jobs_owner_principal_id ON authoring_jobs(owner_principal_id);
CREATE INDEX ix_authoring_jobs_story_id ON authoring_jobs(story_id);
CREATE INDEX ix_authoring_jobs_state ON authoring_jobs(state,created_at);
CREATE TABLE authoring_proposals (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL UNIQUE REFERENCES authoring_jobs(id),
    story_id uuid NOT NULL REFERENCES stories(id),
    base_revision bigint NOT NULL,
    payload jsonb NOT NULL,
    decision text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','applied')),
    applied_revision bigint,
    adopted_change_ids jsonb NOT NULL DEFAULT '[]',
    FOREIGN KEY (job_id,story_id,base_revision) REFERENCES authoring_jobs(id,story_id,base_revision),
    FOREIGN KEY (story_id,applied_revision) REFERENCES story_draft_revisions(story_id,revision),
    CHECK ((decision='applied') = (applied_revision IS NOT NULL))
);
CREATE TABLE authoring_attempts (
    job_id uuid NOT NULL REFERENCES authoring_jobs(id),
    lease_epoch bigint NOT NULL,
    state text NOT NULL,
    request_reserved boolean NOT NULL DEFAULT false,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY(job_id,lease_epoch)
);
CREATE UNIQUE INDEX authoring_one_physical_request ON authoring_attempts(job_id) WHERE request_reserved;
CREATE FUNCTION authoring_job_protect_input() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.story_id,NEW.owner_principal_id,NEW.request_id,NEW.input_hash,NEW.base_revision,
           NEW.snapshot,NEW.snapshot_hash,NEW.kind,NEW.instructions,NEW.approved_outline,
           NEW.model_id,NEW.limits,NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.story_id,OLD.owner_principal_id,OLD.request_id,OLD.input_hash,OLD.base_revision,
           OLD.snapshot,OLD.snapshot_hash,OLD.kind,OLD.instructions,OLD.approved_outline,
           OLD.model_id,OLD.limits,OLD.created_at) THEN
        RAISE EXCEPTION 'authoring job input is immutable' USING ERRCODE='23514';
    END IF;
    IF NEW.physical_requests < OLD.physical_requests OR NEW.lease_epoch < OLD.lease_epoch THEN
        RAISE EXCEPTION 'authoring fence cannot move backwards' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER authoring_jobs_input_immutable BEFORE UPDATE ON authoring_jobs
    FOR EACH ROW EXECUTE FUNCTION authoring_job_protect_input();
CREATE FUNCTION authoring_proposal_protect_result() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.job_id,NEW.story_id,NEW.base_revision,NEW.payload)
       IS DISTINCT FROM ROW(OLD.job_id,OLD.story_id,OLD.base_revision,OLD.payload) THEN
        RAISE EXCEPTION 'authoring proposal is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER authoring_proposals_result_immutable BEFORE UPDATE ON authoring_proposals
    FOR EACH ROW EXECUTE FUNCTION authoring_proposal_protect_result();
""")


def downgrade() -> None:
    op.execute("""
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM authoring_jobs) THEN
        RAISE EXCEPTION 'cannot downgrade: authoring jobs still exist';
    END IF;
END $$;
DROP TABLE authoring_attempts;
DROP TABLE authoring_proposals;
DROP TABLE authoring_jobs;
DROP FUNCTION authoring_proposal_protect_result();
DROP FUNCTION authoring_job_protect_input();
""")
