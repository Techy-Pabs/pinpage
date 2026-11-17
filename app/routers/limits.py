from fastapi import APIRouter, Depends

from app.core.config import settings
from app.dependencies import get_current_user
from app.schemas.limits import LimitsResponse

router = APIRouter(tags=["usage"])


@router.get("/limits", response_model=LimitsResponse, summary="The upload and question limits")
def limits(_=Depends(get_current_user)):
    """The limits this server applies, read from its settings, so the
    frontend can check a file before sending it and say what the limits
    are. The server still checks everything itself."""
    return LimitsResponse(
        max_upload_mb=settings.max_upload_mb,
        max_pages=settings.max_pages,
        max_documents_per_workspace=settings.max_documents_per_workspace,
        questions_per_day=settings.daily_questions_per_user,
    )
