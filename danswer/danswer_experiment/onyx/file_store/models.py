# Standard Library
import base64
from enum import Enum
from typing import NotRequired

# Third Party
from pydantic import BaseModel
from typing_extensions import TypedDict  # noreorder


class ChatFileType(str, Enum):
    # Image types only contain the binary data
    IMAGE = "image"
    # Doc types are saved as both the binary, and the parsed text
    DOC = "document"
    # Plain text only contain the text
    PLAIN_TEXT = "plain_text"
    CSV = "csv"


class FileDescriptor(TypedDict):
    """NOTE: is a `TypedDict` so it can be used as a type hint for a JSONB column
    in Postgres"""

    id: str
    type: ChatFileType
    name: NotRequired[str | None]


class InMemoryChatFile(BaseModel):
    file_id: str
    content: bytes
    file_type: ChatFileType
    filename: str | None = None

    def to_base64(self) -> str:
        if self.file_type == ChatFileType.IMAGE:
            return base64.b64encode(self.content).decode()
        else:
            raise RuntimeError(
                "Should not be trying to convert a non-image file to base64"
            )

    def to_file_descriptor(self) -> FileDescriptor:
        return {
            "id": str(self.file_id),
            "type": self.file_type,
            "name": self.filename,
        }
