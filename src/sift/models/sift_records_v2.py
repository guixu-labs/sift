"""Pydantic schema for the external ``sift-records`` v2 evidence bundle.

These models describe the raw producer-facing protocol. They are deliberately
separate from the internal canonical cc-record dictionaries and from the
runtime ``SessionDigest`` models. Schema and cross-object evidence validation
happen at this boundary; conversion to canonical records remains a reader
responsibility.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SiftRecordsV2Blob(BaseModel):
    """One content-addressed before/after snapshot."""

    model_config = ConfigDict(strict=True, extra="allow")

    encoding: Literal["utf-8", "base64"]
    content: str
    size: int | None = None


class SiftRecordsV2FileEffect(BaseModel):
    """A single file mutation belonging to a capture."""

    model_config = ConfigDict(strict=True, extra="allow")

    path: str = Field(min_length=1)
    before_blob: str | None = None
    after_blob: str | None = None
    additions: int = 0
    deletions: int = 0
    conflict: bool = False
    evidence_ref: str | None = None
    # Reader-derived fields used when the validated capture is materialized as
    # evidence for the canonical record. They remain optional in the raw wire
    # schema and are never required from producers.
    before_content: str | None = None
    after_content: str | None = None
    before_binary: bool = False
    after_binary: bool = False
    before_size: int | None = None
    after_size: int | None = None


class SiftRecordsV2NativeSession(BaseModel):
    """Native runtime session and its optional parent relationship."""

    model_config = ConfigDict(strict=True, extra="allow")

    id: str
    agent: str | None = None
    parent_id: str | None = None


class SiftRecordsV2Session(BaseModel):
    """Bundle-level session metadata."""

    model_config = ConfigDict(strict=True, extra="allow")

    id: str | None = None
    native_sessions: list[SiftRecordsV2NativeSession] = Field(default_factory=list)


class SiftRecordsV2ToolCall(BaseModel):
    """Producer-facing tool call embedded in a turn record."""

    model_config = ConfigDict(strict=True, extra="allow")

    id: str | None = None
    name: str
    input: dict[str, Any] | None = None
    timestamp: str | None = None
    result: Any = None
    is_error: bool | None = None
    capture_id: str | None = None


class SiftRecordsV2Record(BaseModel):
    """One user/assistant turn in the producer's main trajectory."""

    model_config = ConfigDict(strict=True, extra="allow")

    role: str
    text: str | None = None
    tool_calls: list[SiftRecordsV2ToolCall] = Field(default_factory=list)
    agent: str | None = None
    parent_tool_use_id: str | None = None
    session_id: str | None = None
    native_session_id: str | None = None
    parent_native_session_id: str | None = None
    event_id: str | None = None
    iteration: int | None = None
    timestamp: str | None = None


class SiftRecordsV2Capture(BaseModel):
    """Tool lifecycle and file effects captured by the producer."""

    model_config = ConfigDict(strict=True, extra="allow")

    capture_id: str | None = None
    quality: Literal["observed", "incomplete", "conflicted", "ambiguous"]
    actor_kind: str | None = None
    actor: str | None = None
    agent_name: str | None = None
    agent: str | None = None
    native_agent_id: str | None = None
    iteration: int | None = None
    parent_agent: str | None = None
    association_quality: Literal["exact", "ambiguous", "unmatched"] | None = None
    session_id: str | None = None
    native_session_id: str | None = None
    parent_native_session_id: str | None = None
    parent_tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: Any = None
    tool_output: Any = None
    tool_use_id: str | None = None
    runtime: str | None = None
    model: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    succeeded: bool | None = None
    is_error: bool | None = None
    incomplete: bool = False
    conflicted: bool = False
    conflicts: list[Any] = Field(default_factory=list)
    mutation_reasons: list[str] | str = Field(default_factory=list)
    cwd: str | None = None
    head_before: str | None = None
    head_after: str | None = None
    committed_sha: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    effects: list[SiftRecordsV2FileEffect] = Field(default_factory=list)


class SiftRecordsV2(BaseModel):
    """Raw ``sift-records`` version 2 evidence bundle.

    ``strict=True`` prevents accidental coercion such as ``"2"`` to ``2`` or
    ``"1"`` to ``1``. ``extra="allow"`` keeps producer extensions available.
    Cross-object references and content-addressed blob integrity are validated
    here as part of the protocol boundary; conversion to canonical records
    remains a reader responsibility.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    format: Literal["sift-records"]
    version: Literal[2]
    producer: str | None = None
    exported_at: str | None = None
    session: SiftRecordsV2Session = Field(default_factory=SiftRecordsV2Session)
    records: list[SiftRecordsV2Record]
    captures: dict[str, SiftRecordsV2Capture]
    blobs: dict[str, SiftRecordsV2Blob]
    coverage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references_and_blobs(self) -> "SiftRecordsV2":
        """Reject broken capture references and corrupted content-addressed blobs."""
        for capture_id, capture in self.captures.items():
            if capture.capture_id not in (None, capture_id):
                raise ValueError(
                    f"capture key 与 capture_id 不一致: {capture_id}"
                )
            for effect in capture.effects:
                for field_name in ("before_blob", "after_blob"):
                    blob_ref = getattr(effect, field_name)
                    if blob_ref is not None and blob_ref not in self.blobs:
                        raise ValueError(
                            f"capture {capture_id} 引用了缺失 blob {blob_ref}"
                        )

        referenced_capture_ids = {
            tool_call.capture_id
            for record in self.records
            for tool_call in record.tool_calls
            if tool_call.capture_id is not None
        }
        missing_captures = sorted(referenced_capture_ids - self.captures.keys())
        if missing_captures:
            raise ValueError(
                "tool call 引用了缺失 capture " + ", ".join(missing_captures)
            )

        for digest, blob in self.blobs.items():
            try:
                raw = (
                    blob.content.encode("utf-8")
                    if blob.encoding == "utf-8"
                    else base64.b64decode(blob.content, validate=True)
                )
            except (ValueError, TypeError) as exc:
                raise ValueError(f"blob {digest} 内容或编码无效") from exc
            expected = digest.removeprefix("sha256:")
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError(f"blob {digest} SHA-256 校验失败")
            if blob.size is not None and blob.size != len(raw):
                raise ValueError(f"blob {digest} size 不匹配")
        return self
