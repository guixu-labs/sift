"""gate 判决纯函数(块 C)。读 benchmark dict → GateResult,无 IO。"""

from __future__ import annotations

from .models import AxisVerdict, GateResult


def parse_delta(value) -> float | None:
    """ "+0.27" → 0.27;None/无法解析 → None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_sides(benchmark: dict, candidate: str | None) -> tuple[str, str, bool]:
    """解析候选/基线及候选方向;显式候选必须匹配 benchmark 中的一个 side。"""
    run_summary = benchmark.get("run_summary", {}) or {}
    side_keys = [k for k in run_summary if k != "delta"]
    cs = benchmark.get("comparison_summary") or {}
    label_a = cs.get("label_a") or (side_keys[0] if side_keys else "")
    label_b = cs.get("label_b") or (side_keys[1] if len(side_keys) > 1 else "")
    if label_a and label_b and label_a == label_b:
        raise ValueError(f"benchmark comparison sides have duplicate label {label_a!r}")
    if candidate is None:
        return label_a, label_b, False
    if label_a and candidate == label_a:
        return candidate, label_b, False
    if label_b and candidate == label_b:
        return candidate, label_a, True
    available = ", ".join(repr(label) for label in (label_a, label_b) if label)
    raise ValueError(f"--candidate {candidate!r} must match a benchmark side ({available or 'none found'})")


def _eval_programmatic(benchmark: dict, guard_on: bool, candidate_is_b: bool) -> AxisVerdict:
    if not guard_on:
        return AxisVerdict(verdict="N/A", applicable=False, reason="客观守卫已关(--no-programmatic-guard)")
    raw = (benchmark.get("run_summary", {}) or {}).get("delta", {}).get("pass_rate_programmatic")
    raw_delta = parse_delta(raw)
    if raw_delta is None:
        return AxisVerdict(
            verdict="N/A",
            applicable=False,
            reason="delta.pass_rate_programmatic 无值(纯主观题 / 无 programmatic expectation)",
        )
    delta = -raw_delta if candidate_is_b else raw_delta
    if delta < 0:
        return AxisVerdict(
            verdict="FAIL", delta=delta, raw_delta=raw_delta,
            reason=f"candidate 侧客观回归({delta:+.2f})",
        )
    return AxisVerdict(
        verdict="PASS", delta=delta, raw_delta=raw_delta,
        reason=f"candidate 侧客观无回归({delta:+.2f})",
    )


def _eval_comparison(
    benchmark: dict,
    min_win_rate: float | None,
    min_valid: int,
    candidate_is_b: bool,
    warnings: list[str],
) -> AxisVerdict:
    if min_win_rate is None:
        return AxisVerdict(verdict="N/A", applicable=False, reason="主观 gate 未请求(未给 --min-win-rate)")
    cs = benchmark.get("comparison_summary")
    if not cs:
        warnings.append("主观 gate 已请求但 comparison_summary 缺失(纯客观题 / 旧数据 / 比较失败)——落回客观侧")
        return AxisVerdict(verdict="N/A", applicable=False, reason="主观 gate 已请求但 comparison_summary 缺失")
    a_wins = int(cs.get("a_wins") or 0)
    b_wins = int(cs.get("b_wins") or 0)
    ties = int(cs.get("ties") or 0)
    decisive = a_wins + b_wins  # tie-排除:tie 不进胜率分母
    candidate_wins = b_wins if candidate_is_b else a_wins
    common = {
        "win_rate": None,
        "decisive": decisive,
        "a_wins": a_wins,
        "b_wins": b_wins,
        "candidate_wins": candidate_wins,
        "ties": ties,
    }
    if decisive == 0:
        return AxisVerdict(verdict="N/A", applicable=True, reason="无决定性比较(全 tie/failed)", **common)
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
    # candidate 侧排前(顺序)
    side_items = sorted(cs["sides"].items(), key=lambda kv: 0 if kv[0] == candidate else 1)
    for side, per in side_items:
        if not per:
            out[side] = {"triggered": False, "count": 0, "total": 0, "evidence": []}
            continue
        suspects = per.get("high_score_suspects") or []
        triggered = len(suspects) > 0
        out[side] = {"triggered": triggered, "count": len(suspects), "total": per.get("total", 0), "evidence": suspects}
        if not (triggered):
            continue
        tag = f"[candidate {side}]" if side == candidate else f"[{side}]"
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
        if not (r.get("configuration") == candidate):
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
    cand, base, candidate_is_b = _resolve_sides(benchmark, candidate)
    p = _eval_programmatic(benchmark, programmatic_guard, candidate_is_b)
    c = _eval_comparison(benchmark, min_win_rate, min_valid, candidate_is_b, warnings)
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
