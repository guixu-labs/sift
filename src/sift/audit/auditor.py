"""auditor: transcript 对账的核心判定。

``TranscriptAuditor`` 把 skill 声明的指令逐条对照 transcript 的实测行为,
产出 ``AuditFinding``(五态:PASS / FAIL / N/A / UNRESOLVED / INDETERMINATE)。每条 finding
另带 ``method``(PROGRAMMATIC / LLM,怎么查的)。按指令类别分四条判定路径:

- **条件性**(``_judge_conditional``):TOOL_REQUIREMENT / REFERENCE_USAGE
  - 命中 tool_trace → PASS(method=PROGRAMMATIC,**不调 LLM**);
  - 未命中 → 批量交 LLM 判"场景是否该触发该指令"(method=LLM):
    - applicable=true  → FAIL(该做没做)
    - applicable=false → N/A(场景不涉及)
    - indeterminate=true(带 reason) → INDETERMINATE(判不准)
    - LLM 失败/异常/解析失败/未提及/indeterminate 无 reason → **UNRESOLVED**(没判成)
- **禁令**(``_judge_prohibition``):CONSTRAINT / ANTI_FABRICATION
  - 批量交 LLM **主动检测违规是否发生**(method=LLM):
    - violated=true  → FAIL(违规了)
    - violated=false → PASS(没违规)
    - indeterminate=true(带 evidence) → INDETERMINATE(判不准)
    - LLM 失败/异常/解析失败/未提及/畸形值/indeterminate 无 evidence → **UNRESOLVED**(没判成)
- **步骤**(``_judge_step``):HEADING_STEP
  - 批量交 LLM 判(method=LLM):pass→PASS / fail→FAIL / na→N/A / indeterminate→INDETERMINATE;
    未知值/异常/indeterminate 无 evidence → UNRESOLVED(没判成)
  - 空桶(0 条)→ 短路返回 [],不调 LLM(同 prohibition)
- **输出格式**(``_judge_output``):OUTPUT_FORMAT(PATTERN_MATCH)
  - 确定性子串判定(method=PROGRAMMATIC,**不调 LLM**):sample 任一行(strip 后)在 transcript
    命中 → PASS,否则 FAIL
- ``classify`` 把 ``InstructionSource`` 路由到对应 ``AuditCategory``,供聚合主入口用。

**没判成 vs 判不准**(本层核心区分):
- UNRESOLVED(没判成)= 操作性失败:LLM 没出判定(调用/解析失败/未提及/畸形值),**可重跑**救回。
- INDETERMINATE(判不准)= 语义模糊:LLM 看了证据、明确说"证据相互矛盾/缺失、判不了",
  **重跑无益**(只能人工复核),且强制带 indeterminate_reason(写明具体矛盾/缺失)。

设计要点(对照已删的 compliance.checker 旧实现,helper 现归 audit.tool_match):
- 复用 ``match_tool_call`` / ``extract_first_balanced``,不重写。
- OUTPUT 的子串判定照 ``_check_programmatic`` 的 PATTERN_MATCH 分支逻辑。
- LLM 批量 prompt + JSON 解析仿 ``_check_llm_batch`` / ``_parse_applicability``。
- **FAIL 必须站得住**(宁可少报不能乱报):只有 LLM 明确判"违规 / 该做没做"才报 FAIL;
  LLM 不可用 / 解析失败 / 畸形值 / 未提及 → **UNRESOLVED**(没判成),``audit()`` 据此汇总
  "建议重跑"警告。与 compliance 解析失败默认保留(不误剔可遵循项)方向不同:
  compliance 偏不漏(默认遵循),audit 偏不冤(LLM 判准才报 FAIL)。
- **判不准三道滥用闸**:① parser 强制 indeterminate 必须带具体 reason/evidence,空则降级 UNRESOLVED;
  ② audit() 统计 indeterminate 占比,超阈值告警;③ prompt 写明纪律(优先明确结论,
  indeterminate 须写具体矛盾证据,不许偷懒)。

聚合/路由主入口 ``audit``:按 ``classify`` 把指令分到上述四法、标 derived、汇总 summary。
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from sift.audit.declare import derive_declarations
from sift.audit.rebuild import rebuild
from sift.audit.control_flow import (
    OrderPair,
    QuantitySpec,
    context_text,
    detect_order_pairs,
    detect_quantity,
    order_instruction_text,
    quantity_op_label,
)
from sift.audit.tool_match import (
    count_tool_calls,
    extract_first_balanced,
    find_execution_hit,
    load_json_lenient,
    match_tool_call,
)
from sift.audit.aggregate import aggregate_by_instruction, aggregate_coverage
from sift.audit.boundary import aggregate_boundary
from sift.audit.dedup import annotate_conflicts, post_process_findings
from sift.audit import headline_cache, refine_cache
from sift.concurrency import (
    AdaptiveConcurrencyLimiter,
    _LLM_LIMITER,
    _LLM_SEMAPHORE,
    _MAX_BATCH_CONCURRENT,
    _RATE_LIMIT_BACKOFF,
    configure_adaptive,
    configure_max_concurrent,
)
from sift.audit.noise import annotate_aggregate_noise, annotate_noise
from sift.audit.provenance import analyze_provenance, has_provenance_evidence
from sift.compliance.extractor import InstructionExtractor
from sift.errors import SiftError
from sift.evals.opencode import transcript_source
from sift.execution.error_triage import classify_error
from sift.models import InstructionCheck, InstructionSource, ProvenanceAuditResult, ToolCallRecord
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditReport,
    AuditVerdict,
)
from sift.utils import TokenUsageAccumulator, _HttpRateLimited, call_claude

# 每批交 LLM 判定的指令数上限。**小于**已删 compliance.checker 的 30:audit 每条要 LLM 产出
# "verdict + 中文 evidence"(输出量大),且 LLM 延迟方差极大(实测 ~10 条/批 40-257s)。
# 30 条均值就 ~210s、慢尾更甚;10 条均值 ~117s、慢尾 ~257s,配 timeout=600 留足裕量
# (慢尾 257s + ~340s 裕量,几乎不超时)。批数虽多但每批绝对时长低,远比 30 条稳。
_MAX_LLM_IN_BATCH = 10
# 滥用闸 ②:判不准(indeterminate)占比超此阈值、且总条数 >= _MIN → audit() 告警。
# INDETERMINATE 是 LLM 的逃逸口(看不懂就能说"判不准"蒙混),需监控是否被滥用。
# 阈值宽松(20%)+ 最小样本(5):真模糊偶尔会有,只在显著滥用时告警,免误报。
_INDETERMINATE_ABUSE_RATE = 0.20
_INDETERMINATE_ABUSE_MIN_TOTAL = 5
# 判官 prompt 喂的执行记录/场景的最大字符预算。长 transcript(实测 skill/agent 切片
# 60-150KB)远超此值 → 必须截断。旧裸 ``[:_SCENE_TRUNCATE]`` 只留头 → 尾部(最终结果 /
# 总结 / 收尾清理)判官全盲,prohibition 类"段级缺席 / 未见收口"假 PASS 或弃判(实测
# ops-spec-gen / precision-debug 主噪声源之一)。``_window`` 改取头+尾各半 + 中段截断
# 标记:token 成本不变(窗口恒定)、覆盖从单端变双端。
_SCENE_TRUNCATE = 8000


def _window(text: str, limit: int = _SCENE_TRUNCATE) -> str:
    """长文本取头 + 尾窗口(各 ``limit/2``),中段用截断标记替代。

    ``text`` 长度 <= ``limit`` 原样返回;超长则保留头 ``limit//2`` + 尾 ``limit//2``,
    中间用一行 ``[…截断中段约 N 字…]`` 衔接,让判官同时看到早期上下文(任务 / 规划)与
    收尾(最终结果 / 总结)。是旧 ``text[:limit]`` 单端截断的替代——解决长切片尾部证据全盲。
    """
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n\n[…截断中段约 {omitted} 字…]\n\n{text[-half:]}"


def _tool_trace_section(tool_trace, locate_turns: bool) -> str:
    """tool_trace 的 prompt 段(工具调用是硬证据,勿只凭文字声称;canary 缺口修复)。

    step 判官与控制流判官共用——ORDER 判动作位置关系、数量判可数证据,都
    依赖工具调用记录(``_window`` 只留头尾,中段动作全盲)。locate_turns=True 时每条带
    ``[T{idx}]``(若 ToolCallRecord.turn 有标注)让 LLM 能对到具体 turn;False 时不带
    前缀、段标题也不提示 [T{idx}](整链关 turn 定位)。空 tool_trace → ""。
    """
    if not tool_trace:
        return ""
    # 元素支持 attr(ToolCallRecord)/ dict 双风格(同 match_tool_call 惯例)——
    # 条件性判官的 trace 可能来自旧 checker 路径的 dict 记录。
    def _f(t, attr: str):
        return getattr(t, attr) if hasattr(t, attr) else t.get(attr, "")
    if locate_turns:
        def _turn(t):
            return t.get("turn") if isinstance(t, dict) else getattr(t, "turn", None)
        tool_lines = "\n".join(
            f"- [T{_turn(t)}] {_f(t, 'name')}: {_f(t, 'input_summary')}" if _turn(t) is not None
            else f"- {_f(t, 'name')}: {_f(t, 'input_summary')}"
            for t in tool_trace
        )
        return (
            "## 实际工具调用记录(判涉及工具的步骤时以此为准,勿只凭文字声称;"
            "``[T{idx}]`` 标该调用所属 turn)\n"
            f"{tool_lines}\n\n"
        )
    tool_lines = "\n".join(f"- {_f(t, 'name')}: {_f(t, 'input_summary')}" for t in tool_trace)
    return (
        "## 实际工具调用记录(判涉及工具的步骤时以此为准,勿只凭文字声称)\n"
        f"{tool_lines}\n\n"
    )


_DISPATCH_PAIRS_LIMIT = 8000


def _dispatch_pairs_section(pairs, aux_tool_trace=None) -> str:
    """层3:派发-回报对 prompt 段(主 agent 视野内的委派证据,只喂 step 判官)。

    - 统计行:派发次数/角色分布(来自 pairs)+ 子代理实际执行计数(来自 aux trace,
      层1 已构建;无 aux 时缺省执行计数)——直接杀"全程没有任何实际运行测试"式断言。
    - 每对一行:``[T派发→T回报] 角色「prompt 头」 → 回报: 回报头``。回报头显式标注
      **子代理产出**——回报是子代理的报告,不是主 agent 的行为(防判官张冠李戴)。
    - 裁剪(超 _DISPATCH_PAIRS_LIMIT,封顶是不变式——该段随每批 prompt 重复进
      step/prohibition 两判官,超限 token 成本按批次数放大):失败行(只按**回报窗口**
      判定,不含 prompt——修 bug 类工作流的派发 prompt 常含「失败」二字)优先占预算,
      超预算的压「另有 M 份」计数;trivial 回报(<20 字,如 DONE/空)压计数行;
      其余按余量收,中段标省略数。空 pairs → ""(段整体不进)。
    """
    if not pairs:
        return ""

    def _f(p, attr):
        return getattr(p, attr) if hasattr(p, attr) else p.get(attr, "")

    roles: dict[str, int] = {}
    for p in pairs:
        r = _f(p, "role") or "(未标注)"
        roles[r] = roles.get(r, 0) + 1
    role_str = " / ".join(f"{r} {n}" for r, n in sorted(roles.items(), key=lambda x: -x[1]))

    exec_str = ""
    if aux_tool_trace:
        counts: dict[str, int] = {}
        for t in aux_tool_trace:
            n = (_f(t, "name") or "").strip().lower() or "(未知)"
            counts[n] = counts.get(n, 0) + 1
        top = "、".join(f"{n} {c} 次" for n, c in sorted(counts.items(), key=lambda x: -x[1])[:6])
        exec_str = f";子代理实际执行 {top}"

    def _fmt(p):
        d, r = _f(p, "dispatch_turn"), _f(p, "result_turn")
        rng = f"[T{d}→T{r}]" if (d is not None and r is not None) else (
            f"[T{d}→?]" if d is not None else "")
        rh = " ".join(str(_f(p, "result_head") or "(无回报)").split())
        rt = " ".join(str(_f(p, "result_tail") or "").split())
        # 头+尾窗口(改进2):结论惯例在回报尾部("状态:✅通过"/"FAILED X/Y"收尾),
        # 尾窗非空且不与头部重叠时以「…尾」衔接,保证验收结论必然可见。
        shown = rh[:120]
        if rt and rt not in shown:
            shown = f"{shown} …尾: {rt[:60]}"
        ph = " ".join(str(_f(p, "prompt_head") or "").split())
        role = _f(p, "role") or "子代理"
        return f"- {rng} {role}「{ph[:80]}」 → 回报(子代理产出): {shown}"

    def _is_failed(p):
        # 只看回报窗口(result_head/result_tail)——修 bug 类工作流的派发 prompt 常含
        # 「失败」二字(如「修复失败的用例 N」),prompt 命中 ≠ 回报失败。
        rh = str(_f(p, "result_head") or "")
        rt = str(_f(p, "result_tail") or "")
        return any(m in rh or m in rt for m in ("FAILED", "失败", "❌"))

    lines = [_fmt(p) for p in pairs]
    n_failed = sum(1 for p in pairs if _is_failed(p))
    stats = (f"统计:派发 {len(pairs)} 次({role_str}){exec_str};"
             f"回报含失败字样 {n_failed} 份")

    body = "\n".join(lines)
    if len(body) + len(stats) > _DISPATCH_PAIRS_LIMIT:
        # 两遍收行,守 8KB 封顶不变量:
        # ① 失败行优先占预算(验收结论是 W1 门禁的关键证据)——但也受预算约束,
        #   超出的压「另有 M 份」计数行,不无条件全留;
        # ② 其余行:trivial(回报<20字)压计数,余者按余量收,中段标省略数。
        budget = _DISPATCH_PAIRS_LIMIT - len(stats) - 300  # 300 = 段头+压缩标记余量
        keep = [False] * len(pairs)
        for i, (p, l) in enumerate(zip(pairs, lines)):
            if _is_failed(p) and budget > len(l) + 1:
                keep[i] = True
                budget -= len(l) + 1
        kept: list[str] = []
        n_trivial = 0
        for i, (p, l) in enumerate(zip(pairs, lines)):
            if keep[i]:
                kept.append(l)
            elif _is_failed(p):
                pass  # 失败但超预算 → 压计数(下方 n_failed_dropped)
            elif len(str(_f(p, "result_head"))) < 20:
                n_trivial += 1
            elif budget > 0:
                kept.append(l)
                budget -= len(l) + 1
        n_failed_dropped = n_failed - sum(keep)
        dropped = len(lines) - len(kept) - n_trivial - n_failed_dropped
        parts = kept
        if dropped > 0:
            parts.append(f"…(中段省略 {dropped} 条派发)")
        if n_failed_dropped > 0:
            parts.append(f"(另有 {n_failed_dropped} 份含失败字样的回报未展开)")
        if n_trivial > 0:
            parts.append(f"(另有 {n_trivial} 次派发回报为 DONE/空等短文本)")
        body = "\n".join(parts)

    return (
        "## 子代理派发-回报记录(主 agent 视野内的委派证据;回报是**子代理产出**,"
        "判执行/验收/义务类步骤时参考;勿把回报内容当主 agent 亲自执行)\n"
        f"{stats}\n{body}\n\n"
    )


def _dispatch_pairs_rule_section() -> str:
    """层3 判定规则行:验收/结果类步骤以"回报结论 + 处置"为准(治 A3 结果类假 PASS)。"""
    return (
        "- 验收/结果类步骤(验收标准、全量执行、达标类):**派发 prompt 传递了要求 ≠ "
        "达成了结果**。以派发**回报**的结论为准:回报 FAILED/未达标而主 agent 仍派发"
        "后续任务 → fail(门禁失守);回报明确通过且推进合规 → pass;声明要求验收但"
        "找不到对应验收派发 → fail(免验);回报缺失时按窗口规则判,不得仅凭 prompt "
        "传递了要求就 pass。\n"
    )


def _turn_rule_section(locate_turns: bool) -> str:
    """turn 引用规则的 prompt 段(locate_turns=False 时整段不进 prompt,与 judge 侧丢弃同款)。"""
    return (
        "## turn 引用规则\n"
        "执行记录每行开头的 ``[T{idx}]`` 标记该动作所属的全局 turn 序号(从 1 计);"
        "工具调用记录同。当 verdict=fail 或 indeterminate 时,**必须**在 ``turn_refs`` "
        "列出该问题(应走未走 / 证据矛盾)涉及的 turn 序号(如 ``[8]`` 或 ``[8, 15]``)——"
        "指向具体动作,便于人工复核定位。**段级缺席**(所属任务/阶段已开始,但这一步"
        "或这一违规全程未走/未发生)时留空 ``[]`` 并在 evidence 说明是全程性的。"
        "verdict=pass / na 留空 ``[]``。\n\n"
    ) if locate_turns else ""


def _turn_refs_field_hint(locate_turns: bool) -> str:
    """输出格式行里 turn_refs 的示例片段(locate_turns=False 时不要求该键)。"""
    return (
        ', "turn_refs": [8, 15]  // fail/indeterminate 涉及的 turn 序号;段级缺席或 pass/na 留空 []'
    ) if locate_turns else ""
# 整批 LLM 判定 transient 失败(超时 / CLI 非零退出 / 限流)的重试次数与退避间隔。
# call_claude 在超时 / 非零退出时抛 RuntimeError,多为 transient——重试大多能救回整批,
# 避免单次抖动让一整批指令兜底 N/A(audit-out3 实测 2 个整批共 25 条因此放弃)。
# 重试只针对 RuntimeError(语义错误重试无益,且 call_claude 也只抛这一种)。
_LLM_RETRY_MAX = 4
_LLM_RETRY_DELAY = 3.0
# 三个 LLM 判定器(条件性/禁令/步骤)并发上限。= 类别数 = 实测安全并发(1/2/3 档全 0 失败),
# 既是全类别并发的最优值、又天然有界(再高也无更多类别可并发)。非魔法数、无需调参。
_MAX_CONCURRENT = 3
# audit() 的阶段顺序(报告 stage_breakdown 按此序展示)。rebuild/judge 在 transcript
# 循环里累加;token 仅 LLM 阶段(refine/headline/judge)非 0,其余本地阶段 0。
_AUDIT_STAGES: tuple[str, ...] = (
    "extract", "refine", "headline", "rebuild", "judge", "postprocess",
)
# _LLM_LIMITER 单例 / _MAX_BATCH_CONCURRENT / _RATE_LIMIT_BACKOFF / configure_* 已上提到
# sift.concurrency(audit 与 run 侧共享一个 ceiling);上面 from-import 进本模块命名空间,
# 本模块代码(_judge_batch_with_retry 等)与旧测试(auditor._LLM_LIMITER)零改动继续可用。


# 进度回调类型:audit 每批 LLM 判定 40-257s,全程静默对用户极不友好。核心只 emit 字符串,
# 由 CLI 层(传 console.print)/测试层(传 list.append)决定如何呈现,producer/consumer 分离。
ProgressFn = Callable[[str], None]


def _emit(on_progress: ProgressFn | None, msg: str) -> None:
    """None 安全的进度回调:on_progress 为 None 时 no-op(核心默认不耦合 I/O)。"""
    if on_progress is not None:
        on_progress(msg)


# 重试耗尽标记:_judge_batch_with_retry 返回它时,调用方跳过写入、保留预置的全 N/A 兜底。
# 用独立单例类型(非裸 object()):mypy 可据 isinstance 把 _judge_batch_with_retry 返回值
# narrow 成 list,免去调用方 enumerate(parsed) 的类型告精(_judge_batch_with_retry 返回 list | 本类型)。
# 带 cause:耗尽时携带最后失败的原因(第二期附到 UNRESOLVED finding)。调用方用 isinstance(非身份比较)
# 判别,故从单例改成"每次耗尽构造一个带 cause 的实例"不破坏现有判别。
class _RetryExhaustedType:
    __slots__ = ("cause",)

    def __init__(self, cause: str = "unknown") -> None:
        self.cause = cause


_RETRY_EXHAUSTED = _RetryExhaustedType()  # 类型示例(函数耗尽时返回带 cause 的新实例)


# ── 结构化输出 schema:判官走 --json-schema,源头约束格式不可畸形 ───────────────
# CLI 不接受顶层 array schema(spike 实测触发 error_max_structured_output_retries),
# 故 array 判官把输出包成 {"items":[...]};call_claude 透明解开(items)→ judge 拿纯
# array、parser 零改动(extract_first_balanced/load_json_lenient 照常命中,fallback
# 留作 structured_output 缺失时兜底)。required 字段对齐各 prompt「输出格式」段;
# indeterminate/turn_refs/reason 选填(parser 容错缺键)。verdict 用 enum。
def _array_schema(item_schema: dict) -> dict:
    """array 判官 schema:顶层 object 包 items(规避 CLI 顶层 array 限制)。"""
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item_schema}},
        "required": ["items"],
    }


_APPLICABLE_SCHEMA = _array_schema({
    "type": "object",
    "properties": {
        "index": {"type": "integer"},
        "applicable": {"type": "boolean"},
        "indeterminate": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["index", "applicable"],
})
_CLASSIFY_REAL_SCHEMA = _array_schema({
    "type": "object",
    "properties": {
        "index": {"type": "integer"},
        "is_real": {"type": "boolean"},
        "indeterminate": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["index", "is_real"],
})
_HEADLINE_SCHEMA = _array_schema({
    "type": "object",
    "properties": {
        "index": {"type": "integer"},
        "headline": {"type": "string"},
    },
    "required": ["index", "headline"],
})
_PROHIBITION_SCHEMA = _array_schema({
    "type": "object",
    "properties": {
        "index": {"type": "integer"},
        "violated": {"type": "boolean"},
        "indeterminate": {"type": "boolean"},
        "evidence": {"type": "string"},
        "turn_refs": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["index", "violated", "evidence"],
})
_STEP_SCHEMA = _array_schema({
    "type": "object",
    "properties": {
        "index": {"type": "integer"},
        "verdict": {"type": "string", "enum": ["pass", "fail", "na", "indeterminate"]},
        "evidence": {"type": "string"},
        "turn_refs": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["index", "verdict", "evidence"],
})


def _judge_batch_with_retry(
    prompt: str,
    model: str | None,
    timeout: int,
    parse_fn: Callable[[str, int], list],
    bn: int,
    *,
    on_progress: ProgressFn | None,
    on_usage: Callable[[dict], None] | None = None,
    schema: dict | None = None,
    label: str,
    k: int,
    total: int,
    is_unparsed: Callable[[Any], bool],
    transport: str = "cc",
) -> list | _RetryExhaustedType:
    """call+parse 一个 LLM 批,RuntimeError 或整批 0 解析成功则重试(共用 _LLM_RETRY_MAX 预算)。

    合并三种"这批没拿到可用判定"的失败,共用同一份重试预算(不再为 0-解析另设次数):

    - **调用失败**:call_claude 抛 RuntimeError(超时 / CLI 非零退出 / 限流)。
    - **解析为空**:调用成功但 parse_fn 全 None(LLM 输出畸形 / 截断 / 空数组)——原是静默
      盲点(打"完成"却全 N/A),现重试。实锤 0-解析多为瞬态:同 prompt 重试常能解出。
    - **漏答过半**:调用成功、部分解析,但有判定者不足半数(输出契约要求逐条判,过半
      缺席多为瞬态——深思考档偶发漏答/截断救回的短数组)。重试一发常能救回,免得整批
      落"建议重跑";**漏 1 条不重试**(如 1/2:模型系统性漏近重复指令会每批白烧重试)。
      重试期间保留覆盖最高的一份,耗尽时**回它而非弃批**——已判对的保留,漏的由调用方
      标 unmentioned → UNRESOLVED(与弃批的差别:不再把已判对的也变没判成)。

    重试上限 _LLM_RETRY_MAX;退避两档:429 指数(_RATE_LIMIT_BACKOFF × 2^attempt,跨速率窗口)、
    其他失败线性(_LLM_RETRY_DELAY × 尝试序号)。成功返回 parse_fn
    结果;重试耗尽返回 _RETRY_EXHAUSTED(调用方保留预置全 N/A 兜底,audit 汇总"未判定"警告)。
    """
    # 进度前缀:类别名前置、括号化(三类并发交错时一眼分清哪条是哪类);单批不显 [k/总]
    # (恒 [1/1] 是噪声、看着像坏掉的计数器),多批才带 "批 k/total"。
    batch_tag = f" 批 {k}/{total}" if total > 1 else ""
    prefix = f"[{label}]{batch_tag}"
    last_cause = "unknown"
    best: list | None = None  # 漏答过半重试期间覆盖最高的一份(耗尽时回它,不弃批)
    best_ok = 0
    for attempt in range(_LLM_RETRY_MAX + 1):
        if attempt > 0:
            time.sleep(_LLM_RETRY_DELAY * attempt)
            _emit(on_progress, f"  {prefix} 重试 {attempt}/{_LLM_RETRY_MAX}…")
        _emit(on_progress, f"  {prefix} 判定 {bn} 条…")
        t0 = time.monotonic()
        try:
            # 限制器门控全局在飞 call_claude ≤ 当前 ceiling(AIMD 动态调):跨类别 + 同类多批
            # 统一争这一个。只圈住 call_claude(不圈 parse/重试 sleep),故重试退避不占槽。
            with _LLM_LIMITER:
                result_text = call_claude(
                    prompt, model=model, timeout=timeout, max_turns=1,
                    on_usage=on_usage, schema=schema, transport=transport,
                    on_progress=on_progress,  # http 升档/cc 回退的进度行透传(utils 报行)
                )
        except _HttpRateLimited as exc:
            # 429(账户速率/并发限制):AIMD 乘性减并发 + 指数退避后重试(同账号同限,cc 也撞、
            # 不回退)。速率限制按分钟窗口计,固定短退避会在窗口内反复撞死(glm-5.3 实测 8 并发
            # 3 个秒 429、容量低于 5.2)→ 退避按 基数×2^attempt 递增(2/4/8/16s),让连撞的
            # 等待跨过窗口。抖动防多线程同步重试。在 RuntimeError 之前(更 specific,免被吞)。
            last_cause = "rate_limited"  # typed,直接映射(不 classify)
            _LLM_LIMITER.on_throttled()
            _emit(
                on_progress,
                f"  {prefix} 撞限流({str(exc)[:80]}),并发降至 {_LLM_LIMITER.ceiling},退避重试…",
            )
            time.sleep(_RATE_LIMIT_BACKOFF * (2 ** attempt) + random.uniform(0, 1))
            continue
        except RuntimeError as exc:
            last_cause = classify_error(str(exc)).root_cause
            _emit(on_progress, f"  {prefix} 调用失败: {str(exc)[:120]}")
            continue
        _LLM_LIMITER.on_success()
        _emit(on_progress, f"  {prefix} 完成 ⏱ {time.monotonic() - t0:.0f}s")
        parsed = parse_fn(result_text, bn)
        ok = sum(1 for v in parsed if not is_unparsed(v)) if bn else 0
        if bn and ok == 0:
            last_cause = "parse_failed"
            _emit(on_progress, f"  {prefix} ⚠ 0 条解析成功(LLM 输出畸形/截断?),重试…")
            continue
        if ok >= best_ok:
            best, best_ok = parsed, ok
        if not bn or ok * 2 >= bn:
            return parsed  # 覆盖过半(含恰好半数):直接采用本次
        last_cause = "undercovered"
        _emit(on_progress, f"  {prefix} ⚠ 仅 {ok}/{bn} 条有判定(漏答过半),重试…")
    # 耗尽:手里有漏答过半以下但非零覆盖的最优一份 → 回它(已判对的保留,漏的走
    # unmentioned);从没拿到过任何有判定结果(全是调用失败/0-解析)才弃批。
    if best is not None:
        _emit(on_progress, f"  {prefix} 重试耗尽 → 采用覆盖最高的 {best_ok}/{bn} 份,其余未判")
        return best
    _emit(on_progress, f"  {prefix} 重试耗尽 → 本批 {bn} 条全兜底 N/A")
    return _RetryExhaustedType(cause=last_cause)


def _run_batches_concurrent(
    specs: list[tuple[int, int, int, str]],
    parse_fn: Callable[[str, int], list],
    model: str | None,
    timeout: int,
    on_progress: ProgressFn | None,
    on_usage: Callable[[dict], None] | None,
    label: str,
    total: int,
    is_unparsed: Callable[[Any], bool],
    schema: dict | None = None,
    transport: str = "cc",
) -> tuple[list[tuple[int, list]], list[tuple[int, int, str]]]:
    """把一类的所有 LLM 批次**并发**跑,返回 ``(successes, failures)``。

    旧实现每类内批次串行 → 一类 N 批 = N × 单批墙钟。本函数把同类的多批铺到本地
    ``ThreadPoolExecutor`` 并发;全局在飞 ``call_claude`` 由 ``_LLM_SEMAPHORE``(在
    ``_judge_batch_with_retry`` 内)钉在 ``_MAX_BATCH_CONCURRENT`` → 跨类别 + 同类多批
    统一争这一个信号量、按负载自均衡。墙钟从「最慢类批数 × 单批」降到「~单批」。

    - 单批(``len(specs) <= 1``)→ 串行,免池开销(也是多数小 skill 的常态)。
    - 多批 → 本地池 ``max_workers=min(len(specs), _MAX_BATCH_CONCURRENT)``,``as_completed`` 回收。

    返回 ``(successes, failures)``:

    - ``successes`` = ``[(start, parsed)]``(成功批;调用方据此 ``mask[start+j]`` 写回)。
    - ``failures`` = ``[(start, bn, cause)]``(耗尽批带 ``_RETRY_EXHAUSTED.cause``;异常穿透批
      ``cause="judge_error"``)→ 调用方把 ``cause`` 写进并行 ``cause_mask`` 附到 UNRESOLVED finding(第二期)。

    这样调用方拿到的 ``parsed`` 恒为 list(mypy 友好);耗尽/异常批的区段保持预置的全
    ``(None, "")``/``None``(audit 汇总"建议重跑"警告),并多了一份"为什么没判成"的根因。

    每个 spec = ``(start, bn, k, prompt)``:``start`` = 该批在全量 candidates 的起始下标;
    ``bn`` = 本批条数;``k`` = 批序号(进度显示);``prompt`` = 该批的完整 prompt。
    """
    out: list[tuple[int, list]] = []
    failed: list[tuple[int, int, str]] = []  # (start, bn, cause) 耗尽/异常穿透批
    if len(specs) <= 1:
        for start, bn, k, prompt in specs:
            try:
                parsed = _judge_batch_with_retry(
                    prompt, model, timeout, parse_fn, bn,
                    on_progress=on_progress, label=label, k=k, total=total,
                    on_usage=on_usage,
                    schema=schema,
                    is_unparsed=is_unparsed,
                    transport=transport,
                )
            except Exception as exc:
                # _judge_batch_with_retry 只 except RuntimeError/_HttpRateLimited;非 RuntimeError
                # 穿透(parse_fn 抛、代码 bug)→ 该批兜底,cause=judge_error,不击穿。
                _emit(on_progress, f"  [{label}] 批 {k} 异常 → 兜底 N/A: {str(exc)[:120]}")
                failed.append((start, bn, "judge_error"))
                continue
            if isinstance(parsed, _RetryExhaustedType):
                failed.append((start, bn, parsed.cause))
            else:
                out.append((start, parsed))
        return out, failed
    workers = min(len(specs), _MAX_BATCH_CONCURRENT)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                _judge_batch_with_retry, prompt, model, timeout, parse_fn, bn,
                on_progress=on_progress, label=label, k=k, total=total,
                on_usage=on_usage,
                schema=schema,
                is_unparsed=is_unparsed,
                transport=transport,
            ): idx
            for idx, (start, bn, k, prompt) in enumerate(specs)
        }
        for fut in as_completed(futs):
            idx = futs[fut]
            start, bn, _k, _prompt = specs[idx]
            try:
                parsed = fut.result()
            except Exception as exc:
                _emit(on_progress, f"  [{label}] 批 {specs[idx][2]} 异常 → 兜底 N/A: {str(exc)[:120]}")
                failed.append((start, bn, "judge_error"))
                continue
            if isinstance(parsed, _RetryExhaustedType):
                failed.append((start, bn, parsed.cause))
            else:
                out.append((start, parsed))
    return out, failed


def _unresolved_breakdown(findings: list[AuditFinding]) -> dict[str, int]:
    """UNRESOLVED findings 按 unresolved_cause 聚合计数(诊断用,不进任何判决)。

    None cause(理论不应出现——Task 4b 已保证 UNRESOLVED 必带 cause)兜底成 "unknown"。
    非 UNRESOLVED finding 不计(PASS/FAIL/NA/INDETERMINATE 恒 None,不进结果)。
    """
    out: dict[str, int] = {}
    for f in findings:
        if f.verdict == AuditVerdict.UNRESOLVED:
            c = f.unresolved_cause or "unknown"
            out[c] = out.get(c, 0) + 1
    return out


class TranscriptAuditor:
    """transcript 对账器:指令 vs 实测行为 → AuditFinding。

    含四条按类别分的判定路径(``_judge_conditional`` / ``_judge_prohibition`` /
    ``_judge_step`` / ``_judge_output``)+ ``classify`` 类别路由;
    ``audit`` 是聚合/路由主入口(串联上述件 + InstructionExtractor + rebuild + derive)。
    """

    @staticmethod
    def classify(instruction: InstructionCheck) -> AuditCategory:
        """把指令的 ``InstructionSource`` 路由到对应的 ``AuditCategory``。

        - TOOL_REQUIREMENT / REFERENCE_USAGE → CONDITIONAL(条件性,有触发条件)
        - CONSTRAINT / ANTI_FABRICATION      → PROHIBITION(禁令类)
        - HEADING_STEP                       → STEP(步骤)
        - OUTPUT_FORMAT                      → OUTPUT(输出格式)

        供 Task 8 聚合主入口按类别分流到各 ``_judge_*``。未映射的 source 视为编程错误
        (``InstructionSource`` 扩展后须同步更新本路由)→ 抛 ValueError 以便早暴露。
        """
        source = instruction.source
        if source in (InstructionSource.TOOL_REQUIREMENT, InstructionSource.REFERENCE_USAGE):
            return AuditCategory.CONDITIONAL
        if source in (InstructionSource.CONSTRAINT, InstructionSource.ANTI_FABRICATION):
            return AuditCategory.PROHIBITION
        if source == InstructionSource.HEADING_STEP:
            return AuditCategory.STEP
        if source == InstructionSource.OUTPUT_FORMAT:
            return AuditCategory.OUTPUT
        raise ValueError(f"未映射的 InstructionSource: {source!r}")

    def audit(
        self,
        skill_name: str,
        skill_body: str,
        transcripts: list[Path],
        model: str | None = None,
        timeout: int = 600,
        on_progress: ProgressFn | None = None,
        refine_instructions: bool = False,
        generate_headline: bool = False,
        *,
        locate_turns: bool = False,
        declared_tools: dict[str, str] | None = None,
        aux_tool_traces: list[list[ToolCallRecord]] | None = None,
    ) -> AuditReport:
        """聚合主入口:把 skill 声明的指令 + transcript 反推声明逐条对账若干 transcript。

        把 Task 1-7 的零件串起来。流程(每条 transcript 独立处理,findings 平铺
        per-transcript,顺序与 transcripts 入参一致):

        1. ``InstructionExtractor`` 从 skill_body 抽静态指令;
        2. 每 transcript ``rebuild`` 重建(tool_trace / text / digest)——坏/空 transcript
           抛 ``SiftError`` → 进 warnings、跳过该条(不崩,其余继续);
        3. ``derive_declarations`` 从 digest 反推"真实使用里确实需要的"补充指令(只增不减,
           纯新增、去重 vs 静态指令);existing + derived 合并后按 ``classify`` 分四桶
           (CONDITIONAL / PROHIBITION / STEP / OUTPUT);
        4. 四个 ``_judge_*`` 各判各桶 → findings;derived 来的 finding 标
           ``derived_from_transcript=True``(按 instruction_text 命中 derived 文案集合;
           与静态指令**同文**的 derived 文案除外——碰撞时该文本按真声明指令对待,
           不得把静态指令踢出 coverage 分母);
        5. 汇总 summary(total/pass/fail/na)+ warnings → ``AuditReport``。

        Args:
            skill_name: skill 名(填进 report.skill_name)。
            skill_body: SKILL.md 正文(``InstructionExtractor.extract`` 的输入)。
            transcripts: Claude Code session JSONL 路径列表(逐个 rebuild)。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。
            locate_turns: turn 定位开关(默认 False=关)。True 时 rebuild 给 digest 加
                ``[T{idx}]`` 前缀、禁令/步骤 judge 向 LLM 索取 ``turn_refs`` 并挂在
                FAIL/INDETERMINATE finding 上,供能 link 到 turn 内容的下游消费方;
                False 回到无前缀的干净判定(Task 1+2 已让 rebuild/judge 默认 False)。
            declared_tools: 声明 frontmatter 的工具白名单(``boundary.frontmatter_tool_names``
                产,``{lower 名: 显示文本}``),只喂越界视图对账;None = 无 frontmatter
                声明(引擎层不读文件,由 CLI/runner 传入)。
            aux_tool_traces: A1 旁路证据源,与 ``transcripts`` **同序**的子代理全量
                工具痕迹列表(元素可为空列表 = 该 transcript 无旁路;None = 全部无旁路,
                行为与旧版完全一致)。仅 ``--kind root`` 时 CLI 传入:条件类
                (TOOL_REQUIREMENT/REFERENCE_USAGE)root trace 未命中时在对应子代理
                痕迹里做第二遍确定性匹配,命中 → PASS(evidence 标注执行者)。

        Returns:

        Returns:
            ``AuditReport``:findings 为所有**有效** transcript 的平铺结果;坏/空 transcript
            不进 findings,而是带文件名进 warnings。
        """
        _wall_start = time.perf_counter()  # audit 墙钟(头尾测;判官 token 在 token_usage)
        stage_seconds: dict[str, float] = {s: 0.0 for s in _AUDIT_STAGES}
        _t = time.perf_counter()
        instructions = InstructionExtractor().extract(skill_body)
        stage_seconds["extract"] = time.perf_counter() - _t
        warnings: list[str] = []
        refine_dropped_count = 0
        acc = TokenUsageAccumulator()  # 累加所有判官 LLM 调用的 token(audit 末尾 snapshot 进 report)
        _t = time.perf_counter()
        if refine_instructions:
            # per-SKILL hash 缓存:同 instructions + model + prompt 版本只 refine 一次,
            # 跨 audit() 调用复用(runner.py 每 eval×config×run 调一次 audit,同 SKILL.md
            # 反复 refine 实测 10+ 分钟)。compute_fn 默认参数绑定当前值(防 instructions
            # 后面被 reassigned 后闭包看到新值——虽然 get_or_compute 同步调 compute_fn,
            # 默认参数显式绑定更稳)。
            instructions_before = instructions
            key = refine_cache._cache_key(instructions, model)

            def _compute(
                _insts=instructions,
                _model=model,
                _timeout=timeout,
                _on_progress=on_progress,
                _on_usage=acc.scoped("refine"),
            ):
                refined, _warns = self._refine_pseudo_instructions(
                    _insts, model=_model, timeout=_timeout, on_progress=_on_progress,
                    on_usage=_on_usage,
                )
                return refined, len(_insts) - len(refined)

            instructions, refine_dropped_count = refine_cache.get_or_compute(key, _compute)
            # 生成 warning(缓存 hit/miss 都生成,格式一致):基于 instructions_before 与
            # refined 的 diff 算 source 分布。缓存只存 (refined, dropped_count),warning
            # 在线重生成 —— hit/miss 输出一致,避免缓存 hit 丢 warning。
            if refine_dropped_count > 0:
                from collections import Counter
                refined_ids = {
                    (r.source.value, r.section, r.instruction_text)
                    for r in instructions
                }
                dropped_src_counts = Counter(
                    s.source.value
                    for s in instructions_before
                    if (s.source.value, s.section, s.instruction_text) not in refined_ids
                )
                parts = ", ".join(f"{src} {n}" for src, n in dropped_src_counts.items())
                warnings.append(
                    f"refine: 滤除 {refine_dropped_count} 条疑似示例/占位伪指令({parts})"
                )
        stage_seconds["refine"] = time.perf_counter() - _t
        _t = time.perf_counter()
        if generate_headline:
            # headline(人话标题,纯显示):refine 之后、transcript 判定之前。独立 LLM 步骤 +
            # 独立缓存(headline_cache),不碰 refine/is_real/judge → 判定零影响。写回
            # InstructionCheck.headline,透传到 finding 供显示。source_excerpt 进缓存键
            # (extract 改了 bullet 回溯 → headline 应重算)。
            hl_key = headline_cache._cache_key(instructions, model)

            def _compute_hl(
                _insts=instructions,
                _model=model,
                _timeout=timeout,
                _on_progress=on_progress,
                _usage=acc.scoped("headline"),
            ):
                return self._generate_headlines(
                    _insts, model=_model, timeout=_timeout,
                    on_progress=_on_progress, on_usage=_usage,
                )

            headlines = headline_cache.get_or_compute(hl_key, _compute_hl)
            for _i, _inst in enumerate(instructions):
                if _i < len(headlines):
                    _inst.headline = headlines[_i]
        stage_seconds["headline"] = time.perf_counter() - _t
        # 控制流检测:ORDER 顺序对 + 数量指令——纯程序、transcript 无关,循环外
        # 算一次。只吃静态 instructions(derived 恒 REFERENCE_USAGE、反推范围仅
        # required_reads,不沾两检测域);refine/headline 缓存键均不含 params,不受影响。
        order_pairs = detect_order_pairs(instructions)
        quantity_specs = detect_quantity(instructions)
        quantified_ids = {id(inst) for inst, _q in quantity_specs}
        # ORDER related 回填的端点查找表:合成文案 → (A/B 的**原始** instruction_text。
        # 回填按原文查 findings,合成文案里的 headline 仅用于显示——按 headline 查会
        # 全部落空、ORDER 整体静默不产)。
        order_endpoints: dict[str, tuple[str, str]] = {
            order_instruction_text(p.a, p.b): (p.a.instruction_text, p.b.instruction_text)
            for p in order_pairs
        }
        all_findings: list[AuditFinding] = []
        transcript_names: list[str] = []
        transcript_sources: list[str] = []  # 与 transcript_names 同序;"cc"/"opencode"/...
        provenance_results: dict[str, ProvenanceAuditResult] = {}
        boundary_traces: list[tuple[str, list]] = []  # 越界视图输入:(transcript 名, rebuild 的 tool_trace)

        for ti, tp in enumerate(transcripts, 1):
            _emit(on_progress, f"对账 transcript {ti}/{len(transcripts)}: {tp.name}")
            # A1 旁路:与 transcripts 同序的 aux(0-based 索引);None/越界/空 → 无旁路。
            aux_for_tp: list[ToolCallRecord] | None = None
            if aux_tool_traces is not None and ti - 1 < len(aux_tool_traces):
                aux_for_tp = aux_tool_traces[ti - 1] or None
            try:
                _t = time.perf_counter()
                tool_trace, transcript_text, digest = rebuild(tp, locate_turns=locate_turns)
                stage_seconds["rebuild"] += time.perf_counter() - _t
                boundary_traces.append((tp.name, tool_trace))
            except SiftError as e:
                # 坏/空 transcript:不崩,记 warning 跳过(其余 transcript 照常处理)。
                # 未能重建有效 digest 时，声明对账与文件归因分析均跳过该记录。
                warnings.append(f"{tp.name}: {e}")
                continue

            # v2 证据自动进入独立 provenance 区块。Analyzer 是纯本地确定性逻辑，
            # 不改变既有 AuditFinding、coverage、PASS/FAIL 汇总或 exit code。
            if has_provenance_evidence(digest):
                provenance_result = analyze_provenance(digest)
                if provenance_result.available:
                    provenance_results[tp.name] = provenance_result

            derived = derive_declarations(digest, instructions)
            all_inst = instructions + derived
            derived_texts = {d.instruction_text for d in derived}
            # 同文碰撞排除:derived 文案恰好与某条静态声明指令同文时,该文本不参与
            # 事后标 derived(finding 无法区分出处,但文本在静态集里 = 存在真声明
            # 指令,按真声明指令对待)。
            static_texts = {i.instruction_text for i in instructions}
            derived_only_texts = derived_texts - static_texts

            buckets: dict[AuditCategory, list[InstructionCheck]] = {
                AuditCategory.CONDITIONAL: [],
                AuditCategory.PROHIBITION: [],
                AuditCategory.STEP: [],
                AuditCategory.OUTPUT: [],
            }
            for inst in all_inst:
                if id(inst) in quantified_ids:
                    # 带数量词的指令移交控制流判官,原桶不重复判——每条指令
                    # 仍只有一个 verdict(移交路由,避免双判官 verdict 冲突仲裁)。
                    continue
                buckets[self.classify(inst)].append(inst)

            # conditional 判官靠 user_turns 当"场景"判指令该不该触发。skill/agent 切片
            # (--kind)内只有执行 turn、无 user turn(任务在父作用域)→ scene 空 → conditional
            # 全弃判 INDETERMINATE(实测 ops-spec-gen 22%、precision-debug 55% 弃判皆此)。
            # 回退用 transcript_text(执行本身揭示任务上下文)作场景,判官方能判 applicability。
            scene = "\n".join(digest.user_turns) or transcript_text

            # 三个 LLM 判定器(条件性/禁令/步骤)互相独立、无共享可变状态,并发跑省墙钟
            # (实测 zh:串行 13+51+37=101s → 并发 ≈ max=51s,约 2×)。max_workers=3 =
            # 类别数 = 实测安全并发(3 档全 0 失败),天然有界、非魔法数。输出格式判定器
            # 是确定性子串匹配(无 LLM),留主线程不占池位。on_progress(console.print)
            # 线程安全,三类 [k/总] 交错可读。
            _t = time.perf_counter()
            with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT) as pool:
                # 层3:派发-回报对(env SIFT_DISPATCH_PAIRS=0 可关——A/B 实验与逃生口;
                # 默认开)。digest.dispatch_returns 由 rebuild 从切片里的 task/Agent
                # tool_use + tool_result 配对收集(insight 侧已透传 dispatch result;
                # cc 原生 jsonl 天生就有)。step 判官默认喂;prohibition 判官经
                # SIFT_PROHIBITION_PAIRS 共享(层3-改进3,默认开,=0 可关)。aux 复用层1 的
                # 子代理痕迹做执行统计行。
                _pairs = (
                    digest.dispatch_returns
                    if (digest.dispatch_returns
                        and os.environ.get("SIFT_DISPATCH_PAIRS", "1") != "0")
                    else None
                )
                fut_cond = pool.submit(
                    self._judge_conditional, buckets[AuditCategory.CONDITIONAL],
                    tool_trace, scene, tp.name, model, timeout, on_progress=on_progress,
                    on_usage=acc.scoped("judge"), aux_tool_trace=aux_for_tp,
                )
                # 层3-改进3:prohibition 判官共享 pairs 段(默认开,env
                # SIFT_PROHIBITION_PAIRS=0 可关)。CONSTRAINT 形态的 Subagent 规约
                # (如「必须在输出末尾追加【日志摘要】段落」)与 HEADING_STEP 同义务
                # 不同桶——不共享则同一冤案在此桶永不翻。实验(11 会话 A/B):6/7
                # 靶子翻 PASS 且全真、唯一 PASS→FAIL 为真 W1 检出(86% 通过率标✅)、
                # 成本 -1.8%(判官更果断)。段头自带"回报是子代理产出"防张冠李戴声明。
                _proh_pairs = (
                    _pairs if os.environ.get("SIFT_PROHIBITION_PAIRS", "1") != "0" else None
                )
                fut_proh = pool.submit(
                    self._judge_prohibition, buckets[AuditCategory.PROHIBITION],
                    transcript_text, tp.name, model, timeout, on_progress=on_progress,
                    on_usage=acc.scoped("judge"),
                    locate_turns=locate_turns,
                    dispatch_pairs=_proh_pairs, aux_tool_trace=aux_for_tp,
                )
                fut_step = pool.submit(
                    self._judge_step, buckets[AuditCategory.STEP], transcript_text,
                    tp.name, model, timeout, tool_trace=tool_trace, on_progress=on_progress,
                    on_usage=acc.scoped("judge"),
                    locate_turns=locate_turns,
                    dispatch_pairs=_pairs, aux_tool_trace=aux_for_tp,
                )
                # 控制流判官作第 4 个 submit:排队等空位、可搭三类判官拖尾,落在
                # 本 judge 计时段内;内部多批由 _run_batches_concurrent 自带池并发,全局在飞
                # 仍受 _LLM_LIMITER(AIMD)门控(两层无死锁)。无 pairs/移交指令时不 submit。
                fut_cf = (
                    pool.submit(
                        self._judge_control_flow, order_pairs, quantity_specs,
                        transcript_text, tool_trace, tp.name, model, timeout,
                        on_progress=on_progress, on_usage=acc.scoped("judge"),
                        locate_turns=locate_turns,
                    )
                    if order_pairs or quantity_specs else None
                )
                findings: list[AuditFinding] = []
                # 三个判定器并发跑;单个整体抛异常(内部 bug,非 call_claude 的 RuntimeError
                # —— 那已被 _judge_batch_with_retry 捕获)时兜底:该类每条指令 UNRESOLVED
                # (复用 _make_unresolved_finding),不让指令"消失"也不击穿 audit。其余类照常。
                for _fut, _label, _bucket, _cat in (
                    (fut_cond, "条件性", buckets[AuditCategory.CONDITIONAL], AuditCategory.CONDITIONAL),
                    (fut_proh, "禁令", buckets[AuditCategory.PROHIBITION], AuditCategory.PROHIBITION),
                    (fut_step, "步骤", buckets[AuditCategory.STEP], AuditCategory.STEP),
                ):
                    try:
                        findings += _fut.result()
                    except Exception as exc:
                        _emit(
                            on_progress,
                            f"  [{_label}] 判定器异常 → 该类 {len(_bucket)} 条兜底 UNRESOLVED: {str(exc)[:120]}",
                        )
                        for inst in _bucket:
                            findings.append(self._make_unresolved_finding(
                                inst, tp.name,
                                f"{_label}判定器异常,未出判定 → 没判成", _cat,
                                cause="judge_error",
                            ))
                if fut_cf is not None:
                    try:
                        findings += fut_cf.result()
                    except Exception as exc:
                        # 兜底镜像三判官:移交出去的指令已脱离原桶兜底,这里必须接住——
                        # pairs + 移交指令全部 UNRESOLVED(judge_error),不让指令"消失"。
                        _emit(
                            on_progress,
                            f"  [控制流] 判定器异常 → {len(order_pairs)} 对 + "
                            f"{len(quantity_specs)} 条兜底 UNRESOLVED: {str(exc)[:120]}",
                        )
                        for p in order_pairs:
                            findings.append(self._make_order_finding(
                                p, AuditVerdict.UNRESOLVED, tp.name,
                                "控制流判定器异常,未出判定 → 没判成", cause="judge_error",
                            ))
                        for inst, _q in quantity_specs:
                            findings.append(self._make_unresolved_finding(
                                inst, tp.name, "控制流判定器异常,未出判定 → 没判成",
                                category=self.classify(inst), cause="judge_error",
                                control_flow="quantity",
                            ))
            findings += self._judge_output(
                buckets[AuditCategory.OUTPUT], transcript_text, tp.name
            )
            stage_seconds["judge"] += time.perf_counter() - _t

            for f in findings:
                # derived 反推文案与静态声明指令**同文**时(trace 绝对路径 Read 归一后
                # 与声明步骤措辞一致是常态),该文本的 finding 代表的是真声明指令——
                # 纯文本命中会把静态指令误标 derived,踢出 coverage 分母与 suggest 的
                # PASS 保留账本。同文碰撞时宁可不标(两者同判同 verdict,dedup 已视
                # 为同一条;不标只影响"额外多一条 derived 指令"的排除,不丢信号)。
                if f.instruction_text in derived_only_texts:
                    f.derived_from_transcript = True

            all_findings.extend(findings)
            transcript_names.append(tp.name)
            transcript_sources.append(transcript_source(tp))

        # findings 后处理:Case A 同文去重(合并 sources)+ 编 #1..#N + Case B 跨类关联
        # (related 引 #N)。在汇总前做,使 unverified/summary/by_instruction 都基于去重后
        # findings(同文重复不再虚增 fail、by_instruction 不再同 transcript 双计)。
        _t = time.perf_counter()
        all_findings = post_process_findings(all_findings)

        # ORDER related 回填:窗口 = post_process_findings(seq 已定)之后、
        # annotate_noise(读 related)之前;link_related 已跳过 synthetic(不覆写)。
        # 查找键 = 端点的原始 instruction_text;端点查不到 → 该 ORDER finding 丢弃不产
        # (悬空链接不如不产;此时序下丢弃产生 seq 跳号,下游 fail_seqs/related 均按
        # 实际 finding 收集、无 1..N 连续性假设,无害)。
        if order_endpoints and any(f.synthetic for f in all_findings):
            step_seq: dict[tuple[str, str], int] = {}
            for f in all_findings:
                if not f.synthetic and f.category == AuditCategory.STEP:
                    step_seq[(f.transcript, f.instruction_text)] = f.seq
            kept_findings: list[AuditFinding] = []
            for f in all_findings:
                if f.synthetic:
                    ends = order_endpoints.get(f.instruction_text)
                    a_seq = step_seq.get((f.transcript, ends[0])) if ends else None
                    b_seq = step_seq.get((f.transcript, ends[1])) if ends else None
                    if a_seq and b_seq:
                        f.related = [f"#{a_seq}", f"#{b_seq}"]
                        kept_findings.append(f)
                    continue  # 端点缺失 → 丢弃该 ORDER finding
                kept_findings.append(f)
            all_findings = kept_findings

        # FAIL 噪声/skill 判别(每条 FAIL 标 noise_label / noise_reasons,非 FAIL 置 None)。
        # 在 dedup 后、汇总前做:基于去重后的 findings,related 已就位(LLM+related 信号要用)。
        all_findings = annotate_noise(all_findings)

        # Case C 同源矛盾标注(同声明行跨镜头判出 PASS 与 FAIL → 互标 conflict_with)。
        # 纯确定性、标注不仲裁;摘要在 warnings 里报组数 + 例对(实测每份真跑报告 3-4 组,
        # 裸奔的矛盾对最伤报告可信度——不标,读者无从取舍)。
        conflict_groups = annotate_conflicts(all_findings)

        # 没判成(UNRESOLVED)vs 判不准(INDETERMINATE)分开汇总、不同警告:
        # - 没判成是操作性失败(LLM 调用/解析失败/未提及),**可重跑**救回 → 提示重跑。
        # - 判不准是语义模糊(LLM 看了证据真判不了),重跑无益 → 不提示重跑;
        #   但它是 LLM 的逃逸口,占比偏高 → 滥用闸 ② 告警(可能 LLM 偷懒说"判不准"蒙混)。
        # N/A(不适用)是 LLM 明确判"场景不涉及",属已解决结论,不进任何警告。
        unresolved = sum(1 for f in all_findings if f.verdict == AuditVerdict.UNRESOLVED)
        indeterminate = sum(1 for f in all_findings if f.verdict == AuditVerdict.INDETERMINATE)
        total = len(all_findings)
        unresolved_causes = _unresolved_breakdown(all_findings)
        if unresolved:
            warnings.append(
                f"{unresolved} 条指令没判成(UNRESOLVED:LLM 调用/解析失败/未提及)"
                "(本次结果可能不完整,建议重跑)"
            )
        if total >= _INDETERMINATE_ABUSE_MIN_TOTAL and indeterminate / total > _INDETERMINATE_ABUSE_RATE:
            warnings.append(
                f"{indeterminate} 条指令判不准(INDETERMINATE,占 {indeterminate * 100 // total}%),"
                "占比偏高,请人工复核是否 LLM 滥用判不准逃逸口"
            )
        if conflict_groups:
            shown = "; ".join(conflict_groups[:3]) + ("…" if len(conflict_groups) > 3 else "")
            warnings.append(
                f"同源矛盾 {len(conflict_groups)} 组:同一声明行被判出 PASS 与 FAIL"
                f"({shown}),建议人工复核对端证据"
            )

        summary = {
            "total": total,
            "pass": sum(1 for f in all_findings if f.verdict == AuditVerdict.PASS),
            "fail": sum(1 for f in all_findings if f.verdict == AuditVerdict.FAIL),
            "na": sum(1 for f in all_findings if f.verdict == AuditVerdict.NA),
            "unresolved": unresolved,
            "indeterminate": indeterminate,
        }
        by_instruction = aggregate_by_instruction(all_findings)
        # 聚合级噪声判别(fail_count>0 的行标 noise_label / noise_reasons)。在 AuditReport
        # 构造前做,使 report.by_instruction 直接携带标注。
        by_instruction = annotate_aggregate_noise(by_instruction)
        # 声明覆盖视图(纯 post-judge 元数据:dedup/seq 已定,seqs 交叉引用可用)。
        coverage = aggregate_coverage(all_findings)
        # 越界视图(一期信息性:领域工具对账,零 LLM 零缓存;只喂声明侧 instructions,
        # 不喂 derived——拿被审对象自己声明自己会循环论证)。declared_tools = 调用方
        # 从声明 frontmatter 抽的工具白名单(第四源,并集兜底)。
        boundary = aggregate_boundary(boundary_traces, instructions, declared_tools=declared_tools)
        stage_seconds["postprocess"] = time.perf_counter() - _t

        # 分阶段耗时/token(rebuild/judge 在 transcript 循环里累加;token 仅 LLM 阶段非 0)。
        stage_tokens = acc.snapshot_by_stage()
        # calls=0 的 LLM 阶段(refine/headline:开关控制 + 有缓存)区分两种 0——
        # 开关开 → "缓存命中"(LLM 路径跑了、秒回、本次 ~0 token);开关关 → "未启用"(没跑)。
        # extract/rebuild/judge/postprocess 是本地程序阶段(judge 不缓存),calls=0 是常态,不标。
        stage_flag_on = {"refine": refine_instructions, "headline": generate_headline}
        stage_breakdown = []
        for stage in _AUDIT_STAGES:
            tokens = stage_tokens.get(stage, {})
            calls = tokens.get("calls", 0)
            note = ""
            if stage in stage_flag_on:
                note = "未启用" if not stage_flag_on[stage] else ("缓存命中" if calls == 0 else "")
            stage_breakdown.append({
                "stage": stage,
                "seconds": round(stage_seconds.get(stage, 0.0), 3),
                "input_tokens": tokens.get("input_tokens", 0),
                "output_tokens": tokens.get("output_tokens", 0),
                # cache_read/creation 也要分阶段透出:累加器已按 stage 记(_KEYS 含这俩),
                # 旧序列化漏拷 → total 有 cache_read 但 stage 桶显 0(误导)。补上后报告能看出
                # 每阶段是否走 prompt cache(refine/headline 裸 HTTP 无 cache;judge cc 有)。
                "cache_read_input_tokens": tokens.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": tokens.get("cache_creation_input_tokens", 0),
                "calls": calls,
                "note": note,
            })
        return AuditReport(
            skill_name=skill_name,
            transcripts=transcript_names,
            transcript_sources=transcript_sources,
            summary=summary,
            findings=all_findings,
            by_instruction=by_instruction,
            coverage=coverage,
            boundary=boundary,
            warnings=warnings,
            refine_dropped_count=refine_dropped_count,
            token_usage=acc.snapshot(),
            duration_seconds=time.perf_counter() - _wall_start,
            stage_breakdown=stage_breakdown,
            unresolved_breakdown=unresolved_causes,
            provenance=provenance_results,
        )

    def _judge_conditional(
        self,
        instructions: list[InstructionCheck],
        tool_trace: list,
        transcript_scene: str,
        transcript_name: str,
        model: str | None = None,
        timeout: int = 600,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
        aux_tool_trace: list | None = None,
    ) -> list[AuditFinding]:
        """判定条件性指令(TOOL_REQUIREMENT / REFERENCE_USAGE)的三态结论。

        每条指令先走确定性 ``match_tool_call``(命中 tool_trace → PASS,不调 LLM);
        root 未命中且有 ``aux_tool_trace`` 时再走第二遍确定性匹配(命中 → PASS,
        evidence 标注"子代理 <role> 经派发执行"——A1 缺席式误判的洗冤通道,
        命中即事实、不进 LLM);两遍都未命中的批量交 LLM 判"场景是否该触发该
        指令",据此分 FAIL(该做没做) / N/A(场景不涉及);LLM 失败/异常/解析失败
        → 标 N/A(宁少报不乱报)。未命中全量交 LLM,
        >_MAX_LLM_IN_BATCH 由 ``_llm_judge_applicable`` 内部分批循环(无超限兜底 N/A)。

        Args:
            instructions: 已筛出的条件性指令(params 含 tool_name 或 script_path)。
            tool_trace: ToolCallRecord 列表(支持 attr / dict 风格,见 match_tool_call)。
            transcript_scene: 场景描述(调用方从 digest.user_turns 拼接传入)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。
            aux_tool_trace: 子代理作用域全量工具痕迹(slice.build_subagent_tool_trace,
                仅 kind=root 时 CLI 传入;None/空 = 无旁路,行为与旧版完全一致)。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 CONDITIONAL;
            source = 指令的 source 值;derived_from_transcript 暂默认 False
            (Task 8 聚合时按是否来自反推声明再标)。
        """
        # 按原位置填,保证返回顺序与输入一致。
        findings: list[AuditFinding | None] = [None] * len(instructions)
        # 未命中的待 LLM 判定:(原位置 i, 指令)
        unmatched: list[tuple[int, InstructionCheck]] = []

        for i, inst in enumerate(instructions):
            tool_name = inst.params.get("tool_name", "")
            script_path = inst.params.get("script_path", "")
            required_params = inst.params.get("required_params")
            if match_tool_call(tool_name, script_path, tool_trace, required_params=required_params):
                findings[i] = self._make_match_pass_finding(
                    inst, tool_name, script_path, transcript_name, required_params
                )
                continue
            # A1 旁路(第二遍确定性匹配,只在 root 模式 CLI 传入 aux 时激活):
            # 主 scope 未命中 ≠ 未发生——编排范式下执行类动作全部在子代理,
            # 子代理痕迹命中即"经派发执行"的事实,直接 PASS(不进 LLM)。
            # 用 find_execution_hit(严格匹配:只认执行痕迹,不认文档内容提及)——
            # "写过'已使用 X'"不是"执行了 X"。
            aux_hit = (
                find_execution_hit(tool_name, script_path, aux_tool_trace, required_params=required_params)
                if aux_tool_trace else None
            )
            if aux_hit is not None:
                findings[i] = self._make_aux_pass_finding(
                    inst, tool_name, script_path, transcript_name, aux_hit, required_params
                )
            else:
                unmatched.append((i, inst))

        if not unmatched:
            return [f for f in findings if f is not None]

        # 未命中全量交 LLM 判(>_MAX_LLM_IN_BATCH 由 _llm_judge_applicable 内部分批循环,
        # 不再单次截断 + 超限保守 FAIL)。tool_trace 一并喂 LLM:系于「记录窗口外」
        # (从未开始的任务/阶段)的指令不判应触发,防场景文本提过整个计划就一律 true 的假 FAIL。
        applicable_mask, cause_mask = self._llm_judge_applicable(
            [inst for _, inst in unmatched], transcript_scene, model, timeout,
            on_progress, on_usage, tool_trace=tool_trace,
        )

        for local_pos, (orig_i, inst) in enumerate(unmatched):
            tool_name = inst.params.get("tool_name", "")
            script_path = inst.params.get("script_path", "")
            miss_desc = f"调用 {tool_name}" if tool_name else f"读取 {script_path}"
            verdict, reason = applicable_mask[local_pos]
            if verdict == AuditVerdict.FAIL:
                findings[orig_i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    method=AuditMethod.LLM,
                    category=AuditCategory.CONDITIONAL,
                    evidence=f"场景应触发但 trace 未{miss_desc}",
                    transcript=transcript_name,
                )
            elif verdict == AuditVerdict.NA:
                findings[orig_i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.NA,
                    method=AuditMethod.LLM,
                    category=AuditCategory.CONDITIONAL,
                    # 用 LLM 给的"为何不涉及"理由;空才退兜底套话(镜像 step judge 的
                    # ``ev or "场景不涉及该步"``)。N/A 合法高频,空 reason 不降级(非 indeterminate 闸)。
                    evidence=reason or "场景不涉及该指令",
                    transcript=transcript_name,
                )
            elif verdict == AuditVerdict.INDETERMINATE:
                findings[orig_i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.INDETERMINATE,
                    method=AuditMethod.LLM,
                    category=AuditCategory.CONDITIONAL,
                    evidence=reason,
                    transcript=transcript_name,
                )
            else:
                # None:LLM 未给明确结论(调用失败/解析失败/该条未被提及/indeterminate 无 reason)
                # → 没判成(UNRESOLVED):宁少报不乱报,判不准不报 FAIL,FAIL 只留给 LLM 明确判"该做没做"。
                # cause 来自批级失败(infra)或默认 unmentioned(语义),纯诊断附因,不改 verdict。
                findings[orig_i] = self._make_unresolved_finding(
                    inst, transcript_name, "LLM 判定失败,无法确证是否该触发 → 没判成",
                    cause=cause_mask[local_pos],
                )

        return [f for f in findings if f is not None]

    # ---- 禁令类:主动检测违规 -------------------------------------------------

    def _judge_prohibition(
        self,
        instructions: list[InstructionCheck],
        transcript_text: str,
        transcript_name: str,
        model: str | None = None,
        timeout: int = 600,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
        *,
        locate_turns: bool = False,
        dispatch_pairs: list | None = None,
        aux_tool_trace: list | None = None,
    ) -> list[AuditFinding]:
        """判定禁令类指令(CONSTRAINT / ANTI_FABRICATION)——**主动检测违规是否发生**。

        与条件性/步骤判"该不该做/做没做"不同,禁令类问"这次真实执行里有没有发生违反
        该禁令/约束的行为"(如编造数据、跳过必须步骤、用了被禁止的做法)。批量交 LLM,
        每条返回 ``{index, violated: bool, evidence}``:
        - violated=True  → FAIL(违规了)
        - violated=False → PASS(没违规)
        - LLM 异常 / 解析失败 / 未提及 / violated 缺键或非布尔 → **N/A**(宁少报不乱报:
          即便禁令也在无确证时不报 FAIL;严格 ``is True/is False`` 判断,畸形值不当 PASS)。
          全量交 LLM,>_MAX_LLM_IN_BATCH 由 ``_llm_judge_prohibition`` 内部分批循环(无超限兜底 N/A)。

        Args:
            instructions: 已筛出的禁令类指令(CONSTRAINT / ANTI_FABRICATION)。
            transcript_text: 该 transcript 的执行记录文本(截断 [:8000] 防 prompt 过长)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 PROHIBITION。
        """
        if not instructions:
            return []  # 空桶短路:不 spawn LLM(与 audit"零重跑低成本"相悖则不调)

        findings: list[AuditFinding | None] = [None] * len(instructions)

        # 全量交 LLM 判(>_MAX_LLM_IN_BATCH 由 _llm_judge_prohibition 内部分批循环)。
        # locate_turns 透传:False 时 prompt 不含 turn 引用规则、不要求 LLM 输出 turn_refs。
        violated_mask, cause_mask = self._llm_judge_prohibition(
            instructions, transcript_text, model, timeout, on_progress, on_usage,
            locate_turns=locate_turns,
            dispatch_pairs=dispatch_pairs, aux_tool_trace=aux_tool_trace,
        )

        # turn_refs 后验上界:从喂给 LLM 的同一份 transcript_text 的 [T{idx}] 标记反推最大
        # turn 序号。locate_turns=False 时不 sanitize(无需上界,省一次正则扫文本)。
        max_turn = self._max_turn_from_text(transcript_text) if locate_turns else 0

        for i, inst in enumerate(instructions):
            verdict, ev, tr_raw = violated_mask[i]
            if verdict == AuditVerdict.FAIL:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                    evidence=ev or "执行中发生了违反该禁令的行为",
                    transcript=transcript_name,
                    # locate_turns=False 时即便 LLM 给了 turn_refs 也丢(整链关 turn 定位)。
                    turn_refs=self._sanitize_turn_refs(tr_raw, max_turn) if locate_turns else [],
                )
            elif verdict == AuditVerdict.PASS:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.PASS,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                    evidence=ev or "执行中未违反该禁令",
                    transcript=transcript_name,
                    # PASS 不是违规:即便 LLM 给了 turn_refs 也丢弃(不指向问题)。
                    turn_refs=[],
                )
            elif verdict == AuditVerdict.INDETERMINATE:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.INDETERMINATE,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                    evidence=ev,
                    transcript=transcript_name,
                    # locate_turns=False 时即便 LLM 给了 turn_refs 也丢。
                    turn_refs=self._sanitize_turn_refs(tr_raw, max_turn) if locate_turns else [],
                )
            else:
                # None:LLM 未给明确结论(调用失败/解析失败/未提及/畸形值/indeterminate 无 evidence)
                # → 没判成(UNRESOLVED):宁少报不乱报,即便禁令也在无确证时不报 FAIL;
                # audit() 汇总"建议重跑"警告提示人工复核,不靠假 FAIL 充数。
                # turn_refs 默认空(没出判定,谈不上指向哪个 turn)。
                findings[i] = self._make_unresolved_finding(
                    inst, transcript_name, "LLM 判定失败,无法确证是否违规 → 没判成",
                    category=AuditCategory.PROHIBITION,
                    cause=cause_mask[i],
                )

        return [f for f in findings if f is not None]

    # ---- 步骤类:三态判定 ----------------------------------------------------

    def _judge_step(
        self,
        instructions: list[InstructionCheck],
        transcript_text: str,
        transcript_name: str,
        model: str | None = None,
        timeout: int = 600,
        tool_trace: list | None = None,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
        *,
        locate_turns: bool = False,
        dispatch_pairs: list | None = None,
        aux_tool_trace: list | None = None,
    ) -> list[AuditFinding]:
        """判定步骤类指令(HEADING_STEP)的三态结论。

        批量交 LLM,每条返回 ``{index, verdict: "pass"|"fail"|"na", evidence}``,
        判定标准="这次执行有没有走这一步:走了→pass;该走没走→fail;场景不涉及该步→na":
        - pass → PASS
        - fail → FAIL
        - na   → N/A
        - 未知 verdict / LLM 异常 / 解析失败 → **N/A**(宁少报不乱报:判不准不报 FAIL)。
          全量交 LLM,>_MAX_LLM_IN_BATCH 由 ``_llm_judge_step`` 内部分批循环(无超限兜底 N/A)。

        verdict 匹配大小写不敏感(strip+lower);未知值/非字符串 → 标 N/A。
        空桶(0 条)→ 短路返回 [],不调 LLM。

        Args:
            instructions: 已筛出的步骤类指令(HEADING_STEP)。
            transcript_text: 该 transcript 的执行记录文本(截断 [:8000] 防 prompt 过长)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 STEP。
        """
        if not instructions:
            return []  # 空桶短路:不 spawn LLM

        findings: list[AuditFinding | None] = [None] * len(instructions)

        # 全量交 LLM 判(>_MAX_LLM_IN_BATCH 由 _llm_judge_step 内部分批循环)。
        # locate_turns 透传:False 时 prompt 不含 turn 引用规则、tool_section 无 [T{idx}]。
        # 层3:dispatch_pairs(派发-回报对)+ aux(子代理执行统计)喂 step 判官——
        # 缺席式误判("全程没跑测试"/义务链/交付物缺失)的洗冤证据 + 验收结论依据。
        verdict_mask, cause_mask = self._llm_judge_step(
            instructions, transcript_text, model, timeout, tool_trace, on_progress, on_usage,
            locate_turns=locate_turns,
            dispatch_pairs=dispatch_pairs, aux_tool_trace=aux_tool_trace,
        )

        # turn_refs 后验上界:从喂给 LLM 的同一份 transcript_text 的 [T{idx}] 标记反推最大
        # turn 序号。与 _judge_prohibition 同款。locate_turns=False 时不 sanitize。
        max_turn = self._max_turn_from_text(transcript_text) if locate_turns else 0

        for i, inst in enumerate(instructions):
            verdict, ev, tr_raw = verdict_mask[i]
            if verdict == AuditVerdict.PASS:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.PASS,
                    method=AuditMethod.LLM,
                    category=AuditCategory.STEP,
                    evidence=ev or "执行中走过了该步",
                    transcript=transcript_name,
                    # PASS 不是问题:即便 LLM 给了 turn_refs 也丢弃。
                    turn_refs=[],
                )
            elif verdict == AuditVerdict.FAIL:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    method=AuditMethod.LLM,
                    category=AuditCategory.STEP,
                    evidence=ev or "该走这一步但未走",
                    transcript=transcript_name,
                    # locate_turns=False 时即便 LLM 给了 turn_refs 也丢。
                    turn_refs=self._sanitize_turn_refs(tr_raw, max_turn) if locate_turns else [],
                )
            elif verdict == AuditVerdict.NA:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.NA,
                    method=AuditMethod.LLM,
                    category=AuditCategory.STEP,
                    evidence=ev or "场景不涉及该步",
                    transcript=transcript_name,
                    # NA(场景不涉及)不指向问题 turn。
                    turn_refs=[],
                )
            elif verdict == AuditVerdict.INDETERMINATE:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.INDETERMINATE,
                    method=AuditMethod.LLM,
                    category=AuditCategory.STEP,
                    evidence=ev,
                    transcript=transcript_name,
                    # locate_turns=False 时即便 LLM 给了 turn_refs 也丢。
                    turn_refs=self._sanitize_turn_refs(tr_raw, max_turn) if locate_turns else [],
                )
            else:
                # None/未知值/非字符串/indeterminate 无 evidence → 没判成(UNRESOLVED):
                # 宁少报不乱报,判不准不报 FAIL。turn_refs 默认空。
                findings[i] = self._make_unresolved_finding(
                    inst, transcript_name, "LLM 判定失败,无法确证是否走过 → 没判成",
                    category=AuditCategory.STEP,
                    cause=cause_mask[i],
                )

        return [f for f in findings if f is not None]

    # ---- 输出格式:确定性子串判定 -------------------------------------------

    def _judge_output(
        self,
        instructions: list[InstructionCheck],
        transcript_text: str,
        transcript_name: str,
    ) -> list[AuditFinding]:
        """判定 OUTPUT_FORMAT(PATTERN_MATCH)——确定性子串判定,**不调 LLM**。

        每条指令取 ``params.get("sample","")``,按行拆,任一行 strip 后作为子串在
        ``transcript_text`` 命中 → PASS,否则 FAIL(照已删的 ``compliance.checker._check_programmatic``
        的 PATTERN_MATCH 分支逻辑)。method=PROGRAMMATIC(确定性程序判定,不调 LLM)。

        Args:
            instructions: 已筛出的 OUTPUT_FORMAT 指令(params 含 sample)。
            transcript_text: 该 transcript 的执行记录文本(完整传入,作子串匹配源)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 OUTPUT;method=PROGRAMMATIC。
        """
        findings: list[AuditFinding] = []
        for inst in instructions:
            sample = inst.params.get("sample", "")
            # 照 checker PATTERN_MATCH:按行拆、strip、跳过空行、任一行作为子串命中即 True。
            lines = [ln.strip() for ln in sample.split("\n")]
            matched = next((ln for ln in lines if ln and ln in transcript_text), None)
            if matched is not None:
                findings.append(AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.PASS,
                    method=AuditMethod.PROGRAMMATIC,
                    category=AuditCategory.OUTPUT,
                    evidence=f"输出含期望格式 {matched[:80]}",
                    transcript=transcript_name,
                ))
            else:
                label = next((ln for ln in lines if ln), "")
                if label:
                    evidence = f"未找到期望格式 {label[:80]}"
                else:
                    evidence = "未提供期望输出格式样本"
                findings.append(AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
            section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    method=AuditMethod.PROGRAMMATIC,
                    category=AuditCategory.OUTPUT,
                    evidence=evidence,
                    transcript=transcript_name,
                ))
        return findings

    # ---- finding 工厂 -------------------------------------------------------

    @staticmethod
    def _max_turn_from_text(text: str) -> int:
        """从 transcript_text 的 ``[T{idx}]`` 标记提取最大 turn 序号(LLM 能引用的合法上界)。

        transcript_text(rebuild 渲染的执行记录)按 T2 给每条 assistant record 加了
        ``[T{idx}] `` 前缀;``_judge_prohibition`` / ``_judge_step`` 把它喂给 LLM,LLM
        据此回引 ``turn_refs``。本函数从同一份 text 反推上界,judge 自包含——无需把
        record 数 / 全局 turnIndex 透传过 audit/rebuild 签名。

        无标记(旧 digest / 空文本 / 仅 user 记录)→ 0 → ``_sanitize_turn_refs`` 全丢
        (无法校验任何引用是否在范围内)。

        注:``transcript_text`` 只含 assistant 记录(user / tool_result 在 T2 已跳过),
        故上界框的是"LLM 实际能看到的 assistant turn"——恰好就是可被引用的范围。

        **index space ≠ 可见 turn 数**:``_turn_index`` 计**所有** record(Task 1 给每条
        stamp,含 user / tool_result),但 user / tool_result 在 transcript_text 渲染时
        被跳过,所以本函数返回的 max 是"最大可引用 index"、不是"LLM 能看到的 turn 数"。
        一个幻觉 seq 即便指向"纯 user turn"(如 T3 是 user-only,transcript_text 看不到)
        也会过 sanitize(``1 ≤ 3 ≤ max``)——但 LLM 实际只看 assistant turn、自然只引那些,
        实践中不构成问题。本函数只负责"上界钳制",不负责"可见性白名单"。
        """
        return max((int(m) for m in re.findall(r"\[T(\d+)\]", text)), default=0)

    @staticmethod
    def _sanitize_turn_refs(refs: object, max_turn: int) -> list[int]:
        """后验校验 LLM 输出的 turn_refs,防幻觉 seq。

        丢弃:>max_turn / <1 / 非整数 / 重复;返回升序去重 ``list[int]``。

        - 非整数:字符串 ``"12"``、浮点 ``3.5``、布尔(布尔是 Python int 子类,
          但语义上不该当 turn 序号——``isinstance(x, int) and not isinstance(x, bool)``)。
        - ``max_turn <= 0``:无合法 turn 范围已知(无 ``[T{idx}]`` 标记)→ 无法校验任何
          引用 → 全丢(返回 ``[]``),宁可空也不放过幻觉。
        - ``refs`` 非 list / None:全丢。

        LLM judge 的引用是"建议",sanitizer 是"硬过滤"——只要 LLM 给的范围合法就保留,
        非法一律丢(不试图纠正)。返回的 list 长度可能为 0(段级缺席:全程未发生)。
        """
        if max_turn <= 0:
            return []
        if not isinstance(refs, list):
            return []
        seen: set[int] = set()
        for x in refs:
            # 严格 int、排除 bool:True/False 即便当 1/0 也无 turn 语义,且 LLM 偶尔把
            # violated=true 误当 turn_refs=true 漏出来——一律丢弃更稳。
            if isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= max_turn:
                seen.add(x)
        return sorted(seen)

    @staticmethod
    def _make_match_pass_finding(
        inst: InstructionCheck,
        tool_name: str,
        script_path: str,
        transcript_name: str,
        required_params=None,
    ) -> AuditFinding:
        """命中 tool_trace 的 PASS finding(method=PROGRAMMATIC)。evidence 按 params 里设的键选措辞。"""
        if tool_name:
            evidence = f"trace 中调用了 {tool_name}"
        else:
            evidence = f"trace 中读取了 {script_path}"
        params = [p for p in (required_params or []) if p]
        if params and tool_name:
            evidence += f"(含 {' '.join(params)})"
        return AuditFinding(
            instruction_text=inst.instruction_text,
            source_excerpt=inst.source_excerpt,
            headline=inst.headline,
            section=inst.source_section or inst.section,
            source=inst.source.value,
            verdict=AuditVerdict.PASS,
            method=AuditMethod.PROGRAMMATIC,
            category=AuditCategory.CONDITIONAL,
            evidence=evidence,
            transcript=transcript_name,
        )

    @staticmethod
    def _make_aux_pass_finding(
        inst: InstructionCheck,
        tool_name: str,
        script_path: str,
        transcript_name: str,
        tc,
        required_params=None,
    ) -> AuditFinding:
        """root trace 未命中、**子代理**痕迹命中(aux 旁路)的 PASS finding。

        A1:root 切片按设计排除子代理,编排范式下执行类要求(跑命令/读文件/加载 skill)
        全部由子代理实际执行 → 主 scope 恒缺席、LLM 兜底判"该做没做"=缺席式误判。
        子代理痕迹命中是确定性事实 → PASS;evidence 归属到执行者(子代理角色 + turn),
        与"主 agent 亲自执行"的 PASS(_make_match_pass_finding)在证据上区分开——
        保持诚实:W5 语义("该亲自做的没亲自做")由 prohibition/step 判官把守,不经此路。
        ``tc`` 支持 attr / dict 风格(同 match_tool_call 惯例)。
        """
        def _f(attr: str):
            return getattr(tc, attr, None) if not isinstance(tc, dict) else tc.get(attr)

        agent = (_f("agent") or "").strip() or "子代理"
        turn = _f("turn")
        what = f"调用了 {tool_name}" if tool_name else f"读取了 {script_path}"
        where = f"(T{turn})" if turn is not None else ""
        evidence = f"主 trace 未命中;子代理 {agent}{where} 实际{what}(经派发执行)"
        params = [p for p in (required_params or []) if p]
        if params and tool_name:
            evidence += f"(含 {' '.join(params)})"
        return AuditFinding(
            instruction_text=inst.instruction_text,
            source_excerpt=inst.source_excerpt,
            headline=inst.headline,
            section=inst.source_section or inst.section,
            source=inst.source.value,
            verdict=AuditVerdict.PASS,
            method=AuditMethod.PROGRAMMATIC,
            category=AuditCategory.CONDITIONAL,
            evidence=evidence,
            transcript=transcript_name,
        )

    @staticmethod
    def _make_unresolved_finding(
        inst: InstructionCheck,
        transcript_name: str,
        evidence: str,
        category: AuditCategory = AuditCategory.CONDITIONAL,
        cause: str = "unmentioned",
        control_flow: str = "",
    ) -> AuditFinding:
        """没判成(UNRESOLVED)finding:LLM 失败 / 解析失败 / 畸形值 / 未提及 / indeterminate 无 reason。

        与 §48 当初的"保守 FAIL"相反:判不准时**不报 FAIL**(FAIL 必须站得住——LLM
        明确判违规 / 该做没做)。操作性失败 → UNRESOLVED(独立于 N/A:N/A 是"不适用"已解决,
        UNRESOLVED 是"没判成"未解决、**可重跑**;另与 INDETERMINATE 区分:INDETERMINATE 是
        LLM 看了证据真判不了、重跑无益)。method=LLM(该条走了 LLM 路径但没出明确结论)。
        ``audit()`` 据此汇总"建议重跑"警告让人知道本次结果可能不完整。``category`` 默认
        CONDITIONAL,prohibition / step 各自传入对应类别,以反映真实指令类别。
        ``cause`` 标"为什么没判成"(timeout/rate_limited/parse_failed=基础设施,可重跑;
        unmentioned=语义;judge_error=判定器崩溃),纯诊断,不改 verdict。默认 unmentioned。
        ``control_flow`` 透传纯显示记号:数量指令移交控制流判官后的 UNRESOLVED 也要带
        "quantity"(否则最需要人复核的没判成条目在 CLI/HTML 里认不出是数量判定,与
        PASS/FAIL 路径不一致);其它路径默认空。
        """
        return AuditFinding(
            instruction_text=inst.instruction_text,
            source_excerpt=inst.source_excerpt,
            headline=inst.headline,
            section=inst.source_section or inst.section,
            source=inst.source.value,
            verdict=AuditVerdict.UNRESOLVED,
            method=AuditMethod.LLM,
            category=category,
            control_flow=control_flow,
            evidence=evidence,
            transcript=transcript_name,
            unresolved_cause=cause,
        )

    # ---- 控制流判官:ORDER 顺序 + QUANTITY 数量 -----------------------

    def _judge_control_flow(
        self,
        pairs: list[OrderPair],
        quantified: list[tuple[InstructionCheck, QuantitySpec]],
        transcript_text: str,
        tool_trace: list | None,
        transcript_name: str,
        model: str | None = None,
        timeout: int = 600,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
        *,
        locate_turns: bool = False,
    ) -> list[AuditFinding]:
        """判定 ORDER 顺序对与移交来的数量指令(;作为 audit() 第 4 个判官跑)。

        两路:
        - **程序判先行**(count 型 + params.tool_name):``count_tool_calls`` 数 tool_trace
          命中次数 vs 阈值 → PASS/FAIL,method=PROGRAMMATIC,零 LLM。诚实定位:此路覆盖
          面窄(带数字阈值的工具指令很少),是可数场景的精确化,不是主力。
        - **LLM 判官**(其余:distributive 型 / 无工具映射的 count 型 + ORDER pairs):
          输入 = 条目声明 + windowed transcript_text + tool_trace 段(同 step 判官——
          ORDER 判动作位置关系、数量判可数证据,都依赖工具调用记录;_window 只留头尾,
          不喂 tool_trace 会把被截断的动作误判「未发生」= 假 na)。verdict 四态同 step
          判官(indeterminate:证据矛盾时的诚实出口,不逼 LLM 在 pass/fail/na 里猜)。

        数量 na 口径(宁少报的对偶——错误理由的少报也是乱报):na 仅限无可数对象/场景
        不涉及(如整场没发现错误 → 无从引用);应做未做、数量不足一律 fail,evidence
        必须带实测数字与期望(prompt 强制)。失败语义镜像三判官:批失败重试耗尽 →
        UNRESOLVED(cause 入 mask);判官整体异常由 audit() 兜底(不在此 catch)。

        quantity finding 沿用原指令全文/category=原桶类别——不是 synthetic、不是
        derived,coverage/by_instruction/噪声判别照常参与(对聚合透明);ORDER finding
        是合成关系文案(synthetic=True),只在 findings 明细出现。
        """
        findings: list[AuditFinding] = []

        # 程序判:count 型 + tool_name(阈值可程序比对 + 工具可数)。
        # 只数次数、不带 required_params 参数断言——数量词里的数字会被 extractor 误抽成
        # 参数 token(「至少 2 次」→ ['2']),带断言会把空 input 的真实调用全滤掉、
        # 误判「0 次」(db-migration fixture 真跑实证);参数级断言归 TOOL_CALLED 桶
        # (match_tool_call),count 型问的是「调了几次」(docstring 同哲学:不问读了哪个文件)。
        llm_quantified: list[tuple[InstructionCheck, QuantitySpec]] = []
        for inst, q in quantified:
            tool_name = inst.params.get("tool_name", "")
            if q.kind == "count" and tool_name:
                n = count_tool_calls(tool_name, tool_trace)
                ok = (
                    n >= q.threshold if q.op == "gte"
                    else n <= q.threshold if q.op == "lte"
                    else n == q.threshold
                )
                label = quantity_op_label(q.op)
                findings.append(AuditFinding(
                    instruction_text=inst.instruction_text,
                    source_excerpt=inst.source_excerpt,
                    headline=inst.headline,
                    section=inst.source_section or inst.section,
                    source=inst.source.value,
                    verdict=AuditVerdict.PASS if ok else AuditVerdict.FAIL,
                    method=AuditMethod.PROGRAMMATIC,
                    category=self.classify(inst),
                    control_flow="quantity",
                    evidence=(
                        f"实测调用 {tool_name} {n} 次,满足「{label} {q.threshold} 次」"
                        if ok else
                        f"实测调用 {tool_name} {n} 次,不满足「{label} {q.threshold} 次」"
                    ),
                    transcript=transcript_name,
                ))
            else:
                llm_quantified.append((inst, q))

        if not pairs and not llm_quantified:
            return findings

        # LLM 判官:items = ("order", pair) | ("quantity", inst),统一编 index
        items: list[tuple[str, object]] = (
            [("order", p) for p in pairs]
            + [("quantity", inst) for inst, _q in llm_quantified]
        )
        verdict_mask, cause_mask = self._llm_judge_control_flow(
            items, transcript_text, tool_trace, model, timeout, on_progress, on_usage,
            locate_turns=locate_turns,
        )
        max_turn = self._max_turn_from_text(transcript_text) if locate_turns else 0
        ev_fallback: dict[str, dict[AuditVerdict, str]] = {
            "order": {
                AuditVerdict.PASS: "A 的证据不晚于 B(含交错/部分重排)",
                AuditVerdict.FAIL: "B 的全部证据先于 A(违序)",
                AuditVerdict.NA: "A 或 B 未发生/场景不涉及",
            },
            "quantity": {
                AuditVerdict.PASS: "数量满足",
                AuditVerdict.FAIL: "数量不满足",
                AuditVerdict.NA: "无可数对象/场景不涉及",
            },
        }

        def _quantity_llm_finding(
            inst: InstructionCheck, verdict: AuditVerdict, ev: str, tr: list[int],
        ) -> AuditFinding:
            return AuditFinding(
                instruction_text=inst.instruction_text,
                source_excerpt=inst.source_excerpt,
                headline=inst.headline,
                section=inst.source_section or inst.section,
                source=inst.source.value,
                verdict=verdict,
                method=AuditMethod.LLM,
                category=self.classify(inst),
                control_flow="quantity",
                evidence=ev,
                transcript=transcript_name,
                turn_refs=tr,
            )

        for i, (kind, payload) in enumerate(items):
            verdict, ev, tr_raw = verdict_mask[i]
            turn_refs = (
                self._sanitize_turn_refs(tr_raw, max_turn)
                if locate_turns and verdict in (AuditVerdict.FAIL, AuditVerdict.INDETERMINATE)
                else []
            )
            if kind == "order":
                pair: OrderPair = payload
                if verdict is None:
                    findings.append(self._make_order_finding(
                        pair, AuditVerdict.UNRESOLVED, transcript_name,
                        "LLM 判定失败,无法确证顺序 → 没判成", cause=cause_mask[i],
                    ))
                elif verdict == AuditVerdict.INDETERMINATE:
                    findings.append(self._make_order_finding(
                        pair, verdict, transcript_name, ev, turn_refs=turn_refs,
                    ))
                else:
                    findings.append(self._make_order_finding(
                        pair, verdict, transcript_name,
                        ev or ev_fallback["order"][verdict], turn_refs=turn_refs,
                    ))
            else:
                inst: InstructionCheck = payload
                if verdict is None:
                    findings.append(self._make_unresolved_finding(
                        inst, transcript_name, "LLM 判定失败,无法确证数量 → 没判成",
                        category=self.classify(inst), cause=cause_mask[i],
                        control_flow="quantity",
                    ))
                elif verdict == AuditVerdict.INDETERMINATE:
                    findings.append(_quantity_llm_finding(inst, verdict, ev, turn_refs))
                else:
                    findings.append(_quantity_llm_finding(
                        inst, verdict, ev or ev_fallback["quantity"][verdict], turn_refs,
                    ))
        return findings

    def _llm_judge_control_flow(
        self,
        items: list[tuple[str, object]],
        transcript_text: str,
        tool_trace: list | None,
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
        *,
        locate_turns: bool = False,
    ) -> tuple[list[tuple[AuditVerdict | None, str, list[int]]], list[str]]:
        """分批判定控制流条目;批/prompt/重试模式同 _llm_judge_step(每批 ≤10 条)。

        items 元素 = ("order", OrderPair) | ("quantity", InstructionCheck),批内统一编
        index。输出 schema/解析复用 step 判官(_STEP_SCHEMA / _parse_step——verdict 四态
        + evidence + turn_refs 完全同构,不另造 parser)。
        """
        n = len(items)
        results: list[tuple[AuditVerdict | None, str, list[int]]] = [(None, "", [])] * n
        cause_mask: list[str] = ["unmentioned"] * n
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        specs: list[tuple[int, int, int, str]] = []
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = items[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            item_list = "".join(
                f"[{i}] {self._cf_item_line(kind, payload)}\n"
                for i, (kind, payload) in enumerate(batch)
            )
            prompt = (
                "判定以下顺序/数量要求在本次真实执行里是否被满足。\n\n"
                "## 判定标准(宁少报:拿不准别判违规;但也别把该报的洗成不适用)\n"
                "- 顺序对(声明:A 须先于 B):\n"
                "  - pass:A 的证据不晚于 B(交错/部分重排也算 pass)。\n"
                "  - fail:B 的**全部**证据都先于 A(先做了本该后做的)。\n"
                "  - na:A 或 B 压根没发生/本场景不涉及(evidence 注明哪步没发生)。\n"
                "- 数量要求:\n"
                "  - pass:数量满足。\n"
                "  - fail:数量不满足——**应做未做、数量不足都算 fail**;evidence 必须带"
                "实测数字与期望(如「3 个错误仅 2 个有引用」)。\n"
                "  - na:**仅限**无可数对象/场景不涉及(如整场没发现错误 → 无从引用),"
                "或符合「窗口与条件边界」;除此之外不得把「没做」洗成 na。\n"
                "  - 窗口与条件边界(单条任务级条目适用):条目所属的任务/阶段在记录里"
                "**从未开始**(记录止于某处、该条目排在其后),或**条件触发型条目**"
                "(「据 X 的失败报告/输出修复」类)的触发条件在记录里从未发生 →"
                " na(证据不覆盖/条件未触发;evidence 注明记录止于何处或前置未发生)。"
                "但所属阶段已开始、条目在中途没走的仍判 fail,不得借本边界洗成 na。\n"
                "  - 完成度要求(「逐一完成/全部完成/完成其中的 Tasks/项」类):**以本执行"
                "记录为全集**——对账对象就是这份记录,记录结束时未完成的项就是数量不足"
                "(fail),**不得推测「记录之外可能继续」来洗成 pass/na**;「按顺序」与"
                "「完成全部」同现时,完成度是主判据,记录内的顺序合规不掩盖数量不足"
                "(evidence 带实测 N/期望 M)。整体缺口由这一类完成度条目兜底报告——"
                "单条任务级条目按「窗口与条件边界」判 na,不算漏报。\n"
                "- indeterminate(两类通用):证据真的相互矛盾或关键缺失才用,**必须**在"
                " evidence 写明具体矛盾/缺失;不要用它偷懒回避判断。\n\n"
                f"{_turn_rule_section(locate_turns)}"
                f"## 执行记录摘录\n{_window(transcript_text)}\n\n"
                f"{_tool_trace_section(tool_trace, locate_turns)}"
                f"## 待判条目 ({bn} 条)\n{item_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:'
                '{"index": 0, "verdict": "pass"|"fail"|"na"|"indeterminate", '
                '"evidence": "中文简述(具体到证据;数量类必须带实测数字与期望)"'
                f"{_turn_refs_field_hint(locate_turns)}"
                "}\n"
            )
            specs.append((start, bn, k, prompt))
        successes, failures = _run_batches_concurrent(
            specs, self._parse_step, model, timeout, on_progress, on_usage,
            "控制流", total, lambda v: v[0] is None,
            schema=_STEP_SCHEMA,
            transport="http",
        )
        for start, parsed in successes:
            for j, val in enumerate(parsed):
                results[start + j] = val
        for start, bn, cause in failures:
            for j in range(bn):
                cause_mask[start + j] = cause
        return results, cause_mask

    @staticmethod
    def _cf_item_line(kind: str, payload) -> str:
        """控制流判官的单条条目渲染(顺序对列 A/B 两条 bullet 原文;数量列完整 bullet)。

        文本用 source_excerpt 优先(含被切走的主语前缀——数量词多在那里;判官与
        detect_quantity 看到同一份文本)。
        """
        if kind == "order":
            p: OrderPair = payload
            return f"(顺序) 先做:「{context_text(p.a)}」 后做:「{context_text(p.b)}」"
        inst: InstructionCheck = payload
        return f"(数量) 「{context_text(inst)}」"

    def _make_order_finding(
        self,
        pair: OrderPair,
        verdict: AuditVerdict,
        transcript_name: str,
        evidence: str,
        *,
        turn_refs: list[int] | None = None,
        cause: str | None = None,
    ) -> AuditFinding:
        """ORDER finding:审计器合成的关系文案(synthetic=True;category=STEP、source=A 端)。

        source_excerpt/headline 留空(合成文案无原文 bullet,模型字段约定);related 由
        audit() 在 post_process 之后按端点原始 instruction_text 回填(不在此设)。
        """
        return AuditFinding(
            instruction_text=order_instruction_text(pair.a, pair.b),
            source_excerpt="",
            headline="",
            section=pair.a.source_section or pair.a.section,
            source=InstructionSource.HEADING_STEP.value,
            verdict=verdict,
            method=AuditMethod.LLM,
            category=AuditCategory.STEP,
            evidence=evidence,
            transcript=transcript_name,
            synthetic=True,
            turn_refs=turn_refs or [],
            unresolved_cause=cause,
        )

    # ---- LLM 场景触发判定 ----------------------------------------------------

    def _llm_judge_applicable(
        self,
        candidates: list[InstructionCheck],
        transcript_scene: str,
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
        tool_trace: list | None = None,
    ) -> tuple[list[tuple[AuditVerdict | None, str]], list[str]]:
        """分批 call_claude 判每条未命中指令在该场景是否"该触发"(每批 _MAX_LLM_IN_BATCH(10) 条,处理全部)。

        返回 ``(mask, cause_mask)``,均与 candidates 等长:

        - ``mask`` 每元素 ``(verdict, reason)``:
          - (FAIL, "")     = 该触发(该做没做)
          - (NA, reason)   = 不涉及(场景不触发,带 LLM 给的"为何不涉及";空则调用方退兜底套话;
            含窗口外——指令系于从未开始的任务/阶段,证据不覆盖不判应触发)
          - (INDETERMINATE, reason) = 判不准(LLM 看了场景但证据真矛盾/缺失,带具体 reason)
          - (None, "")     = 没判成(调用/解析失败/未提及/indeterminate 无 reason)
        - ``cause_mask`` 每元素 str(verdict 为 None 时读,附到 UNRESOLVED finding):
          批级失败(耗尽/异常)覆写成 infra cause(timeout/rate_limited/parse_failed/…),
          默认 "unmentioned"(批成功但该条 None = LLM 未提及)。

        按 ``_MAX_LLM_IN_BATCH`` 分批循环:每批独立 call_claude + ``_parse_applicable``,
        合并成完整 mask(长度=len(candidates))。批内 prompt 的 index 从 0 计(本地 index),
        ``_parse_applicable`` 只认 ``[0, bn)`` 的本地 index;批结果按 ``mask[start:start+bn]``
        写回全量 mask 的对应区段——**绝不**用批内 index 直接当下标(会错位/覆盖)。
        某批调用异常(RuntimeError)→ 先重试 ``_LLM_RETRY_MAX`` 次,仍失败则该批保持
        (None, "")(调用方标 UNRESOLVED + audit 汇总"建议重跑"警告),其余批照常处理。
        空 candidates → range 空 → 不调 LLM,返回 ([], [])(调用方 ``_judge_conditional`` 已在
        unmatched 为空时短路,这里作兜底)。
        """
        n = len(candidates)
        mask: list[tuple[AuditVerdict | None, str]] = [(None, "")] * n
        cause_mask: list[str] = ["unmentioned"] * n  # 批成功但该条 None(未提及)的默认 cause
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        # 先攒齐所有批次的 prompt(build-only),再统一交 _run_batches_concurrent 并发跑
        # (同类多批并发;全局在飞 call_claude 由 _LLM_SEMAPHORE 门控)。
        specs: list[tuple[int, int, int, str]] = []
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = candidates[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            instruction_list = "".join(
                f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(batch)
            )
            prompt = (
                "判断以下每条条件性指令在给定场景下是否应该被触发执行。\n\n"
                "## 判定标准(优先给出明确结论;只有证据真的相互矛盾或关键缺失时才用 indeterminate)\n"
                "- ✅ true(该触发):这个场景满足指令的隐含条件——执行者本应做该动作"
                "(如场景涉及该工具应被调用、该参考文件应被读取),却没做 = 疑似违规。"
                "该指令所属任务/阶段已开始的,照常判断该不该触发,不得借「记录窗口」"
                "把已开始的阶段洗成不涉及。\n"
                "- ❌ false(不涉及):这个场景根本不涉及该指令(场景未触及该工具 / 参考"
                "文件的适用范围,本就不该触发;或该指令系于某个具体任务/阶段/分支——"
                "如某 Task 的验收标准、某 Branch 的实现要求——而工具调用记录显示记录"
                "结束时该任务/阶段从未开始:执行记录是有限窗口,窗口外属证据不覆盖,"
                "不判应触发)。**reason 必须写明:场景为何不触及该指令的适用范围**"
                "(不许空口'不涉及',要给可核对的依据)。\n"
                "- ⚠ indeterminate(判不准):你看了场景,但证据真的相互矛盾或关键信息缺失,"
                "判断不了该不该触发。**必须**在 reason 写明具体矛盾 / 缺失的证据;"
                "不要用 indeterminate 偷懒回避判断。\n\n"
                f"## 场景描述\n{_window(transcript_scene)}\n\n"
                f"{_tool_trace_section(tool_trace, False)}"
                f"## 待判指令 ({bn} 条)\n{instruction_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:'
                '{"index": 0, "applicable": true/false, "indeterminate": true(仅判不准时), '
                '"reason": "applicable=false 时必填(写为何场景不涉及该指令);indeterminate 时必填(写具体矛盾/缺失)"}\n'
            )
            specs.append((start, bn, k, prompt))
        successes, failures = _run_batches_concurrent(
            specs, self._parse_applicable, model, timeout, on_progress, on_usage,
            "条件性", total, lambda v: v[0] is None,
            schema=_APPLICABLE_SCHEMA,
            transport="http",
        )
        for start, parsed in successes:
            for j, val in enumerate(parsed):
                mask[start + j] = val  # 本地 j → 全量 start+j(批间 index 映射)
        for start, bn, cause in failures:
            for j in range(bn):
                cause_mask[start + j] = cause  # 批级失败(耗尽/异常)覆写成 infra cause
        return mask, cause_mask

    @staticmethod
    def _parse_applicable(text: str, n: int) -> list[tuple[AuditVerdict | None, str]]:
        """解析 LLM 场景触发判定 → ``(AuditVerdict | None, reason)``;默认 ``(None, "")`` = 没判成。

        - applicable=true  → (FAIL, "")(该做没做)
        - applicable=false → (NA, reason)(场景不涉及;reason 为 LLM 给的"为何不涉及",可空→调用方退兜底)
        - indeterminate=true 且 reason 非空 → (INDETERMINATE, reason)(判不准)
        - indeterminate=true 但 reason 空 → **滥用闸 ①** 降级 (None, "") = 没判成
          (不许空口"判不准",必须带具体矛盾/缺失证据)
        - 其余(缺键/None/非布尔/未提及)→ (None, "") = 没判成(宁少报不乱报,不报假 FAIL)

        严格 ``is True/is False`` 判断(畸形值如字符串 "false" 不洗成明确结论)。
        """
        mask: list[tuple[AuditVerdict | None, str]] = [(None, "")] * n
        arr_str = extract_first_balanced(text)
        if arr_str is None:
            return mask
        items = load_json_lenient(arr_str)
        if not isinstance(items, list):
            return mask
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < n:
                if item.get("indeterminate") is True:
                    reason = str(item.get("reason", "") or "").strip()
                    if reason:
                        mask[idx] = (AuditVerdict.INDETERMINATE, reason)
                    # else: indeterminate 无具体 reason → 滥用闸 ① 降级没判成(保持 (None,""))
                    continue
                val = item.get("applicable")
                if val is True:
                    mask[idx] = (AuditVerdict.FAIL, "")
                elif val is False:
                    # 捕获 LLM 给的"为何不涉及"理由(空则调用方退兜底套话)。N/A 是合法高频
                    # 判决,空 reason 不降级(对齐 step judge 的 ev or 兜底,非 indeterminate 滥用闸)。
                    mask[idx] = (AuditVerdict.NA, str(item.get("reason", "") or "").strip())
                # else: 缺 applicable / None / 非布尔 → 保持 (None,"") = 没判成
        return mask

    # ---- LLM 精筛伪指令(extract 后、判定前)-------------------------------

    def _refine_pseudo_instructions(
        self,
        instructions: list[InstructionCheck],
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
    ) -> tuple[list[InstructionCheck], list[str]]:
        """LLM 精筛:砍掉 extractor 误抽的示例/占位文字(非真实可检查指令)。

        正则粗召回保召回,这里提精度:对每条候选(除 OUTPUT_FORMAT——它是纯程序
        子串匹配,精筛无收益)问 LLM「真实可检查的指令 vs 示例/占位」,只砍明确
        ``is_real=False`` 的。**精度 > 召回**:True/None(没判成/判不准/整批失败)
        全保留——宁可留伪指令让下游判 N/A,也别砍真指令。

        返回 ``(精筛后指令列表, warnings)``:warnings 报滤除数(按 source 分),
        无砍则空。per-skill 调一次(audit() 在 per-transcript 循环外调它)。
        """
        # 候选 = 除 OUTPUT_FORMAT(PATTERN_MATCH 程序判定)外的全部
        cand_idx = [
            i for i, inst in enumerate(instructions)
            if inst.source != InstructionSource.OUTPUT_FORMAT
        ]
        candidates = [instructions[i] for i in cand_idx]
        if not candidates:
            return instructions, []  # 无候选,不调 LLM
        mask = self._llm_classify_real(candidates, model, timeout, on_progress, on_usage)
        keep = [True] * len(instructions)  # 默认全保留(精度>召回)
        dropped_src: list[str] = []
        for k, verdict in enumerate(mask):
            if verdict is False:  # 只砍明确 is_real=False
                keep[cand_idx[k]] = False
                dropped_src.append(candidates[k].source.value)
        refined = [inst for inst, k in zip(instructions, keep) if k]
        warns: list[str] = []
        if dropped_src:
            from collections import Counter
            parts = ", ".join(f"{src} {n}" for src, n in Counter(dropped_src).items())
            warns.append(f"refine: 滤除 {len(dropped_src)} 条疑似示例/占位伪指令({parts})")
        return refined, warns

    def _llm_classify_real(
        self,
        candidates: list[InstructionCheck],
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
    ) -> list[bool | None]:
        """分批 call_claude 判每条候选「真实可检查指令 vs 示例/占位」(每批 _MAX_LLM_IN_BATCH 条)。

        返回与 candidates 等长,每元素:True=真指令(留)/ False=示例占位(砍)/
        None=没判成(调用/解析失败/indeterminate 判不准)→ 调用方保守保留。
        镜像 ``_llm_judge_applicable`` 的分批循环 + ``_judge_batch_with_retry`` + ``mask[start+j]``。
        """
        n = len(candidates)
        mask: list[bool | None] = [None] * n
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        # 先攒齐所有批次的 prompt,再统一交 _run_batches_concurrent 并发跑(refine 多批也并发)。
        specs: list[tuple[int, int, int, str]] = []
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = candidates[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            instruction_list = "".join(
                f"[{i}] (来源 {inst.source.value} / 标题:\"{inst.section}\") {inst.instruction_text}"
                + (f"\n    摘录:{inst.source_excerpt[:200]}" if inst.source_excerpt else "")
                + "\n"
                for i, inst in enumerate(batch)
            )
            prompt = (
                "判断以下每条从 SKILL.md 抽出的候选文本:它是**真实可检查的指令**"
                "(要求执行者做/不做某件可在执行记录里对账的事),"
                "还是**示例/演示结果/占位/描述性文字**?\n\n"
                "## 判定标准(优先给出明确结论;只有真的分不清时才用 indeterminate)\n"
                "- ✅ is_real=true(真指令):祈使句/约束/要求,可判做了没做"
                "(如\"Step 1: 读取 X\"、\"NEVER 编造数据\"、\"运行 validate.py\")。"
                "**任务清单行是真指令**:以 `[ ]`/`[x]` 开头的行、或与其他任务行同构"
                "(同句式、仅分支/编号不同)的行,哪怕该任务本次没执行、排在计划末尾"
                "——\"执行没执行\"是判官的事(判 N/A/fail),不是精筛的事,不得当示例砍。\n"
                "- ❌ is_real=false(非指令):教程示例文字、示例输出结果、说明性描述、"
                "占位文本(如\"3 agents dispatched in parallel\"是演示结果、"
                "\"6 failures across 3 files\"是例句);或**命令行/代码片段里的标识符被抽成的"
                "伪工具要求**——\"工具名\"其实是编译目标/函数名/脚本参数/任务编号词,只出现在"
                "摘录的某条命令行/代码或任务编号表述里、不是执行者可调用的工具(如\"调用 "
                "TilingFunc\"里的 TilingFunc 是测试目标名;\"`./run.sh` 跑 Task 25 用例\"抽出"
                "的\"跑 Task\"里 Task 是任务编号词,真命令是 run.sh)。\n"
                "- ⚠ indeterminate(分不清):只在真的无法判断时用,**必须**在 reason 写明;"
                "不要用 indeterminate 偷懒回避。\n\n"
                f"## 待判候选 ({bn} 条)\n{instruction_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:'
                '{"index": 0, "is_real": true/false, '
                '"indeterminate": true(仅分不清时), "reason": "indeterminate 时必填"}\n'
            )
            specs.append((start, bn, k, prompt))
        successes, _failures = _run_batches_concurrent(
            specs, self._parse_real, model, timeout, on_progress, on_usage,
            "精筛", total, lambda v: v is None,
            schema=_CLASSIFY_REAL_SCHEMA,
            transport="http",
        )
        for start, parsed in successes:
            for j, val in enumerate(parsed):
                mask[start + j] = val
        return mask

    @staticmethod
    def _parse_real(text: str, n: int) -> list[bool | None]:
        """解析 LLM 真伪指令判定 → ``bool | None``;默认 ``None`` = 没判成(调用方保留)。

        - is_real=true  → True(真指令)
        - is_real=false → False(示例/占位 → 砍)
        - indeterminate=true → None(分不清 → 保守保留,不砍)
        - 其余(缺键/None/非布尔/未提及)→ None(没判成 → 保留)
        严格 ``is True/is False`` 判断(畸形值如字符串 "false" 不洗成明确结论)。
        """
        mask: list[bool | None] = [None] * n
        arr_str = extract_first_balanced(text)
        if arr_str is None:
            return mask
        items = load_json_lenient(arr_str)
        if not isinstance(items, list):
            return mask
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < n:
                if item.get("indeterminate") is True:
                    continue  # 分不清 → 保持 None(保留)
                val = item.get("is_real")
                if val is True:
                    mask[idx] = True
                elif val is False:
                    mask[idx] = False
                # else: 缺 is_real / None / 非布尔 → 保持 None(保留)
        return mask

    # ---- headline(人话标题,纯显示;判定不读)-----------------------------

    def _generate_headlines(
        self,
        instructions: list[InstructionCheck],
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
    ) -> list[str]:
        """分批 call_claude 给每条指令产一句人话 headline(去噪 + 补主语 + 压一句)。

        吃 instruction_text + source_excerpt(原文 bullet,含主语上下文)→ 返回与
        instructions 等长的 list[str]。空串 = 没生成(批失败/解析失败/LLM 漏条)→
        显示回退 source_excerpt/instruction_text。**判定不读 headline**(judge 吃
        instruction_text)。per-declaration 一次,headline_cache 跨 audit() 复用。
        分批/并发/重试复用 _run_batches_concurrent + _LLM_SEMAPHORE(与 refine/judge 同套)。
        """
        n = len(instructions)
        if n == 0:
            return []
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        specs: list[tuple[int, int, int, str]] = []
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = instructions[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            item_list = "".join(
                f"[{i}]\n  判定指令:{inst.instruction_text}\n"
                f"  原文上下文:{inst.source_excerpt or '(无)'}\n"
                for i, inst in enumerate(batch)
            )
            prompt = (
                "把下面每条「判定指令」改写成一句**给人看的简短标题**(headline)。\n\n"
                "## 要求\n"
                "- 用「原文上下文」补全被切走的主语(如判定指令是「必须存在」、原文上下文里"
                "有文件名 → headline 写「<文件名> 必须存在」)。\n"
                "- 去掉 markdown 噪声:反引号、星号、括号里的解释性注释、多余换行。\n"
                "- 压成一句顺口的话,不丢关键信息(条件/对象/动作)、不改原意。\n"
                "- 判定指令**信息完整**(主语+动作+必要条件都在)时 → 只去噪;但若像是被切残的"
                "碎片(只剩动词+宾语、丢了条件或主语,如「跑 build」「必须存在」),不算自含主语"
                "——按前一条用「原文上下文」补全重写,**不要照抄**判定指令。\n\n"
                f"## 待改写 ({bn} 条)\n{item_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:{"index": 0, "headline": "一句简短标题"}\n'
            )
            specs.append((start, bn, k, prompt))
        out: list[str] = [""] * n
        successes, _failures = _run_batches_concurrent(
            specs, self._parse_headline, model, timeout, on_progress, on_usage,
            "headline", total, lambda v: not v,
            schema=_HEADLINE_SCHEMA,
            transport="http",
        )
        for start, parsed in successes:
            for j, val in enumerate(parsed):
                out[start + j] = val
        return out

    @staticmethod
    def _parse_headline(text: str, n: int) -> list[str]:
        """解析 LLM headline 输出 → list[str](默认 n 个空串 = 没生成 → 回退)。"""
        out: list[str] = [""] * n
        arr_str = extract_first_balanced(text)
        if arr_str is None:
            return out
        items = load_json_lenient(arr_str)
        if not isinstance(items, list):
            return out
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            h = item.get("headline")
            if isinstance(idx, int) and 0 <= idx < n and isinstance(h, str):
                out[idx] = h.strip()
        return out

    # ---- 禁令类 LLM 违规检测 -------------------------------------------------

    def _llm_judge_prohibition(
        self,
        candidates: list[InstructionCheck],
        transcript_text: str,
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
        *,
        locate_turns: bool = False,
        dispatch_pairs: list | None = None,
        aux_tool_trace: list | None = None,
    ) -> tuple[list[tuple[AuditVerdict | None, str, list[int]]], list[str]]:
        """分批 call_claude 判每条禁令在本次执行里是否被违反(每批 _MAX_LLM_IN_BATCH(10) 条,处理全部)。

        返回 ``(results, cause_mask)``,均与 candidates 等长:

        - ``results`` 每元素 ``(verdict, evidence, turn_refs_raw)``:
          - (FAIL, ev, tr)          = 已违反
          - (PASS, ev, tr)          = 未违反
          - (INDETERMINATE, ev, tr) = 判不准(LLM 看了证据真矛盾/缺失,带具体 evidence)
          - (None, "", [])          = 没判成(调用/解析失败/未提及/畸形值/indeterminate 无 evidence)
        - ``cause_mask`` 每元素 str(verdict 为 None 时读):批级失败覆写成 infra cause,
          默认 "unmentioned"(批成功但该条 None = LLM 未提及)。

        ``turn_refs_raw`` 是 LLM 输出的原样引用(未 sanitize,可能含垃圾);``_judge_prohibition``
        调 ``_sanitize_turn_refs`` 后验过滤后,只在 FAIL/INDETERMINATE finding 上挂。

        分批模式同 ``_llm_judge_applicable``:每批独立 call_claude + ``_parse_prohibition``,
        批内 index 从 0 计、结果按 ``results[start:start+bn]`` 写回全量(批间 index 映射,
        绝不用批内 index 直接当下标)。某批 RuntimeError → 先重试 ``_LLM_RETRY_MAX`` 次,
        仍失败则该批保持 (None, "", [])(调用方标 UNRESOLVED + "建议重跑"警告),
        其余批继续。空 candidates → 不调 LLM,返回 ([], [])。
        """
        n = len(candidates)
        results: list[tuple[AuditVerdict | None, str, list[int]]] = [(None, "", [])] * n
        cause_mask: list[str] = ["unmentioned"] * n
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        # 先攒齐所有批次的 prompt,再统一交 _run_batches_concurrent 并发跑(同类多批并发)。
        specs: list[tuple[int, int, int, str]] = []
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = candidates[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            instruction_list = "".join(
                f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(batch)
            )
            # locate_turns=False 时整段 turn 引用规则不进 prompt,输出格式也不要求 turn_refs
            # (judge 不向 LLM 索取 turn 引用 → 返回的 turn_refs 即便有也由 _judge 层丢弃)。
            turn_rule_section = (
                "## turn 引用规则\n"
                "执行记录每行开头的 ``[T{idx}]`` 标记该动作所属的全局 turn 序号(从 1 计)。"
                "当 violated=true 或 indeterminate=true 时,**必须**在 ``turn_refs`` 列出"
                "该违规 / 矛盾发生的 turn 序号(如 ``[12]`` 或 ``[12, 34]``)——指向具体动作,"
                "便于人工复核定位。**段级缺席**(如「整段全程未发生违规」「全程未编造数据」)"
                "时留空 ``[]`` 并在 evidence 说明是全程性的。violated=false / PASS 留空 ``[]``。\n\n"
            ) if locate_turns else ""
            turn_refs_field = (
                ', "turn_refs": [12, 34]  // 违规/矛盾发生的 turn 序号;段级缺席或 PASS 留空 []'
            ) if locate_turns else ""
            prompt = (
                "判断以下每条禁令/约束在本次真实执行里是否被违反。\n\n"
                "## 判定标准(优先给出明确结论;只有证据真的相互矛盾或关键缺失时才用 indeterminate)\n"
                "- ✅ violated=true(已违反):这次执行里确实发生了违反该禁令/约束的行为"
                "(如编造了数据、跳过了必须步骤、用了被禁止的做法)。\n"
                "- ❌ violated=false(未违反):这次执行没有违反该禁令/约束。\n"
                "- ⚠ indeterminate=true(判不准):你看了执行记录,但证据真的相互矛盾或关键信息缺失,"
                "判断不了是否违反。**必须**在 evidence 写明具体矛盾 / 缺失的证据;"
                "不要用 indeterminate 偷懒回避判断。\n\n"
                f"{turn_rule_section}"
                f"## 执行记录摘录\n{_window(transcript_text)}\n\n"
                f"{_dispatch_pairs_section(dispatch_pairs, aux_tool_trace)}"
                f"## 待判禁令 ({bn} 条)\n{instruction_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:'
                '{"index": 0, "violated": true/false, "indeterminate": true(仅判不准时), '
                '"evidence": "中文简述为何违反/未违反/判不准(具体到证据)"'
                f"{turn_refs_field}"
                "}\n"
            )
            specs.append((start, bn, k, prompt))
        successes, failures = _run_batches_concurrent(
            specs, self._parse_prohibition, model, timeout, on_progress, on_usage,
            "禁令", total, lambda v: v[0] is None,
            schema=_PROHIBITION_SCHEMA,
            transport="http",
        )
        for start, parsed in successes:
            for j, val in enumerate(parsed):
                results[start + j] = val  # 本地 j → 全量 start+j
        for start, bn, cause in failures:
            for j in range(bn):
                cause_mask[start + j] = cause
        return results, cause_mask

    @staticmethod
    def _parse_prohibition(
        text: str, n: int
    ) -> list[tuple[AuditVerdict | None, str, list[int]]]:
        """解析 LLM 禁令违规判定 → ``(AuditVerdict | None, evidence, turn_refs_raw)``;默认 ``(None, "", [])``。

        - violated=true  → (FAIL, ev, tr)
        - violated=false → (PASS, ev, tr)
        - indeterminate=true 且 evidence 非空 → (INDETERMINATE, ev, tr)(判不准)
        - indeterminate=true 但 evidence 空 → **滥用闸 ①** 降级 (None, "", []) = 没判成
        - 其余(缺键/None/非布尔/未提及)→ (None, "", []) = 没判成

        与 ``_parse_applicable`` 同构(同 JSON 格式 + extract_first_balanced + 严格
        ``is True/is False``),只是多取一个 evidence 字段。畸形值(如字符串 "true")
        不用 bool() 洗,保持 (None, "", []) = 没判成(不误判违规)。

        第三个元素 ``turn_refs_raw``:LLM 输出的 turn 引用(**未经 sanitize** 的原样透传,
        可能含负数 / 超范围 / 非整数)。sanitize 职责交 ``_judge_prohibition`` 调
        ``_sanitize_turn_refs`` 统一做——parser 保持"忠实镜像 LLM 输出",不在两处过滤。
        默认空 list(LLM 没输出该键 / 段级缺席)。
        """
        results: list[tuple[AuditVerdict | None, str, list[int]]] = [(None, "", [])] * n
        arr_str = extract_first_balanced(text)
        if arr_str is None:
            return results
        items = load_json_lenient(arr_str)
        if not isinstance(items, list):
            return results
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < n:
                ev = str(item.get("evidence", "") or "")
                # turn_refs 原样透传:LLM 偶尔写成非 list / 含垃圾,sanitizer 在 judge 里兜底。
                # 这里只做最小兼容:list 才收,其余(单 int / 字符串)当空。
                # 注解 list[Any] 而非 list[int]:``list(raw)`` 保 LLM junk(如 [5, -1, 999, "x"]),
                # 校验是 sanitizer 的职责,这里重复校验会破坏 raw 透传契约。
                raw = item.get("turn_refs")
                tr: list[Any] = list(raw) if isinstance(raw, list) else []
                if item.get("indeterminate") is True:
                    if ev.strip():
                        results[idx] = (AuditVerdict.INDETERMINATE, ev, tr)
                    # else: indeterminate 无 evidence → 滥用闸 ① 降级没判成(保持 (None,"",[]))
                    continue
                violated = item.get("violated")
                if violated is True:
                    results[idx] = (AuditVerdict.FAIL, ev, tr)
                elif violated is False:
                    results[idx] = (AuditVerdict.PASS, ev, tr)
                # else: 缺 violated 键 / None / 非布尔 → 保持 (None,"",[]) = 没判成
        return results

    # ---- 步骤类 LLM 三态判定 -------------------------------------------------

    def _llm_judge_step(
        self,
        candidates: list[InstructionCheck],
        transcript_text: str,
        model: str | None,
        timeout: int,
        tool_trace: list | None = None,
        on_progress: ProgressFn | None = None,
        on_usage: Callable[[dict], None] | None = None,
        *,
        locate_turns: bool = False,
        dispatch_pairs: list | None = None,
        aux_tool_trace: list | None = None,
    ) -> tuple[list[tuple[AuditVerdict | None, str, list[int]]], list[str]]:
        """分批 call_claude 判每个步骤在本次执行里是否被走过(每批 _MAX_LLM_IN_BATCH(10) 条,处理全部)。

        返回 ``(results, cause_mask)``,均与 candidates 等长:

        - ``results`` 每元素 ``(verdict, evidence, turn_refs_raw)``:
          - (PASS, ev, tr)          = 走过
          - (FAIL, ev, tr)          = 该走没走
          - (NA, ev, tr)            = 场景不涉及(含窗口外:所属任务/阶段在记录里从未开始)
          - (INDETERMINATE, ev, tr) = 判不准(LLM 看了证据真矛盾/缺失,带具体 evidence)
          - (None, "", [])          = 没判成(调用/解析失败/未提及/未知 verdict/非字符串/indeterminate 无 evidence)
        - ``cause_mask`` 每元素 str(verdict 为 None 时读):批级失败覆写成 infra cause,
          默认 "unmentioned"(批成功但该条 None = LLM 未提及)。

        ``turn_refs_raw`` 是 LLM 输出的原样引用(未 sanitize);``_judge_step`` 调
        ``_sanitize_turn_refs`` 后验过滤后,只在 FAIL/INDETERMINATE finding 上挂。

        分批模式同 ``_llm_judge_applicable``:每批独立 call_claude + ``_parse_step``,
        批内 index 从 0 计、结果按 ``results[start:start+bn]`` 写回全量(批间 index 映射,
        绝不用批内 index 直接当下标)。某批 RuntimeError → 先重试 ``_LLM_RETRY_MAX`` 次,
        仍失败则该批保持 (None, "", [])(调用方标 UNRESOLVED + "建议重跑"警告),
        其余批继续。空 candidates → 不调 LLM,返回 ([], [])。
        """
        n = len(candidates)
        results: list[tuple[AuditVerdict | None, str, list[int]]] = [(None, "", [])] * n
        cause_mask: list[str] = ["unmentioned"] * n
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        # 先攒齐所有批次的 prompt,再统一交 _run_batches_concurrent 并发跑(同类多批并发)。
        specs: list[tuple[int, int, int, str]] = []
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = candidates[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            instruction_list = "".join(
                f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(batch)
            )
            # tool_trace 进 prompt:判涉及工具的步骤时让 LLM 看到实际工具调用,而非只凭
            # assistant 文字声称(canary 暴露的缺口 —— 否则"Read input log"步骤明明调了
            # Read 却因 transcript_text 只剩文字声称而误判 FAIL)。
            # locate_turns=True 时每条 tool call 带 [T{idx}](若 ToolCallRecord.turn 有标注),
            # 让 LLM 能把工具调用对到具体 turn → step 的 turn_refs 引用更准(turn 粒度定位);
            # locate_turns=False 时不带前缀、段标题也不提示 [T{idx}](整链关 turn 定位)。
            tool_section = _tool_trace_section(tool_trace, locate_turns)
            # 层3:派发-回报对段(每批同段——证据是 transcript 级的,非批级;8KB 封顶)
            # + 验收类判定规则行(传递≠达成)。段/规则只在有 pairs 时进(无派发的
            # 单 agent transcript 行为与旧版完全一致)。
            pairs_section = _dispatch_pairs_section(dispatch_pairs, aux_tool_trace)
            pairs_rule = _dispatch_pairs_rule_section() if pairs_section else ""
            # locate_turns=False 时整段 turn 引用规则不进 prompt,输出格式也不要求 turn_refs
            # (与 _llm_judge_prohibition 同款;judge 层 locate_turns=False 时返回的 turn_refs 丢弃)。
            turn_rule_section = _turn_rule_section(locate_turns)
            turn_refs_field = _turn_refs_field_hint(locate_turns)
            prompt = (
                "判断以下每个步骤在本次执行里是否被走过。\n\n"
                "## 判定标准(优先给出明确结论;只有证据真的相互矛盾或关键缺失时才用 indeterminate)\n"
                "- pass:这次执行走过了这一步。\n"
                "- fail:这个场景本该走这一步但没走。\n"
                "- na:这个场景根本不涉及该步骤;或该步骤所属的任务/阶段在执行记录里"
                "从未开始——执行记录是有限窗口,记录止于某处、排在它之后的计划步骤在"
                "记录里自然无从发生,属证据不覆盖而非「本该走没走」(evidence 注明记录"
                "止于何处)。但所属任务/阶段已经开始、只是中途没走某步的仍判 fail,"
                "不得借此洗成 na;**证据中段的空洞**(如工具记录从 Tn 直接跳到 Tm、"
                "其间无该步骤任何痕迹)就是没走,判 fail——不得脑补「中间的调用未"
                "收录但已执行」,窗口判 na 只适用于记录末尾之后的计划步骤。\n"
                f"{pairs_rule}"
                "- indeterminate:你看了执行记录,但证据真的相互矛盾或关键信息缺失,"
                "判断不了走没走。**必须**在 evidence 写明具体矛盾 / 缺失的证据;"
                "不要用 indeterminate 偷懒回避判断。\n\n"
                f"{turn_rule_section}"
                f"## 执行记录摘录\n{_window(transcript_text)}\n\n"
                f"{tool_section}"
                f"{pairs_section}"
                f"## 待判步骤 ({bn} 条)\n{instruction_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:'
                '{"index": 0, "verdict": "pass"|"fail"|"na"|"indeterminate", '
                '"evidence": "中文简述(具体到证据)"'
                f"{turn_refs_field}"
                "}\n"
            )
            specs.append((start, bn, k, prompt))
        successes, failures = _run_batches_concurrent(
            specs, self._parse_step, model, timeout, on_progress, on_usage,
            "步骤", total, lambda v: v[0] is None,
            schema=_STEP_SCHEMA,
            transport="http",
        )
        for start, parsed in successes:
            for j, val in enumerate(parsed):
                results[start + j] = val  # 本地 j → 全量 start+j
        for start, bn, cause in failures:
            for j in range(bn):
                cause_mask[start + j] = cause
        return results, cause_mask

    @staticmethod
    def _parse_step(
        text: str, n: int
    ) -> list[tuple[AuditVerdict | None, str, list[int]]]:
        """解析 LLM 步骤判定 → ``(AuditVerdict | None, evidence, turn_refs_raw)``;默认 ``(None, "", [])``。

        verdict 匹配大小写不敏感(strip + lower):
        - "pass"/"fail"/"na" → (PASS/FAIL/NA, ev, tr)
        - "indeterminate" 且 evidence 非空 → (INDETERMINATE, ev, tr)(判不准)
        - "indeterminate" 但 evidence 空 → **滥用闸 ①** 降级 (None, "", []) = 没判成
        - 未知值 / 非字符串 / 缺键 → (None, "", []) = 没判成(不误判 N/A 放过疑似漏步)

        第三个元素 ``turn_refs_raw``:LLM 输出的 turn 引用(原样透传,sanitize 在 judge 做)。
        """
        results: list[tuple[AuditVerdict | None, str, list[int]]] = [(None, "", [])] * n
        arr_str = extract_first_balanced(text)
        if arr_str is None:
            return results
        items = load_json_lenient(arr_str)
        if not isinstance(items, list):
            return results
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < n:
                v = item.get("verdict")
                ev = str(item.get("evidence", "") or "")
                # 注解 list[Any](同 _parse_prohibition):保 LLM junk 透传给 sanitizer。
                raw = item.get("turn_refs")
                tr: list[Any] = list(raw) if isinstance(raw, list) else []
                if isinstance(v, str):
                    vl = v.strip().lower()
                    if vl == "pass":
                        results[idx] = (AuditVerdict.PASS, ev, tr)
                    elif vl == "fail":
                        results[idx] = (AuditVerdict.FAIL, ev, tr)
                    elif vl == "na":
                        results[idx] = (AuditVerdict.NA, ev, tr)
                    elif vl == "indeterminate":
                        if ev.strip():
                            results[idx] = (AuditVerdict.INDETERMINATE, ev, tr)
                        # else: indeterminate 无 evidence → 滥用闸 ① 降级没判成(保持 (None,"",[]))
                    # else: 未知 verdict 字符串 → 保持 (None,"",[]) = 没判成
                # else: 缺 verdict 键 / 非字符串 → 保持 (None,"",[]) = 没判成
        return results
