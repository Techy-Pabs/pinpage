# Import every model here so they all register on Base.metadata.
# Alembic imports this package to see the full set of tables.
from app.models.document import Document, DocumentPage
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

__all__ = ["Document", "DocumentPage", "User", "Workspace", "WorkspaceMember"]
