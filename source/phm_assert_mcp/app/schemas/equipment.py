from pydantic import Field
from .common import StrictModel


class QueryDevicesRequest(StrictModel):
    space_id: str = Field(min_length=1)
    recursive: bool = True
    keyword: str | None = None
    limit: int = Field(default=1000, ge=1, le=10000)


class QueryEquipmentInfoRequest(StrictModel):
    equip_no: str = Field(min_length=1, max_length=128)
