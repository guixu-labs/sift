"""gate 判决模型(块 C)。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AxisVerdict(BaseModel):
    """单轴判决。applicable=False = 此轴未介入(未请求/被关/无信号),不参与合并。"""

    verdict: str  # "PASS" | "FAIL" | "N/A"(applicable=False 不适用 / applicable=True 信号不足→最终 UNCLEAR)
    reason: str
    applicable: bool = True
    delta: float | None = None  # programmatic: candidate-relative (B is sign-inverted)
    raw_delta: float | None = None  # programmatic: original benchmark A-B delta
    win_rate: float | None = None  # comparison: candidate wins / decisive
    decisive: int | None = None  # comparison: raw A wins + raw B wins; ties excluded
    a_wins: int | None = None  # comparison: raw benchmark side A count
    b_wins: int | None = None  # comparison: raw benchmark side B count
    candidate_wins: int | None = None  # comparison: wins for the selected candidate
    ties: int | None = None
    failed_ids: list[int] | None = None  # must_pass 轴:未通过的 eval id(结构化);其它轴 None


class GateResult(BaseModel):
    decision: str  # "PASS" | "FAIL" | "N/A"(不适用) | "UNCLEAR"(判不准,信号不足)
    exit_code: int  # 0 | 1 | 2
    reason: str
    artifact: str = ""
    candidate: str = ""
    baseline: str = ""
    thresholds: dict[str, Any] = Field(default_factory=dict)
    axes: dict[str, AxisVerdict] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    calibration: dict[str, Any] | None = None  # soft warning 容器(分诊两侧 HIGH_SCORE_SUSPECT,不进 decision)
