"""org owner role — a head account per group

Adds 'owner' above 'manager' in org_members.role.

Why a third role rather than reusing manager: someone has to be un-removable, or a
group can lock itself out. Two managers can each remove the other; whoever clicks
first wins and the loser loses access to their own stores. An owner cannot be
removed by a manager, and the last owner cannot be removed at all.

Backfill: the earliest whole-group manager in each org becomes its owner. That is
the account that has been overseeing the group longest, which is the closest thing
to a head account the old model recorded. Orgs with no whole-group manager are left
without an owner — a platform admin assigns one — rather than promoting a viewer,
because promoting a read-only account to un-removable is not a safe guess.

Down-migration demotes owners back to manager, which is lossless: manager was the
top role before this.

Revision ID: 0014_org_owner_role
Revises: 0013_org_price_overrides
"""
from alembic import op

revision = "0014_org_owner_role"
down_revision = "0013_org_price_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # role is a free-text column with a default, not an enum, so nothing to alter
    # structurally — but the pending-invite table carries a role too and both must
    # accept the new value.
    op.execute(
        """
        WITH first_manager AS (
            SELECT DISTINCT ON (org_id) org_id, clerk_user_id
            FROM org_members
            WHERE role = 'manager' AND tenant_id IS NULL
            ORDER BY org_id, created_at ASC, clerk_user_id ASC
        )
        UPDATE org_members m
        SET role = 'owner'
        FROM first_manager f
        WHERE m.org_id = f.org_id
          AND m.clerk_user_id = f.clerk_user_id
          AND m.tenant_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("UPDATE org_members SET role = 'manager' WHERE role = 'owner'")
    op.execute("UPDATE org_invites SET role = 'manager' WHERE role = 'owner'")
