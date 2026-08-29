"""Workspace location transport models."""

from __future__ import annotations

from yoke.http.models.common import ApiModel
from yoke.http.models.common import LocationInfo


class GitLocationInfo(ApiModel):
    root: str
    branch: str | None = None


class ResolvedLocation(ApiModel):
    directory: str
    name: str
    git: GitLocationInfo | None = None


class LocationResponse(ApiModel):
    data: ResolvedLocation


class RecentLocationsResponse(ApiModel):
    data: list[LocationInfo]


class LocationBrowseEntry(ApiModel):
    name: str
    directory: str


class LocationBrowseResult(ApiModel):
    browse_directory: str
    parent_directory: str | None
    selectable_directory: str | None
    separator: str
    entries: list[LocationBrowseEntry]


class LocationBrowseResponse(ApiModel):
    data: LocationBrowseResult
