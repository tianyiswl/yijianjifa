"""Add image posts without changing any video or publishing record."""
from alembic import op
import sqlalchemy as sa

revision = "0017_image_posts"
down_revision = "0016_publish_deliveries"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("image_posts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tags", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_image_posts_created_at", "image_posts", ["created_at"])
    op.create_table("image_post_images",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("post_id", sa.String(36), sa.ForeignKey("image_posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("thumbnail_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.UniqueConstraint("post_id", "position", name="uq_image_post_position"))
    op.create_index("ix_image_post_images_post_id", "image_post_images", ["post_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM image_posts LIMIT 1")).first():
        raise RuntimeError("Image posts exist; export them before removing this schema")
    op.drop_table("image_post_images")
    op.drop_table("image_posts")
