# Import every model here so they all register on Base.metadata.
# Alembic imports this package to see the full set of tables.
from app.models.chunk import DocumentChunk, DocumentSection
from app.models.conversation import Conversation, Message, QuestionUsage
from app.models.document import Document, DocumentPage
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

__all__ = [
    "Conversation", "Document", "DocumentChunk", "DocumentPage", "DocumentSection", "Message", "QuestionUsage", "User",
    "Workspace", "WorkspaceMember",
]
