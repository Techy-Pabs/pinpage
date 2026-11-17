from pydantic import BaseModel


class LimitsResponse(BaseModel):
    """The limits the frontend checks before sending, from the settings."""

    max_upload_mb: int  # MAX_UPLOAD_MB: the largest PDF, in MB (1 MB = 1024 * 1024 bytes)
    max_pages: int  # MAX_PAGES: a longer document fails with too_many_pages
    max_documents_per_workspace: int  # MAX_DOCUMENTS_PER_WORKSPACE: processing or ready ones
    questions_per_day: int  # DAILY_QUESTIONS_PER_USER, counted in UTC
