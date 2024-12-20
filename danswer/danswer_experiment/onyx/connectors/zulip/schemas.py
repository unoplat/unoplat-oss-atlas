# Standard Library
from typing import Any, List, Optional

# Third Party
from pydantic import BaseModel, Field


class Message(BaseModel):
    id: int
    sender_id: int
    content: str
    recipient_id: int
    timestamp: int
    client: str
    is_me_message: bool
    sender_full_name: str
    sender_email: str
    sender_realm_str: str
    subject: str
    topic_links: Optional[List[Any]] = None
    last_edit_timestamp: Optional[int]
    edit_history: Any = None
    reactions: List[Any]
    submessages: List[Any]
    flags: List[str] = Field(default_factory=list)
    display_recipient: Optional[str] = None
    type: Optional[str] = None
    stream_id: int
    avatar_url: Optional[str]
    content_type: Optional[str]
    rendered_content: Optional[str] = None


class GetMessagesResponse(BaseModel):
    result: str
    msg: str
    found_anchor: Optional[bool] = None
    found_oldest: Optional[bool] = None
    found_newest: Optional[bool] = None
    history_limited: Optional[bool] = None
    anchor: Optional[str] = None
    messages: List[Message] = Field(default_factory=list)
