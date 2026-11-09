"""add sections and chunks

Revision ID: c5d2e7f84a16
Revises: a3f1c8e6b902
Create Date: 2026-11-09 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d2e7f84a16'
down_revision: Union[str, Sequence[str], None] = 'a3f1c8e6b902'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # pgvector provides the halfvec type and the HNSW index below. Works
    # locally (the pgvector image) and on Neon.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table('document_sections',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=50), nullable=True),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('level', sa.Integer(), nullable=False),
    sa.Column('start_page', sa.Integer(), nullable=False),
    sa.Column('end_page', sa.Integer(), nullable=False),
    sa.CheckConstraint('position >= 0', name=op.f('ck_document_sections_position_not_negative')),
    sa.CheckConstraint('level > 0', name=op.f('ck_document_sections_level_positive')),
    sa.CheckConstraint('start_page > 0 AND end_page >= start_page', name=op.f('ck_document_sections_pages_valid')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_sections_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_sections')),
    sa.UniqueConstraint('document_id', 'position', name=op.f('uq_document_sections_document_id_position'))
    )
    op.create_table('document_chunks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('section_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('page_start', sa.Integer(), nullable=False),
    sa.Column('page_end', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    # 2048 = the embedding model's output size. Fixed here on purpose: a
    # model with a different size needs a new migration.
    sa.Column('embedding', pgvector.sqlalchemy.halfvec.HALFVEC(dim=2048), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('position >= 0', name=op.f('ck_document_chunks_position_not_negative')),
    sa.CheckConstraint('page_start > 0 AND page_end >= page_start', name=op.f('ck_document_chunks_pages_valid')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_chunks_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['section_id'], ['document_sections.id'], name=op.f('fk_document_chunks_section_id_document_sections'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_chunks')),
    sa.UniqueConstraint('document_id', 'position', name=op.f('uq_document_chunks_document_id_position'))
    )
    op.create_index(op.f('ix_document_chunks_document_id'), 'document_chunks', ['document_id'], unique=False)
    op.create_index('ix_document_chunks_embedding_hnsw', 'document_chunks', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'halfvec_cosine_ops'})


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_document_chunks_embedding_hnsw', table_name='document_chunks', postgresql_using='hnsw', postgresql_ops={'embedding': 'halfvec_cosine_ops'})
    op.drop_index(op.f('ix_document_chunks_document_id'), table_name='document_chunks')
    op.drop_table('document_chunks')
    op.drop_table('document_sections')
    op.execute("DROP EXTENSION IF EXISTS vector")
