"""自适应并发限制器 + AIMD 控制器。

替代固定 ``threading.Semaphore``,动态调在飞数上限:撞限流(429)乘性减、连续成功
加性增(AIMD),收敛到当前后端的真实容量边界。

**为什么对并发限制 + 速率限制两类后端都通用**:控在飞数 = 控吞吐
(``吞吐 = 在飞数 ÷ 单请求时长``)。AIMD 收敛到"不撞限的最大在飞数":
并发限制后端 → 并发容量;速率限制后端 → ``速率限 × 单请求时长``(吞吐逼近速率限
= 该后端真实上限)。不假设限制类型、只对限流信号反应,故适配任意后端。
"""

from __future__ import annotations

import logging
import random
import threading
import time

from sift.llm_transport import HttpRateLimited, LLMCallConfig, TransportUnavailable, resolve_llm_transport
from sift.utils import call_claude


class AdaptiveConcurrencyLimiter:
    """可动态调上限的并发限制器 + AIMD 控制器。

    - acquire/release/context manager:语义同 ``threading.Semaphore``(在飞数 ≤ ceiling)。
    - on_success():加性增(连续 ``probe_every`` 次 → ceiling+1,封顶 cap)。
    - on_throttled():乘性减(ceiling//=2,不破 floor)。
    - set_fixed(n):锁死 ceiling=n、关 AIMD(= 旧行为,供 ``--max-concurrent`` 向后兼容)。
    - set_adaptive():恢复 AIMD(默认模式)。
    - reset(initial):还原(测试用)。

    线程安全:所有状态读写均在 ``_cond`` 锁下。降 ceiling 不强杀在飞请求(等其 release
    自然减 ``_in_flight``,降到 < ceiling 才放行新请求);升 ceiling ``notify_all`` 唤醒等待者。
    """

    def __init__(self, initial: int, floor: int = 2, cap: int = 32, probe_every: int = 8):
        if initial < 1:
            raise ValueError(f"initial 必须 ≥ 1,收到 {initial}")
        self._cond = threading.Condition()
        self._in_flight = 0
        self._ceiling = initial
        self._floor = max(1, floor)
        self._cap = max(initial, cap)
        self._probe_every = max(1, probe_every)
        self._streak = 0
        self._adaptive = True

    def __enter__(self) -> "AdaptiveConcurrencyLimiter":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    # ── 只读视图(测试/诊断)──────────────────────────────────────────────
    @property
    def ceiling(self) -> int:
        with self._cond:
            return self._ceiling

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    @property
    def adaptive(self) -> bool:
        with self._cond:
            return self._adaptive

    # ── 并发门控(同 Semaphore 语义)────────────────────────────────────────
    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """获取一个在飞名额。``blocking=False`` 满则立即返 ``False``(不阻塞);
        ``blocking=True`` 阻塞到有名额,``timeout`` 给定则超时返 ``False``。
        """
        with self._cond:
            if not blocking:
                if self._in_flight >= self._ceiling:
                    return False
            else:
                if not self._wait_for_slot(timeout):
                    return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        """释放一个在飞名额。降到 ``_in_flight < _ceiling`` 则 notify 等待者。"""
        with self._cond:
            self._in_flight -= 1
            if self._in_flight < self._ceiling:
                self._cond.notify_all()

    # ── AIMD 调节 ──────────────────────────────────────────────────────────
    def on_success(self) -> None:
        """成功一次:streak+1,达 ``probe_every`` 则加性增(ceiling+1 封顶 cap)。
        升 ceiling 后 ``notify_all`` 唤醒等待者。固定模式 no-op。
        """
        with self._cond:
            if not self._adaptive:
                return
            self._streak += 1
            if self._streak >= self._probe_every:
                old = self._ceiling
                self._ceiling = min(self._cap, self._ceiling + 1)
                self._streak = 0
                if self._ceiling > old:
                    self._cond.notify_all()

    def on_throttled(self) -> None:
        """撞限流:乘性减(ceiling//=2 不破 floor)、streak 清零。固定模式 no-op。
        降 ceiling **不 notify**:_in_flight 可能 > 新 ceiling,等其 release 自然减后
        才放行(否则 notify 会让等待者立即通过 → 瞬时超新 ceiling)。
        """
        with self._cond:
            if not self._adaptive:
                return
            self._ceiling = max(self._floor, self._ceiling // 2)
            self._streak = 0

    # ── 模式切换 ───────────────────────────────────────────────────────────
    def set_fixed(self, n: int) -> None:
        """锁死 ceiling=n、关 AIMD(= 旧行为,供 ``--max-concurrent`` 固定向后兼容)。"""
        if n < 1:
            raise ValueError(f"固定并发必须 ≥ 1,收到 {n}")
        with self._cond:
            self._adaptive = False
            self._ceiling = n
            self._streak = 0
            self._cond.notify_all()  # 升/降都可能让等待者通过,唤醒重检

    def set_adaptive(self, initial: int | None = None) -> None:
        """恢复 AIMD 自适应(默认模式)。``initial`` 给则重置起点,否则保留当前 ceiling。"""
        with self._cond:
            self._adaptive = True
            if initial is not None:
                self._ceiling = initial
            self._streak = 0
            self._cond.notify_all()

    def reset(self, initial: int) -> None:
        """还原到初始状态(测试用:清 in_flight、ceiling=initial、streak=0、adaptive=True)。"""
        with self._cond:
            self._adaptive = True
            self._ceiling = initial
            self._streak = 0
            self._in_flight = 0
            self._cond.notify_all()

    def _wait_for_slot(self, timeout: float | None) -> bool:
        """持有条件锁时等待名额；调用方负责占用名额，保持超时语义。"""
        end = None if timeout is None else time.monotonic() + timeout
        while self._in_flight >= self._ceiling:
            if end is None:
                self._cond.wait()
            else:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True


# ── 模块级单例 + 配置(audit 与 eval 侧 grading/comparator/analyzer 共享一个 ceiling)────
# 全局自适应并发限制器:judge_batch_with_retry / call_llm_with_retry 在 call_claude 前后
# acquire/release,把跨所有调用方(判定器批 + eval 侧单次判定)的在飞 LLM 调用钉在 ≤ ceiling。
# AIMD 调 ceiling:撞 429(HttpRateLimited)乘性减、连续成功加性增,收敛到后端真实容量边界。
# 模块级单例 → 跨调用共享(端点容量稳定),audit 与 eval 侧争同一个(同账号同容量预算)。
MAX_BATCH_CONCURRENT = 6  # AIMD 探测起点(保守);真实边界由 on_throttled/on_success 摸出
LLM_LIMITER = AdaptiveConcurrencyLimiter(MAX_BATCH_CONCURRENT)
# 向后兼容别名:旧引用/测试按 LLM_SEMAPHORE 名访问(limiter 兼容 acquire/release/__enter__)。
LLM_SEMAPHORE = LLM_LIMITER
# AIMD 退避:撞 429 后按 基数×2^attempt 指数递增 sleep(+ random 抖动防多线程同步重试)
# 再重试——速率限制按分钟窗口计,固定短退避会在窗口内反复撞死(glm-5.3 实测容量低于 5.2)。
RATE_LIMIT_BACKOFF = 2.0


def configure_max_concurrent(n: int) -> None:
    """锁死全局并发上限 = n、关 AIMD(= 旧行为,供 ``--max-concurrent`` 固定向后兼容)。

    供 CLI ``--max-concurrent`` / env ``SIFT_MAX_CONCURRENT`` 在大声明、低内存机器、
    或多 audit 共存场景下调小(用时间换内存)。在 audit/run 起判定前调。
    """
    if n < 1:
        raise ValueError(f"max_concurrent 必须 ≥ 1,收到 {n}")
    LLM_LIMITER.set_fixed(n)


def configure_adaptive(initial: int | None = None) -> None:
    """恢复 AIMD 自适应并发(默认模式)。``initial`` 给则重设探测起点,否则保留当前 ceiling。

    通用工具默认自适应——动态探当前端容量边界(严苛后端收敛到低值、宽松后端探到更高),
    不写死固定值。
    """
    LLM_LIMITER.set_adaptive(initial)


# ── 调用级 LLM 重试壳(供 eval 侧 grading/comparator/analyzer 切 http 用)──────────────
class LLMRetryExhausted(Exception):
    """``call_llm_with_retry`` 重试耗尽。

    非 ``RuntimeError`` 子类(镜像 ``HttpRateLimited``,防被通用 ``except RuntimeError`` 误吞),
    调用方须显式 catch 后各降级(grader→UNEVAL / comparator→failed / analyzer→skip),永不进 FAIL。
    """


_LLM_WRAPPER_RETRY_MAX = 4  # 调用级重试上限(瞬态 429 / RuntimeError;4 次×指数退避跨速率窗口)
_LLM_WRAPPER_RETRY_DELAY = 3.0  # 线性退避基数(× 尝试序号)
_LLM_WRAPPER_BACKOFF = RATE_LIMIT_BACKOFF  # 429 退避基数(+ random 抖动),与 audit 同源


def _emit(on_progress, msg: str) -> None:
    if on_progress:
        try:
            on_progress(msg)
        except Exception:
            logging.getLogger(__name__).debug("Optional operation failed; continuing with fallback", exc_info=True)


def call_llm_with_retry(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int = 120,
    schema: dict | None = None,
    on_usage=None,
    transport: str | None = None,
    on_progress=None,
    max_retries: int = _LLM_WRAPPER_RETRY_MAX,
    max_tokens: int | None = None,
) -> str:
    """调用级 LLM 重试壳:AIMD 门控(``LLM_LIMITER`` 限在飞)+ 429 退避重试(同份重发)→ 耗尽抛
    ``LLMRetryExhausted``。

    给 eval 侧 grading/comparator/analyzer(单次结构化/文本判定)用;audit 的批级 parse 重试
    (0 解析重试)不在此范围,audit 继续用 ``judge_batch_with_retry``(共享同一 limiter 实例)。

    结构严格镜像 audit ``judge_batch_with_retry``:``with LLM_LIMITER:`` 只圈 ``call_claude``
    (重试退避不占槽);**成功赋值不早返**(早返会跳过 with 外的 ``on_success`` → AIMD 永不加性增
    → 静默劣化),``on_success``/``on_throttled``/sleep 都在 with 外。site default ``http``
    (forced tool_use、schema-safe),``transport=None`` 时经 ``resolve_llm_transport`` 让
    ``SIFT_LLM_TRANSPORT`` 全局覆盖(opencode/dsh 等)。
    """
    resolved_transport = resolve_llm_transport(transport, default="http")
    for attempt in range(max_retries + 1):
        if attempt > 0:
            time.sleep(_LLM_WRAPPER_RETRY_DELAY * attempt)
            _emit(on_progress, f"  LLM 重试 {attempt}/{max_retries}…")
        try:
            with LLM_LIMITER:
                result = call_claude(
                    prompt=prompt,
                    on_usage=on_usage,
                    on_progress=on_progress,
                    config=LLMCallConfig(
                        model=model,
                        timeout=timeout,
                        schema=schema,
                        transport=resolved_transport,
                        max_tokens=max_tokens,
                    ),
                )
        except HttpRateLimited as exc:
            # 429 指数退避(基数×2^attempt,镜像 audit judge_batch_with_retry):速率窗口按
            # 分钟计,固定短退避会在窗口内反复撞死;让连撞的等待跨过窗口。
            LLM_LIMITER.on_throttled()
            _emit(
                on_progress,
                f"  撞限流({str(exc)[:80]}),并发降至 {LLM_LIMITER.ceiling},退避重试…",
            )
            time.sleep(_LLM_WRAPPER_BACKOFF * (2**attempt) + random.uniform(0, 1))
            continue
        except TransportUnavailable as exc:
            # 静态缺位(二进制不在 PATH)是永久错误:不退避不重试,立即以耗尽形态上抛
            # —— 调用方的 except LLMRetryExhausted 降级路径(grader→UNEVAL 等)不变,
            # 消息保留原报错,人能看清是环境问题不是瞬态抖动。
            _emit(on_progress, f"  LLM 调用失败(transport 不可用,不重试): {str(exc)[:120]}")
            raise LLMRetryExhausted(str(exc)) from exc
        except RuntimeError as exc:
            _emit(on_progress, f"  LLM 调用失败: {str(exc)[:120]}")
            continue
        LLM_LIMITER.on_success()  # 成功才到这(with 已 release),AIMD 加性增
        return result
    raise LLMRetryExhausted(f"LLM 调用重试耗尽(max_retries={max_retries})")
