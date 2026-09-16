"""Turn単位のworker所有権、予算、retry制御を永続化する。"""

from alembic import op

revision = "0003_turn_worker_controls"
down_revision = "0002_mvp_v1_canonical"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
ALTER TABLE turns
    ADD COLUMN llm_call_budget integer NOT NULL DEFAULT 3 CHECK(llm_call_budget BETWEEN 1 AND 3),
    ADD COLUMN resolution_attempt_count integer NOT NULL DEFAULT 0 CHECK(resolution_attempt_count>=0),
    ADD COLUMN resolution_started_at timestamptz,
    ADD COLUMN resolution_deadline timestamptz,
    ADD COLUMN resolution_next_attempt_at timestamptz,
    ADD COLUMN resolution_failure_code text CHECK(resolution_failure_code IS NULL OR length(resolution_failure_code) BETWEEN 1 AND 100),
    ADD COLUMN narration_worker_epoch bigint NOT NULL DEFAULT 0 CHECK(narration_worker_epoch>=0),
    ADD COLUMN narration_lease_until timestamptz,
    ADD COLUMN narration_attempt_count integer NOT NULL DEFAULT 0 CHECK(narration_attempt_count>=0),
    ADD COLUMN narration_started_at timestamptz,
    ADD COLUMN narration_deadline timestamptz,
    ADD COLUMN narration_next_attempt_at timestamptz,
    ADD COLUMN narration_failure_code text CHECK(narration_failure_code IS NULL OR length(narration_failure_code) BETWEEN 1 AND 100),
    ADD CONSTRAINT llm_call_count_within_budget CHECK(llm_call_count<=llm_call_budget),
    ADD CONSTRAINT resolution_timing_pair CHECK((resolution_started_at IS NULL)=(resolution_deadline IS NULL)),
    ADD CONSTRAINT narration_timing_pair CHECK((narration_started_at IS NULL)=(narration_deadline IS NULL));

DROP INDEX one_unresolved_turn;
CREATE UNIQUE INDEX one_open_turn ON turns(campaign_id)
WHERE resolution_status IN ('pending','resolving')
   OR narration_status IN ('pending','generating');
"""
    )


def downgrade() -> None:
    op.execute(
        r"""
DROP INDEX one_open_turn;
CREATE UNIQUE INDEX one_unresolved_turn ON turns(campaign_id)
WHERE resolution_status IN ('pending','resolving');

ALTER TABLE turns
    DROP CONSTRAINT narration_timing_pair,
    DROP CONSTRAINT resolution_timing_pair,
    DROP CONSTRAINT llm_call_count_within_budget,
    DROP COLUMN narration_failure_code,
    DROP COLUMN narration_next_attempt_at,
    DROP COLUMN narration_deadline,
    DROP COLUMN narration_started_at,
    DROP COLUMN narration_attempt_count,
    DROP COLUMN narration_lease_until,
    DROP COLUMN narration_worker_epoch,
    DROP COLUMN resolution_failure_code,
    DROP COLUMN resolution_next_attempt_at,
    DROP COLUMN resolution_deadline,
    DROP COLUMN resolution_started_at,
    DROP COLUMN resolution_attempt_count,
    DROP COLUMN llm_call_budget;
"""
    )
