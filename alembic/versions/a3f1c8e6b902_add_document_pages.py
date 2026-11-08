"""add document pages

Revision ID: a3f1c8e6b902
Revises: 7c2e9a41d5b3
Create Date: 2026-11-08 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f1c8e6b902'
down_revision: Union[str, Sequence[str], None] = '7c2e9a41d5b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('document_pages',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('page_number', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('char_count', sa.Integer(), nullable=False),
    sa.CheckConstraint('page_number > 0', name=op.f('ck_document_pages_page_number_positive')),
    sa.CheckConstraint('char_count >= 0', name=op.f('ck_document_pages_char_count_not_negative')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_pages_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_pages')),
    sa.UniqueConstraint('document_id', 'page_number', name=op.f('uq_document_pages_document_id_page_number'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('document_pages')
