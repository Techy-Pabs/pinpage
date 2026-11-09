"""add question usage

Revision ID: f4a9c1d6e2b8
Revises: e8b3f0a2c7d4
Create Date: 2026-11-09 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a9c1d6e2b8'
down_revision: Union[str, Sequence[str], None] = 'e8b3f0a2c7d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('question_usage',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('used_on', sa.Date(), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.CheckConstraint('count >= 0', name=op.f('ck_question_usage_count_not_negative')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_question_usage_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'used_on', name=op.f('pk_question_usage'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('question_usage')
