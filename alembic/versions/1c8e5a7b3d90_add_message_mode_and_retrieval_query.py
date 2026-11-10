"""add message mode and retrieval query

Revision ID: 1c8e5a7b3d90
Revises: 0b6d3e9f1a57
Create Date: 2026-11-10 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1c8e5a7b3d90'
down_revision: Union[str, Sequence[str], None] = '0b6d3e9f1a57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('messages', sa.Column('mode', sa.String(length=20), nullable=True))
    op.add_column('messages', sa.Column('retrieval_query', sa.Text(), nullable=True))
    # Every answer saved so far was an ordinary question
    op.execute("UPDATE messages SET mode = 'question' WHERE role = 'assistant'")
    op.create_check_constraint(
        op.f('ck_messages_mode_matches_role'),
        'messages',
        "(role = 'user' AND mode IS NULL AND retrieval_query IS NULL) OR "
        "(role = 'assistant' AND mode IS NOT NULL AND mode IN ('question', 'page', 'section'))",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('ck_messages_mode_matches_role'), 'messages', type_='check')
    op.drop_column('messages', 'retrieval_query')
    op.drop_column('messages', 'mode')
