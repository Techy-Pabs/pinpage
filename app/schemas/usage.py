from datetime import datetime

from pydantic import BaseModel


class UsageResponse(BaseModel):
    questions_used_today: int
    questions_per_day: int
    # The next midnight UTC, when questions_used_today goes back to 0
    resets_at: datetime
