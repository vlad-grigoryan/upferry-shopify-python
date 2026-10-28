"""Initial installation, job and webhook schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001_launch_kit"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shops", sa.Column("name", sa.String(255), primary_key=True), sa.Column("shop_id", sa.String(100)), sa.Column("token_ciphertext", sa.Text()), sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("jobs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("shop", sa.String(255), sa.ForeignKey("shops.name", ondelete="CASCADE"), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("lease", sa.String(36)), sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("available_at", sa.DateTime(timezone=True), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("result", sa.JSON()), sa.Column("error", sa.Text()))
    op.create_index("ix_jobs_shop", "jobs", ["shop"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_table("webhook_receipts", sa.Column("id", sa.String(64), primary_key=True), sa.Column("shop", sa.String(255), nullable=False), sa.Column("topic", sa.String(80), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_webhook_receipts_shop", "webhook_receipts", ["shop"])


def downgrade():
    op.drop_table("webhook_receipts")
    op.drop_table("jobs")
    op.drop_table("shops")
