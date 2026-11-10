"""add section kind

Revision ID: 0b6d3e9f1a57
Revises: f4a9c1d6e2b8
Create Date: 2026-11-10 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0b6d3e9f1a57'
down_revision: Union[str, Sequence[str], None] = 'f4a9c1d6e2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The server default sets every existing section to body
    op.add_column('document_sections', sa.Column('kind', sa.String(length=20), server_default='body', nullable=False))
    op.create_check_constraint(op.f('ck_document_sections_kind_valid'), 'document_sections', "kind IN ('body', 'front_matter')")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('ck_document_sections_kind_valid'), 'document_sections', type_='check')
    op.drop_column('document_sections', 'kind')
