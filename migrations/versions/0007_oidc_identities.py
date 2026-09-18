"""OIDC subjectと内部principalの不変な対応を保存する。"""

from alembic import op

revision = "0007_oidc_identities"
down_revision = "0006_entity_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
CREATE TABLE principals (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE principal_identities (
    issuer text NOT NULL,
    subject text NOT NULL,
    principal_id uuid NOT NULL REFERENCES principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    disabled_at timestamptz,
    PRIMARY KEY (issuer,subject),
    CONSTRAINT principal_identity_issuer_nonempty CHECK (length(issuer)>0),
    CONSTRAINT principal_identity_subject_nonempty CHECK (length(subject)>0),
    CONSTRAINT principal_identity_disabled_after_created CHECK (
        disabled_at IS NULL OR disabled_at>=created_at
    )
);

CREATE INDEX principal_identities_principal ON principal_identities(principal_id);

CREATE FUNCTION guard_principal_identity_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'principal identity history is immutable';
    END IF;
    IF NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.disabled_at IS NULL
       OR OLD.disabled_at IS NOT NULL THEN
        RAISE EXCEPTION 'principal identity can only be disabled once';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER guard_principal_identity
BEFORE UPDATE OR DELETE ON principal_identities
FOR EACH ROW EXECUTE FUNCTION guard_principal_identity_history();
"""
    )


def downgrade() -> None:
    op.execute(
        """
DROP TRIGGER guard_principal_identity ON principal_identities;
DROP FUNCTION guard_principal_identity_history();
DROP TABLE principal_identities;
DROP TABLE principals;
"""
    )
