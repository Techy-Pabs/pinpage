"""add documents

Revision ID: 7c2e9a41d5b3
Revises: 0da6b09ad8ef
Create Date: 2026-11-08 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c2e9a41d5b3'
down_revision: Union[str, Sequence[str], None] = '0da6b09ad8ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('documents',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('uploaded_by', sa.Uuid(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('storage_key', sa.String(length=255), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('page_count', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error_code', sa.String(length=50), nullable=True),
    sa.Column('error_message', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('processing', 'ready', 'failed')", name=op.f('ck_documents_status_valid')),
    sa.CheckConstraint('size_bytes > 0', name=op.f('ck_documents_size_positive')),
    sa.CheckConstraint('page_count IS NULL OR page_count > 0', name=op.f('ck_documents_page_count_positive')),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f('ck_documents_sha256_hex')),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name=op.f('fk_documents_uploaded_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_documents_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents')),
    sa.UniqueConstraint('storage_key', name=op.f('uq_documents_storage_key')),
    sa.UniqueConstraint('workspace_id', 'sha256', name=op.f('uq_documents_workspace_id_sha256'))
    )
    op.create_index('ix_documents_workspace_id_created_at', 'documents', ['workspace_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_documents_workspace_id_created_at', table_name='documents')
    op.drop_table('documents')
