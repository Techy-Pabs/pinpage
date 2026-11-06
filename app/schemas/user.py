import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserResponse(BaseModel):
    # from_attributes lets this be built straight from a User model object.
    # Only these fields are sent back, so the password hash never is.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    is_admin: bool
    created_at: datetime
