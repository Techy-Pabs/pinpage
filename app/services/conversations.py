"""Conversations: every query here checks access inside the query itself."""

import uuid

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import Conversation, Document, Message, WorkspaceMember


def conversations_visible_to(user_id: uuid.UUID) -> Select[tuple[Conversation]]:
    """Conversations the user started, about documents they can still reach.

    Both conditions are part of the query: the conversation's user_id, and a
    join through its document to the user's workspace membership. Once
    workspaces are shared (Milestone 7), members share documents but never
    each other's conversations.
    """
    return (
        select(Conversation)
        .join(Document, Document.id == Conversation.document_id)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Document.workspace_id)
        .where(Conversation.user_id == user_id, WorkspaceMember.user_id == user_id)
    )


def find_visible_conversation(
    db: Session, user_id: uuid.UUID, conversation_id: uuid.UUID, *, lock: bool = False
) -> Conversation | None:
    query = conversations_visible_to(user_id).where(Conversation.id == conversation_id)
    if lock:
        query = query.with_for_update(of=Conversation)
    return db.scalar(query)


def list_conversations(db: Session, user_id: uuid.UUID, document_id: uuid.UUID) -> list[Conversation]:
    return db.scalars(
        conversations_visible_to(user_id)
        .where(Conversation.document_id == document_id)
        .order_by(Conversation.created_at.desc(), Conversation.id.desc())
    ).all()


def messages_of(db: Session, conversation_id: uuid.UUID) -> list[Message]:
    """Oldest first."""
    return db.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)
    ).all()
