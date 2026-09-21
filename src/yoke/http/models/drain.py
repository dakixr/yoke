"""Physical worker completion response."""

from pydantic import Field

from yoke.http.models.common import ApiModel


class DrainData(ApiModel):
    drained: bool
    active_workers: int = Field(ge=0)


class DrainResponse(ApiModel):
    data: DrainData
