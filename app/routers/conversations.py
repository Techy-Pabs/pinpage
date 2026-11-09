import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.dependencies import get_current_user, get_db
from app.models import Conversation, Document, User
from app.models.document import STATUS_READY
from app.routers.documents import document_not_ready, visible_document
from app.schemas.conversation import ConversationDetailResponse, ConversationResponse
from app.services import conversations as convs

router = APIRouter(tags=["conversations"])


def conversation_not_found() -> ApiError:
    # The same answer for a missing conversation and someone else's
    return ApiError(status.HTTP_404_NOT_FOUND, "conversation_not_found", "Conversation not found.")


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

