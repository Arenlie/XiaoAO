from __future__ import annotations

from enum import StrEnum


class ExecutionMode(StrEnum):
    QUICK = "quick"
    NORMAL = "normal"
    EXPERT = "expert"


class ConversationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"


class TitleSource(StrEnum):
    DEFAULT = "DEFAULT"
    AUTO_RULE = "AUTO_RULE"
    AUTO_LLM = "AUTO_LLM"
    MANUAL = "MANUAL"


class BranchForkType(StrEnum):
    ROOT = "ROOT"
    EDIT = "EDIT"
    REGENERATE = "REGENERATE"


class MessageRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"


class MessageStatus(StrEnum):
    PENDING = "PENDING"
    STREAMING = "STREAMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    DELETED = "DELETED"


class GenerationTaskStatus(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    STREAMING = "STREAMING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_SELECTION = "WAITING_SELECTION"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOPPED = "STOPPED"
    TIMEOUT = "TIMEOUT"


class OperationType(StrEnum):
    SEND = "SEND"
    EDIT = "EDIT"
    REGENERATE = "REGENERATE"


class EntityStatus(StrEnum):
    NO_LOOKUP = "NO_LOOKUP"
    UNIQUE = "UNIQUE"
    MULTIPLE = "MULTIPLE"
    COLLECTION = "COLLECTION"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"
