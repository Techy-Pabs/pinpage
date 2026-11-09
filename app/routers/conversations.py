import logging
import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ApiError
from app.dependencies import get_ai, get_current_user, get_db
from app.models import Conversation, Document, User
from app.models.conversation import OUTCOME_ANSWERED, OUTCOME_NOT_FOUND
from app.models.document import STATUS_READY
from app.routers.documents import document_not_ready, visible_document
from app.schemas.conversation import (
    ConversationDetailResponse,
    ConversationResponse,
    MessageResponse,
    QuestionRequest,
)
from app.services import answering
from app.services import conversations as convs
from app.services.ai import AIClient, AIServiceError
from app.services.citations import cite
from app.services.prompts import NOT_FOUND_MESSAGE, build_grounding_prompt
from app.services.retrieval import similar_chunks

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
    message. Answers 201 with the answer; the question and answer are saved
    together. 429 daily_limit_reached after DAILY_QUESTIONS_PER_USER
    questions in a day (UTC); 503 ai_unavailable, with nothing saved, if
    the AI can't answer."""
    question = body.content

    # 1. The daily limit
    if answering.questions_today(db, current_user.id) >= settings.daily_questions_per_user:
        raise daily_limit_reached()
    document = db.get(Document, conversation.document_id)  # reachable: checked by visible_conversation
    if document.status != STATUS_READY:
        raise document_not_ready()
    history = answering.recent_history(db, conversation.id)
    db.commit()  # no transaction stays open while the AI works

    try:
        # 3. Embed the question
        [vector] = ai.embed_batch([question], "query")
        # 4. Chunks of this document only, through the membership query
        chunks = similar_chunks(
            db, current_user.id, document.id, vector,
            top_k=settings.retrieval_top_k, min_similarity=settings.retrieval_min_similarity,
        )
        db.commit()
        if not chunks:
            # 5. Nothing close enough: no chat request at all
            answer, outcome, citations = NOT_FOUND_MESSAGE, OUTCOME_NOT_FOUND, []
        else:
            # 6. The grounding prompt with numbered excerpts, the recent history, the question
            reply = ai.generate_reply(
                build_grounding_prompt(document.title, chunks),
                [*history, {"role": "user", "content": question}],
            )
            # 7. Only valid markers become citations; none means not found
            cited = cite(reply, chunks)
            if cited.citations:
                answer, outcome, citations = cited.text, OUTCOME_ANSWERED, cited.citations
            else:
                answer, outcome, citations = NOT_FOUND_MESSAGE, OUTCOME_NOT_FOUND, []
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
    reply_message = answering.save_pair(db, locked, question, answer, outcome, citations)
    db.commit()
    db.refresh(reply_message)
    # Ids and outcomes only, never the question or the answer
    logger.info(
        "Answered in conversation %s: %s with %d citation(s)", conversation.id, outcome, len(citations)
    )
    return reply_message
