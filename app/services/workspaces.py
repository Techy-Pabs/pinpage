import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, Workspace, WorkspaceMember
from app.models.workspace import KIND_PERSONAL, PERSONAL_WORKSPACE_NAME, ROLE_OWNER


def get_personal_workspace(db: Session, user_id: uuid.UUID, *, lock: bool = False) -> Workspace | None:
    """The user's personal workspace, or None if they have none.

    The membership check is part of the query itself (a join on
    workspace_members), so a workspace can only come back for a member of it.
    lock=True takes a row lock (SELECT ... FOR UPDATE) on the workspace,
    held until the transaction ends: another upload to the same workspace
    waits here, so two uploads can't both pass the document limit.
    """
    query = (
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id, Workspace.kind == KIND_PERSONAL)
    )
    if lock:
        query = query.with_for_update(of=Workspace)
    return db.scalar(query)


def create_personal_workspace(db: Session, user: User) -> Workspace:
    """Add a personal workspace with the user as its owner.

    Never commits: the caller owns the transaction, so registration can save
    the user, the workspace and the membership together or not at all.
    """
    workspace = Workspace(name=PERSONAL_WORKSPACE_NAME, kind=KIND_PERSONAL)
    db.add(workspace)
    db.flush()  # sends the INSERT inside the open transaction, giving workspace.id
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=ROLE_OWNER))
    db.flush()
    return workspace
