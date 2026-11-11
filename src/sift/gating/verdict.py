"""gate 判决纯函数(块 C)。读 benchmark dict → GateResult,无 IO。"""

from __future__ import annotations

import math

from .models import AxisVerdict, GateResult


def parse_delta(value) -> float | None:
    """ "+0.27" → 0.27;None/无法解析 → None。"""
    if value is None:
        return None
    try:
        number = float(value)
        return number if not isinstance(value, bool) and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _configuration(benchmark: dict, side: str) -> str:
    summary = benchmark.get("run_summary") or {}
    if side in summary and side != "delta":
        return side
    # These are SIFT's own default labels/configuration IDs (build_sides).
    aliases = {"with": "with_skill", "without": "without_skill"}
    if aliases.get(side) in summary:
        return aliases[side]
    cs = benchmark.get("comparison_summary") or {}
    if not [key for key in summary if key != "delta"] and side in [cs.get("label_a"), cs.get("label_b")]:
        return side
    raise ValueError(f"Unknown candidate/baseline side: {side!r}")


def _resolve_sides(benchmark: dict, candidate: str | None) -> tuple[str, str]:
    keys = [key for key in (benchmark.get("run_summary") or {}) if key != "delta"]
    cs = benchmark.get("comparison_summary") or {}
    labels = [cs.get("label_a"), cs.get("label_b")]
    labels = labels if all(labels) else keys[:2]
    cand = candidate or (labels[0] if labels else "")
    if not cand:
        return "", ""
    canonical = _configuration(benchmark, cand)
    matching = [label for label in labels if _configuration(benchmark, label) == canonical]
    if labels and not matching:
        raise ValueError(f"Unknown candidate side: {candidate!r}")
    other = next((label for label in labels if _configuration(benchmark, label) != canonical), "")
    base = _configuration(benchmark, other) if other and cand in keys else other
    return cand, base


def _candidate_is_second(benchmark: dict, candidate: str | None) -> bool:
    cs = benchmark.get("comparison_summary") or {}
    if not candidate or not cs.get("label_b"):
        return False
    return _configuration(benchmark, candidate) == _configuration(benchmark, cs["label_b"])


def _eval_programmatic(benchmark: dict, guard_on: bool, candidate: str | None = None) -> AxisVerdict:
    if not guard_on:
        return AxisVerdict(verdict="N/A", applicable=False, reason="客观守卫已关(--no-programmatic-guard)")
    summary = benchmark.get("run_summary") or {}
    cand, base = _resolve_sides(benchmark, candidate)
    candidate_key = _configuration(benchmark, cand) if cand else ""
    baseline_key = _configuration(benchmark, base) if base else ""
    def mean_for(key):
        raw = ((summary.get(key) or {}).get("pass_rate_programmatic") or {}).get("mean")
        value = parse_delta(raw)
        if raw is not None and value is None:
            raise ValueError(f"Invalid programmatic mean for {key!r}")
        return value
    candidate_mean = mean_for(candidate_key)
    baseline_mean = mean_for(baseline_key)
    if candidate_mean is not None and baseline_mean is not None:
        delta = candidate_mean - baseline_mean
    else:
        # Legacy summaries may contain only delta. Its direction follows the
        # run_summary dictionary order, NOT randomized comparison labels.
        delta = parse_delta((summary.get("delta") or {}).get("pass_rate_programmatic"))
        keys = [key for key in summary if key != "delta"]
        if delta is not None and (candidate_key not in keys[:2] or baseline_key not in keys[:2] or candidate_key == baseline_key):
            return AxisVerdict(verdict="N/A", applicable=True, reason="Cannot bind programmatic delta to candidate and baseline")
        if delta is not None and len(keys) > 1 and candidate_key == keys[1]:
            delta = -delta
    if delta is None:
        return AxisVerdict(verdict="N/A", applicable=False, reason="No programmatic comparison evidence")
    if delta == 0:
        delta = 0.0
    if delta < 0:
        return AxisVerdict(verdict="FAIL", delta=delta, reason=f"客观侧回归({delta:+.2f})")
    return AxisVerdict(verdict="PASS", delta=delta, reason=f"客观侧无回归({delta:+.2f})")


def _eval_comparison(benchmark: dict, min_win_rate: float | None, min_valid: int, warnings: list[str], candidate: str | None = None) -> AxisVerdict:
    if min_win_rate is None:
        return AxisVerdict(verdict="N/A", applicable=False, reason="主观 gate 未请求(未给 --min-win-rate)")
    cs = benchmark.get("comparison_summary")
    if not cs:
        warnings.append("主观 gate 已请求但 comparison_summary 缺失(纯客观题 / 旧数据 / 比较失败)——落回客观侧")
        return AxisVerdict(verdict="N/A", applicable=False, reason="主观 gate 已请求但 comparison_summary 缺失")
    if not cs.get("label_a") or not cs.get("label_b"):
        return AxisVerdict(verdict="N/A", applicable=True, reason="Comparison side labels are missing; candidate win rate is unknown")
    a_wins = int(cs.get("a_wins") or 0)
    b_wins = int(cs.get("b_wins") or 0)
    ties = int(cs.get("ties") or 0)
    decisive = a_wins + b_wins  # tie-排除:tie 不进胜率分母
    common = dict(win_rate=None, decisive=decisive, a_wins=a_wins, b_wins=b_wins, ties=ties)
    if decisive == 0:
        return AxisVerdict(verdict="N/A", applicable=True, reason="无决定性比较(全 tie/failed)", **common)
    candidate_wins = b_wins if _candidate_is_second(benchmark, candidate) else a_wins
    wr = candidate_wins / decisive
    common["win_rate"] = wr
    if decisive < min_valid:
        return AxisVerdict(
            verdict="N/A", applicable=True, reason=f"样本不足({decisive} 决定性 < --min-valid {min_valid})", **common
        )
    if wr < min_win_rate:
        return AxisVerdict(
            verdict="FAIL",
            applicable=True,
            reason=f"主观胜率 {wr:.2f} < {min_win_rate:.2f}({decisive} 决定性)",
            **common,
        )
    return AxisVerdict(
        verdict="PASS", applicable=True, reason=f"主观胜率 {wr:.2f} >= {min_win_rate:.2f}({decisive} 决定性)", **common
    )


def _eval_calibration(benchmark: dict, candidate: str, warnings: list[str]) -> dict | None:
    """分诊两侧 HIGH_SCORE_SUSPECT(soft warning)。不进 axes、不改 decision。
    with/without:without 侧 None(无 compliance);v1/v2 两侧都有。candidate 侧 warning 排前。"""
    cs = benchmark.get("calibration_summary")
    if not cs or not cs.get("sides"):
        return None
    out: dict[str, dict] = {}
    def is_candidate(side):
        try:
            return _configuration(benchmark, side) == _configuration(benchmark, candidate)
        except ValueError:
            return side == candidate
    # Calibration keys may be comparison labels, while runs use configuration IDs.
    side_items = sorted(cs["sides"].items(), key=lambda kv: 0 if is_candidate(kv[0]) else 1)
    for side, per in side_items:
        if not per:
            out[side] = {"triggered": False, "count": 0, "total": 0, "evidence": []}
            continue
        suspects = per.get("high_score_suspects") or []
        triggered = len(suspects) > 0
        out[side] = {"triggered": triggered, "count": len(suspects), "total": per.get("total", 0), "evidence": suspects}
        if not (triggered):
            continue
        tag = f"[candidate {side}]" if is_candidate(side) else f"[{side}]"
        warnings.append(
            f"⚠️ {tag} 高分低遵循 {len(suspects)} 个 HIGH_SCORE_SUSPECT"
            f"(结果对但过程硬违规,evidence 见 calibration.sides.{side}.evidence)"
        )
    return {"sides": out}


def _run_failed(run: dict) -> bool:
    """单个 run 是否未全过(total>0 且 passed==total 才算全过;total==0 空案视为未验证)。"""
    res = run.get("result") or {}
    total = res.get("total", 0)
    passed = res.get("passed", 0)
    return not (total > 0 and passed == total)


def _eval_must_pass(benchmark: dict, candidate: str, ids: list[int], warnings: list[str]) -> AxisVerdict:
    """必过题:candidate 侧这些 eval 的所有 run 都得全过,任一挂 → FAIL。"""
    if not ids:
        return AxisVerdict(verdict="N/A", applicable=False, reason="必过题未指定(--must-pass)")
    runs = benchmark.get("runs") or []
    by_eval: dict[int, list[dict]] = {}
    for r in runs:
        if not (r.get("configuration") == _configuration(benchmark, candidate)):
            continue
        by_eval.setdefault(r.get("eval_id"), []).append(r)
    failed: list[int] = []
    for eid in ids:
        ev_runs = by_eval.get(eid)
        if not ev_runs:
            warnings.append(f"必过题 eval {eid} 在 candidate 侧({candidate})无 run —— 视为未通过")
            failed.append(eid)
            continue
        if not (any(_run_failed(r) for r in ev_runs)):
            continue
        failed.append(eid)
    if failed:
        return AxisVerdict(
            verdict="FAIL", applicable=True, reason=f"必过题未通过 {failed}(candidate={candidate})", failed_ids=failed
        )
    return AxisVerdict(verdict="PASS", applicable=True, reason=f"必过题 {len(ids)} 道全过(candidate={candidate})")


def _eval_drift(
    benchmark: dict, baseline_ids: set[int] | None, strict: bool, warnings: list[str]
) -> AxisVerdict | None:
    """题集漂移:benchmark.runs 的 eval_id 集合 vs baseline 基线集合,增删 → 报告。
    baseline_ids=None → 不启用(返回 None,不加进 axes)。
    非 strict:有漂移只 warning(返回 None);strict 且有漂移 → FAIL 轴。"""
    if baseline_ids is None:
        return None
    run_ids = {r.get("eval_id") for r in (benchmark.get("runs") or []) if r.get("eval_id") is not None}
    added = sorted(run_ids - baseline_ids)
    removed = sorted(baseline_ids - run_ids)
    if not added and not removed:
        return AxisVerdict(verdict="PASS", applicable=True, reason="题集与基线一致,无漂移")
    detail = []
    if added:
        detail.append(f"增 {added}")
    if removed:
        detail.append(f"删 {removed}")
    msg = f"题集漂移: {' '.join(detail)}"
    warnings.append(msg)
    if strict:
        return AxisVerdict(verdict="FAIL", applicable=True, reason=msg)
    return None


def evaluate_gate(
    benchmark: dict,
    *,
    min_win_rate: float | None = None,
    min_valid: int = 3,
    programmatic_guard: bool = True,
    candidate: str | None = None,
    must_pass_ids: list[int] | None = None,
    baseline_ids: set[int] | None = None,
    strict_drift: bool = False,
) -> GateResult:
    warnings: list[str] = []
    cand, base = _resolve_sides(benchmark, candidate)
    p = _eval_programmatic(benchmark, programmatic_guard, cand)
    c = _eval_comparison(benchmark, min_win_rate, min_valid, warnings, cand)
    axes = {"programmatic": p, "comparison": c}
    axes["must_pass"] = _eval_must_pass(benchmark, cand, must_pass_ids or [], warnings)
    drift = _eval_drift(benchmark, baseline_ids, strict_drift, warnings)
    if drift is not None:
        axes["drift"] = drift

    engaged = [v for v in axes.values() if v.applicable]
    if not engaged:
        # 无适用轴(轴都 applicable=False:守卫关 / 未请求 / 无信号)→ 不适用。与"判不准"
        # 区分:不适用=已解决(本次没该轴可判),判不准=有轴但信号不足。都 exit 2(消费方定)。
        decision, exit_code = "N/A", 2
    elif any(v.verdict == "FAIL" for v in engaged):
        decision, exit_code = "FAIL", 1
    elif any(v.verdict == "N/A" for v in engaged):
        # 有适用轴但信号不足(样本不足 / 全 tie)→ 判不准(UNCLEAR),独立于"不适用"的 N/A。
        decision, exit_code = "UNCLEAR", 2
    else:
        decision, exit_code = "PASS", 0
    reason = "; ".join(v.reason for v in engaged) if engaged else "无适用轴可判"

    calibration = _eval_calibration(benchmark, cand, warnings)
    return GateResult(
        decision=decision,
        exit_code=exit_code,
        reason=reason,
        candidate=cand,
        baseline=base,
        thresholds={
            "min_win_rate": min_win_rate,
            "min_valid": min_valid,
            "programmatic_guard": programmatic_guard,
            "must_pass_ids": must_pass_ids,
            "baseline_ids": sorted(baseline_ids) if baseline_ids else None,
            "strict_drift": strict_drift,
        },
        axes=axes,
        warnings=warnings,
        calibration=calibration,
    )
