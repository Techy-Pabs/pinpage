import logging
import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ApiError
from app.dependencies import get_ai, get_current_user, get_db
from app.models import Conversation, Document, User
from app.models.document import STATUS_READY
from app.routers.documents import document_not_ready, visible_document
from app.schemas.conversation import (
    ConversationDetailResponse,
    ConversationResponse,
    MessageResponse,
    QuestionRequest,
)
from app.services import answering, modes
from app.services import conversations as convs
from app.services.ai import AIClient, AIServiceError
from app.services.requests import MODE_PAGE, MODE_SECTION, parse_request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversations"])


def conversation_not_found() -> ApiError:
    # The same answer for a missing conversation and someone else's
    return ApiError(status.HTTP_404_NOT_FOUND, "conversation_not_found", "Conversation not found.")


def daily_limit_reached() -> ApiError:
    return ApiError(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "daily_limit_reached",
        f"You've asked {settings.daily_questions_per_user} questions today, the daily limit. "
        "Try again tomorrow (the limit resets at midnight UTC).",
    )


def ai_unavailable() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "ai_unavailable",
        "Pinpage can't answer right now. Try again in a moment.",
    )


def visible_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Conversation:
    """The conversation in the path, if the current user started it and can
    still reach its document. A malformed id gets the same 404."""
    try:
        parsed = uuid.UUID(conversation_id)
    except ValueError:
        raise conversation_not_found() from None
    conversation = convs.find_visible_conversation(db, current_user.id, parsed)
    if conversation is None:
        raise conversation_not_found()
    return conversation


@router.post(
    "/documents/{document_id}/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a conversation",
)
def start_conversation(
    document: Document = Depends(visible_document),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Starts a conversation with a ready document (409 document_not_ready
    otherwise). Only you can see it."""
    if document.status != STATUS_READY:
        raise document_not_ready()
    conversation = Conversation(document_id=document.id, user_id=current_user.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get(
    "/documents/{document_id}/conversations",
    response_model=list[ConversationResponse],
    summary="List your conversations with a document",
)
def list_conversations(
    document: Document = Depends(visible_document),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Your conversations with this document, newest first. Never anyone else's."""
    return convs.list_conversations(db, current_user.id, document.id)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse, summary="Get a conversation")
def get_conversation(conversation: Conversation = Depends(visible_conversation), db: Session = Depends(get_db)):
    """One of your conversations with its messages, oldest first."""
    return ConversationDetailResponse(
        **ConversationResponse.model_validate(conversation).model_dump(),
        messages=convs.messages_of(db, conversation.id),
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a conversation",
)
def delete_conversation(conversation: Conversation = Depends(visible_conversation), db: Session = Depends(get_db)):
    """Deletes one of your conversations and its messages."""
    db.execute(delete(Conversation).where(Conversation.id == conversation.id))  # messages go with it (CASCADE)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ask a question",
)
def ask_question(
    body: QuestionRequest,
    conversation: Conversation = Depends(visible_conversation),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai: AIClient = Depends(get_ai),
):
    """Asks a question about the conversation's document (up to 2,000
    characters). Answers come only from that document and cite their
    sources as [S1] markers, each with its pages and section. If the
    document doesn't cover the question, the answer is the fixed not found
    message.

    The answer's mode says what kind it is:
    - page: the question names pages ("What does page 12 say?", "pages 10
      to 11", "p. 5"), up to MAX_PAGES_PER_QUESTION. Page numbers are the
      PDF's own page positions (page 1 is the first page of the file), as a
      PDF viewer shows them.
    - section: it names a section ("Summarise section 4", "section 4.6",
      "chapter 3"), or asks for a summary of one by title ("Give me an
      overview of probation"). Subsections are included.
    - question: anything else, answered from the most relevant excerpts.

    Answers 201 with the answer; the question and answer are saved together,
    and count as one question however many AI requests the answer used.
    429 daily_limit_reached after DAILY_QUESTIONS_PER_USER questions in a
    day (UTC); 503 ai_unavailable, with nothing saved, if the AI can't answer."""
    question = body.content

    # 1. The daily limit
    if answering.questions_today(db, current_user.id) >= settings.daily_questions_per_user:
        raise daily_limit_reached()
    document = db.get(Document, conversation.document_id)  # reachable: checked by visible_conversation
    if document.status != STATUS_READY:
        raise document_not_ready()
    history = answering.recent_history(db, conversation.id)
    sections = modes.section_infos(db, current_user.id, document.id)
    db.commit()  # no transaction stays open while the AI works

    # 2. What the question asks for: no AI request
    request = parse_request(question, document.page_count or 0, sections)

    try:
        if request.mode == MODE_PAGE:
            answer = modes.answer_pages(db, ai, current_user.id, document, request, history, question)
        elif request.mode == MODE_SECTION:
            answer = modes.answer_section(db, ai, current_user.id, document, request, sections, history, question)
        else:
            # 3 to 7. Embed, retrieve from this document only, answer with citations
            answer = modes.answer_question(db, ai, current_user.id, document, history, question, question)
    except AIServiceError:
        db.rollback()
        raise ai_unavailable() from None

    # 8. The question and answer together, in one transaction
    answering.lock_user(db, current_user.id)
    locked = convs.find_visible_conversation(db, current_user.id, conversation.id, lock=True)
    if locked is None:  # deleted, or its document became unreachable, meanwhile
        raise conversation_not_found()
    if answering.questions_today(db, current_user.id) >= settings.daily_questions_per_user:
        raise daily_limit_reached()  # another question got there first
    reply_message = answering.save_pair(
        db, locked, question, answer.content, answer.outcome, answer.citations,
        mode=answer.mode, retrieval_query=answer.retrieval_query,
    )
    answering.count_question(db, current_user.id)  # same transaction, still under the user lock
    db.commit()
    db.refresh(reply_message)
    # Ids and outcomes only, never the question or the answer
    logger.info(
        "Answered in conversation %s: %s %s with %d citation(s)",
        conversation.id, answer.mode, answer.outcome, len(answer.citations),
    )
    return reply_message
