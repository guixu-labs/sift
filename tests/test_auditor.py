"""tests for sift.audit.auditor.

_judge_conditional: 条件性指令(TOOL_REQUIREMENT / REFERENCE_USAGE)的三态对账。
全程 mock ``sift.audit.auditor.call_claude``——不真跑 LLM。

覆盖矩阵:
- 命中 tool_trace → PASS 且**不调** call_claude
- 未命中 + LLM applicable=true  → FAIL
- 未命中 + LLM applicable=false → N/A
- 未命中 + LLM 抛 RuntimeError(重试耗尽)→ N/A(低置信度,宁少报不乱报)
- 未命中 + LLM 解析失败          → N/A
- 混合(命中 PASS + 未命中走 LLM)→ 各自正确 verdict
- 批量超 _MAX_LLM_IN_BATCH(10)→ **分批循环处理**(每批 10 条,处理全部,无"批量超限"兜底)
- 空 prohibition/step 桶 → 不调 call_claude(短路返回 [])
- evidence 中文 / category 全 CONDITIONAL / transcript 字段填了 transcript_name
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sift.audit import auditor
from sift.audit.auditor import (
    _MAX_LLM_IN_BATCH, _LLM_RETRY_MAX, TranscriptAuditor,
    _judge_batch_with_retry, _RetryExhaustedType, _run_batches_concurrent,
)
from sift.audit.tool_match import count_tool_calls, match_tool_call
from sift.models import InstructionCheck, ToolCallRecord, ProvenanceAuditResult
from sift.evals.transcript import SessionDigest
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditReport,
    AuditVerdict,
)
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)


@pytest.fixture(autouse=True)
def _no_llm_retry_sleep(monkeypatch) -> None:
    """重试退避的 time.sleep → no-op,保持测试快(retry 逻辑由专门测试覆盖)。"""
    monkeypatch.setattr("sift.audit.auditor.time.sleep", lambda *a, **k: None)


def _tool_inst(text: str = "必须使用 Bash 工具", tool_name: str = "Bash") -> InstructionCheck:
    """造一条 TOOL_REQUIREMENT(params 含 tool_name)。"""
    return InstructionCheck(
        source=InstructionSource.TOOL_REQUIREMENT,
        instruction_text=text,
        section="Tools",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.TOOL_CALL_CHECK,
        params={"tool_name": tool_name},
    )


def _ref_inst(text: str = "读取 design.md", script_path: str = "design.md") -> InstructionCheck:
    """造一条 REFERENCE_USAGE(params 含 script_path)。"""
    return InstructionCheck(
        source=InstructionSource.REFERENCE_USAGE,
        instruction_text=text,
        section="References",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.TOOL_CALL_CHECK,
        params={"script_path": script_path},
    )


def _tc(name: str, input_summary: str = "") -> dict:
    """造一个 dict 风格的 tool_trace 元素(match_tool_call 支持 attr / dict)。"""
    return {"name": name, "input_summary": input_summary}


def _prohibition_inst(
    text: str = "禁止编造测试数据",
    source: InstructionSource = InstructionSource.CONSTRAINT,
) -> InstructionCheck:
    """造一条禁令类指令(CONSTRAINT / ANTI_FABRICATION)。"""
    return InstructionCheck(
        source=source,
        instruction_text=text,
        section="Constraints",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _step_inst(text: str = "Step 1: 读取需求文档") -> InstructionCheck:
    """造一条步骤类指令(HEADING_STEP)。"""
    return InstructionCheck(
        source=InstructionSource.HEADING_STEP,
        instruction_text=text,
        section="Process",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _output_inst(text: str = "输出代码块", sample: str = "```python\nprint('hi')\n```") -> InstructionCheck:
    """造一条 OUTPUT_FORMAT 指令(params 含 sample,check_method=PATTERN_MATCH)。"""
    return InstructionCheck(
        source=InstructionSource.OUTPUT_FORMAT,
        instruction_text=text,
        section="Output",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.PATTERN_MATCH,
        params={"sample": sample},
    )


class TestMatchedNoLlm:
    def test_tool_match_pass_and_no_call(self, monkeypatch) -> None:
        """命中 tool_name → PASS,evidence 中文,method=PROGRAMMATIC,call_claude 未被调用。"""
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "[]"

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        trace = [_tc("Bash", "echo hi")]

        findings = auditor._judge_conditional(
            [_tool_inst(tool_name="Bash")], trace, "某场景", "t1.jsonl"
        )

        assert len(findings) == 1
        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.category == AuditCategory.CONDITIONAL
        assert f.method == AuditMethod.PROGRAMMATIC
        assert f.transcript == "t1.jsonl"
        assert f.evidence == "trace 中调用了 Bash"
        assert f.source == "TOOL_REQUIREMENT"
        assert calls["n"] == 0  # 命中即 PASS,绝不调 LLM

    def test_script_path_match_pass(self, monkeypatch) -> None:
        """命中 script_path → PASS,evidence "trace 中读取了 {path}"。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: pytest.fail("不该调 LLM")
        )
        auditor = TranscriptAuditor()
        trace = [_tc("Read", "cat design.md")]

        findings = auditor._judge_conditional(
            [_ref_inst(script_path="design.md")], trace, "某场景", "t2.jsonl"
        )

        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[0].evidence == "trace 中读取了 design.md"
        assert findings[0].source == "REFERENCE_USAGE"

    def test_section_passthrough_source_section_wins(self, monkeypatch) -> None:
        """finding.section 透传:source_section(真标题回填)优先,缺省回退 section。

        覆盖三条构造路径:命中 PASS 工厂(_make_match_pass_finding)、LLM 失败
        UNRESOLVED 工厂(_make_unresolved_finding)——声明分组标题在一切 verdict 路径
        上都跟指令走(覆盖视图分组键)。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: pytest.fail("不该调 LLM")
        )
        auditor = TranscriptAuditor()
        trace = [_tc("Bash", "echo hi")]

        # 回退路径:source_section 空 → 用 section 伪节名/真标题
        plain = _tool_inst(tool_name="Bash")
        f_plain = auditor._judge_conditional([plain], trace, "某场景", "t1.jsonl")[0]
        assert f_plain.section == "Tools"

        # 回填路径:source_section 有值 → 覆盖 section
        backfilled = _tool_inst(tool_name="Bash")
        backfilled.source_section = "安全红线"
        f_back = auditor._judge_conditional([backfilled], trace, "某场景", "t1.jsonl")[0]
        assert f_back.section == "安全红线"

        # UNRESOLVED 工厂同样透传(不命中 trace + LLM 抛错 → 没判成)
        def boom(*a, **k):
            raise RuntimeError("LLM 挂了")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        f_unres = auditor._judge_conditional([backfilled], [], "某场景", "t1.jsonl")[0]
        assert f_unres.verdict == AuditVerdict.UNRESOLVED
        assert f_unres.section == "安全红线"


class TestUnmatchedLlm:
    def test_applicable_true_is_fail(self, monkeypatch) -> None:
        """未命中 + LLM applicable=true → FAIL(该做没做),method=LLM。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": true}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.CONDITIONAL
        assert f.transcript == "t.jsonl"
        assert f.method == AuditMethod.LLM
        assert f.evidence.startswith("场景应触发")
        assert "Bash" in f.evidence

    def test_applicable_false_is_na(self, monkeypatch) -> None:
        """未命中 + LLM applicable=false(带 reason)→ N/A,evidence 用 LLM 给的具体理由(非套话)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false, "reason": "本任务只生成 spec,未跑测试"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_ref_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.NA
        assert f.category == AuditCategory.CONDITIONAL
        assert f.method == AuditMethod.LLM
        assert "本任务只生成 spec,未跑测试" in f.evidence  # LLM 给的具体理由,非硬编码套话

    def test_applicable_false_without_reason_keeps_na_with_fallback(self, monkeypatch) -> None:
        """applicable=false 但 LLM 未给 reason → 仍 N/A,evidence 退兜底套话(不降级,对齐 step)。

        与 indeterminate 滥用闸不同:N/A 是合法高频判决,空 reason **不**降级 UNRESOLVED,
        只退兜底 evidence——镜像 step judge 的 ``ev or "场景不涉及该步"``(捕获+用,非惩罚闸)。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_ref_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.NA  # 空 reason 不降级,仍 N/A
        assert "场景不涉及" in f.evidence      # 退兜底套话

    def test_runtime_error_unverifiable_na(self, monkeypatch) -> None:
        """call_claude 持续抛 RuntimeError(重试耗尽)→ 标 N/A(宁少报不乱报)。"""
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("claude CLI down")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 不报 FAIL,标 UNRESOLVED(没判成)
        assert f.unresolved_cause == "unknown"  # "claude CLI down" 未命中模式 → unknown
        assert f.category == AuditCategory.CONDITIONAL
        assert f.method == AuditMethod.LLM  # 低置信度(判不准兜底,非 LLM 明确判 N/A)
        assert "无法确证" in f.evidence
        # 重试耗尽:1 次首发 + _LLM_RETRY_MAX 次重试,全失败才兜底
        assert calls["n"] == 1 + _LLM_RETRY_MAX

    def test_parse_failure_unverifiable_na(self, monkeypatch) -> None:
        """LLM 返回非 JSON → 解析失败 → 标 N/A(宁少报不乱报)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: "我觉得都不该触发"
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert "无法确证" in f.evidence
        assert f.method == AuditMethod.LLM

    def test_missing_applicable_key_is_unverifiable_na(self, monkeypatch) -> None:
        """条目有 index 但缺 applicable 键 → 标 N/A(宁少报不乱报)。

        与 I1 当初相反:I1 担心"缺键被洗成 N/A → 漏报违规"故保守 FAIL;但用户原则
        '宁可少报不能乱报'推翻之——LLM 没给明确 applicable = 判不准 = N/A,绝不报假 FAIL。
        严格 ``is True/is False`` 判断保留(畸形值不洗成明确 N/A,仍走判不准兜底 N/A)。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED,不报 FAIL
        assert f.method == AuditMethod.LLM  # 低置信度(判不准兜底)
        assert "无法确证" in f.evidence

    def test_string_applicable_value_is_unverifiable_na(self, monkeypatch) -> None:
        """applicable 是字符串 "false"(非布尔)→ 标 N/A(宁少报不乱报)。

        回归 M1:旧实现 ``bool("false")`` = True(非空串恒真)会洗畸形值。现保留严格
        ``is True/is False`` 判断(畸形非布尔值不当明确结论),落到判不准兜底 → N/A。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": "false"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED,不报 FAIL
        assert f.method == AuditMethod.LLM
        assert "无法确证" in f.evidence

    def test_applicable_prompt_has_trace_and_window_rule(self, monkeypatch) -> None:
        """未命中交 LLM 时,prompt 必须带工具调用记录 + 「窗口外不判应触发」规则。

        2026-08-30 误报修复的守卫:条件性判官原只看场景描述、看不到记录止于何处——
        场景文本提过整个计划,就对「系于从未开始阶段的指令」一律 applicable=true →
        trace 未调用 → 假 FAIL(真跑暴露:验收标准段里的标识符会被抽成工具要求)。
        补 tool_trace 可见性 + 窗口规则(含反向闸:已开始阶段不得借窗口洗掉)。
        """
        prompts: list[str] = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{
                "index": 0, "applicable": False,
                "reason": "记录止于 Task 32 验收,Task 37 从未开始",
            }])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        trace = [ToolCallRecord(name="Task", input_summary="Verify Task 32 - Branch-1 KernelST")]

        TranscriptAuditor()._judge_conditional(
            [_tool_inst(text="调用 TilingFunc", tool_name="TilingFunc")],
            trace, "某场景", "w.jsonl",
        )

        assert len(prompts) == 1
        assert "实际工具调用记录" in prompts[0]
        assert "Verify Task 32" in prompts[0]  # trace 内容真进了 prompt(不是空段)
        assert "从未开始" in prompts[0]        # 窗口规则
        assert "不判应触发" in prompts[0]
        assert "不得借「记录窗口」" in prompts[0]  # 反向闸


class TestRetryRecovery:
    """整批 transient 失败时重试救回:前几次 RuntimeError、后续成功 → 正常判定,不兜底 N/A。

    回归 audit-out3:2 个整批(25 条)撞限流直接放弃 → N/A。重试后 transient 失败大多
    能救回,真判(给出 resolved verdict)而非兜底。
    """

    def test_retry_recovers_then_judges_normally(self, monkeypatch) -> None:
        """call_claude 前 _LLM_RETRY_MAX 次 RuntimeError、第 3 次成功 → 重试救回,resolved N/A。"""
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] <= _LLM_RETRY_MAX:  # 前 _LLM_RETRY_MAX 次 transient 失败
                raise RuntimeError("transient rate limit")
            return '[{"index": 0, "applicable": false}]'  # 第 3 次:N/A resolved

        monkeypatch.setattr("sift.audit.auditor.call_claude", flaky)
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.NA  # applicable=false → N/A
        # 关键:resolved(0.8)而非兜底(0.3)→ 重试成功救回,真判了
        assert f.method == AuditMethod.LLM
        assert "场景不涉及" in f.evidence  # LLM 给的 evidence,非"无法确证"兜底
        assert calls["n"] == 1 + _LLM_RETRY_MAX  # 重试 _LLM_RETRY_MAX 次后第 3 次成功

    def test_429_backoff_is_exponential(self, monkeypatch) -> None:
        """429 连撞 → 退避指数递增(2/4/8/16s,基数×2^attempt)。

        速率限制按分钟窗口计,固定 2s 退避在窗口内反复撞死(glm-5.3 实测 8 并发 3 个
        秒 429);指数退避让连撞的等待跨过窗口。抖动置 0 断言精确序列。
        """
        import sift.audit.auditor as aud_mod
        from sift.utils import _HttpRateLimited
        sleeps: list[float] = []

        def always_429(*a, **k):
            raise _HttpRateLimited("裸 HTTP 429(账户速率/并发限制)")

        monkeypatch.setattr("sift.audit.auditor.call_claude", always_429)
        monkeypatch.setattr(aud_mod.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(aud_mod.random, "uniform", lambda a, b: 0.0)

        findings = TranscriptAuditor()._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        assert findings[0].verdict == AuditVerdict.UNRESOLVED  # 耗尽 → 没判成
        # 429 专属退避(except 分支):基数 × 2^attempt → 2/4/8/16(attempt 0 起)
        throttle_waits = [s for s in sleeps if s in (2.0, 4.0, 8.0, 16.0)]
        assert throttle_waits == [2.0, 4.0, 8.0, 16.0]


class TestMixed:
    def test_match_and_unmixed_verdicts(self, monkeypatch) -> None:
        """两条指令:一条命中 PASS、一条未命中走 LLM → 各自正确 verdict + 顺序保持。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        auditor = TranscriptAuditor()
        insts = [
            _tool_inst(tool_name="Bash"),       # 命中 → PASS
            _ref_inst(script_path="design.md"),  # 未命中 → LLM applicable=false → N/A
        ]
        trace = [_tc("Bash", "echo hi")]

        findings = auditor._judge_conditional(insts, trace, "某场景", "mix.jsonl")

        assert len(findings) == 2
        # 顺序与输入一致
        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[1].verdict == AuditVerdict.NA
        # 全部 CONDITIONAL + transcript 字段填了
        for f in findings:
            assert f.category == AuditCategory.CONDITIONAL
            assert f.transcript == "mix.jsonl"
            # evidence 全中文(不含 ASCII 句号;粗略断言非空中文)
            assert f.evidence
        # derived_from_transcript 暂默认 False(Task 8 聚合时再标)
        for f in findings:
            assert f.derived_from_transcript is False


class TestBatching:
    """分批判定:>_MAX_LLM_IN_BATCH(10)条时分批循环处理全部,无"批量超限"兜底 N/A。"""

    def test_conditional_over_batch_size_batched_all_judged(self, monkeypatch) -> None:
        """32 条全未命中:分 4 批(10+10+10+2)各调一次 LLM,32 条全判,无溢出兜底。

        mock 按每批 prompt 的条数返回全 applicable=false → 全 N/A;验证 call_claude
        被调 4 次、32 条全 N/A、无"批量超限"/"无法确证"兜底。
        """
        calls: list[int] = []

        def fake(prompt, *a, **k):
            m = re.search(r"待判指令 \((\d+) 条\)", prompt)
            bn = int(m.group(1)) if m else 0
            calls.append(bn)
            return json.dumps([{"index": i, "applicable": False} for i in range(bn)])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        insts = [_tool_inst(tool_name=f"Tool{i}") for i in range(32)]

        findings = auditor._judge_conditional(insts, [], "大场景", "big.jsonl")

        assert len(findings) == 32
        assert calls == [10, 10, 10, 2]  # 分 10+10+10+2 四批
        # 全 N/A(applicable=false),无兜底 N/A / 无"批量超限"
        assert all(f.verdict == AuditVerdict.NA for f in findings)
        assert all("批量超限" not in f.evidence for f in findings)
        assert all("无法确证" not in f.evidence for f in findings)


# ---------------------------------------------------------------------------
# classify: 指令类别路由
# ---------------------------------------------------------------------------


class TestClassify:
    """classify 把 InstructionSource 路由到对应的 AuditCategory。"""

    @pytest.mark.parametrize(
        "source,expected",
        [
            (InstructionSource.TOOL_REQUIREMENT, AuditCategory.CONDITIONAL),
            (InstructionSource.REFERENCE_USAGE, AuditCategory.CONDITIONAL),
            (InstructionSource.CONSTRAINT, AuditCategory.PROHIBITION),
            (InstructionSource.ANTI_FABRICATION, AuditCategory.PROHIBITION),
            (InstructionSource.HEADING_STEP, AuditCategory.STEP),
            (InstructionSource.OUTPUT_FORMAT, AuditCategory.OUTPUT),
        ],
    )
    def test_routing(self, source: InstructionSource, expected: AuditCategory) -> None:
        inst = InstructionCheck(
            source=source,
            instruction_text="x",
            section="s",
            priority=InstructionPriority.REQUIRED,
            check_method=CheckMethod.LLM_JUDGE,
            params={},
        )
        assert TranscriptAuditor.classify(inst) == expected


# ---------------------------------------------------------------------------
# _judge_prohibition: 禁令类(CONSTRAINT / ANTI_FABRICATION)主动检测违规
# ---------------------------------------------------------------------------


class TestJudgeProhibition:
    def test_violated_true_is_fail(self, monkeypatch) -> None:
        """violated=true → FAIL(违规了),evidence 取 LLM 返回,method=LLM。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": true, "evidence": "执行中编造了测试数据"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        assert len(findings) == 1
        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.PROHIBITION
        assert f.transcript == "p.jsonl"
        assert f.evidence == "执行中编造了测试数据"
        assert f.method == AuditMethod.LLM
        assert f.source == "CONSTRAINT"

    def test_violated_false_is_pass(self, monkeypatch) -> None:
        """violated=false → PASS(没违规)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": "未发现编造行为"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.category == AuditCategory.PROHIBITION
        assert f.evidence == "未发现编造行为"
        assert f.method == AuditMethod.LLM

    def test_runtime_error_unverifiable_na(self, monkeypatch) -> None:
        """call_claude 抛 RuntimeError → 标 N/A(宁少报不乱报:禁令也不在无确证时报 FAIL)。"""
        def boom(*a, **k):
            raise RuntimeError("claude CLI down")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED,不报假 FAIL
        assert f.category == AuditCategory.PROHIBITION
        assert f.method == AuditMethod.LLM
        assert "无法确证" in f.evidence

    def test_parse_failure_unverifiable_na(self, monkeypatch) -> None:
        """LLM 返回非 JSON → 解析失败 → 标 N/A(宁少报不乱报)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: "看起来没违规"
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.method == AuditMethod.LLM
        assert "无法确证" in f.evidence

    def test_missing_violated_key_is_unverifiable_na(self, monkeypatch) -> None:
        """条目有 index 但缺 violated 键 → 严格判断,标 N/A(不被误当 PASS,也不报假 FAIL)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "evidence": "不确定"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED,既不当 PASS 也不报 FAIL
        assert f.method == AuditMethod.LLM
        assert "无法确证" in f.evidence

    def test_nonbool_violated_is_unverifiable_na(self, monkeypatch) -> None:
        """violated 是字符串 "true"(非布尔)→ 严格 is True/is False,标 N/A。

        回归:bool("true")=True 会把畸形值洗成 FAIL(方向碰巧对但原理错);
        "false" 会被 bool() 洗成 True(非空串恒真)→ 错判违规。严格判断避免洗 PASS;
        畸形值落判不准兜底 → N/A(宁少报不乱报,不报假 FAIL)。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": "false"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED
        assert f.method == AuditMethod.LLM
        assert "无法确证" in f.evidence

    def test_anti_fabrication_source(self, monkeypatch) -> None:
        """ANTI_FABRICATION 也走 prohibition 判定。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": "无编造"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst(source=InstructionSource.ANTI_FABRICATION)],
            "某执行记录",
            "p.jsonl",
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.source == "ANTI_FABRICATION"
        assert f.category == AuditCategory.PROHIBITION

    def test_empty_instructions_no_llm(self, monkeypatch) -> None:
        """空 prohibition 桶(0 条)→ 不调 call_claude,直接返回 [](短路)。

        回归 Issue 1:空桶仍 spawn claude -p(生成"待判 0 条"prompt)浪费调用,
        与 audit"零重跑低成本"相悖。短路后不再调 LLM。
        """
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "[]"

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition([], "某执行记录", "empty.jsonl")

        assert findings == []
        assert calls["n"] == 0  # 空桶短路,未 spawn LLM

    def test_over_batch_size_batched_all_judged(self, monkeypatch) -> None:
        """35 条禁令:分 4 批(10+10+10+5)处理,全部判 PASS,无"批量超限"兜底。

        回归 Issue 2:旧实现单次截断、超限兜底("批量超限未判"),假 FAIL 淹没真问题。
        分批后 35 条全判(mock 按批返回 violated=false → 全 PASS)。
        """
        calls: list[int] = []

        def fake(prompt, *a, **k):
            m = re.search(r"待判禁令 \((\d+) 条\)", prompt)
            bn = int(m.group(1)) if m else 0
            calls.append(bn)
            return json.dumps([
                {"index": i, "violated": False, "evidence": "未违反"}
                for i in range(bn)
            ])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        insts = [_prohibition_inst(text=f"禁令 #{i}") for i in range(35)]

        findings = auditor._judge_prohibition(insts, "某执行记录", "batch.jsonl")

        assert len(findings) == 35
        assert calls == [10, 10, 10, 5]  # 分 10+10+10+5 四批
        assert all(f.verdict == AuditVerdict.PASS for f in findings)
        assert all(f.category == AuditCategory.PROHIBITION for f in findings)
        assert all("批量超限" not in f.evidence for f in findings)
        assert all("保守" not in f.evidence for f in findings)


# ---------------------------------------------------------------------------
# _judge_step: 步骤类(HEADING_STEP)三态判定
# ---------------------------------------------------------------------------


class TestJudgeStep:
    def test_verdict_pass(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "pass", "evidence": "走过该步"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.category == AuditCategory.STEP
        assert f.transcript == "s.jsonl"
        assert f.evidence == "走过该步"
        assert f.method == AuditMethod.LLM

    def test_verdict_fail(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "fail", "evidence": "该走没走"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")

        assert findings[0].verdict == AuditVerdict.FAIL
        assert findings[0].category == AuditCategory.STEP

    def test_verdict_na(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "na", "evidence": "场景不涉及"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")

        assert findings[0].verdict == AuditVerdict.NA
        assert findings[0].category == AuditCategory.STEP

    def test_verdict_case_insensitive(self, monkeypatch) -> None:
        """verdict 大小写不敏感:PASS / Fail / NA 都能识别。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "PASS"}]',
        )
        auditor = TranscriptAuditor()
        assert auditor._judge_step([_step_inst()], "x", "s.jsonl")[0].verdict == AuditVerdict.PASS

    def test_tool_trace_included_in_prompt(self, monkeypatch) -> None:
        """涉及工具的步骤,tool_trace 应进 prompt 让 LLM 看到实际工具调用。

        canary(log-error-analyzer)暴露的缺口:Read input log 步骤明明调了 Read,
        但 transcript_text 只剩 assistant 文字声称 → LLM 看不到实际调用 → 假 FAIL。
        修复:tool_trace 进 step 判定 prompt。
        """
        from sift.models import ToolCallRecord

        captured: dict = {}

        def fake(prompt, *a, **k):
            captured["prompt"] = prompt
            return '[{"index": 0, "verdict": "pass", "evidence": "走过"}]'

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        trace = [ToolCallRecord(name="Read", input_summary='{"file_path": "/x/logs/app.log"}')]

        auditor._judge_step([_step_inst()], "执行记录", "s.jsonl", tool_trace=trace)

        assert "app.log" in captured["prompt"], "tool_trace 应进 step 判定 prompt"
        assert "Read" in captured["prompt"]

    def test_no_tool_trace_omits_section(self, monkeypatch) -> None:
        """不传 tool_trace 时 prompt 不含工具调用段(向后兼容,现有调用不破)。

        断 ``## 实际工具调用记录`` 段头(而非裸串 ``工具调用记录``):turn 引用规则的说明文字
        里会顺带提到"工具调用记录同"(讲明工具调用也带 [T{idx}] 标记),裸串会误命中。
        段头才是真正"是否渲染了 tool_trace 段"的标志。
        """
        captured: dict = {}

        def fake(prompt, *a, **k):
            captured["prompt"] = prompt
            return '[{"index": 0, "verdict": "pass", "evidence": "走过"}]'

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        auditor._judge_step([_step_inst()], "执行记录", "s.jsonl")
        assert "## 实际工具调用记录" not in captured["prompt"]

    def test_unknown_verdict_unverifiable_na(self, monkeypatch) -> None:
        """未知 verdict(如 'maybe')→ 标 N/A(宁少报不乱报:判不准不报 FAIL)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "maybe"}]',
        )
        auditor = TranscriptAuditor()

        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED,不报 FAIL
        assert f.category == AuditCategory.STEP
        assert f.method == AuditMethod.LLM
        assert "无法确证" in f.evidence

    def test_runtime_error_unverifiable_na(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        auditor = TranscriptAuditor()

        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED
        assert f.method == AuditMethod.LLM
        assert "无法确证" in f.evidence

    def test_missing_verdict_key_unverifiable_na(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "evidence": "?"}]',
        )
        auditor = TranscriptAuditor()

        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED
        assert f.method == AuditMethod.LLM
        assert "无法确证" in f.evidence

    def test_empty_instructions_no_llm(self, monkeypatch) -> None:
        """空 step 桶(0 条)→ 不调 call_claude,直接返回 [](短路)。

        回归 Issue 1:空桶仍 spawn claude -p 浪费调用。短路后不再调 LLM。
        """
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "[]"

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()

        findings = auditor._judge_step([], "某执行记录", "empty.jsonl")

        assert findings == []
        assert calls["n"] == 0  # 空桶短路,未 spawn LLM

    def test_over_60_batched_index_mapping(self, monkeypatch) -> None:
        """62 条步骤:分 7 批(10×6+2)处理,7 次 call_claude,全判且 index 不错位。

        回归 Issue 2 + 验证分批易错点(index 映射):mock 按调用次序把全局位置写进
        evidence(批 k 的本地 i → 全局 k*10+i),据此验证批间 index 映射——批 2 本地
        [0] 正确落到全局 [10]、批 7 本地 [0]→[60],而非覆盖 [0]。错位会把某批结果
        写回错位置或漏写,被 evidence 的 global# 与原位置不一致抓出。
        """
        calls: list[int] = []

        def fake(prompt, *a, **k):
            m = re.search(r"待判步骤 \((\d+) 条\)", prompt)
            bn = int(m.group(1)) if m else 0
            call_idx = len(calls)
            start = call_idx * _MAX_LLM_IN_BATCH
            calls.append(bn)
            return json.dumps([
                {"index": i, "verdict": "pass", "evidence": f"global#{start + i}"}
                for i in range(bn)
            ])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        insts = [_step_inst(text=f"步骤 #{i}") for i in range(62)]

        findings = auditor._judge_step(insts, "某执行记录", "big.jsonl")

        assert len(findings) == 62
        assert calls == [10, 10, 10, 10, 10, 10, 2]  # 7 批(10×6+2)
        assert all(f.verdict == AuditVerdict.PASS for f in findings)
        assert all("批量超限" not in f.evidence for f in findings)
        # index 映射:evidence 的 global# 与原位置严格一致
        # (批 2 本地[0]→全局[10]、批 7 本地[0]→[60],不错位、不覆盖)
        for i, f in enumerate(findings):
            assert f.evidence == f"global#{i}", f"位置 {i} 映射错位: {f.evidence}"

    def test_step_prompt_has_window_boundary_rule(self, monkeypatch) -> None:
        """步骤判官 prompt 必须含「记录窗口外 → na」规则 + 防洗回闸。

        2026-08-30 判定质量回归的守卫:深思考档模型把「记录止于 Tn、其后的计划任务
        从未开始」的步骤批量判 fail(64/72),prompt 不写明窗口语义时该口径随模型
        漂移。钉住三条:① 窗口外(所属任务/阶段从未开始)判 na 不是 fail;② 反向闸
        ——任务/阶段已开始、中途没走的仍判 fail,防规则被滥用成洗 fail 的口子;
        ③ 证据中段空洞(工具记录 Tn 跳到 Tm、其间无痕迹)判 fail,防判官脑补
        「中间调用未收录但已执行」把段级缺席洗成 pass。
        """
        prompts: list[str] = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "verdict": "na", "evidence": "记录止于 T120"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()

        auditor._judge_step([_step_inst()], "某执行记录", "w.jsonl")

        assert len(prompts) == 1
        assert "执行记录是有限窗口" in prompts[0]
        assert "从未开始" in prompts[0]
        assert "不得借此洗成 na" in prompts[0]
        # ③ 证据中段空洞防脑补:同证据下「工具记录 Tn 跳到 Tm」被判官时而过 fail
        # (r4)、时而脑补「中间调用未收录但已执行」过 pass(r5)——FAIL↔PASS 摆动,
        # 钉死为段级缺席判 fail。
        assert "证据中段的空洞" in prompts[0]
        assert "不得脑补「中间的调用未收录但已执行」" in prompts[0]
        assert "窗口判 na 只适用于记录末尾之后的计划步骤" in prompts[0]


# ---------------------------------------------------------------------------
# _judge_output: OUTPUT_FORMAT(PATTERN_MATCH)确定性子串判定,不调 LLM
# ---------------------------------------------------------------------------


class TestJudgeOutput:
    def test_no_llm_called(self, monkeypatch) -> None:
        """_judge_output 是确定性判定,绝不调 call_claude。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: pytest.fail("_judge_output 不该调 LLM"),
        )
        auditor = TranscriptAuditor()
        auditor._judge_output([_output_inst()], "任意文本", "o.jsonl")

    def test_sample_substring_match_is_pass(self) -> None:
        """sample 某行(strip 后)作为子串出现在 transcript_text → PASS,method=PROGRAMMATIC。"""
        auditor = TranscriptAuditor()
        # sample 含 "print('hi')" 这一行;transcript 里出现该子串 → PASS
        findings = auditor._judge_output(
            [_output_inst(sample="```python\nprint('hi')\n```")],
            "结果是 print('hi') 的输出",
            "o.jsonl",
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.category == AuditCategory.OUTPUT
        assert f.method == AuditMethod.PROGRAMMATIC
        assert f.transcript == "o.jsonl"
        assert f.source == "OUTPUT_FORMAT"
        assert "期望格式" in f.evidence

    def test_sample_no_match_is_fail(self) -> None:
        """sample 行都不在 transcript_text → FAIL,method=PROGRAMMATIC。"""
        auditor = TranscriptAuditor()
        findings = auditor._judge_output(
            [_output_inst(sample="```python\nprint('hi')\n```")],
            "完全无关的文本",
            "o.jsonl",
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.OUTPUT
        assert f.method == AuditMethod.PROGRAMMATIC
        assert "未找到期望格式" in f.evidence

    def test_multiline_any_line_matches(self) -> None:
        """多行 sample:任一行(strip 后)命中 → PASS(照 checker PATTERN_MATCH 逻辑)。"""
        auditor = TranscriptAuditor()
        inst = _output_inst(sample="line one\nSPECIAL_TOKEN\nline three")
        findings = auditor._judge_output([inst], "中间有 SPECIAL_TOKEN 出现", "o.jsonl")

        assert findings[0].verdict == AuditVerdict.PASS

    def test_strips_lines_before_match(self) -> None:
        """sample 行带前后空白 → strip 后再作子串匹配(照 checker 逻辑)。"""
        auditor = TranscriptAuditor()
        inst = _output_inst(sample="  padded_token  ")
        findings = auditor._judge_output([inst], "hit padded_token here", "o.jsonl")

        assert findings[0].verdict == AuditVerdict.PASS

    def test_empty_sample_is_fail(self) -> None:
        """sample 为空 → 无可匹配行 → FAIL(照 checker PATTERN_MATCH 空样本行为)。"""
        auditor = TranscriptAuditor()
        inst = _output_inst(sample="")
        findings = auditor._judge_output([inst], "任意文本", "o.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.OUTPUT
        assert f.method == AuditMethod.PROGRAMMATIC

    def test_multiple_instructions_order_preserved(self) -> None:
        """多条 OUTPUT_FORMAT 指令:顺序与输入一致,各自判定。"""
        auditor = TranscriptAuditor()
        insts = [
            _output_inst(text="格式A", sample="AAA_TOKEN"),
            _output_inst(text="格式B", sample="BBB_TOKEN"),
        ]
        findings = auditor._judge_output([insts[0], insts[1]], "只有 AAA_TOKEN", "o.jsonl")

        assert len(findings) == 2
        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[1].verdict == AuditVerdict.FAIL
        for f in findings:
            assert f.category == AuditCategory.OUTPUT
            assert f.transcript == "o.jsonl"


# ---------------------------------------------------------------------------
# INDETERMINATE(判不准):LLM 看了证据真模糊 → 独立于 UNRESOLVED(没判成)。
# 三判官各支持 indeterminate 出口;滥用闸 ① 空 reason/evidence → 降级 UNRESOLVED;
# 滥用闸 ② audit() 统计 indeterminate 占比超阈值 → 告警。
# ---------------------------------------------------------------------------


class TestIndeterminate:
    def test_conditional_indeterminate_with_reason(self, monkeypatch) -> None:
        """条件性 LLM 返回 indeterminate=true + reason → INDETERMINATE(method=LLM,evidence=reason)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "indeterminate": true, "reason": "场景是否涉及该工具语焉不详"}]',
        )
        auditor = TranscriptAuditor()
        f = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")[0]
        assert f.verdict == AuditVerdict.INDETERMINATE
        assert f.method == AuditMethod.LLM
        assert f.evidence == "场景是否涉及该工具语焉不详"

    def test_conditional_indeterminate_without_reason_downgrades(self, monkeypatch) -> None:
        """滥用闸 ①:indeterminate=true 但 reason 空 → 降级 UNRESOLVED(不许空口判不准)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "indeterminate": true}]',
        )
        auditor = TranscriptAuditor()
        f = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 空 reason → 降级没判成
        assert f.method == AuditMethod.LLM

    def test_prohibition_indeterminate_with_reason(self, monkeypatch) -> None:
        """禁令 LLM 返回 indeterminate=true + evidence → INDETERMINATE。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "indeterminate": true, "evidence": "执行记录相互矛盾无法判定"}]',
        )
        auditor = TranscriptAuditor()
        f = auditor._judge_prohibition([_prohibition_inst()], "某执行记录", "p.jsonl")[0]
        assert f.verdict == AuditVerdict.INDETERMINATE
        assert f.method == AuditMethod.LLM
        assert f.evidence == "执行记录相互矛盾无法判定"

    def test_step_indeterminate_with_reason(self, monkeypatch) -> None:
        """步骤 LLM 返回 verdict=indeterminate + evidence → INDETERMINATE。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "indeterminate", "evidence": "看不出走没走这一步"}]',
        )
        auditor = TranscriptAuditor()
        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.INDETERMINATE
        assert f.method == AuditMethod.LLM
        assert f.evidence == "看不出走没走这一步"

    def test_step_indeterminate_without_evidence_downgrades(self, monkeypatch) -> None:
        """滥用闸 ①:步骤 verdict=indeterminate 但 evidence 空 → 降级 UNRESOLVED。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "indeterminate"}]',
        )
        auditor = TranscriptAuditor()
        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED

    def test_abuse_gate_warns_high_indeterminate_rate(self, tmp_path, monkeypatch) -> None:
        """滥用闸 ②:5 条禁令全判不准(100% > 20%、total>=5)→ audit() 产滥用告警。

        判不准是 LLM 逃逸口(看不懂就说"判不准"蒙混),占比偏高需告警让人工复核。
        阈值 _INDETERMINATE_ABUSE_RATE=0.20 + 最小样本 5。
        """
        # fake:按 prompt 里的"待判禁令 (N 条)"返回 N 条 indeterminate+evidence
        def fake(prompt, *a, **k):
            m = re.search(r"待判禁令 \((\d+) 条\)", prompt)
            bn = int(m.group(1)) if m else 0
            return json.dumps([
                {"index": i, "indeterminate": True, "evidence": "证据矛盾"}
                for i in range(bn)
            ])
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)

        body = "\n\n".join([
            "NEVER fabricate data",
            "NEVER skip the test step",
            "NEVER delete production data",
            "NEVER ignore validation errors",
            "NEVER hardcode secrets",
        ])
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )
        report = TranscriptAuditor().audit("s", body, [tp])

        # 5 条禁令全 INDETERMINATE,占比 100% > 20% → 滥用告警
        assert report.summary["indeterminate"] >= 5
        assert any("判不准" in w and "占比偏高" in w for w in report.warnings), report.warnings


# ---------------------------------------------------------------------------
# audit: 聚合主入口(串联 InstructionExtractor + rebuild + derive + 四 _judge_*)
# 全程 mock sift.audit.auditor.call_claude —— 不真跑 LLM。
# mock 设计:fake 返回一个覆盖 0..59 的 JSON 数组,每条同时给 applicable / violated /
# verdict + evidence 三个键。三个 LLM 判定方法各只读自己关心的键(_parse_applicable 读
# applicable、_parse_prohibition 读 violated、_parse_step 读 verdict),互不干扰 ——
# 一个通用结构兼容三种判定格式,无需按 prompt 内容分支。
# ---------------------------------------------------------------------------


def _pass_fake():
    """通用 mock call_claude:返回全"通过"语义的 JSON 数组(覆盖 0..59)。

    - 条件性未命中 → applicable=false → N/A(场景不涉及)
    - 禁令 → violated=false → PASS(没违规)
    - 步骤 → verdict="pass" → PASS(走过了)

    一个结构喂三个判定方法,便于聚合测试数 finding / 算 summary。
    """
    def fake(prompt, *a, **k):
        return json.dumps([
            {
                "index": i,
                "applicable": False,
                "violated": False,
                "verdict": "pass",
                "evidence": "通过",
            }
            for i in range(60)
        ])

    return fake


def _headline_fake():
    """通用 mock call_claude:全通过 + 带 headline(供 headline 生成/透传测试)。

    一个结构兼容三种判定格式 + headline(key 各取所需):headline 步骤读 headline,
    conditional 读 applicable、prohibition 读 violated、step 读 verdict。
    """
    def fake(prompt, *a, **k):
        return json.dumps([
            {
                "index": i,
                "applicable": False,
                "violated": False,
                "verdict": "pass",
                "evidence": "通过",
                "headline": f"标题 {i}",
            }
            for i in range(60)
        ])

    return fake


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    """把 records 写成 JSONL(每行一个 JSON 对象),返回该路径。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _read_file_records(
    file_path: str,
    content: str = "文件内容",
    user_text: str = "请分析这个文件",
) -> list[dict]:
    """造一组 session JSONL 记录:user turn + Read(file_path) + 其 tool_result。

    让 build_digest 产出非空 digest:user_turns 非空、tool_trace 含 Read、
    input_files 含 file_path(未被改写 → 计入输入),供反推/对账用。
    """
    return [
        {"message": {"role": "user", "content": user_text}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": file_path},
                    }
                ],
            }
        },
        {
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": content}
                ],
            }
        },
    ]


class TestAuditAggregate:
    """audit 主入口:多 transcript 聚合、summary 计数、derived 标记、坏 transcript 降级。"""

    def test_two_transcripts_aggregate_with_summary_and_derived(
        self, tmp_path, monkeypatch
    ) -> None:
        """2 transcript 各 Read foo.md:每条产 2 finding(1 N/A + 1 PASS-derived),平铺 4 条。

        - skill_body "use Bash" → 1 条 TOOL_REQUIREMENT(未命中 trace → LLM N/A);
        - foo.md 不在 skill_body → derive_declarations 反推 "读取 foo.md"(REFERENCE_USAGE),
          且 transcript 真读了 → 命中 trace → PASS,标 derived_from_transcript=True。
        每 transcript 2 finding → 共 4;summary total/pass/fail/na 精确。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())

        foo = tmp_path / "foo.md"
        tp1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        tp2 = _write_jsonl(tmp_path / "t2.jsonl", _read_file_records(str(foo)))

        auditor = TranscriptAuditor()
        report = auditor.audit("myskill", "use Bash", [tp1, tp2])

        assert isinstance(report, AuditReport)
        assert report.skill_name == "myskill"
        # transcripts 字段含 2 个文件名(顺序与入参一致)
        assert report.transcripts == ["t1.jsonl", "t2.jsonl"]
        assert report.warnings == []

        # 平铺 per-transcript:每 transcript 2 finding → 共 4
        assert len(report.findings) == 4
        # 每 transcript 的 finding 各自带正确的 transcript 名(2 + 2)
        assert sorted(f.transcript for f in report.findings) == ["t1.jsonl", "t1.jsonl", "t2.jsonl", "t2.jsonl"]

        # summary 精确:每 transcript (1 N/A [Bash 未调] + 1 PASS [读取 foo.md 命中])
        assert report.summary == {"total": 4, "pass": 2, "fail": 0, "na": 2, "unresolved": 0, "indeterminate": 0}

        # derived 标记:2 个 PASS finding(读取 foo.md)标 derived_from_transcript=True,
        # 2 个 N/A finding(use Bash,来自 skill_body 静态指令)保持 False
        derived = [f for f in report.findings if f.derived_from_transcript]
        assert len(derived) == 2
        assert all(f.verdict == AuditVerdict.PASS for f in derived)
        assert all(f.category == AuditCategory.CONDITIONAL for f in derived)
        assert all("foo.md" in f.instruction_text for f in derived)
        non_derived = [f for f in report.findings if not f.derived_from_transcript]
        assert len(non_derived) == 2
        assert all(f.verdict == AuditVerdict.NA for f in non_derived)

        # 声明分组标题透传:body "use Bash" 无标题 → 标题级 extractor 的 section="Root"
        bash_rows = [r for r in report.by_instruction if "Bash" in r.instruction_text]
        assert len(bash_rows) == 1
        assert bash_rows[0].section == "Root"
        assert all(f.section == "Root" for f in non_derived)

        # 声明覆盖视图:唯一声明指令(use Bash)两段都 N/A → uncovered;derived
        # (读取 foo.md)不计分母 → total=1。分组 "Root"。
        cov = report.coverage
        assert cov.total == 1
        assert (cov.covered, cov.uncovered, cov.unknown) == (0, 1, 0)
        assert cov.coverage_rate == 0.0
        assert [s.section for s in cov.sections] == ["Root"]
        assert cov.sections[0].instructions[0].instruction_text.startswith("use")

    def test_audit_populates_token_usage_from_call_claude(
        self, tmp_path, monkeypatch
    ) -> None:
        """on_usage 穿透整条判官漏斗到 call_claude:mock 取出 on_usage 回灌 usage →
        report.token_usage 累计(calls>=1、input>0、cost>0)。

        证明 on_usage 从 audit() 经 _judge_* → _llm_judge_* → _run_batches_concurrent →
        _judge_batch_with_retry → call_claude 一路传到位(任一环漏传就 calls==0)。
        """
        inner = _pass_fake()

        def fake(prompt, *a, **k):
            ou = k.get("on_usage")
            if ou:  # call_claude json 模式抓到 usage 才回调
                ou({"input_tokens": 100, "output_tokens": 50, "total_cost_usd": 0.02})
            return inner(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)

        foo = tmp_path / "foo.md"
        tp = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        report = TranscriptAuditor().audit("myskill", "use Bash", [tp])

        tu = report.token_usage
        assert tu.get("calls", 0) >= 1
        assert tu.get("input_tokens", 0) > 0
        assert tu.get("total_cost_usd", 0) > 0

    def test_audit_token_usage_empty_when_no_llm(self, tmp_path, monkeypatch) -> None:
        """空 transcripts → 不跑任何判官 → token_usage 空(无 LLM 调用,report 干净)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        report = TranscriptAuditor().audit("myskill", "use Bash", [])
        assert report.token_usage == {}

    def test_audit_populates_duration_seconds(self, tmp_path, monkeypatch) -> None:
        """audit() 头尾 perf_counter → duration_seconds > 0(audit 跑过即有正墙钟,不依赖判官是否调用)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        foo = tmp_path / "foo.md"
        tp = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        report = TranscriptAuditor().audit("myskill", "use Bash", [tp])
        assert report.duration_seconds > 0  # 实现前 default 0.0 → fail;实现后 audit 墙钟 >0

    def test_audit_duration_seconds_nonneg_when_no_llm(self, tmp_path, monkeypatch) -> None:
        """空 transcripts → audit 仍跑过,duration_seconds 非负(墙钟存在,即便无判官)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        report = TranscriptAuditor().audit("myskill", "use Bash", [])
        assert report.duration_seconds >= 0.0

    def test_audit_populates_stage_breakdown(self, tmp_path, monkeypatch) -> None:
        """audit() 分阶段计时 + token:stage_breakdown 含全部 6 阶段;判官走 scoped("judge")
        → judge 阶段有 token/calls(含 cache_read 归属),本地阶段(extract/rebuild/postprocess)0 token;
        refine/headline 默认关 → 0 token。各阶段是顺序非重叠墙钟片段 → 秒数之和 ≤ duration_seconds。"""
        inner = _pass_fake()

        def fake(prompt, *a, **k):
            ou = k.get("on_usage")
            if ou:
                ou({"input_tokens": 100, "output_tokens": 50,
                    "cache_read_input_tokens": 200, "total_cost_usd": 0.02})
            return inner(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        foo = tmp_path / "foo.md"
        tp = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        report = TranscriptAuditor().audit("myskill", "use Bash", [tp])

        stages = {s["stage"]: s for s in report.stage_breakdown}
        assert set(stages) == {"extract", "refine", "headline", "rebuild", "judge", "postprocess"}
        assert all(s["seconds"] >= 0 for s in stages.values())
        # judge 跑了 LLM → 有 token + calls(含 cache_read 归属到 judge 桶);refine/headline 默认关 + 本地阶段 → 0 token
        assert stages["judge"]["input_tokens"] > 0
        assert stages["judge"]["cache_read_input_tokens"] > 0
        assert stages["judge"]["calls"] > 0
        for local_stage in ("extract", "refine", "headline", "rebuild", "postprocess"):
            assert stages[local_stage]["calls"] == 0
            assert stages[local_stage]["input_tokens"] == 0
            assert stages[local_stage]["cache_read_input_tokens"] == 0
        # note:开关默认关 → refine/headline "未启用";本地阶段 + judge 的 note 空(calls=0 是常态)
        assert stages["refine"]["note"] == "未启用"
        assert stages["headline"]["note"] == "未启用"
        for noteless in ("extract", "rebuild", "judge", "postprocess"):
            assert stages[noteless]["note"] == ""
        # 顺序非重叠片段 → 阶段和 ≤ 总墙钟(允许 1s 浮点/timing 缝隙)
        stage_sum = sum(s["seconds"] for s in stages.values())
        assert stage_sum <= report.duration_seconds + 1.0

    def test_audit_token_usage_survives_model_dump_roundtrip(
        self, tmp_path, monkeypatch
    ) -> None:
        """token_usage 经 model_dump_json / model_validate_json 往返不丢。"""
        inner = _pass_fake()

        def fake(prompt, *a, **k):
            ou = k.get("on_usage")
            if ou:
                ou({"input_tokens": 7, "output_tokens": 3, "total_cost_usd": 0.001})
            return inner(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        foo = tmp_path / "foo.md"
        tp = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        report = TranscriptAuditor().audit("myskill", "use Bash", [tp])

        restored = AuditReport.model_validate_json(report.model_dump_json())
        assert restored.token_usage == report.token_usage
        assert restored.token_usage.get("calls", 0) >= 1

    def test_bad_transcript_goes_to_warning_others_processed(
        self, tmp_path, monkeypatch
    ) -> None:
        """坏 transcript(不存在的路径)→ rebuild 抛 SiftError → 进 warnings、不崩;
        好 transcript 照常处理(findings 正常产出)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())

        foo = tmp_path / "foo.md"
        good = _write_jsonl(tmp_path / "good.jsonl", _read_file_records(str(foo)))
        bad = tmp_path / "missing.jsonl"  # 不存在

        auditor = TranscriptAuditor()
        report = auditor.audit("s", "use Bash", [good, bad])

        # 好的正常处理(进 transcripts + findings)
        assert report.transcripts == ["good.jsonl"]
        assert len(report.findings) == 2  # good 的 2 finding
        # 坏的不进 transcripts / findings
        assert all("missing" not in f.transcript for f in report.findings)
        # 坏的进 warnings(消息含文件名)
        assert len(report.warnings) == 1
        assert "missing.jsonl" in report.warnings[0]

    def test_empty_transcripts_returns_empty_report(self, monkeypatch) -> None:
        """空 transcripts 列表 → findings 空、summary 全 0、transcripts 空,不崩。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())

        auditor = TranscriptAuditor()
        report = auditor.audit("s", "use Bash", [])

        assert isinstance(report, AuditReport)
        assert report.findings == []
        assert report.transcripts == []
        assert report.warnings == []
        assert report.summary == {"total": 0, "pass": 0, "fail": 0, "na": 0, "unresolved": 0, "indeterminate": 0}

    def test_routes_each_category(self, tmp_path, monkeypatch) -> None:
        """skill_body 含四类指令 + REFERENCES → 各自路由到正确 category 判定(覆盖四桶)。

        TOOL_REQUIREMENT/REFERENCE_USAGE→CONDITIONAL、CONSTRAINT→PROHIBITION、
        HEADING_STEP→STEP、OUTPUT_FORMAT→OUTPUT。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())

        body = (
            "use Bash\n\n"
            "NEVER delete production data\n\n"
            "1. Analyze input format\n\n"
            "## 输出格式\n\n"
            "```\nEXPECTED_TOKEN\n```\n\n"
            "read references/design.md\n"
        )
        # 最小 transcript:仅 user turn(rebuild 非空即可;无工具 → 条件性全未命中走 LLM)
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )

        auditor = TranscriptAuditor()
        report = auditor.audit("s", body, [tp])

        # 四个类别都被路由到(各自走了对应的 _judge_*)
        cats = {f.category for f in report.findings}
        assert cats == {
            AuditCategory.CONDITIONAL,
            AuditCategory.PROHIBITION,
            AuditCategory.STEP,
            AuditCategory.OUTPUT,
        }
        assert len(report.findings) == 5  # 2 CONDITIONAL + 1 + 1 + 1


def test_audit_attaches_available_provenance_without_changing_findings(tmp_path, monkeypatch) -> None:
    """v2 digest 自动写入独立 provenance 区块，既有 findings 汇总保持不变。"""
    transcript = tmp_path / "v2.jsonl"
    transcript.write_text("placeholder", encoding="utf-8")
    digest = SessionDigest(evidence_metadata={"version": 2})
    calls: list[SessionDigest] = []

    def fake_analyze(value: SessionDigest) -> ProvenanceAuditResult:
        calls.append(value)
        return ProvenanceAuditResult(
            available=True,
            statistics={"capture_count": 0},
        )

    monkeypatch.setattr("sift.audit.auditor.rebuild", lambda *args, **kwargs: ([], "", digest))
    monkeypatch.setattr("sift.audit.auditor.analyze_provenance", fake_analyze)

    report = TranscriptAuditor().audit("skill", "", [transcript])

    assert calls == [digest]
    assert list(report.provenance) == ["v2.jsonl"]
    assert report.provenance["v2.jsonl"].available is True
    assert report.summary["total"] == 0
    assert report.findings == []


@pytest.mark.parametrize("has_refs", [False, True])
def test_audit_prechecks_provenance_without_losing_reference_only_evidence(tmp_path, monkeypatch, has_refs):
    transcript = tmp_path / "session.jsonl"
    digest = SessionDigest()
    if has_refs:
        from sift.models import FileEffectRecord
        digest.evidence_refs = {"capture:test": FileEffectRecord(
            path="a.py", evidence_ref="capture:test", quality="observed", capture_id="test",
        )}
    calls = []

    def analyze(value):
        calls.append(value)
        return ProvenanceAuditResult(available=True)

    monkeypatch.setattr("sift.audit.auditor.rebuild", lambda *args, **kwargs: ([], "", digest))
    monkeypatch.setattr("sift.audit.auditor.analyze_provenance", analyze)
    report = TranscriptAuditor().audit("skill", "", [transcript])
    assert len(calls) == int(has_refs)
    assert bool(report.provenance) == has_refs


def test_failed_rebuild_skips_provenance(tmp_path, monkeypatch):
    from sift.errors import SiftError

    def rebuild(*args, **kwargs):
        raise SiftError("invalid evidence")

    def analyze(*args):
        pytest.fail("failed rebuild must not reach analyzer")

    monkeypatch.setattr("sift.audit.auditor.rebuild", rebuild)
    monkeypatch.setattr("sift.audit.auditor.analyze_provenance", analyze)
    report = TranscriptAuditor().audit("skill", "", [tmp_path / "bad.jsonl"])
    assert report.provenance == {}
    assert report.findings == []
    assert any("invalid evidence" in warning for warning in report.warnings)


class TestAuditNoiseWiring:
    """Task 4 接线:audit() 在 post_process_findings 后跑 annotate_noise、
    在 aggregate_by_instruction 后跑 annotate_aggregate_noise,AuditReport 携带标注后的
    findings / by_instruction。

    全程 mock call_claude(不真跑 LLM)。构造 1 FAIL(prohibition violated=true)+
    1 PASS(step verdict=pass)的最小混档,断言:FAIL 的 noise_label 非 None、PASS 的为 None、
    by_instruction 里 fail_count>0 的行 noise_label 非 None。
    """

    @staticmethod
    def _mixed_fake():
        """通用 mock call_claude:prohibition 全 FAIL、conditional 全 N/A、step 全 PASS。"""
        def fake(prompt, *a, **k):
            return json.dumps([
                {
                    "index": i,
                    "applicable": False,
                    "violated": True,
                    "verdict": "pass",
                    "evidence": "violated",
                }
                for i in range(60)
            ])
        return fake

    def test_findings_and_by_instruction_are_noise_annotated(
        self, tmp_path, monkeypatch
    ) -> None:
        """audit() 产出的 findings / by_instruction 都被 noise 标注。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", self._mixed_fake())

        body = "NEVER fabricate data\n\n1. Analyze input format"
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )
        report = TranscriptAuditor().audit("s", body, [tp])

        # 最少:1 FAIL(prohibition)+ 1 PASS(step),覆盖 FAIL / 非 FAIL 两侧
        fails = [f for f in report.findings if f.verdict == AuditVerdict.FAIL]
        non_fails = [f for f in report.findings if f.verdict != AuditVerdict.FAIL]
        assert fails, "前置:至少有 1 条 FAIL finding(否则测试无意义)"
        assert non_fails, "前置:至少有 1 条非 FAIL finding(否则测不到 None 分支)"

        # ① FAIL finding 的 noise_label ∈ {tool_noise, skill_issue, uncertain}(非 None)
        for f in fails:
            assert f.noise_label in {"tool_noise", "skill_issue", "uncertain"}, (
                f"FAIL finding 应被标 noise_label,得到 {f.noise_label!r}"
            )
            assert f.noise_reasons, "FAIL finding 应附非空 noise_reasons"

        # ② PASS / N/A / 其它非 FAIL finding 的 noise_label 为 None、reasons 空
        for f in non_fails:
            assert f.noise_label is None, (
                f"非 FAIL finding 不应被标 noise_label,得到 {f.noise_label!r}"
            )
            assert f.noise_reasons == []

        # ③ by_instruction 里 fail_count>0 的行 noise_label 非 None;
        #    fail_count==0 的行 noise_label 为 None
        fail_rows = [r for r in report.by_instruction if r.fail_count > 0]
        no_fail_rows = [r for r in report.by_instruction if r.fail_count <= 0]
        assert fail_rows, "前置:至少有 1 行 fail_count>0 的聚合"
        for r in fail_rows:
            assert r.noise_label in {"tool_noise", "uncertain"}, (
                f"fail_count>0 的行应被标 noise_label,得到 {r.noise_label!r}"
            )
        for r in no_fail_rows:
            assert r.noise_label is None


class TestProgressCallback:
    """on_progress 回调:audit 长跑(每批 LLM 40-257s)时让用户看到进度,而非全程静默。

    契约:audit() / _judge_conditional / _judge_prohibition / _judge_step 接可选
    on_progress(str)->None,默认 None(核心不耦合 I/O)。进 transcript 打头;每个 LLM
    批前后各打一行——类别名前置括号化([类别],三类并发交错可分清)、单批不显序号、
    多批显 "批 k/总"、+ 完成耗时。on_progress=None 时行为与旧版一致。
    """

    def test_conditional_judge_emits_per_batch_progress(self, monkeypatch) -> None:
        """未命中走 LLM → on_progress 收到 [条件性] 批 + 完成耗时行(单批不显 [1/1])。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_conditional(
            [_tool_inst()], [], "某场景", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "[条件性]" in flat  # 类别名前置括号化(三类并发交错可分清)
        assert "[1/1]" not in flat  # 单批不显 [k/总](原 [1/1] 是噪声)
        assert "⏱" in flat  # 完成耗时行

    def test_conditional_multi_batch_progress_indexed(self, monkeypatch) -> None:
        """>_MAX_LLM_IN_BATCH(12 条)→ 分 2 批,on_progress 标 "批 1/2" 与 "批 2/2"。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps(
                [{"index": i, "applicable": False} for i in range(_MAX_LLM_IN_BATCH)]
            ),
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_conditional(
            [_tool_inst()] * 12, [], "场景", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "批 1/2" in flat
        assert "批 2/2" in flat

    def test_prohibition_judge_emits_progress(self, monkeypatch) -> None:
        """禁令批 → on_progress 含 [禁令] 标签 + ⏱(单批不显 [1/1])。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": "未违反"}]',
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_prohibition(
            [_prohibition_inst()], "执行记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "[禁令]" in flat
        assert "[1/1]" not in flat
        assert "⏱" in flat

    def test_step_judge_emits_progress(self, monkeypatch) -> None:
        """步骤批 → on_progress 含 [步骤] 标签 + ⏱(单批不显 [1/1])。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "pass", "evidence": "走过"}]',
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_step(
            [_step_inst()], "执行记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "[步骤]" in flat
        assert "[1/1]" not in flat
        assert "⏱" in flat

    def test_empty_bucket_does_not_call_progress(self, monkeypatch) -> None:
        """空禁令桶短路 → 不 spawn LLM → on_progress 不被调(不报假进度)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: pytest.fail("空桶不应调 LLM"),
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_prohibition([], "执行记录", "t.jsonl", on_progress=msgs.append)
        assert msgs == []

    def test_audit_emits_transcript_header(self, tmp_path, monkeypatch) -> None:
        """audit() 进每个 transcript 打头:'对账 transcript i/N: <name>'。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        body = (
            "use Bash\n\n"
            "NEVER delete production data\n\n"
            "1. Analyze the input format carefully\n\n"
            "```\nEXPECTED_TOKEN\n```\n"
        )
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )
        msgs: list[str] = []
        TranscriptAuditor().audit("s", body, [tp], on_progress=msgs.append)
        flat = " ".join(msgs)
        assert "对账 transcript 1/1" in flat
        assert "only_user.jsonl" in flat

    def test_progress_none_default_is_noop(self, monkeypatch) -> None:
        """on_progress=None(默认)→ 与旧版完全一致,无 AttributeError。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": ""}]',
        )
        auditor = TranscriptAuditor()
        findings = auditor._judge_prohibition([_prohibition_inst()], "记录", "t.jsonl")
        assert len(findings) == 1  # 不传 on_progress 也能正常判定

    def test_failure_progress_includes_reason(self, monkeypatch) -> None:
        """整批 LLM 失败时,进度行要带**原因**(str(exc)),不只'兜底 N/A'。

        回归真实痛点:旧版 ``except RuntimeError: continue`` 把异常吞了,进度只写
        '失败(整批兜底 N/A)'——用户看到'都判定失败'却不知为什么(超时?exit 1?限流?)。
        带 str(exc) 后,失败原因(如 'Reached max turns' / 'timed out after 600s')
        即时可见,才好诊断"都判定失败"的根因。
        """
        def boom(*a, **k):
            raise RuntimeError("claude CLI failed (exit 1): Reached max turns (1)")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "失败" in flat
        assert "Reached max turns" in flat  # 原因带出来了,不只'兜底 N/A'
        # 判定本身仍兜底为 UNRESOLVED(没判成,失败不报假 FAIL)
        assert findings[0].verdict == AuditVerdict.UNRESOLVED

    def test_zero_parsed_batch_emits_warning_prohibition(self, monkeypatch) -> None:
        """调用成功但整批 0 条解析成功(LLM 输出畸形/空)→ 打可见警告,不只静默 N/A。

        回归盲点:批"完成 ⏱ Xs"看着像成功,实则 LLM 返回畸形/截断/空数组 → _parse_*
        全 None → 全兜底 N/A,却**毫无信号**(失败原因日志只覆盖 RuntimeError 调用失败,
        不覆盖这种"调用成功但解析为空")。补一条 ⚠ 警告,让静默 N/A 可见。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: "[]",  # 空数组 → 0 条解析
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        findings = auditor._judge_prohibition(
            [_prohibition_inst(), _prohibition_inst()], "记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "完成" in flat  # 调用确实成功了(不是 RuntimeError)
        assert "0 条解析成功" in flat  # ⚠ 但解析为空,可见警告
        assert all(f.verdict == AuditVerdict.UNRESOLVED for f in findings)  # 仍兜底 UNRESOLVED(没判成)

    def test_zero_parsed_batch_emits_warning_conditional(self, monkeypatch) -> None:
        """条件性批(标量 mask 形状)同样:0 条解析成功 → ⚠ 警告。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: "[]",  # 空数组 → mask 全 None
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        findings = auditor._judge_conditional(
            [_tool_inst(), _tool_inst()], [], "场景", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "0 条解析成功" in flat
        assert all(f.verdict == AuditVerdict.UNRESOLVED for f in findings)

    def test_zero_parsed_triggers_retry_and_recovers(self, monkeypatch) -> None:
        """0 解析成功 → 自动重试,重试拿到能解析的输出 → 真 verdict(非兜底 N/A)。

        实锤价值:同 prompt 重试常能解出(0-解析多为 LLM 输出方差的瞬态),故重试而非直接
        放弃。第一次返回空数组(0 解析)→ 重试 → 第二次返回正常 JSON → 解出 resolved PASS。
        """
        calls = {"n": 0}

        def flaky(prompt, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return "[]"  # 第一次:0 解析
            return '[{"index": 0, "violated": false, "evidence": "重试后解出"}]'  # 重试成功

        monkeypatch.setattr("sift.audit.auditor.call_claude", flaky)
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "重试" in flat  # 触发了重试
        assert calls["n"] == 2  # 第一次 0 解析 + 重试一次成功
        assert findings[0].verdict == AuditVerdict.PASS  # violated=false → PASS(resolved)
        assert findings[0].method == AuditMethod.LLM  # resolved LLM,非兜底


# ---------------------------------------------------------------------------
# LLM 精筛伪指令(_refine_pseudo_instructions):extract 后、判定前用 LLM 砍掉
# extractor 误抽的示例/占位文字。精度>召回:只砍明确 is_real=False,失败/模糊全保留。
# 全程 mock call_claude;_refine 单元测 + audit() 端到端(opt-in、per-skill 只一次)。
# 端到端用 mock InstructionExtractor 避开 extract 正则的脆弱性,让候选数可控。
# ---------------------------------------------------------------------------


class _FakeExtractor:
    """替身:跳过 extract 正则,直接返回造好的指令(audit() 内 ``InstructionExtractor().extract``)。"""

    def __init__(self, insts):
        self._insts = insts

    def extract(self, body):
        return list(self._insts)


class TestMatchFullInputForExistence:
    """存在性匹配拓宽到完整结构化 input(频次不拓宽)——root 对账对的是「派发里有没有交代」。

    input_summary 是 200 字截断的显示串:Task 派发 prompt 里交代的命令(mkdir 等)常被
    截掉,存在性检查假未命中 → 假 FAIL。存在性(match_tool_call)搜全文;频次
    (count_tool_calls)不拓宽——派发里**提到**命令 ≠ 执行了命令,计数拓宽会虚增。
    """

    @staticmethod
    def _dispatch_tc() -> ToolCallRecord:
        # 派发调用:summary 只有开头 200 字(无 mkdir),完整 input 的 prompt 深处才有
        return ToolCallRecord(
            name="Task",
            input_summary="派发 developer agent:创建当前算子开发目录,按 STATE.md 的 Task 2 "
                          "执行环境准备,读取 proto/desc/golden 并初始化工作区,注意基线与目录结构约定"
                          + ",补充说明文字" * 12,
            input={"prompt": "…(长 prompt)… 请执行 mkdir -p docs/{OP}/develop/ 创建当前算子开发目录 …"},
        )

    def test_existence_hits_command_in_full_input(self) -> None:
        """命令词只在完整 input(超出 200 字摘要)→ 存在性命中(root:派发交代过)。"""
        assert match_tool_call("mkdir", "", [self._dispatch_tc()]) is True

    def test_count_does_not_count_dispatch_mention(self) -> None:
        """同一条派发:频次不数它(提到 ≠ 执行)。"""
        assert count_tool_calls("mkdir", [self._dispatch_tc()]) == 0

    def test_count_still_counts_real_execution(self) -> None:
        """真执行(Bash command 里)照常:存在性与频次都命中。"""
        tc = ToolCallRecord(name="Bash", input_summary="mkdir build", input={"command": "mkdir build"})
        assert count_tool_calls("mkdir", [tc]) == 1
        assert match_tool_call("mkdir", "", [tc]) is True

    def test_placeholder_param_dropped_and_full_input_params(self) -> None:
        """required_params 的模板占位符 token 不参与字面断言,实参搜完整 input。

        声明写 ``docs/{OP}/develop/``、派发 prompt 里是实值——占位符字面比对永远
        失败。参数(如 ``-p``)在派发 input 的 prompt 深处、200 字 summary 截不到:
        存在性路径参数搜全文(d070717 ② 的补全);频次路不搜。
        """
        tc = self._dispatch_tc()
        # 声明原文:执行 `mkdir -p docs/{OP}/develop/` → params=["-p", "docs/{OP}/develop/"]
        assert match_tool_call(
            "mkdir", "", [tc], required_params=["-p", "docs/{OP}/develop/"]
        ) is True
        # 频次:同 tc 参数不搜全文(summary 无 -p)→ 不计(提到 ≠ 执行)
        assert count_tool_calls("mkdir", [tc], required_params=["-p"]) == 0
        # 真执行(Bash command):两条路径都满足(summary=现实的 200 字序列化形)
        bash = ToolCallRecord(
            name="Bash",
            input_summary='{"command": "mkdir -p docs/x/develop/"}',
            input={"command": "mkdir -p docs/x/develop/"},
        )
        assert match_tool_call("mkdir", "", [bash], required_params=["-p", "docs/x/develop/"]) is True
        assert count_tool_calls("mkdir", [bash], required_params=["-p", "docs/x/develop/"]) == 1
        # 非占位符参数在全文也找不到 → 不命中(参数级断言不放开)
        assert match_tool_call("mkdir", "", [tc], required_params=["--parents"]) is False


class TestRefineInstructions:
    """精筛伪指令:extractor 误抽的示例文字 → LLM 判 is_real=False → 砍。

    含 per-SKILL hash 缓存(``audit.refine_cache``)的端到端覆盖:同 instructions +
    model 跨 audit() 调用复用、并发安全(double-check locking)、缓存 hit 产出的
    findings 与直接 refine 等价。

    缓存目录隔离由 ``conftest._isolate_refine_cache_dir`` 提供(全局 autouse),
    保证每个测试从空缓存开始、绝不污染 ``~/.cache``。
    """

    def test_drops_pseudo_keeps_real(self, monkeypatch) -> None:
        """is_real=False 的砍、True 的留;warnings 报滤除数。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps([
                {"index": 0, "is_real": True},
                {"index": 1, "is_real": False},
            ]),
        )
        insts = [_step_inst("Step 1: 真步骤"), _step_inst("示例:3 agents 并行")]
        refined, warns = TranscriptAuditor()._refine_pseudo_instructions(
            insts, model=None, timeout=600, on_progress=None
        )
        assert len(refined) == 1
        assert refined[0].instruction_text == "Step 1: 真步骤"
        assert any("refine:" in w and "1 条" in w for w in warns)

    def test_refine_prompt_carries_excerpt_and_pseudo_tool_rule(self, monkeypatch) -> None:
        """refine prompt 喂 source_excerpt + 含「命令行标识符伪工具」类目。

        判「调用 TilingFunc 是不是真指令」必须看到原文上下文(它只活在一条命令行
        里)——旧 prompt 只喂指令文本,伪工具要求判不出,漏成假 FAIL。摘录进 prompt
        也进缓存键(v2 bump),摘录变则失效重筛。
        """
        prompts: list[str] = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "is_real": False}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        inst = _tool_inst(text="调用 TilingFunc", tool_name="TilingFunc")
        inst.source_excerpt = (
            "验收标准:UT 可编译——cd operators/tests/tiling && cmake .. && make TilingFunc"
        )

        TranscriptAuditor()._refine_pseudo_instructions(
            [inst], model=None, timeout=600, on_progress=None
        )

        assert len(prompts) == 1
        assert "cmake" in prompts[0]      # 摘录真进了 prompt
        assert "伪工具" in prompts[0]     # 新类目在
        assert "TilingFunc" in prompts[0]
        # v3:任务编号词类目(「`./run.sh` 跑 Task 25 用例」抽出「跑 Task」——Task 不是工具)
        assert "任务编号词" in prompts[0]
        assert "跑 Task" in prompts[0]
        # v4:任务清单行是真指令——v3 实测误砍同构任务行(同句式不同分支的行被当示例)
        assert "任务清单行是真指令" in prompts[0]
        assert "不得当示例砍" in prompts[0]

    def test_output_format_skipped_not_sent_to_llm(self, monkeypatch) -> None:
        """OUTPUT_FORMAT(PATTERN_MATCH,纯程序判定)不进精筛候选 → 直接保留、不发给 LLM。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps([
                {"index": 0, "is_real": True},
                {"index": 1, "is_real": False},
            ]),
        )
        insts = [_output_inst(), _step_inst("真"), _step_inst("伪")]
        refined, _ = TranscriptAuditor()._refine_pseudo_instructions(
            insts, model=None, timeout=600, on_progress=None
        )
        # output 保留(没进 LLM)+ 真保留;伪砍 → 共 2 条
        assert len(refined) == 2
        assert any(r.source == InstructionSource.OUTPUT_FORMAT for r in refined)
        assert any(r.instruction_text == "真" for r in refined)
        assert not any(r.instruction_text == "伪" for r in refined)

    def test_fallback_keeps_all_on_call_failure(self, monkeypatch) -> None:
        """call_claude 整批失败(重试耗尽)→ 全保留(精度>召回,宁留伪指令判 N/A 也不砍真)。"""
        def boom(*a, **k):
            raise RuntimeError("timeout")
        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        insts = [_step_inst("a"), _step_inst("b"), _output_inst()]
        refined, warns = TranscriptAuditor()._refine_pseudo_instructions(
            insts, model=None, timeout=600, on_progress=None
        )
        assert len(refined) == 3  # 全保留
        assert not any("refine:" in w for w in warns)  # 没砍 → 不报

    def test_fallback_keeps_all_on_indeterminate(self, monkeypatch) -> None:
        """全 indeterminate(分不清真假)→ 全保留(不砍)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps([
                {"index": 0, "indeterminate": True, "reason": "模糊"},
                {"index": 1, "indeterminate": True, "reason": "模糊"},
            ]),
        )
        insts = [_step_inst("a"), _step_inst("b")]
        refined, _ = TranscriptAuditor()._refine_pseudo_instructions(
            insts, model=None, timeout=600, on_progress=None
        )
        assert len(refined) == 2  # indeterminate → 保留

    def test_refine_runs_once_per_skill_not_per_transcript(
        self, tmp_path, monkeypatch
    ) -> None:
        """精筛在 per-transcript 循环外 → 多 transcript 只精筛一次(per-skill 共享)。"""
        refine_calls = {"n": 0}

        def fake(prompt, *a, **k):
            if "真实可检查的指令" in prompt:
                refine_calls["n"] += 1
                return json.dumps([{"index": 0, "is_real": True}])
            return _pass_fake()(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor([_step_inst("Step 1: 真步骤")]),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        t2 = _write_jsonl(tmp_path / "t2.jsonl", _read_file_records("a.md"))
        TranscriptAuditor().audit("s", "body", [t1, t2], refine_instructions=True)
        assert refine_calls["n"] == 1  # 2 transcript,精筛仍只 1 次

    def test_refine_off_by_default(self, tmp_path, monkeypatch) -> None:
        """audit() 默认 refine_instructions=False → 不精筛、不报 refine warning。"""
        refine_calls = {"n": 0}

        def fake(prompt, *a, **k):
            if "真实可检查的指令" in prompt:
                refine_calls["n"] += 1
            return _pass_fake()(prompt, *a, **k)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor([_step_inst("Step 1: 真步骤")]),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        report = TranscriptAuditor().audit("s", "body", [t1])  # 默认关
        assert refine_calls["n"] == 0
        assert not any("refine:" in w for w in report.warnings)

    # ---- per-SKILL hash 缓存(audit.refine_cache)--------------------------------

    def test_refine_cache_skips_second_audit_call(
        self, tmp_path, monkeypatch
    ) -> None:
        """同 SKILL.md 第二次 audit() 命中缓存 → 不再调 ``_refine_pseudo_instructions``。

        跨 audit() 调用复用是缓存的核心价值:runner.py 每 (eval×config×run) 调一次
        audit(),同 SKILL.md 反复 refine 浪费 LLM。用计数 mock 验证两次 audit() 只调一次。
        """
        refine_calls = {"n": 0}
        original = TranscriptAuditor._refine_pseudo_instructions

        def counting(self_, insts, **kw):
            refine_calls["n"] += 1
            return original(self_, insts, **kw)

        monkeypatch.setattr(TranscriptAuditor, "_refine_pseudo_instructions", counting)
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor([_step_inst("Step 1: 真步骤")]),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        TranscriptAuditor().audit("s", "body", [t1], refine_instructions=True)
        assert refine_calls["n"] == 1  # 首次:miss → 调
        TranscriptAuditor().audit("s", "body", [t1], refine_instructions=True)
        assert refine_calls["n"] == 1  # 二次:hit → 不调(double-check locking 生效)

    def test_refine_dropped_count_populated(self, tmp_path, monkeypatch) -> None:
        """``AuditReport.refine_dropped_count`` = 砍掉的条数(len(before) - len(after))。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps([
                {"index": 0, "is_real": True},   # 保留
                {"index": 1, "is_real": False},  # 砍
                {"index": 2, "is_real": False},  # 砍
            ]),
        )
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor([
                _step_inst("真"),
                _step_inst("伪1"),
                _step_inst("伪2"),
            ]),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        report = TranscriptAuditor().audit("s", "body", [t1], refine_instructions=True)
        assert report.refine_dropped_count == 2
        assert any("refine:" in w and "2 条" in w for w in report.warnings)

    def test_refine_cache_hit_equivalent_findings(
        self, tmp_path, monkeypatch
    ) -> None:
        """缓存 hit 产出的 findings/instructions 等价于直接 refine(无损序列化)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: json.dumps([
                {"index": 0, "is_real": True},   # 保留(进 candidates 的 index 0)
                {"index": 1, "is_real": False},  # 砍
            ]),
        )
        insts = [_step_inst("真"), _step_inst("伪")]
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor(insts),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        report1 = TranscriptAuditor().audit("s", "body", [t1], refine_instructions=True)
        report2 = TranscriptAuditor().audit("s", "body", [t1], refine_instructions=True)
        # findings 等价:同指令集 → 同样的 verdict(只有"真"那条进判定的桶)
        assert [f.instruction_text for f in report1.findings] == [
            f.instruction_text for f in report2.findings
        ]
        assert [f.verdict for f in report1.findings] == [
            f.verdict for f in report2.findings
        ]
        # 伪被砍(两次都砍):不在 findings 里;真保留(两次都留):至少一条含"真"。
        # (findings 里可能还有 derive_declarations 从 transcript 反推的指令,
        # 不影响"伪被砍"这一核心断言。)
        assert all("伪" not in f.instruction_text for f in report1.findings)
        assert all("伪" not in f.instruction_text for f in report2.findings)
        assert any("真" in f.instruction_text for f in report1.findings)
        assert any("真" in f.instruction_text for f in report2.findings)
        # dropped_count 两次都对(缓存 hit 不丢 dropped_count)
        assert report1.refine_dropped_count == report2.refine_dropped_count == 1
        # 第二次跑 refine 缓存命中(_compute 不跑 → 0 LLM 调用)→ stage note="缓存命中"
        stages2 = {s["stage"]: s for s in report2.stage_breakdown}
        assert stages2["refine"]["note"] == "缓存命中"
        assert stages2["refine"]["calls"] == 0

    def test_refine_off_does_not_touch_cache(
        self, tmp_path, monkeypatch
    ) -> None:
        """``refine_instructions=False`` → 不查缓存、不精筛、``refine_dropped_count=0``。

        让 ``get_or_compute`` 一旦被调就 raise —— 验证默认路径完全不触缓存。
        """
        from sift.audit import refine_cache

        def bomb(*a, **k):
            raise AssertionError(
                "cache should not be touched when refine_instructions=False"
            )

        monkeypatch.setattr(refine_cache, "get_or_compute", bomb)
        monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
        monkeypatch.setattr(
            "sift.audit.auditor.InstructionExtractor",
            lambda: _FakeExtractor([_step_inst("Step 1: 真步骤")]),
        )
        t1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records("a.md"))
        report = TranscriptAuditor().audit("s", "body", [t1])  # 默认 refine_instructions=False
        assert report.refine_dropped_count == 0
        assert not any("refine:" in w for w in report.warnings)


_FIXTURES = Path(__file__).parent.parent / "fixtures" / "example-skills"


def test_audit_populates_transcript_sources_cc_and_opencode(monkeypatch) -> None:
    """audit 把每条 transcript 来源(cc/opencode)同序填进 transcript_sources。

    与 rebuild 分派同源(is_opencode_session)。transcript_sources 只取决于文件格式,
    在 rebuild 循环里无条件累积(不依赖指令抽取/判定结果)。
    """
    monkeypatch.setattr(
        "sift.audit.auditor.call_claude",
        lambda *a, **k: '[{"index":0,"violated":false,"evidence":"未违规"}]',
    )
    cc = _FIXTURES / "log-error-analyzer-zh" / "transcripts" / "synthetic-violation.jsonl"
    oc = _FIXTURES / "log-error-analyzer-zh" / "opencode-sessions" / "synthetic-violation.json"
    report = TranscriptAuditor().audit("lea", "# x\n\nNEVER fabricate data.\n", [cc, oc])
    assert report.transcripts == [cc.name, oc.name]
    assert report.transcript_sources == ["cc", "opencode"]


# ---- 尾逗号 JSON 容错:LLM 长批输出偶发 [...,] / {...,} 不该让整批变 UNRESOLVED ----
# 旧逻辑 _parse_* 直接 json.loads,尾逗号 → JSONDecodeError → 整批回退默认(没判成 →
# UNRESOLVED,audit 模式下是主交付物有洞)。load_json_lenient 兜底:strip ]/} 前尾逗号重试。


def test_parse_step_recovers_array_trailing_comma() -> None:
    """LLM 输出数组尾逗号([... ,])→ 旧逻辑硬失败整批没判成;现应救回 pass/fail。"""
    text = '[{"index":0,"verdict":"pass"},{"index":1,"verdict":"fail"},]'
    res = TranscriptAuditor._parse_step(text, 2)
    assert res[0][0] == AuditVerdict.PASS
    assert res[1][0] == AuditVerdict.FAIL


def test_parse_prohibition_recovers_object_trailing_comma() -> None:
    """对象内尾逗号({...,})同样兜底;禁令 violated=true 不该因尾逗号漏判成 UNRESOLVED。"""
    text = '[{"index":0,"violated":true,"evidence":"编造了数据",},]'
    res = TranscriptAuditor._parse_prohibition(text, 1)
    assert res[0][0] == AuditVerdict.FAIL
    assert res[0][1] == "编造了数据"


def test_parse_applicable_recovers_trailing_comma() -> None:
    """条件性 applicable 判定同样兜底;true→FAIL(该做没做)/false→NA(不涉及)。"""
    text = '[{"index":0,"applicable":true},{"index":1,"applicable":false},]'
    res = TranscriptAuditor._parse_applicable(text, 2)
    assert res[0][0] == AuditVerdict.FAIL
    assert res[1][0] == AuditVerdict.NA


def test_load_json_lenient_clean_unchanged_and_garbage_none() -> None:
    """干净 JSON 直通不跑 strip(含合法 ', ]' 的字符串值不被破坏);真垃圾 → None。"""
    from sift.audit.tool_match import load_json_lenient

    assert load_json_lenient('[1, 2, 3]') == [1, 2, 3]
    # 含 ", ]" 的合法字符串值:直接 parse 成功 → strip 不跑 → 原样保留
    assert load_json_lenient('["a, ]", "b"]') == ["a, ]", "b"]
    # 尾逗号 → 直接失败 → strip 重试 → 救回
    assert load_json_lenient('[1, 2, 3,]') == [1, 2, 3]
    # 真垃圾 → None(调用方兜底默认 / UNRESOLVED)
    assert load_json_lenient("not json at all") is None


# ---- 场景饥饿 + 截断:两处结构性噪声的修法(_window 头尾窗 + conditional 场景回退)----
# 实测(ops-spec-gen 22%、precision-debug 55% 弃判):skill/agent 切片无 user turn →
# conditional 判官拿空场景全弃判;长切片 transcript_text[:8000] 裸切 → prohibition 类
# 尾部(最终结果/总结)全盲。_window 头+尾窗 + 空 scene 回退 transcript_text 治这两条。


def test_window_short_text_unchanged() -> None:
    """短文本(<= limit)原样返回;恰好 limit 不截(边界)。"""
    from sift.audit.auditor import _window, _SCENE_TRUNCATE

    assert _window("短文本") == "短文本"
    assert _window("x" * _SCENE_TRUNCATE) == "x" * _SCENE_TRUNCATE


def test_window_long_text_keeps_head_tail_and_marker() -> None:
    """长文本取头+尾(各 limit/2)+ 中段截断标记;头尾 canary 都在(旧裸 [:limit] 会丢尾)。"""
    from sift.audit.auditor import _window, _SCENE_TRUNCATE

    text = "CANARY_HEAD" + "h" * 9000 + "t" * 9000 + "CANARY_TAIL"
    out = _window(text)
    assert "CANARY_HEAD" in out                       # 头保留
    assert "CANARY_TAIL" in out                       # 尾保留(旧 [:8000] 只有头,丢这个)
    assert "截断" in out                              # 中段标记
    assert len(out) < len(text)                       # 确实缩短
    half = _SCENE_TRUNCATE // 2
    assert "CANARY_HEAD" in out[: half + 20]          # 头 canary 在前半窗
    assert "CANARY_TAIL" in out[-(half + 20):]        # 尾 canary 在后半窗


def test_prohibition_long_transcript_uses_head_tail_window(monkeypatch) -> None:
    """长 transcript_text(>8000):prohibition prompt 含头+尾 canary + 截断标记。"""
    captured: dict = {}

    def fake(prompt, *a, **k):
        captured["prompt"] = prompt
        return "[]"

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
    auditor = TranscriptAuditor()
    # ~18000 字:尾 canary 在 [:8000] 之外 → 旧裸切会丢
    transcript_text = "CANARY_HEAD_PART" + "h" * 9000 + "t" * 9000 + "CANARY_TAIL_PART"
    auditor._judge_prohibition([_prohibition_inst()], transcript_text, "p.jsonl")

    assert "CANARY_HEAD_PART" in captured["prompt"]
    assert "CANARY_TAIL_PART" in captured["prompt"]   # 旧 [:8000] 实现会缺
    assert "截断" in captured["prompt"]


def test_conditional_scene_fallback_when_no_user_turn(tmp_path, monkeypatch) -> None:
    """skill/agent 切片无 user turn → conditional 判官原拿空场景全弃判(实测主噪声源);

    回退用 transcript_text(执行记录)当场景 → prompt 场景描述含执行 canary、非空,
    判官方能判 applicability 而非一律 INDETERMINATE。
    """
    captured: dict = {}

    def fake(prompt, *a, **k):
        captured["prompt"] = prompt
        return json.dumps([{"index": i, "applicable": False} for i in range(60)])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
    # 无 user turn 的切片:仅 assistant 文字(canary)+ 一个不匹配 "use Bash" 的 Read
    tp = _write_jsonl(
        tmp_path / "slice.jsonl",
        [
            {"message": {"role": "assistant", "content": [
                {"type": "text", "text": "执行中 CANARY_SCENE_FALLBACK 标记。"},
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/x/foo.md"}},
            ]}},
        ],
    )
    TranscriptAuditor().audit("s", "use Bash", [tp])  # use Bash → conditional, 不匹配 Read → LLM

    p = captured["prompt"]
    assert "## 场景描述" in p
    m = re.search(r"## 场景描述\n(.*?)\n\n## 待判指令", p, re.S)
    assert m, "未找到 conditional prompt 的场景描述段"
    scene = m.group(1)
    assert scene.strip() != "", "skill 切片无 user turn → 场景描述不应为空(应回退 transcript_text)"
    assert "CANARY_SCENE_FALLBACK" in scene


def test_source_excerpt_propagates_to_finding(tmp_path, monkeypatch):
    """audit 产出的 finding 带上 instruction 的 source_excerpt(主语上下文)。

    判定不变:judge 仍吃 instruction_text(谓语片段);source_excerpt 只透传给 finding
    供显示层补全被切的主语。回归:st-verifier「必须存在且非空」无主语(主语在列表项
    首行被切走)→ finding.source_excerpt 带回 design_doc_cases.json。
    """
    monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
    body = (
        "## 标准产物存在性\n\n"
        "- **用例生成类**:`design_doc_cases.json` 与 `ttk_design_doc_cases.csv`\n"
        "  (黑盒为其 TTK 用例 CSV)必须存在且非空 —— 缺任一 → FAILED。\n"
    )
    tp = _write_jsonl(tmp_path / "s.jsonl", _read_file_records("/tmp/x"))
    report = TranscriptAuditor().audit("s", body, [tp])

    must = [
        f for f in report.findings
        if "必须存在" in f.instruction_text and "非空" in f.instruction_text
    ]
    assert must, "应有「必须存在且非空」的 finding"
    # 主语从列表项首行经 instruction → finding 透传
    assert any("design_doc_cases.json" in f.source_excerpt for f in must), (
        "source_excerpt 应带回主语 design_doc_cases.json"
    )
    # 判定用的 instruction_text 仍是谓语片段(主语不在其中)
    assert all("design_doc_cases.json" not in f.instruction_text for f in must)


def test_derived_text_collision_with_static_instruction(tmp_path, monkeypatch):
    """derived 反推文案与静态声明指令同文 → 静态指令不得被误标 derived。

    回归钉:trace 里绝对路径 Read 归一后,derived 反推的「读取 references/x.md」
    与声明步骤同文——按纯文本命中会把静态 HEADING_STEP finding 标成 derived,
    踢出 coverage 分母(实测 total 少 1)与 suggest 的 PASS 保留账本。同文碰撞时
    该文本按真声明指令对待(finding 无法区分出处,但静态集里存在即按静态算)。
    """
    monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
    body = (
        "# 步骤\n\n"
        "1. 读取 references/error-codes.md\n"
        "2. 总结错误类别\n\n"
        "## 规则\n\n禁止编造数据。\n"
    )
    tp = _write_jsonl(tmp_path / "s.jsonl",
                      _read_file_records("/ws/references/error-codes.md"))
    report = TranscriptAuditor().audit("s", body, [tp])

    step = [f for f in report.findings
            if f.source == "HEADING_STEP"
            and f.instruction_text == "读取 references/error-codes.md"]
    assert step, "静态步骤指令应有 finding"
    assert step[0].derived_from_transcript is False
    # coverage 分母里保留这条真声明指令(修复前被 derived 标记踢出)
    covered_texts = {
        i.instruction_text
        for sec in report.coverage.sections
        for i in sec.instructions
    }
    assert "读取 references/error-codes.md" in covered_texts


def test_generate_headlines_feeds_source_excerpt_and_returns_list(monkeypatch):
    """_generate_headlines 把 instruction_text + source_excerpt 喂 LLM,返回等长 headline 列表。

    判定不读 headline;空 instruction 集返回 []。LLM 据源 excerpt 补主语(测试只验管道)。
    """
    captured = {}

    def fake(prompt, *a, **k):
        captured["prompt"] = prompt
        return json.dumps([{"index": 0, "headline": "design_doc_cases.json 必须存在且非空"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
    auditor = TranscriptAuditor()
    inst = InstructionCheck(
        source=InstructionSource.CONSTRAINT,
        instruction_text="必须存在",
        section="Constraint",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        source_excerpt="用例生成类:`design_doc_cases.json` 必须存在",
    )
    headlines = auditor._generate_headlines([inst], model=None, timeout=10)
    assert headlines == ["design_doc_cases.json 必须存在且非空"]
    # prompt 喂了 source_excerpt(LLM 据此补主语)
    assert "design_doc_cases.json" in captured["prompt"]
    # 空集短路,不调 LLM
    assert auditor._generate_headlines([], model=None, timeout=10) == []


def test_headline_propagates_to_finding_when_enabled(tmp_path, monkeypatch):
    """generate_headline=True → audit 产出的 finding 带 headline(独立步骤,判定不变)。

    judge 仍吃 instruction_text;headline 是纯显示字段。headline_cache 指 tmp 不污染 ~/.cache。
    """
    monkeypatch.setattr("sift.audit.auditor.call_claude", _headline_fake())
    monkeypatch.setattr(
        "sift.audit.auditor.headline_cache._cache_dir", lambda: tmp_path
    )
    body = "- **用例生成类**:`design_doc_cases.json` 必须存在且非空 —— 缺 → FAILED。\n"
    tp = _write_jsonl(tmp_path / "s.jsonl", _read_file_records("/tmp/x"))
    report = TranscriptAuditor().audit("s", body, [tp], generate_headline=True)

    must = [f for f in report.findings if "必须存在" in f.instruction_text]
    assert must, "应有「必须存在」的 finding"
    assert any(f.headline for f in must), "generate_headline=True → finding 应带 headline"


def test_headline_off_by_default(tmp_path, monkeypatch):
    """默认不生成 headline(generate_headline=False)→ finding.headline 恒空。"""
    monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
    monkeypatch.setattr(
        "sift.audit.auditor.headline_cache._cache_dir", lambda: tmp_path
    )
    body = "- **用例生成类**:`design_doc_cases.json` 必须存在。\n"
    tp = _write_jsonl(tmp_path / "s.jsonl", _read_file_records("/tmp/x"))
    report = TranscriptAuditor().audit("s", body, [tp])  # 不传 generate_headline
    assert all(not f.headline for f in report.findings)


# ── 裸 HTTP 通道:refine / headline 经调度链传 transport='http' ──


def test_refine_passes_http_transport_down_the_chain(monkeypatch):
    """refine(_refine_pseudo_instructions → _llm_classify_real)把 transport='http'
    穿到 call_claude(mock 整替 call_claude,断言收到的 transport=='http')。"""
    seen = {}

    def fake_call_claude(prompt, *, model=None, timeout=120, max_turns=None,
                         on_usage=None, schema=None, transport="cc", on_progress=None):
        seen["transport"] = transport
        # 返 is_real=true(真指令,保留一条)
        return json.dumps([{"index": 0, "is_real": True}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor()._refine_pseudo_instructions(
        [_step_inst("Step 1: 真步骤")], model="glm-5.2[1m]", timeout=10,
    )
    assert seen.get("transport") == "http"


def test_headline_passes_http_transport_down_the_chain(monkeypatch):
    """headline(_generate_headlines)把 transport='http' 穿到 call_claude。"""
    seen = {}

    def fake_call_claude(prompt, *, model=None, timeout=120, max_turns=None,
                         on_usage=None, schema=None, transport="cc", on_progress=None):
        seen["transport"] = transport
        return json.dumps([{"index": 0, "headline": "标题"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor()._generate_headlines(
        [_step_inst("做某事")], model="glm-5.2[1m]", timeout=10,
    )
    assert seen.get("transport") == "http"


def test_judge_applicable_passes_http_transport(monkeypatch):
    """条件性 judge(_llm_judge_applicable)走 http 通道(与 refine/headline 一致)。"""
    seen = {}

    def fake_call_claude(prompt, *, model=None, timeout=120, max_turns=None,
                         on_usage=None, schema=None, transport="cc", on_progress=None):
        seen["transport"] = transport
        return json.dumps([{"index": 0, "applicable": False, "reason": "不涉及"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor()._llm_judge_applicable(
        [_tool_inst()], "某场景", model="glm-5.2[1m]", timeout=10,
    )
    assert seen.get("transport") == "http"


def test_judge_prohibition_passes_http_transport(monkeypatch):
    """禁令 judge(_llm_judge_prohibition)走 http 通道。"""
    seen = {}

    def fake_call_claude(prompt, *, model=None, timeout=120, max_turns=None,
                         on_usage=None, schema=None, transport="cc", on_progress=None):
        seen["transport"] = transport
        return json.dumps([{"index": 0, "violated": False, "evidence": "未违反"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor()._llm_judge_prohibition(
        [_prohibition_inst()], "某执行记录", model="glm-5.2[1m]", timeout=10,
    )
    assert seen.get("transport") == "http"


def test_judge_step_passes_http_transport(monkeypatch):
    """步骤 judge(_llm_judge_step)走 http 通道。"""
    seen = {}

    def fake_call_claude(prompt, *, model=None, timeout=120, max_turns=None,
                         on_usage=None, schema=None, transport="cc", on_progress=None):
        seen["transport"] = transport
        return json.dumps([{"index": 0, "verdict": "pass", "evidence": "做过"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor()._llm_judge_step(
        [_step_inst()], "某执行记录", model="glm-5.2[1m]", timeout=10,
    )
    assert seen.get("transport") == "http"


def test_judge_batch_with_retry_forwards_transport_to_call_claude(monkeypatch):
    """_judge_batch_with_retry 把 transport 参转发给 call_claude(cc 默认 / http 两向)。"""
    import sift.audit.auditor as A
    seen = {}

    def fake_call_claude(prompt, *, model=None, timeout=120, max_turns=None,
                         on_usage=None, schema=None, transport="cc", on_progress=None):
        seen.setdefault("transports", []).append(transport)
        return json.dumps([{"index": 0, "applicable": False}])

    monkeypatch.setattr(A, "call_claude", fake_call_claude)
    # parse 返非 unparsed 值(v[0] 非 None)→ 首次即成功、不重试(否则重试会重复记 transport)
    parse = lambda text, n: [("ok", "")] * n
    # transport 默认 cc
    A._judge_batch_with_retry(
        "p", None, 10, parse, 1,
        on_progress=None, on_usage=None, schema=None, label="X", k=1, total=1,
        is_unparsed=lambda v: v[0] is None,
    )
    # 显式 http
    A._judge_batch_with_retry(
        "p", None, 10, parse, 1,
        on_progress=None, on_usage=None, schema=None, label="X", k=1, total=1,
        is_unparsed=lambda v: v[0] is None, transport="http",
    )
    assert seen["transports"] == ["cc", "http"]


class TestRetryExhaustedCause:
    """_judge_batch_with_retry 重试耗尽时,哨兵携带最后失败的原因。"""

    def _judge(self, monkeypatch, fake_call):
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(auditor.random, "uniform", lambda *a, **k: 0)
        return _judge_batch_with_retry(
            "p", None, 600, lambda text, n: [(None, "")], 1,
            on_progress=None, label="测试", k=1, total=1,
            is_unparsed=lambda v: v[0] is None, transport="http",
        )

    def _raise(self, exc):
        def f(*a, **k):
            raise exc
        return f

    def test_rate_limited_cause(self, monkeypatch):
        from sift.utils import _HttpRateLimited
        r = self._judge(monkeypatch, self._raise(_HttpRateLimited("429")))
        assert isinstance(r, _RetryExhaustedType) and r.cause == "rate_limited"

    def test_timeout_cause(self, monkeypatch):
        r = self._judge(monkeypatch, self._raise(RuntimeError("execution timed out")))
        assert isinstance(r, _RetryExhaustedType) and r.cause == "timeout"

    def test_unknown_runtime_cause(self, monkeypatch):
        r = self._judge(monkeypatch, self._raise(RuntimeError("claude CLI down")))
        assert isinstance(r, _RetryExhaustedType) and r.cause == "unknown"

    def test_parse_failed_cause(self, monkeypatch):
        # call 成功但 parse 全 None(LLM 返畸形)→ 0-解析重试耗尽
        r = self._judge(monkeypatch, lambda *a, **k: "not json at all")
        assert isinstance(r, _RetryExhaustedType) and r.cause == "parse_failed"


class TestUndercoveredBatchRetry:
    """批内漏答过半的覆盖率重试闸(_judge_batch_with_retry)。

    2026-08-30:不稳期实锤一个批返回只覆盖 2/10 的合法数组——原闸只认「整批 0 解析」,
    部分省略直通 → 8 条 UNRESOLVED(建议重跑整单)。现过半缺席也重试;耗尽回覆盖
    最高的那份而非弃批(已判对的不再变没判成);恰好半数(1/2)不重试——模型系统性
    漏近重复指令是常态,别为它每批白烧重试。
    """

    @staticmethod
    def _step_json(*verdicts: str) -> str:
        return json.dumps([
            {"index": i, "verdict": v, "evidence": f"ev{i}"}
            for i, v in enumerate(verdicts)
        ])

    def _judge(self, monkeypatch, replies: list[str], bn: int):
        calls = {"n": 0}

        def fake(prompt, *a, **k):
            r = replies[min(calls["n"], len(replies) - 1)]
            calls["n"] += 1
            return r

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)
        out = _judge_batch_with_retry(
            "p", None, 600, TranscriptAuditor._parse_step, bn,
            on_progress=None, label="测试", k=1, total=1,
            is_unparsed=lambda v: v[0] is None, transport="http",
        )
        return out, calls["n"]

    def test_under_half_retries_and_recovers(self, monkeypatch) -> None:
        """10 条只答 2 条 → 视同批失败重试;第二次全答 → 采用第二次。"""
        out, calls = self._judge(monkeypatch, [
            self._step_json("pass", "na"),        # 2/10 → 漏答过半,重试
            self._step_json(*(["pass"] * 10)),    # 10/10
        ], 10)

        assert calls == 2
        assert all(v[0] is AuditVerdict.PASS for v in out)

    def test_exhausted_returns_best_not_sentinel(self, monkeypatch) -> None:
        """始终 2/10 → 重试耗尽后回覆盖最高的一份(非 _RetryExHAUSTED 弃批)。"""
        out, calls = self._judge(monkeypatch, [self._step_json("pass", "na")], 10)

        assert calls == _LLM_RETRY_MAX + 1
        assert not isinstance(out, _RetryExhaustedType)
        assert out[0][0] is AuditVerdict.PASS   # 已判的保留
        assert out[1][0] is AuditVerdict.NA
        assert out[2][0] is None                # 漏的保持没判成(unmentioned 语义)

    def test_exactly_half_no_retry(self, monkeypatch) -> None:
        """1/2 恰好半数 → 不重试;1 条有判定、1 条 None(漏 1 条是常态税)。"""
        out, calls = self._judge(monkeypatch, [self._step_json("pass")], 2)

        assert calls == 1
        assert out[0][0] is AuditVerdict.PASS
        assert out[1][0] is None


class TestRunBatchesReturnsFailures:
    """_run_batches_concurrent 返回 (successes, failures);耗尽批带 cause,异常批=judge_error。"""

    def _run(self, monkeypatch, fake_judge, specs):
        monkeypatch.setattr(auditor, "_judge_batch_with_retry", fake_judge)
        successes, failures = _run_batches_concurrent(
            specs, lambda text, n: [], None, 600, None, None, "测试", len(specs),
            lambda v: False,
        )
        return successes, failures

    def test_successes_and_failures_split(self, monkeypatch):
        # spec = (start, bn, k, prompt);批2 耗尽(cause=timeout)。3 批 → 走并发分支,
        # as_completed 完成序不确定 → successes 用 dict 比较(按 start 键,与顺序无关)。
        specs = [(0, 2, 1, "p1"), (2, 2, 2, "p2"), (4, 1, 3, "p3")]

        def fake(prompt, *a, **k):
            if prompt == "p2":
                return _RetryExhaustedType(cause="timeout")
            return [{"v": prompt}]  # parsed list

        successes, failures = self._run(monkeypatch, fake, specs)
        assert dict(successes) == {0: [{"v": "p1"}], 4: [{"v": "p3"}]}
        assert failures == [(2, 2, "timeout")]

    def test_exception_is_judge_error(self, monkeypatch):
        specs = [(0, 1, 1, "p1")]

        def boom(prompt, *a, **k):
            raise ValueError("parse_fn bug")

        successes, failures = self._run(monkeypatch, boom, specs)
        assert successes == []
        assert failures == [(0, 1, "judge_error")]

    def test_no_failures_when_all_succeed(self, monkeypatch):
        specs = [(0, 1, 1, "p1")]
        successes, failures = self._run(monkeypatch, lambda p, *a, **k: [{}], specs)
        assert successes == [(0, [{}])]
        assert failures == []


class TestUnresolvedCauseThreading:
    """红线 + 贯通:UNRESOLVED finding 带 infra cause;非 UNRESOLVED 恒 None。"""

    def test_runtime_error_unknown_cause(self, monkeypatch):
        """call_claude 持续 RuntimeError(未命中模式)→ UNRESOLVED,cause=unknown。"""
        def boom(*a, **k):
            raise RuntimeError("claude CLI down")
        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)  # 重试退避免真等(3s×2)
        f = TranscriptAuditor()._judge_conditional([_tool_inst()], [], "场景", "t.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.unresolved_cause == "unknown"

    def test_timeout_cause(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("execution timed out after 600s")
        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)
        f = TranscriptAuditor()._judge_conditional([_tool_inst()], [], "场景", "t.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.unresolved_cause == "timeout"

    def test_rate_limited_cause(self, monkeypatch):
        from sift.utils import _HttpRateLimited
        def boom(*a, **k):
            raise _HttpRateLimited("429")
        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(auditor.random, "uniform", lambda *a, **k: 0)
        f = TranscriptAuditor()._judge_conditional([_tool_inst()], [], "场景", "t.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.unresolved_cause == "rate_limited"

    def test_parse_failed_cause(self, monkeypatch):
        monkeypatch.setattr("sift.audit.auditor.call_claude",
                            lambda *a, **k: "not json at all")
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)  # 0-解析重试退避免真等
        f = TranscriptAuditor()._judge_conditional([_tool_inst()], [], "场景", "t.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.unresolved_cause == "parse_failed"

    def test_cause_only_on_unresolved(self, monkeypatch):
        """红线:FAIL finding 的 unresolved_cause 恒 None。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude",
                            lambda *a, **k: '[{"index": 0, "applicable": true}]')
        f = TranscriptAuditor()._judge_conditional([_tool_inst()], [], "场景", "t.jsonl")[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.unresolved_cause is None

    def test_unmentioned_when_llm_omits_one_in_batch(self, monkeypatch):
        """批成功但 LLM 只提了部分指令 → 未被提及的那条 UNRESOLVED,cause=unmentioned(语义,非 infra)。

        单条 + LLM 返空数组会走 0-解析重试(parse_failed),触发不了 unmentioned。
        unmentioned 需:多指令同批、LLM 解出部分、漏掉的部分留 None → 批成功(非全 None)
        → 漏的那条 cause_mask 保持默认 unmentioned。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude",
                            lambda *a, **k: '[{"index": 0, "applicable": true}]')  # 只提 index 0
        findings = TranscriptAuditor()._judge_conditional(
            [_tool_inst(), _tool_inst()], [], "场景", "t.jsonl")
        assert findings[0].verdict == AuditVerdict.FAIL        # 被提及 → 判定
        assert findings[1].verdict == AuditVerdict.UNRESOLVED  # 未提及 → 没判成
        assert findings[1].unresolved_cause == "unmentioned"


class TestUnresolvedBreakdown:
    """_unresolved_breakdown:UNRESOLVED findings 按 cause 聚合计数(诊断,不进判决)。"""

    def test_counts_by_cause(self):
        from sift.audit.auditor import _unresolved_breakdown
        findings = [
            AuditFinding(instruction_text="a", source="X", verdict=AuditVerdict.UNRESOLVED,
                         method=AuditMethod.LLM, category=AuditCategory.PROHIBITION,
                         evidence="e", transcript="t", unresolved_cause="timeout"),
            AuditFinding(instruction_text="b", source="X", verdict=AuditVerdict.UNRESOLVED,
                         method=AuditMethod.LLM, category=AuditCategory.PROHIBITION,
                         evidence="e", transcript="t", unresolved_cause="timeout"),
            AuditFinding(instruction_text="c", source="X", verdict=AuditVerdict.UNRESOLVED,
                         method=AuditMethod.LLM, category=AuditCategory.PROHIBITION,
                         evidence="e", transcript="t", unresolved_cause="rate_limited"),
            AuditFinding(instruction_text="d", source="X", verdict=AuditVerdict.PASS,
                         method=AuditMethod.LLM, category=AuditCategory.PROHIBITION,
                         evidence="e", transcript="t", unresolved_cause=None),
        ]
        assert _unresolved_breakdown(findings) == {"timeout": 2, "rate_limited": 1}

    def test_none_cause_counts_as_unknown(self):
        from sift.audit.auditor import _unresolved_breakdown
        findings = [AuditFinding(instruction_text="a", source="X", verdict=AuditVerdict.UNRESOLVED,
                                 method=AuditMethod.LLM, category=AuditCategory.PROHIBITION,
                                 evidence="e", transcript="t", unresolved_cause=None)]
        assert _unresolved_breakdown(findings) == {"unknown": 1}

    def test_empty_when_no_unresolved(self):
        from sift.audit.auditor import _unresolved_breakdown
        assert _unresolved_breakdown([]) == {}


class TestAuxSubagentRescue:
    """A1 旁路:条件类 root trace 未命中 → aux 子代理痕迹第二遍确定性匹配。

    编排范式下执行类要求(跑命令/读文件)全部由子代理实际执行,主 scope 恒缺席;
    aux 命中即"经派发执行"的事实 → PASS(确定性,不进 LLM),evidence 归属执行者。
    """

    def _aux_tc(self, name: str, inp: dict, agent: str = "verifier", turn: int = 42) -> ToolCallRecord:
        return ToolCallRecord(
            name=name, input_summary=json.dumps(inp, ensure_ascii=False)[:200],
            input=inp, turn=turn, agent=agent,
        )

    def test_aux_hit_pass_no_llm(self, monkeypatch) -> None:
        """root 未命中 + aux 命中 → PASS,method=PROGRAMMATIC,evidence 标注子代理
        角色/turn/经派发执行,call_claude 未被调用。"""
        calls = {"n": 0}
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "[]"
        )
        auditor = TranscriptAuditor()
        root_trace = [_tc("Read", "读 STATE.md")]
        aux = [self._aux_tc("Bash", {"command": "nm -D libdemo_op.so"}, agent="code-verifier", turn=118)]

        findings = auditor._judge_conditional(
            [_tool_inst(text="使用 nm 检查符号", tool_name="nm")], root_trace, "场景", "t.jsonl",
            aux_tool_trace=aux,
        )
        assert calls["n"] == 0  # 确定性命中,不调 LLM
        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.method == AuditMethod.PROGRAMMATIC
        assert "code-verifier" in f.evidence
        assert "T118" in f.evidence
        assert "经派发执行" in f.evidence
        assert "主 trace 未命中" in f.evidence  # 与主路径 PASS 的 evidence 区分

    def test_root_hit_takes_priority_over_aux(self, monkeypatch) -> None:
        """root 命中优先:两条 trace 都有 → 走主路径 evidence("trace 中调用了"),
        不提子代理(主 agent 亲自执行)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: "[]")
        auditor = TranscriptAuditor()
        root_trace = [_tc("Bash", "nm -D x.so")]
        aux = [self._aux_tc("Bash", {"command": "nm -D y.so"})]

        findings = auditor._judge_conditional(
            [_tool_inst(tool_name="nm")], root_trace, "场景", "t.jsonl", aux_tool_trace=aux,
        )
        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[0].evidence.startswith("trace 中调用了")

    def test_aux_miss_falls_to_llm(self, monkeypatch) -> None:
        """root + aux 都未命中 → 走 LLM 兜底(行为与旧版一致)。"""
        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": True, "reason": "该做"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        findings = auditor._judge_conditional(
            [_tool_inst(tool_name="curl")], [_tc("Bash", "ls")], "场景", "t.jsonl",
            aux_tool_trace=[self._aux_tc("Read", {"filePath": "a.md"})],
        )
        assert findings[0].verdict == AuditVerdict.FAIL
        assert findings[0].method == AuditMethod.LLM

    def test_aux_none_and_empty_identical(self, monkeypatch) -> None:
        """aux=None 与 aux=[]:均无旁路,未命中走 LLM(向后兼容)。"""
        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": False, "reason": "不涉及"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        for aux in (None, []):
            findings = auditor._judge_conditional(
                [_tool_inst(tool_name="curl")], [_tc("Bash", "ls")], "场景", "t.jsonl",
                aux_tool_trace=aux,
            )
            assert findings[0].verdict == AuditVerdict.NA

    def test_aux_reference_usage_filepath(self, monkeypatch) -> None:
        """REFERENCE_USAGE 的 aux 命中:子代理 Read 的 filePath(opencode camelCase)
        匹配声明 script_path → PASS,evidence 标注"读取"。"""
        calls = {"n": 0}
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "[]"
        )
        auditor = TranscriptAuditor()
        aux = [self._aux_tc(
            "Read",
            {"filePath": "/home/dev/skills/reporting/references/demo-contract.md"},
            agent="developer", turn=1531,
        )]
        findings = auditor._judge_conditional(
            [_ref_inst(text="Use reference file: references/demo-contract.md",
                       script_path="references/demo-contract.md")],
            [], "场景", "t.jsonl", aux_tool_trace=aux,
        )
        assert calls["n"] == 0
        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert "developer" in f.evidence and "读取" in f.evidence

    def test_aux_required_params_still_enforced(self, monkeypatch) -> None:
        """带 required_params 的条目:aux 命中工具但参数不满足 → 不算命中(走 LLM)。
        参数级断言不因旁路放宽("工具名对、参数没照做"不放过)。"""
        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": True, "reason": "该做"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        inst = _tool_inst(text="执行 pytest --tb=short", tool_name="pytest")
        inst.params["required_params"] = ["--tb=short"]
        aux = [self._aux_tc("Bash", {"command": "pytest"})]  # 无 --tb=short
        findings = auditor._judge_conditional(
            [inst], [], "场景", "t.jsonl", aux_tool_trace=aux,
        )
        assert findings[0].verdict == AuditVerdict.FAIL  # 参数不满足,旁路不洗

    def test_aux_strict_no_text_mention_rescue(self, monkeypatch) -> None:
        """严格匹配:Write/Edit 文档**内容**里提及工具名不算执行(假救援防护)。
        实测回归:子代理 Write 状态文件内容含"docs-gen"字样,被宽匹配误判
        "已调用"——该 skill 实际从未被加载(收尾任务未开始)。"""
        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": True, "reason": "该做"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        aux = [self._aux_tc(
            "Write", {"content": "# 工作流\n- [ ] 40. 调用 docs-gen 生成文档"},
            agent="state-generator",
        )]
        findings = auditor._judge_conditional(
            [_tool_inst(text="调用 `docs-gen", tool_name="docs-gen")],
            [], "场景", "t.jsonl", aux_tool_trace=aux,
        )
        assert findings[0].verdict == AuditVerdict.FAIL  # 文本提及不洗

    def test_aux_strict_skill_invoke_rescues(self, monkeypatch) -> None:
        """严格匹配:Skill tool_use 且 input.skill 含工具名 = 真加载 → PASS。"""
        calls = {"n": 0}
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "[]"
        )
        auditor = TranscriptAuditor()
        aux = [self._aux_tc("Skill", {"skill": "env-check"}, agent="developer", turn=7)]
        findings = auditor._judge_conditional(
            [_tool_inst(text="使用 env-check skill", tool_name="env-check")],
            [], "场景", "t.jsonl", aux_tool_trace=aux,
        )
        assert calls["n"] == 0
        assert findings[0].verdict == AuditVerdict.PASS
        assert "developer" in findings[0].evidence

    def test_aux_strict_command_field_only_for_bash(self, monkeypatch) -> None:
        """严格匹配:Bash 命令词只搜 command 字段;input 其他文本负载(如 description)
        提及不算。"""
        def fake(prompt, *a, **k):
            return json.dumps([{"index": 0, "applicable": True, "reason": "该做"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        aux = [self._aux_tc("Bash", {"command": "ls -la", "description": "运行 nm 检查前先看目录"})]
        findings = auditor._judge_conditional(
            [_tool_inst(text="使用 nm", tool_name="nm")], [], "场景", "t.jsonl", aux_tool_trace=aux,
        )
        assert findings[0].verdict == AuditVerdict.FAIL  # description 提及不洗


class TestStepDispatchPairs:
    """层3:派发-回报对喂 step 判官——prompt 含"派发-回报记录"段与"传递≠达成"规则;
    SIFT_DISPATCH_PAIRS=0 或 pairs=None 时行为与旧版一致。全程 mock call_claude。"""

    def _pair(self, role="verifier", result="FAILED: 254/275"):
        from sift.evals.transcript import DispatchReturn
        return DispatchReturn(dispatch_turn=9, result_turn=10, role=role,
                              prompt_head="验收 Task 47:黑盒全量", result_head=result,
                              result_len=len(result))

    def _step_inst(self):
        return InstructionCheck(
            source=InstructionSource.HEADING_STEP,
            instruction_text="验收标准:黑盒全部用例全量执行且全部通过",
            section="Tasks", priority=InstructionPriority.REQUIRED,
            check_method=CheckMethod.LLM_JUDGE, params={},
        )

    def test_pairs_section_in_step_judge_prompt(self, monkeypatch) -> None:
        """_llm_judge_step 的 prompt 含 pairs 段 + 规则行(传递≠达成)。"""
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "verdict": "pass", "evidence": "走过"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        auditor._llm_judge_step(
            [self._step_inst()], "执行记录文本", None, 600,
            dispatch_pairs=[self._pair()], aux_tool_trace=None,
        )
        assert len(prompts) == 1
        assert "派发-回报记录" in prompts[0]
        assert "回报(子代理产出)" in prompts[0]
        assert "FAILED: 254/275" in prompts[0]
        assert "传递了要求 ≠ 达成了结果" in prompts[0]  # 规则行进 prompt

    def test_pairs_none_keeps_old_prompt(self, monkeypatch) -> None:
        """pairs=None → 段/规则都不进(prompt 与旧版同构)。"""
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "verdict": "na", "evidence": "不涉及"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        auditor._llm_judge_step(
            [self._step_inst()], "执行记录文本", None, 600, dispatch_pairs=None,
        )
        assert "派发-回报记录" not in prompts[0]
        assert "传递了要求 ≠ 达成了结果" not in prompts[0]

    def test_judge_step_passes_pairs_through(self, monkeypatch) -> None:
        """_judge_step → _llm_judge_step 穿线:pairs 到达 prompt。"""
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "verdict": "pass", "evidence": "ok"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        findings = auditor._judge_step(
            [self._step_inst()], "文本", "t.jsonl",
            dispatch_pairs=[self._pair()],
        )
        assert findings[0].verdict == AuditVerdict.PASS
        assert "派发-回报记录" in prompts[0]


class TestProhibitionPairsShare:
    """层3-改进3:prohibition 判官共享 pairs 段(SIFT_PROHIBITION_PAIRS=1)。"""

    def test_default_none_pairs_keeps_old_prompt(self, monkeypatch) -> None:
        """pairs 不传/None(或 env 显式关):prohibition prompt 不含 pairs 段。
        默认开(实验转正),env SIFT_PROHIBITION_PAIRS=0 是逃生口——这里验关路。"""
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "violated": False, "evidence": "未违反"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        monkeypatch.setenv("SIFT_PROHIBITION_PAIRS", "0")
        auditor = TranscriptAuditor()
        from sift.evals.transcript import DispatchReturn
        pairs = [DispatchReturn(dispatch_turn=1, result_turn=2, role="verifier",
                                prompt_head="验收", result_head="FAILED", result_len=6)]
        auditor._llm_judge_prohibition(
            [_prohibition_inst()], "文本", None, 600, dispatch_pairs=pairs,
        )
        # 注:env 门在 audit() 调用点(默认开才传 pairs);_llm 层直传 pairs 仍渲染段。
        # 本测试锚定的是"不传 pairs 的默认 prompt 形态"(旧版兼容路径)。
        auditor._llm_judge_prohibition(
            [_prohibition_inst()], "文本", None, 600,
        )
        assert "派发-回报记录" not in prompts[1]

    def test_on_with_env(self, monkeypatch) -> None:
        """env=1:pairs 段进 prohibition prompt(由 _judge_prohibition 穿线;
        这里直接验 _llm_judge_prohibition 的段拼装)。"""
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps([{"index": 0, "violated": False, "evidence": "未违反"}])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        from sift.evals.transcript import DispatchReturn
        pairs = [DispatchReturn(dispatch_turn=1, result_turn=2, role="verifier",
                                prompt_head="验收 Task 47", result_head="FAILED: 254/275",
                                result_len=15)]
        auditor = TranscriptAuditor()
        auditor._llm_judge_prohibition(
            [_prohibition_inst()], "文本", None, 600, dispatch_pairs=pairs,
        )
        assert "派发-回报记录" in prompts[0]
        assert "FAILED: 254/275" in prompts[0]
