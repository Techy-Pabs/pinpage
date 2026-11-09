import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

DEFAULT_TITLE = "New conversation"
TITLE_LENGTH = 80  # a conversation's title is the start of its first question

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

OUTCOME_ANSWERED = "answered"
OUTCOME_NOT_FOUND = "not_found"


class Conversation(Base):
    """One user's questions about one document. Private to user_id, even
    when workspaces are shared (Milestone 7). Deleted with its document."""

    __tablename__ = "conversations"
    __table_args__ = (
        # "Your conversations with this document, newest first", and the
        # daily question count, which starts from the user's conversations
        Index("ix_conversations_user_id_document_id_created_at", "user_id", "document_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200), default=DEFAULT_TITLE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    """A question (role user) or an answer (role assistant). Only answers
    have an outcome and citations. Deleted with its conversation."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role_valid"),
        CheckConstraint(
            "(role = 'user' AND outcome IS NULL AND citations IS NULL) OR "
            "(role = 'assistant' AND outcome IS NOT NULL AND outcome IN ('answered', 'not_found') "
            "AND citations IS NOT NULL)",
            name="outcome_matches_role",
        ),
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(20))
    # [{"marker", "page_start", "page_end", "section_number", "section_title", "snippet"}]
    # none_as_null: Python None is stored as SQL NULL, not the JSON value null,
    # so the check above can tell a question (no citations) from an answer
    citations: Mapped[list[dict] | None] = mapped_column(JSONB(none_as_null=True))
    # clock_timestamp(), not now(): a question and its answer are saved in one
    # transaction, where now() would give both the same time and lose their order
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class QuestionUsage(Base):
    """How many questions a user asked on one day (UTC), for the daily limit.

    Kept apart from the messages on purpose: deleting a conversation or a
    document deletes its messages, but never this count, so it can't reset
    the limit. Deleted only with the user.
    """

    __tablename__ = "question_usage"
    __table_args__ = (CheckConstraint("count >= 0", name="count_not_negative"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    used_on: Mapped[date] = mapped_column(Date, primary_key=True)  # the UTC date
    count: Mapped[int] = mapped_column(Integer)
