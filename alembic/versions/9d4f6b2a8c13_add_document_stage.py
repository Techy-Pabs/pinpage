"""add stage to documents

Revision ID: 9d4f6b2a8c13
Revises: 5e7a2c9d4b61
Create Date: 2026-11-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d4f6b2a8c13'
down_revision: Union[str, Sequence[str], None] = '5e7a2c9d4b61'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('documents', sa.Column('stage', sa.String(length=20), nullable=True))
    # A document already processing is shown at the first stage; it moves on
    # (or fails at startup if it is stuck) as processing goes
    op.execute("UPDATE documents SET stage = 'extracting' WHERE status = 'processing'")
    op.create_check_constraint(
        op.f('ck_documents_stage_valid'),
        'documents',
        "stage IS NULL OR (status = 'processing' AND stage IN ('extracting', 'finding_sections', 'embedding'))",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('ck_documents_stage_valid'), 'documents', type_='check')
    op.drop_column('documents', 'stage')
