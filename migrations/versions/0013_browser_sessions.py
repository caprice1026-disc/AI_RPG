"""Persist browser login attempts and opaque sessions without copying subjects."""

from alembic import op

revision = "0013_browser_sessions"
down_revision = "0012_registered_action_input"
branch_labels = None
depends_on = None


def _identity_guard(*, include_identity_id: bool) -> None:
    identity_id_guard = (
        "OR NEW.identity_id IS DISTINCT FROM OLD.identity_id" if include_identity_id else ""
    )
    op.execute(
        f"""
CREATE OR REPLACE FUNCTION guard_principal_identity_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'principal identity history is immutable';
    END IF;
    IF NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       {identity_id_guard}
       OR NEW.disabled_at IS NULL
       OR OLD.disabled_at IS NOT NULL THEN
        RAISE EXCEPTION 'principal identity can only be disabled once';
    END IF;
    RETURN NEW;
END
$$;
"""
    )


def upgrade() -> None:
    # ADD COLUMN with a volatile default backfills every legacy row without UPDATE.
    op.execute(
        """
ALTER TABLE principal_identities
    ADD COLUMN identity_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid();

CREATE TABLE login_attempts (
    state_digest text PRIMARY KEY,
    binding_digest text NOT NULL,
    nonce_digest text NOT NULL,
    code_verifier text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX login_attempts_expires_at ON login_attempts(expires_at);

CREATE TABLE browser_sessions (
    token_digest text PRIMARY KEY,
    identity_id uuid NOT NULL REFERENCES principal_identities(identity_id),
    csrf_token text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CONSTRAINT browser_session_expires_after_created CHECK (expires_at>created_at)
);
CREATE INDEX browser_sessions_expires_at ON browser_sessions(expires_at);
"""
    )
    _identity_guard(include_identity_id=True)


def downgrade() -> None:
    op.execute("DROP TABLE browser_sessions; DROP TABLE login_attempts;")
    _identity_guard(include_identity_id=False)
    op.execute("ALTER TABLE principal_identities DROP COLUMN identity_id;")
