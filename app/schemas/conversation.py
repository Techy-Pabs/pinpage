import uuid
from datetime import datetime

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

MAX_QUESTION_LENGTH = 2000


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    title: str  # "New conversation" until the first question, then its first 80 characters
    created_at: datetime
    updated_at: datetime


class Citation(BaseModel):
    marker: str  # "S1": where the answer cites it, e.g. "... five days [S1]."
    page_start: int
    page_end: int
    section_number: str | None
    section_title: str
    snippet: str  # up to 200 characters from the cited text


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str  # user or assistant
    content: str
    outcome: str | None  # answered or not_found, for the assistant's answers
    citations: list[Citation] | None  # the assistant's sources; empty for not_found
    created_at: datetime


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse]  # oldest first


class QuestionRequest(BaseModel):
    # Spaces around it don't count; empty or over 2,000 characters gets 422
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_LENGTH)]
