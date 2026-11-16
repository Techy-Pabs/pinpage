"""add embedded_with_fake_ai to documents

Revision ID: 5e7a2c9d4b61
Revises: 1c8e5a7b3d90
Create Date: 2026-11-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e7a2c9d4b61'
down_revision: Union[str, Sequence[str], None] = '1c8e5a7b3d90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Every document embedded so far used the real AI
    op.add_column(
        'documents',
        sa.Column('embedded_with_fake_ai', sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'embedded_with_fake_ai')
