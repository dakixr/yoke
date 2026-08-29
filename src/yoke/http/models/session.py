"""Session, message, and tree transport models."""

from __future__ import annotations

from typing import Annotated
from typing import Literal

from pydantic import Field

from yoke.http.models.common import ApiModel
from yoke.http.models.common import CursorInfo
from yoke.http.models.common import LocationInfo


class SessionSelection(ApiModel):
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class SessionSelectionRequest(ApiModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str | None = None


class SessionSelectionResult(ApiModel):
    effective: SessionSelection
    applies: Literal["immediately"] = "immediately"


class SessionSelectionResponse(ApiModel):
    data: SessionSelectionResult


class SessionCompactionRequest(ApiModel):
    reason: Literal["manual"] = "manual"


class SessionCompactionData(ApiModel):
    accepted: bool = True
    operation_id: str


class SessionCompactionResponse(ApiModel):
    data: SessionCompactionData


class SessionTime(ApiModel):
    created: str | None = None
    updated: str | None = None


class SessionTreeSummary(ApiModel):
    leaf_id: str | None = None
    entry_count: int = 0


class SessionQueueSummary(ApiModel):
    total: int = 0
    steering: int = 0
    queued: int = 0
    paused: int = 0
    revision: int = 0


class SessionInfo(ApiModel):
    id: str
    title: str | None = None
    pinned: bool = False
    archived_at: str | None = None
    location: LocationInfo
    time: SessionTime
    selection: SessionSelection
    context_usage: dict[str, object] | None = None
    tree: SessionTreeSummary
    queue: SessionQueueSummary


class SessionResponse(ApiModel):
    data: SessionInfo


class SessionListResponse(ApiModel):
    data: list[SessionInfo]
    cursor: CursorInfo
    total: int


class SessionCreateSelection(ApiModel):
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class SessionCreateRequest(ApiModel):
    id: str | None = None
    location: LocationInfo
    title: str | None = None
    selection: SessionCreateSelection | None = None


class SessionPatchRequest(ApiModel):
    title: str | None = None
    pinned: bool | None = None
    archived: bool | None = None


class SessionForkRequest(ApiModel):
    id: str | None = None
    title: str | None = None
    from_entry_id: str | None = None


class ActiveRuntimeInfo(ApiModel):
    state: Literal["idle", "running", "stopping", "waiting_input", "error"]
    turn_id: int | None = None
    started_at: str | None = None
    activity: str | None = None


class SessionActiveResponse(ApiModel):
    data: dict[str, ActiveRuntimeInfo]


class InterruptData(ApiModel):
    interrupted: bool
    turn_id: int | None = None


class InterruptResponse(ApiModel):
    data: InterruptData


class WaitResponse(ApiModel):
    data: ActiveRuntimeInfo


class TextContent(ApiModel):
    type: Literal["text"] = "text"
    text: str


class ImageContent(ApiModel):
    type: Literal["image"] = "image"
    name: str
    uri: str | None = None


type ProjectedContent = Annotated[
    TextContent | ImageContent,
    Field(discriminator="type"),
]


class ToolCallSummary(ApiModel):
    id: str
    name: str
    arguments: str


class TurnSummaryInfo(ApiModel):
    duration_seconds: float
    tool_count: int


class ProjectedMessageBase(ApiModel):
    id: str
    time_created: str
    kind: str
    turn_summary: TurnSummaryInfo | None = None


class UserProjectedMessage(ProjectedMessageBase):
    type: Literal["user"] = "user"
    input_id: str | None = None
    content: list[ProjectedContent] = Field(default_factory=list)


class AssistantProjectedMessage(ProjectedMessageBase):
    type: Literal["assistant"] = "assistant"
    phase: Literal["commentary", "final_answer"] | None = None
    content: list[ProjectedContent] = Field(default_factory=list)
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)


class ToolProjectedMessage(ProjectedMessageBase):
    type: Literal["tool"] = "tool"
    call_id: str | None = None
    status: Literal["completed"] = "completed"
    result: str | None = None


class ControlProjectedMessage(ProjectedMessageBase):
    type: Literal["control"] = "control"
    control: str
    text: str | None = None


type ProjectedMessage = Annotated[
    UserProjectedMessage
    | AssistantProjectedMessage
    | ToolProjectedMessage
    | ControlProjectedMessage,
    Field(discriminator="type"),
]


class MessageListResponse(ApiModel):
    data: list[ProjectedMessage]
    cursor: CursorInfo
    snapshot_seq: int = 0


class MessageResponse(ApiModel):
    data: ProjectedMessage


class ContextMessage(ApiModel):
    role: str
    content: list[ProjectedContent] = Field(default_factory=list)
    tool_call_id: str | None = None
    phase: Literal["commentary", "final_answer"] | None = None


class ContextData(ApiModel):
    messages: list[ContextMessage]
    total_entries: int = 0
    retained_entries: int = 0
    retained_chars: int = 0
    max_chars: int = 0
    truncated: bool = False


class ContextResponse(ApiModel):
    data: ContextData


class TreeEntryInfo(ApiModel):
    id: str
    parent_id: str | None = None
    kind: str
    created_at: str
    label: str | None = None
    active: bool = False
    current: bool = False
    preview: str | None = None
    child_count: int = 0


class TreeData(ApiModel):
    revision: int
    leaf_id: str | None = None
    entries: list[TreeEntryInfo]
    total_entries: int = 0
    cursor: CursorInfo = Field(default_factory=CursorInfo)


class TreeResponse(ApiModel):
    data: TreeData


class TreeNavigationAbandonedEntry(ApiModel):
    id: str
    kind: str
    preview: str | None = None


class TreeNavigationPreviewData(ApiModel):
    target_id: str
    current: bool
    editor_text: str | None = None
    abandoned: list[TreeNavigationAbandonedEntry] = Field(default_factory=list)
    abandoned_total: int = 0
    abandoned_truncated: bool = False


class TreeNavigationPreviewResponse(ApiModel):
    data: TreeNavigationPreviewData


class TreeNavigateRequest(ApiModel):
    expected_revision: int = Field(ge=0)
    target_id: str
    branch_summary: str | None = None


class TreeNavigateData(ApiModel):
    revision: int
    leaf_id: str | None = None
    editor_text: str | None = None
    summary_added: bool = False


class TreeNavigateResponse(ApiModel):
    data: TreeNavigateData


class TreeEntryPatchRequest(ApiModel):
    expected_revision: int = Field(ge=0)
    label: str | None = None


class TreeEntryPatchData(ApiModel):
    revision: int
    entry: TreeEntryInfo


class TreeEntryPatchResponse(ApiModel):
    data: TreeEntryPatchData
