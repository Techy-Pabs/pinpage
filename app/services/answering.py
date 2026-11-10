"""The parts of answering a question that touch the database: the daily
limit, the history sent to the chat model, and saving the pair."""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Conversation, Message, QuestionUsage, User
from app.models.conversation import DEFAULT_TITLE, ROLE_ASSISTANT, ROLE_USER, TITLE_LENGTH
from app.services.ai import ChatMessage
from app.services.citations import strip_markers

# The last 3 question and answer pairs go to the chat model with a question
HISTORY_PAIRS = 3


def today_utc() -> date:
    return datetime.now(UTC).date()


def questions_today(db: Session, user_id: uuid.UUID) -> int:
    """The user's questions today (UTC), from question_usage: deleting
    conversations or documents never lowers it."""
    count = db.scalar(
        select(QuestionUsage.count).where(QuestionUsage.user_id == user_id, QuestionUsage.used_on == today_utc())
    )
    return count or 0


def count_question(db: Session, user_id: uuid.UUID) -> None:
    """One more question today, in the caller's transaction (the one that
    saves the question and answer), under lock_user."""
    db.execute(
        insert(QuestionUsage)
        .values(user_id=user_id, used_on=today_utc(), count=1)
        .on_conflict_do_update(
            index_elements=[QuestionUsage.user_id, QuestionUsage.used_on],
            set_={"count": QuestionUsage.count + 1},
        )
    )


def lock_user(db: Session, user_id: uuid.UUID) -> None:
    """Holds the user's row until commit, so two questions saved at once
    can't both pass the final daily limit check and count."""
    db.execute(select(User.id).where(User.id == user_id).with_for_update())


def recent_history(db: Session, conversation_id: uuid.UUID) -> list[ChatMessage]:
    """The last HISTORY_PAIRS questions and answers, oldest first. Answers
    lose their markers: those pointed at that question's excerpts."""
    recent = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(HISTORY_PAIRS * 2)
    ).all()
    return [
        {"role": m.role, "content": strip_markers(m.content) if m.role == ROLE_ASSISTANT else m.content}
        for m in reversed(recent)
    ]


def title_from(question: str) -> str:
    return " ".join(question.split())[:TITLE_LENGTH].rstrip()


def save_pair(
    db: Session,
    conversation: Conversation,
    question: str,
    answer: str,
    outcome: str,
    citations: list[dict],
    *,
    mode: str = "question",
    retrieval_query: str | None = None,
) -> Message:
    """The question and its answer, in the caller's transaction. Separate
    INSERTs, so clock_timestamp() gives the question the earlier time."""
    first_question = conversation.title == DEFAULT_TITLE and not db.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )
    db.add(Message(conversation_id=conversation.id, role=ROLE_USER, content=question))
    db.flush()
    reply = Message(
        conversation_id=conversation.id, role=ROLE_ASSISTANT, content=answer, outcome=outcome,
        citations=citations, mode=mode, retrieval_query=retrieval_query,
    )
    db.add(reply)
    db.flush()
    if first_question:
        conversation.title = title_from(question)
    conversation.updated_at = func.now()
    return reply
