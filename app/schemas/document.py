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
