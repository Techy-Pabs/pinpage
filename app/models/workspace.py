import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

KIND_PERSONAL = "personal"
KIND_ORGANISATION = "organisation"  # arrives in Milestone 7

ROLE_OWNER = "owner"
ROLE_MEMBER = "member"  # arrives in Milestone 7

PERSONAL_WORKSPACE_NAME = "Personal"


class Workspace(Base):
    """Documents belong to a workspace; people reach them through a membership."""

    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("kind IN ('personal', 'organisation')", name="kind_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkspaceMember(Base):
    """One person in one workspace. The pair is the primary key, so nobody
    can be a member of the same workspace twice."""

    __tablename__ = "workspace_members"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'member')", name="role_valid"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    # Indexed on its own: the primary key starts with workspace_id, so it
    # can't answer "which workspaces is this user in?", which every
    # documents query will ask
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
