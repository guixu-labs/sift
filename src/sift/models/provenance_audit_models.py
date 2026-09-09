"""Models for deterministic file-provenance behaviour analysis.

These findings are intentionally separate from :class:`AuditFinding`: they
describe properties of the evidence and execution, rather than whether a
Markdown instruction passed or failed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ProvenanceDimension = Literal[
    "evidence_health",
    "attribution_integrity",
    "file_churn",
    "actor_ownership",
    "verification_closure",
]
ProvenanceSeverity = Literal["info", "warning", "indeterminate"]
ProvenanceQuality = Literal["observed", "incomplete", "conflicted", "ambiguous"]
ProvenanceAssociation = Literal["exact", "ambiguous", "unmatched"]


class ProvenanceFinding(BaseModel):
    """One deterministic observation about the provenance timeline."""

    dimension: ProvenanceDimension
    severity: ProvenanceSeverity
    code: str
    summary: str
    explanation: str = ""
    paths: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    capture_ids: list[str] = Field(default_factory=list)
    tool_use_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    turn_refs: list[int] = Field(default_factory=list)


class ProvenanceTimelineEntry(BaseModel):
    """One compact, report-facing node in a per-file provenance timeline."""

    sequence: int = 0
    timestamp: str | None = None
    capture_id: str
    tool_use_id: str | None = None
    actor: str = ""
    tool_name: str = ""
    quality: ProvenanceQuality
    association_quality: ProvenanceAssociation
    trusted: bool = False
    evidence_ref: str
    before_blob: str | None = None
    after_blob: str | None = None
    additions: int = 0
    deletions: int = 0
    conflict: bool = False


class ProvenanceAuditResult(BaseModel):
    """Result of the local, non-LLM provenance analyser."""

    available: bool = False
    coverage: dict = Field(default_factory=dict)
    statistics: dict = Field(default_factory=dict)
    findings: list[ProvenanceFinding] = Field(default_factory=list)
    file_timelines: dict[str, list[ProvenanceTimelineEntry]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def get_count(self, name: str) -> int:
        """读取摘要计数；缺失或非负整数以外的值按零展示，保留原始统计。"""
        value = self.statistics.get(name)
        return value if type(value) is int and value >= 0 else 0
