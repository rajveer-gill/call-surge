"""per-channel overage billing guard

Mirrors the same additive DDL applied idempotently in database.init_db().

Why: overage_processed records only "this tenant/month is done", which is too
coarse for a job that creates two Stripe invoice items. When the voice item was
created and the SMS item then raised, no marker was written at all, so the next
run charged the customer for voice a second time. This table records each channel
as soon as its own invoice item lands, so a retry resumes rather than repeats.

No backfill. Rows are only consulted for a (client_id, month) that
overage_processed has not already marked complete, and every historical month is
marked complete — so an empty table on existing installs reproduces today's
behavior exactly.

Revision ID: 0015_overage_processed_item
Revises: 0014_org_owner_role
"""
from alembic import op

revision = "0015_overage_processed_item"
down_revision = "0014_org_owner_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS overage_processed_item (
            client_id TEXT NOT NULL,
            month TEXT NOT NULL CHECK (month ~ '^\\d{4}-\\d{2}$'),
            channel TEXT NOT NULL CHECK (channel IN ('voice', 'sms')),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (client_id, month, channel)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS overage_processed_item")
