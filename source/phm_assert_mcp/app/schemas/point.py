from pydantic import Field
from .common import StrictModel


class QueryPointsRequest(StrictModel):
    equip_no: str = Field(min_length=1)
    point_type: str | None = None
    keyword: str | None = None
    limit: int = Field(default=1000, ge=1, le=10000)
