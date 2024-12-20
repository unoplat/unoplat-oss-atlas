# Third Party
from pydantic import BaseModel

# First Party
from onyx.auth.schemas import UserRole


class APIKeyArgs(BaseModel):
    name: str | None = None
    role: UserRole = UserRole.BASIC
