from datetime import UTC, datetime, time, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dependencies import get_current_user, get_db
from app.models import User
from app.schemas.usage import UsageResponse
from app.services import answering

router = APIRouter(tags=["usage"])


@router.get("/usage", response_model=UsageResponse, summary="Your questions today")
def usage(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """How many questions you have asked today (UTC), out of
    DAILY_QUESTIONS_PER_USER, and when the count resets (the next midnight
    UTC). Read from the same count the daily limit uses, so deleting
    conversations or documents never lowers it. No AI request."""
    today = answering.today_utc()
    return UsageResponse(
        questions_used_today=answering.questions_today(db, current_user.id, today),  # the same day as resets_at
        questions_per_day=settings.daily_questions_per_user,
        resets_at=datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC),
    )
