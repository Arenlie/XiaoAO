from enum import StrEnum


class EntityType(StrEnum):
    SPACE = "space"
    EQUIPMENT = "equipment"
    POINT = "point"


class RequiredEntityLevel(StrEnum):
    ANY = "any"
    SPACE = "space"
    AREA = "area"
    LINE = "line"
    EQUIPMENT = "equipment"
    POINT = "point"


class ResolveStatus(StrEnum):
    RESOLVED = "RESOLVED"
    NEEDS_DISAMBIGUATION = "NEEDS_DISAMBIGUATION"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    NO_LOOKUP = "NO_LOOKUP"
    RESOLVED_COLLECTION = "RESOLVED_COLLECTION"
    DATABASE_ERROR = "DATABASE_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
