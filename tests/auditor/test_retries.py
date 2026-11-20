"""Auditor retries behavior tests."""

from __future__ import annotations

import json

from sift.audit import auditor
from sift.audit.auditor import (
    _LLM_RETRY_MAX,
    TranscriptAuditor,
    _RetryExhaustedType,
    _run_batches_concurrent,
    judge_batch_with_retry,
    parse_step_results,
)
from sift.audit.judge_context import (
    BatchJudgeOptions,
    JudgeBatch,
    JudgeCallOptions,
    JudgeTranscript,
)
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditVerdict,
)

from .helpers import _tool_inst


class TestRetryExhaustedCause:
    """judge_batch_with_retry 重试耗尽时,哨兵携带最后失败的原因。"""

    @staticmethod
    def _judge(monkeypatch, fake_call):
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(auditor.random, "uniform", lambda *a, **k: 0)
        return judge_batch_with_retry(
            parse_fn=lambda text, n: [(None, "")],
            is_unparsed=lambda v: v[0] is None,
            batch=JudgeBatch(prompt="p", count=1, number=1, total=1),
            options=BatchJudgeOptions(model=None, timeout=600, on_progress=None, label="测试", transport="http"),
        )

    @staticmethod
    def _raise(exc):
        def f(*a, **k):
            raise exc

        return f

    def test_rate_limited_cause(self, monkeypatch):
        from sift.llm_transport import HttpRateLimited

        r = self._judge(monkeypatch, self._raise(HttpRateLimited("429")))
        assert isinstance(r, _RetryExhaustedType) and r.cause == "rate_limited"

    def test_timeout_cause(self, monkeypatch):
        r = self._judge(monkeypatch, self._raise(RuntimeError("execution timed out")))
        assert isinstance(r, _RetryExhaustedType) and r.cause == "timeout"

    def test_unknown_runtime_cause(self, monkeypatch):
        r = self._judge(monkeypatch, self._raise(RuntimeError("claude CLI down")))
        assert isinstance(r, _RetryExhaustedType) and r.cause == "unknown"

    def test_transport_unavailable_cause_no_retry(self, monkeypatch):
        """静态缺位(CLI 不在 PATH)是永久错误:单次即耗尽、cause typed,不烧重试预算
        (普通 RuntimeError 会重试 _LLM_RETRY_MAX 次,这里必须只调一次)。"""
        from sift.llm_transport import TransportUnavailable

        calls = []

        def fake(*a, **k):
            calls.append(1)
            raise TransportUnavailable("claude CLI 未找到")

        r = self._judge(monkeypatch, fake)
        assert isinstance(r, _RetryExhaustedType) and r.cause == "transport_unavailable"
        assert calls == [1]

    def test_parse_failed_cause(self, monkeypatch):
        # call 成功但 parse 全 None(LLM 返畸形)→ 0-解析重试耗尽
        r = self._judge(monkeypatch, lambda *a, **k: "not json at all")
        assert isinstance(r, _RetryExhaustedType) and r.cause == "parse_failed"


class TestUndercoveredBatchRetry:
    """批内漏答过半的覆盖率重试闸(judge_batch_with_retry)。

    2026-08-30:不稳期实锤一个批返回只覆盖 2/10 的合法数组——原闸只认「整批 0 解析」,
    部分省略直通 → 8 条 UNRESOLVED(建议重跑整单)。现过半缺席也重试;耗尽回覆盖
    最高的那份而非弃批(已判对的不再变没判成);恰好半数(1/2)不重试——模型系统性
    漏近重复指令是常态,别为它每批白烧重试。
    """

    @staticmethod
    def _step_json(*verdicts: str) -> str:
        return json.dumps([{"index": i, "verdict": v, "evidence": f"ev{i}"} for i, v in enumerate(verdicts)])

    @staticmethod
    def _judge(monkeypatch, replies: list[str], bn: int):
        calls = {"n": 0}

        def fake(prompt, *a, **k):
            r = replies[min(calls["n"], len(replies) - 1)]
            calls["n"] += 1
            return r

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)
        out = judge_batch_with_retry(
            parse_fn=parse_step_results,
            is_unparsed=lambda v: v[0] is None,
            batch=JudgeBatch(prompt="p", count=bn, number=1, total=1),
            options=BatchJudgeOptions(model=None, timeout=600, on_progress=None, label="测试", transport="http"),
        )
        return out, calls["n"]

    def test_under_half_retries_and_recovers(self, monkeypatch) -> None:
        """10 条只答 2 条 → 视同批失败重试;第二次全答 → 采用第二次。"""
        out, calls = self._judge(
            monkeypatch,
            [
                self._step_json("pass", "na"),  # 2/10 → 漏答过半,重试
                self._step_json(*(["pass"] * 10)),  # 10/10
            ],
            10,
        )

        assert calls == 2
        assert all(v[0] is AuditVerdict.PASS for v in out)

    def test_exhausted_returns_best_not_sentinel(self, monkeypatch) -> None:
        """始终 2/10 → 重试耗尽后回覆盖最高的一份(非 _RetryExHAUSTED 弃批)。"""
        out, calls = self._judge(monkeypatch, [self._step_json("pass", "na")], 10)

        assert calls == _LLM_RETRY_MAX + 1
        assert not isinstance(out, _RetryExhaustedType)
        assert out[0][0] is AuditVerdict.PASS  # 已判的保留
        assert out[1][0] is AuditVerdict.NA
        assert out[2][0] is None  # 漏的保持没判成(unmentioned 语义)

    def test_exactly_half_no_retry(self, monkeypatch) -> None:
        """1/2 恰好半数 → 不重试;1 条有判定、1 条 None(漏 1 条是常态税)。"""
        out, calls = self._judge(monkeypatch, [self._step_json("pass")], 2)

        assert calls == 1
        assert out[0][0] is AuditVerdict.PASS
        assert out[1][0] is None


class TestRunBatchesReturnsFailures:
    """_run_batches_concurrent 返回 (successes, failures);耗尽批带 cause,异常批=judge_error。"""

    @staticmethod
    def _run(monkeypatch, fake_judge, specs):
        monkeypatch.setattr(auditor, "judge_batch_with_retry", fake_judge)
        successes, failures = _run_batches_concurrent(
            specs=specs,
            parse_fn=lambda text, n: [],
            total=len(specs),
            is_unparsed=lambda v: False,
            options=BatchJudgeOptions(model=None, timeout=600, on_progress=None, on_usage=None, label="测试"),
        )
        return successes, failures

    def test_successes_and_failures_split(self, monkeypatch):
        # spec = (start, bn, k, prompt);批2 耗尽(cause=timeout)。3 批 → 走并发分支,
        # as_completed 完成序不确定 → successes 用 dict 比较(按 start 键,与顺序无关)。
        specs = [(0, 2, 1, "p1"), (2, 2, 2, "p2"), (4, 1, 3, "p3")]

        def fake(batch, *a, **k):
            prompt = batch.prompt
            if prompt == "p2":
                return _RetryExhaustedType(cause="timeout")
            return [{"v": prompt}]  # parsed list

        successes, failures = self._run(monkeypatch, fake, specs)
        assert dict(successes) == {0: [{"v": "p1"}], 4: [{"v": "p3"}]}
        assert failures == [(2, 2, "timeout")]

    def test_exception_is_judge_error(self, monkeypatch):
        specs = [(0, 1, 1, "p1")]

        def boom(batch, *a, **k):
            raise ValueError("parse_fn bug")

        successes, failures = self._run(monkeypatch, boom, specs)
        assert successes == []
        assert failures == [(0, 1, "judge_error")]

    def test_no_failures_when_all_succeed(self, monkeypatch):
        specs = [(0, 1, 1, "p1")]
        successes, failures = self._run(monkeypatch, lambda batch, *a, **k: [{}], specs)
        assert successes == [(0, [{}])]
        assert failures == []


class TestUnresolvedCauseThreading:
    """红线 + 贯通:UNRESOLVED finding 带 infra cause;非 UNRESOLVED 恒 None。"""

    @staticmethod
    def test_runtime_error_unknown_cause(monkeypatch):
        """call_claude 持续 RuntimeError(未命中模式)→ UNRESOLVED,cause=unknown。"""

        def boom(*a, **k):
            raise RuntimeError("claude CLI down")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)  # 重试退避免真等(3s×2)
        f = TranscriptAuditor().engine.judge_conditional(
            instructions=[_tool_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.unresolved_cause == "unknown"

    @staticmethod
    def test_timeout_cause(monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("execution timed out after 600s")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)
        f = TranscriptAuditor().engine.judge_conditional(
            instructions=[_tool_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.unresolved_cause == "timeout"

    @staticmethod
    def test_rate_limited_cause(monkeypatch):
        from sift.llm_transport import HttpRateLimited

        def boom(*a, **k):
            raise HttpRateLimited("429")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(auditor.random, "uniform", lambda *a, **k: 0)
        f = TranscriptAuditor().engine.judge_conditional(
            instructions=[_tool_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.unresolved_cause == "rate_limited"

    @staticmethod
    def test_parse_failed_cause(monkeypatch):
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: "not json at all")
        monkeypatch.setattr(auditor.time, "sleep", lambda *a, **k: None)  # 0-解析重试退避免真等
        f = TranscriptAuditor().engine.judge_conditional(
            instructions=[_tool_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.unresolved_cause == "parse_failed"

    @staticmethod
    def test_cause_only_on_unresolved(monkeypatch):
        """红线:FAIL finding 的 unresolved_cause 恒 None。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: '[{"index": 0, "applicable": true}]')
        f = TranscriptAuditor().engine.judge_conditional(
            instructions=[_tool_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.unresolved_cause is None

    @staticmethod
    def test_unmentioned_when_llm_omits_one_in_batch(monkeypatch):
        """批成功但 LLM 只提了部分指令 → 未被提及的那条 UNRESOLVED,cause=unmentioned(语义,非 infra)。

        单条 + LLM 返空数组会走 0-解析重试(parse_failed),触发不了 unmentioned。
        unmentioned 需:多指令同批、LLM 解出部分、漏掉的部分留 None → 批成功(非全 None)
        → 漏的那条 cause_mask 保持默认 unmentioned。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude", lambda *a, **k: '[{"index": 0, "applicable": true}]'
        )  # 只提 index 0
        findings = TranscriptAuditor().engine.judge_conditional(
            instructions=[_tool_inst(), _tool_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="场景", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )
        assert findings[0].verdict == AuditVerdict.FAIL  # 被提及 → 判定
        assert findings[1].verdict == AuditVerdict.UNRESOLVED  # 未提及 → 没判成
        assert findings[1].unresolved_cause == "unmentioned"


class TestUnresolvedBreakdown:
    """_unresolved_breakdown:UNRESOLVED findings 按 cause 聚合计数(诊断,不进判决)。"""

    @staticmethod
    def test_counts_by_cause():
        from sift.audit.auditor import _unresolved_breakdown

        findings = [
            AuditFinding(
                **_audit_finding_values(
                    verdict=AuditVerdict.UNRESOLVED, method=AuditMethod.LLM, category=AuditCategory.PROHIBITION
                )
            ),
            AuditFinding(
                **_audit_finding_values(
                    instruction_text="b",
                    verdict=AuditVerdict.UNRESOLVED,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                )
            ),
            AuditFinding(
                **_audit_finding_values(
                    instruction_text="c",
                    verdict=AuditVerdict.UNRESOLVED,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                    unresolved_cause="rate_limited",
                )
            ),
            AuditFinding(
                **_audit_finding_values(
                    instruction_text="d",
                    verdict=AuditVerdict.PASS,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                    unresolved_cause=None,
                )
            ),
        ]
        assert _unresolved_breakdown(findings) == {"timeout": 2, "rate_limited": 1}

    @staticmethod
    def test_none_cause_counts_as_unknown():
        from sift.audit.auditor import _unresolved_breakdown

        findings = [
            AuditFinding(
                **_audit_finding_values(
                    verdict=AuditVerdict.UNRESOLVED,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                    unresolved_cause=None,
                )
            )
        ]
        assert _unresolved_breakdown(findings) == {"unknown": 1}

    @staticmethod
    def test_empty_when_no_unresolved():
        from sift.audit.auditor import _unresolved_breakdown

        assert _unresolved_breakdown([]) == {}


def _audit_finding_values(**changes):
    data = {
        "instruction_text": "a",
        "source": "X",
        "verdict": None,
        "method": None,
        "category": None,
        "evidence": "e",
        "transcript": "t",
        "unresolved_cause": "timeout",
    }
    data.update(changes)
    return data
