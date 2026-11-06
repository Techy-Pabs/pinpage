from sqlalchemy.orm import Session

from app.models import User, Workspace, WorkspaceMember
from app.models.workspace import KIND_PERSONAL, PERSONAL_WORKSPACE_NAME, ROLE_OWNER


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
