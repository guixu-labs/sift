"""审计判官共享的具名输入，避免内部调用传递过长的参数列表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, NamedTuple

from sift.utils import TokenUsageAccumulator

ProgressFn = Callable[[str], None]


class JudgeOptions(NamedTuple):
    """判官共用的模型、进度、用量和定位配置。"""

    model: str | None
    timeout: int
    on_progress: ProgressFn | None
    usage: TokenUsageAccumulator
    locate_turns: bool


class JudgeTranscript(NamedTuple):
    """单份执行记录提供给各类判官的证据。"""

    name: str = ""
    text: str = ""
    tool_trace: list | None = None
    scene: str = ""
    dispatch_returns: list | None = None
    aux_trace: list | None = None


class JudgeCallOptions(NamedTuple):
    """单类判官的调用配置；用量回调已由上层限定统计范围。"""

    model: str | None = None
    timeout: int = 600
    on_progress: ProgressFn | None = None
    on_usage: Callable[[dict], None] | None = None
    locate_turns: bool = False


class JudgeControlFlow(NamedTuple):
    """跨判官共用的顺序约束和数量约束。"""

    order_pairs: list
    quantity_specs: list


class JudgeContext(NamedTuple):
    """一个 transcript 的判定输入，供调度和失败兜底共用。"""

    options: JudgeOptions
    transcript: JudgeTranscript
    control: JudgeControlFlow
    buckets: dict


class FindingContext(NamedTuple):
    """单条判定写回报告时共用的 transcript 定位信息。"""

    transcript_name: str
    locate_turns: bool
    max_turn: int


class JudgePromptContext(NamedTuple):
    """生成单批 prompt 所需的执行证据和定位开关。"""

    text: str
    locate_turns: bool = False
    tool_trace: list | None = None
    dispatch_pairs: list | None = None
    aux_tool_trace: list | None = None


class BatchJudgeOptions(NamedTuple):
    """批量判定的模型、用量回调和结果格式。"""

    model: str | None
    timeout: int
    on_progress: ProgressFn | None
    on_usage: Callable[[dict], None] | None = None
    label: str = ""
    schema: dict | None = None
    transport: str = "cc"


class AuditFeatures(NamedTuple):
    """可选的指令准备功能。"""

    refine_instructions: bool
    generate_headline: bool


@dataclass
class AuditCollected:
    """单次审计收集的结果和阶段状态，不跨调用共享。"""

    warnings: list[str]
    stage_seconds: dict[str, float]
    findings: list = field(default_factory=list)
    transcript_names: list[str] = field(default_factory=list)
    transcript_sources: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    boundary_traces: list = field(default_factory=list)


class AuditContext(NamedTuple):
    """静态声明、判官配置和本次审计的收集状态。"""

    instructions: list
    quantified_ids: set[int]
    control: JudgeControlFlow
    options: JudgeOptions
    collected: AuditCollected


class AuditFinishOptions(NamedTuple):
    """报告后处理需要的关联索引和本次审计设置。"""

    order_endpoints: dict[str, tuple[str, str]]
    declared_tools: dict[str, str] | None
    refine_dropped_count: int
    features: AuditFeatures
    wall_start: float
    # 对账时刻的声明正文快照(text_coverage 切分回指 + report.skill_body 落盘用)。
    # 默认空串向后兼容:eval 内 compliance 等旧调用方不传则文本覆盖视图全 0(不显)。
    skill_body: str = ""


class JudgeBatch(NamedTuple):
    """单批提示词、条目数和显示序号；不依赖线程开始或结束顺序。"""

    prompt: str
    count: int
    number: int
    total: int


class UnresolvedEvidence(NamedTuple):
    """判定失败的证据和诊断信息。"""

    text: str
    cause: str = "unmentioned"
    control_flow: str = ""


class AuditOptions(NamedTuple):
    """一次审计的模型配置、可选处理和记录显示信息。"""

    model: str | None = None
    timeout: int = 600
    on_progress: ProgressFn | None = None
    refine_instructions: bool = False
    generate_headline: bool = False
    locate_turns: bool = False
    declared_tools: dict[str, str] | None = None
    aux_tool_traces: list[list] | None = None
    display_names: list[str] | None = None
