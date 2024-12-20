# Standard Library
from enum import Enum as PyEnum


class IndexingStatus(str, PyEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    CANCELED = "canceled"
    FAILED = "failed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"

    def is_terminal(self) -> bool:
        terminal_states = {
            IndexingStatus.SUCCESS,
            IndexingStatus.COMPLETED_WITH_ERRORS,
            IndexingStatus.CANCELED,
            IndexingStatus.FAILED,
        }
        return self in terminal_states


class IndexingMode(str, PyEnum):
    UPDATE = "update"
    REINDEX = "reindex"


# these may differ in the future, which is why we're okay with this duplication
class DeletionStatus(str, PyEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"


# Consistent with Celery task statuses
class TaskStatus(str, PyEnum):
    PENDING = "PENDING"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class IndexModelStatus(str, PyEnum):
    PAST = "PAST"
    PRESENT = "PRESENT"
    FUTURE = "FUTURE"


class ChatSessionSharedStatus(str, PyEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class ConnectorCredentialPairStatus(str, PyEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DELETING = "DELETING"

    def is_active(self) -> bool:
        return self == ConnectorCredentialPairStatus.ACTIVE


class AccessType(str, PyEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SYNC = "sync"
