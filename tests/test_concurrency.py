# tests/test_concurrency.py
"""AdaptiveConcurrencyLimiter 单元测:基础门控 + AIMD 调节 + 动态 ceiling 并发边界。

替代固定 threading.Semaphore,动态调在飞数上限,撞限流乘性减、连续成功加性增。
"""

from __future__ import annotations

import json
import threading

import pytest
from sift.audit import auditor
from sift.audit.judge_context import BatchJudgeOptions, JudgeBatch
from sift.concurrency import AdaptiveConcurrencyLimiter
from sift.llm_transport import HttpRateLimited

# ── 基础门控(Semaphore 语义)─────────────────────────────────────────────


def test_acquire_up_to_ceiling_then_nonblocking_fails():
    lim = AdaptiveConcurrencyLimiter(initial=3)
    assert lim.acquire(blocking=False)
    assert lim.acquire(blocking=False)
    assert lim.acquire(blocking=False)
    assert lim.in_flight == 3
    assert lim.acquire(blocking=False) is False  # 满


def test_release_frees_slot():
    lim = AdaptiveConcurrencyLimiter(initial=2)
    lim.acquire()
    lim.acquire()
    assert lim.acquire(blocking=False) is False
    lim.release()
    assert lim.acquire(blocking=False) is True


def test_context_manager_releases_on_exit():
    lim = AdaptiveConcurrencyLimiter(initial=2)
    with lim:
        assert lim.in_flight == 1
    assert lim.in_flight == 0
    assert lim.acquire(blocking=False) is True  # 退出后名额归还


def test_context_manager_releases_on_exception():
    lim = AdaptiveConcurrencyLimiter(initial=1)
    with pytest.raises(RuntimeError):
        with lim:
            raise RuntimeError("boom")
    assert lim.in_flight == 0


def test_initial_validation():
    with pytest.raises(ValueError):
        AdaptiveConcurrencyLimiter(initial=0)


def test_blocking_acquire_times_out():
    lim = AdaptiveConcurrencyLimiter(initial=1)
    lim.acquire()  # 占满
    assert lim.acquire(blocking=True, timeout=0.1) is False  # 超时


# ── AIMD 调节 ──────────────────────────────────────────────────────────────


def test_on_success_additive_increase_after_probe_every():
    lim = AdaptiveConcurrencyLimiter(initial=4, probe_every=3)
    for _ in range(3):
        lim.on_success()
    assert lim.ceiling == 5  # +1


def test_on_success_streak_resets_after_increase():
    lim = AdaptiveConcurrencyLimiter(initial=4, probe_every=2)
    lim.on_success()
    lim.on_success()  # +1 → ceiling 5, streak reset
    assert lim.ceiling == 5
    lim.on_success()  # streak=1, 不够 probe_every
    assert lim.ceiling == 5  # 不变


def test_on_success_caps_at_cap():
    lim = AdaptiveConcurrencyLimiter(initial=4, cap=5, probe_every=1)
    lim.on_success()  # →5 (cap)
    assert lim.ceiling == 5
    lim.on_success()  # 封顶不超
    assert lim.ceiling == 5


def test_on_throttled_multiplicative_decrease():
    lim = AdaptiveConcurrencyLimiter(initial=8)
    lim.on_throttled()
    assert lim.ceiling == 4  # 8//2


def test_on_throttled_floored():
    lim = AdaptiveConcurrencyLimiter(initial=3, floor=2)
    lim.on_throttled()  # 3//2=1 → floor 2
    assert lim.ceiling == 2
    lim.on_throttled()  # 2//2=1 → floor 2
    assert lim.ceiling == 2


def test_fixed_mode_on_success_noop():
    lim = AdaptiveConcurrencyLimiter(initial=4, probe_every=1)
    lim.set_fixed(3)
    assert lim.adaptive is False
    for _ in range(10):
        lim.on_success()
    assert lim.ceiling == 3  # 固定,不变


def test_fixed_mode_on_throttled_noop():
    lim = AdaptiveConcurrencyLimiter(initial=4)
    lim.set_fixed(3)
    lim.on_throttled()
    assert lim.ceiling == 3


def test_set_adaptive_resumes():
    lim = AdaptiveConcurrencyLimiter(initial=4, probe_every=1)
    lim.set_fixed(3)
    lim.set_adaptive()
    assert lim.adaptive is True
    lim.on_success()
    assert lim.ceiling == 4  # 恢复后 +1


def test_set_fixed_validation():
    lim = AdaptiveConcurrencyLimiter(initial=4)
    with pytest.raises(ValueError):
        lim.set_fixed(0)


# ── 动态 ceiling 并发边界(关键正确性)──────────────────────────────────────


def test_blocking_acquire_waits_for_release():
    """acquire 阻塞到名额释放。"""
    lim = AdaptiveConcurrencyLimiter(initial=1)
    lim.acquire()  # 主线程占满
    acquired = threading.Event()

    def worker():
        lim.acquire()  # 阻塞
        acquired.set()

    t = threading.Thread(target=worker)
    t.start()
    assert not acquired.wait(0.1)  # 还在等
    lim.release()  # 释放 → worker 拿到
    assert acquired.wait(2.0)
    t.join()
    lim.release()  # worker 的名额


def test_on_throttled_lowering_keeps_inflight_blocked():
    """降 ceiling 后,in_flight 仍 >= 新 ceiling → 新 acquire 继续阻塞,
    直到在飞 release 自然减到 < ceiling 才放行(验证不强杀、不超限)。
    """
    lim = AdaptiveConcurrencyLimiter(initial=4)
    for _ in range(4):  # 占满 4
        lim.acquire()
    lim.on_throttled()  # ceiling 4→2, in_flight 仍 4
    assert lim.ceiling == 2
    assert lim.acquire(blocking=False) is False  # 4 >= 2,拒绝
    lim.release()  # in_flight 3,仍 >= 2
    assert lim.acquire(blocking=False) is False
    lim.release()  # in_flight 2,仍 >= 2
    assert lim.acquire(blocking=False) is False
    lim.release()  # in_flight 1 < 2,放行
    assert lim.acquire(blocking=False) is True


def test_on_success_raising_wakes_waiter():
    """升 ceiling 后,等待的 acquire 被 notify 唤醒通过。"""
    lim = AdaptiveConcurrencyLimiter(initial=1, probe_every=1)
    lim.acquire()  # 占满(ceiling=1)
    got = threading.Event()

    def worker():
        lim.acquire()
        got.set()

    t = threading.Thread(target=worker)
    t.start()
    assert not got.wait(0.1)  # 阻塞(ceiling=1 满)
    lim.on_success()  # ceiling 1→2 → notify → worker 通过(仍有 1 名额)
    assert got.wait(2.0)
    t.join()


# ── judge_batch_with_retry 接 AIMD(集成)─────────────────────────────────


def test_judge_batch_aimd_on_rate_limit_then_success(monkeypatch):
    """call_claude 先抛 HttpRateLimited 再成功 → on_throttled(降 ceiling)+ 退避 sleep +
    成功后 on_success。
    """
    auditor.LLM_LIMITER.reset(6)
    seq = []

    def fake_call(prompt, **kw):
        seq.append(1)
        if len(seq) == 1:
            raise HttpRateLimited("429 test")
        return '[{"index":0,"verdict":"pass","evidence":"ok"}]'

    monkeypatch.setattr(auditor, "call_claude", fake_call)
    sleeps = []
    monkeypatch.setattr(auditor.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(auditor.random, "uniform", lambda a, b: 0.0)

    result = auditor.judge_batch_with_retry(
        parse_fn=lambda t, n: json.loads(t),
        is_unparsed=lambda v: not v,
        batch=JudgeBatch(prompt="p", count=1, number=1, total=1),
        options=BatchJudgeOptions(model=None, timeout=10, on_progress=None, label="测", transport="http"),
    )
    assert result == [{"index": 0, "verdict": "pass", "evidence": "ok"}]
    assert auditor.LLM_LIMITER.ceiling == 3  # 6//2 降了
    assert auditor.RATE_LIMIT_BACKOFF in sleeps  # AIMD 退避发生
    auditor.LLM_LIMITER.reset(6)


def test_judge_batch_success_keeps_ceiling_below_probe(monkeypatch):
    """成功 → on_success(streak+1),但一次 < probe_every(8),ceiling 不变。"""
    auditor.LLM_LIMITER.reset(6)
    monkeypatch.setattr(auditor, "call_claude", lambda *a, **k: '[{"index":0,"verdict":"pass","evidence":"ok"}]')
    monkeypatch.setattr(auditor.time, "sleep", lambda s: None)
    auditor.judge_batch_with_retry(
        parse_fn=lambda t, n: json.loads(t),
        is_unparsed=lambda v: not v,
        batch=JudgeBatch(prompt="p", count=1, number=1, total=1),
        options=BatchJudgeOptions(model=None, timeout=10, on_progress=None, label="测", transport="http"),
    )
    assert auditor.LLM_LIMITER.ceiling == 6  # 一次成功不够 probe_every,不升不降
    auditor.LLM_LIMITER.reset(6)


def test_judge_batch_fixed_mode_skips_aimd_on_rate_limit(monkeypatch):
    """固定模式(set_fixed)下撞 429:on_throttled no-op(ceiling 不降),但仍退避重试救回。"""
    auditor.LLM_LIMITER.set_fixed(4)
    seq = []

    def fake_call(prompt, **kw):
        seq.append(1)
        if len(seq) == 1:
            raise HttpRateLimited("429")
        return '[{"index":0,"verdict":"pass","evidence":"ok"}]'

    monkeypatch.setattr(auditor, "call_claude", fake_call)
    monkeypatch.setattr(auditor.time, "sleep", lambda s: None)
    monkeypatch.setattr(auditor.random, "uniform", lambda a, b: 0.0)
    auditor.judge_batch_with_retry(
        parse_fn=lambda t, n: json.loads(t),
        is_unparsed=lambda v: not v,
        batch=JudgeBatch(prompt="p", count=1, number=1, total=1),
        options=BatchJudgeOptions(model=None, timeout=10, on_progress=None, label="测", transport="http"),
    )
    assert auditor.LLM_LIMITER.ceiling == 4  # 固定模式不降
    assert auditor.LLM_LIMITER.adaptive is False
    auditor.LLM_LIMITER.reset(6)


def test_judge_batch_emits_throttle_progress(monkeypatch):
    """撞 429 时 on_progress 收到「撞限流…并发降至 N」进度行(可观测,诊断用)。"""
    auditor.LLM_LIMITER.reset(8)
    seq = []

    def fake_call(prompt, **kw):
        seq.append(1)
        if len(seq) == 1:
            raise HttpRateLimited("429")
        return '[{"index":0,"verdict":"pass","evidence":"ok"}]'

    monkeypatch.setattr(auditor, "call_claude", fake_call)
    monkeypatch.setattr(auditor.time, "sleep", lambda s: None)
    monkeypatch.setattr(auditor.random, "uniform", lambda a, b: 0.0)
    progs: list[str] = []
    auditor.judge_batch_with_retry(
        parse_fn=lambda t, n: json.loads(t),
        is_unparsed=lambda v: not v,
        batch=JudgeBatch(prompt="p", count=1, number=1, total=1),
        options=BatchJudgeOptions(model=None, timeout=10, on_progress=progs.append, label="测", transport="http"),
    )
    assert any("撞限流" in m and "降至 4" in m for m in progs)  # 8//2=4
    auditor.LLM_LIMITER.reset(6)


def test_limiter_singleton_shared_across_modules():
    """LLM_LIMITER 模块级单例:auditor(上提后 from-import)与 concurrency 必须同一对象。
    否则 audit 与 eval 侧各用一个 → 共享容量失效(B2 防回归)。
    """
    from sift import concurrency
    from sift.concurrency import LLM_LIMITER as cc_limiter

    assert auditor.LLM_LIMITER is cc_limiter
    assert concurrency.LLM_SEMAPHORE is cc_limiter  # 别名也指向同一单例


# ── call_llm_with_retry(调用级重试壳:AIMD 门控 + 429 退避 + 耗尽抛 LLMRetryExhausted)──


def _reset_limiter_and_time(monkeypatch):
    import sift.concurrency as cc

    cc.LLM_LIMITER.reset(cc.MAX_BATCH_CONCURRENT)
    monkeypatch.setattr(cc.time, "sleep", lambda *a: None)
    monkeypatch.setattr(cc.random, "uniform", lambda *a: 0.0)
    return cc


def test_llm_retry_exhausted_not_runtime_error():
    """LLMRetryExhausted 非 RuntimeError 子类(镜像 HttpRateLimited,防被通用 except 误吞,
    调用方须显式 catch 才降级)。
    """
    import sift.concurrency as cc

    assert not issubclass(cc.LLMRetryExhausted, RuntimeError)


def test_call_llm_with_retry_success_calls_on_success(monkeypatch):
    """成功必须调 on_success——抓早返 bug(return 在 with 内 → on_success 永不调 → AIMD 不增)。"""
    cc = _reset_limiter_and_time(monkeypatch)
    monkeypatch.setattr(cc, "call_claude", lambda *a, **k: "ok")
    spied = []
    monkeypatch.setattr(cc.LLM_LIMITER, "on_success", lambda: spied.append(1))
    assert cc.call_llm_with_retry("p") == "ok"
    assert spied == [1]


def test_call_llm_with_retry_forwards_on_progress_to_call_claude(monkeypatch):
    """on_progress/max_tokens 透传进 call_claude(进度行从这走;max_tokens = 调用方按
    工作量给的首档上限,suggest 按簇数)。
    """
    cc = _reset_limiter_and_time(monkeypatch)
    captured = {}

    def fake_call_claude(*a, **k):
        captured.update(k)
        return "ok"

    monkeypatch.setattr(cc, "call_claude", fake_call_claude)
    assert cc.call_llm_with_retry("p", on_progress=print, max_tokens=12345) == "ok"
    assert captured.get("on_progress") is print
    assert captured["config"].max_tokens == 12345


def test_call_llm_with_retry_429_then_success_calls_on_throttled(monkeypatch):
    cc = _reset_limiter_and_time(monkeypatch)
    seq = [HttpRateLimited("429"), "ok"]

    def fake(*a, **k):
        v = seq.pop(0)
        if isinstance(v, BaseException):
            raise v
        return v

    monkeypatch.setattr(cc, "call_claude", fake)
    thr = []
    monkeypatch.setattr(cc.LLM_LIMITER, "on_throttled", lambda: thr.append(1))
    monkeypatch.setattr(cc.LLM_LIMITER, "on_success", lambda: None)
    assert cc.call_llm_with_retry("p") == "ok"
    assert thr == [1]  # 429 必触发降并发


def test_call_llm_with_retry_exhaustion_raises(monkeypatch):
    cc = _reset_limiter_and_time(monkeypatch)

    def always_429(*a, **k):
        raise HttpRateLimited("429")

    monkeypatch.setattr(cc, "call_claude", always_429)
    monkeypatch.setattr(cc.LLM_LIMITER, "on_throttled", lambda: None)
    with pytest.raises(cc.LLMRetryExhausted):
        cc.call_llm_with_retry("p", max_retries=1)


def test_call_llm_with_retry_transport_unavailable_fast_fails(monkeypatch):
    """静态缺位(二进制不在 PATH)不重试:单次调用即以 LLMRetryExhausted 上抛,
    消息保留原报错(调用方的 except LLMRetryExhausted 降级路径不变)。"""
    from sift.llm_transport import TransportUnavailable

    cc = _reset_limiter_and_time(monkeypatch)
    calls = []

    def fake(*a, **k):
        calls.append(1)
        raise TransportUnavailable("claude CLI 未找到")

    monkeypatch.setattr(cc, "call_claude", fake)
    with pytest.raises(cc.LLMRetryExhausted, match="claude CLI 未找到"):
        cc.call_llm_with_retry("p", max_retries=3)
    assert calls == [1]  # 不重试(RuntimeError 会重试满 max_retries)


def test_call_llm_with_retry_runtime_error_retried(monkeypatch):
    cc = _reset_limiter_and_time(monkeypatch)
    seq = [RuntimeError("boom"), "ok"]

    def fake(*a, **k):
        v = seq.pop(0)
        if isinstance(v, BaseException):
            raise v
        return v

    monkeypatch.setattr(cc, "call_claude", fake)
    monkeypatch.setattr(cc.LLM_LIMITER, "on_success", lambda: None)
    assert cc.call_llm_with_retry("p") == "ok"
