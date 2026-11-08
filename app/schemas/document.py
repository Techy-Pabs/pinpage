import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentResponse(BaseModel):
    # Only these fields are ever sent back: never storage_key, sha256 or
    # workspace ids, even though the Document model has them
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    original_filename: str
    size_bytes: int
    page_count: int | None
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    processed_at: datetime | None


class PageResponse(BaseModel):
    page_number: int  # starts at 1
    page_count: int  # pages in the whole document, for moving between pages
    text: str
    char_count: int


class FileLinkResponse(BaseModel):
    url: str  # a signed link to the original PDF; anyone holding it can open it until it expires
    expires_in: int  # seconds
