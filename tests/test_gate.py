"""gate 判决逻辑纯函数测试(块 C)。"""

import pytest

from sift.gating.verdict import evaluate_gate, parse_delta


def _bench(delta_prog, comparison_summary=None):
    """构造最小 benchmark dict;delta_prog=None → 该项缺省。"""
    delta = {"pass_rate_programmatic": delta_prog}
    b = {"run_summary": {"v1": {"pass_rate": {"mean": 0.8}}, "v2": {"pass_rate": {"mean": 0.6}}, "delta": delta}}
    if comparison_summary is not None:
        b["comparison_summary"] = comparison_summary
    return b


def test_parse_delta():
    assert parse_delta("+0.27") == 0.27
    assert parse_delta("-0.10") == -0.10
    assert parse_delta(None) is None
    assert parse_delta("not-a-number") is None


def test_gate_result_calibration_defaults_none():
    """GateResult.calibration 默认 None(soft warning 容器,无 calibration_summary 时)。"""
    from sift.gating.models import GateResult

    gr = GateResult(decision="PASS", exit_code=0, reason="ok")
    assert gr.calibration is None


def test_programmatic_pass_no_regression():
    r = evaluate_gate(_bench("+0.27"))
    assert r.decision == "PASS"
    assert r.exit_code == 0
    assert r.axes["programmatic"].verdict == "PASS"
    assert r.axes["programmatic"].delta == 0.27


def test_programmatic_fail_regression():
    r = evaluate_gate(_bench("-0.10"))
    assert r.decision == "FAIL"
    assert r.exit_code == 1
    assert r.axes["programmatic"].verdict == "FAIL"


def test_programmatic_null_no_comparison_is_na():
    """纯主观题(programmatic null)+ 未请求 comparison → 无适用轴 → N/A。"""
    r = evaluate_gate(_bench(None))
    assert r.decision == "N/A"
    assert r.exit_code == 2


def test_candidate_and_baseline_resolved_from_side_keys():
    """_resolve_sides:无 --candidate → 从 run_summary side key 取候选/基线。"""
    r = evaluate_gate(_bench("+0.27"))  # _bench 建 v1/v2 side keys,无 comparison_summary
    assert r.candidate == "v1"
    assert r.baseline == "v2"


def test_programmatic_guard_disabled_makes_axis_not_applicable():
    """--no-programmatic-guard → 客观轴不介入(applicable=False)。"""
    r = evaluate_gate(_bench("+0.27"), programmatic_guard=False)
    assert r.axes["programmatic"].applicable is False
    assert r.decision == "N/A"  # 客观关 + comparison 桩未介入 → 无适用轴
    assert r.exit_code == 2


def _cs(a_wins, b_wins, ties=0, label_a="v1", label_b="v2"):
    return {
        "a_wins": a_wins,
        "b_wins": b_wins,
        "ties": ties,
        "failed": 0,
        "valid": a_wins + b_wins + ties,
        "win_rate": None,
        "label_a": label_a,
        "label_b": label_b,
    }


def test_comparison_pass():
    r = evaluate_gate(_bench("+0.27", _cs(6, 2)), min_win_rate=0.5, min_valid=3)
    assert r.decision == "PASS"
    cv = r.axes["comparison"]
    assert cv.verdict == "PASS" and cv.win_rate == 0.75 and cv.decisive == 8


def test_comparison_boundary_at_threshold_is_pass():
    """wr == min_win_rate → PASS(>= 语义:达标即过,边界不算失败)。"""
    r = evaluate_gate(_bench("+0.27", _cs(3, 3)), min_win_rate=0.5, min_valid=3)
    assert r.axes["comparison"].verdict == "PASS"
    assert r.axes["comparison"].win_rate == 0.5


def test_comparison_fail_below_threshold():
    r = evaluate_gate(_bench("+0.27", _cs(2, 6)), min_win_rate=0.5, min_valid=3)
    assert r.decision == "FAIL"  # 主观不过 → 整体 FAIL(客观虽 PASS)
    assert r.axes["comparison"].verdict == "FAIL"


def test_candidate_b_uses_its_subjective_win_rate():
    r = evaluate_gate(
        _bench("+0.27", _cs(2, 6)),
        min_win_rate=0.5,
        candidate="v2",
        programmatic_guard=False,
    )
    assert r.decision == "PASS"
    assert r.candidate == "v2" and r.baseline == "v1"
    assert r.axes["comparison"].win_rate == 0.75


@pytest.mark.parametrize(("delta", "verdict", "oriented"), [("+0.10", "FAIL", -0.1), ("-0.10", "PASS", 0.1)])
def test_candidate_b_orients_programmatic_delta(delta, verdict, oriented):
    r = evaluate_gate(_bench(delta), candidate="v2")
    assert r.axes["programmatic"].verdict == verdict
    assert r.axes["programmatic"].delta == oriented
    assert r.axes["programmatic"].raw_delta == float(delta)


def test_candidate_comparison_exposes_oriented_win_count():
    r = evaluate_gate(_bench("+0.27", _cs(2, 8)), min_win_rate=0.5, candidate="v2")
    assert r.axes["comparison"].candidate_wins == 8
    assert r.axes["comparison"].a_wins == 2
    assert r.axes["comparison"].b_wins == 8


def test_comparison_unclear_insufficient_sample():
    r = evaluate_gate(_bench("+0.27", _cs(2, 0)), min_win_rate=0.5, min_valid=3)
    assert r.axes["comparison"].verdict == "N/A"  # decisive 2 < min_valid 3(轴级仍 N/A,applicable=True 信号不足)
    assert r.decision == "UNCLEAR"  # 客观 PASS 但主观样本不足 → 判不准(UNCLEAR,非不适用 N/A)


def test_comparison_na_all_ties():
    r = evaluate_gate(_bench("+0.27", _cs(0, 0, ties=5)), min_win_rate=0.5, min_valid=3)
    assert r.axes["comparison"].verdict == "N/A"  # decisive 0


def test_comparison_not_requested_is_not_applicable():
    """未给 --min-win-rate → comparison 不介入,只走客观。"""
    r = evaluate_gate(_bench("+0.27", _cs(6, 2)))
    assert r.axes["comparison"].applicable is False
    assert r.decision == "PASS"  # 仅客观


def test_comparison_requested_but_summary_missing_warns():
    """请求了主观 gate 但 comparison_summary 缺失 → 不介入 + 警告;落回客观。"""
    r = evaluate_gate(_bench("+0.27"), min_win_rate=0.5)  # 无 comparison_summary
    assert r.axes["comparison"].applicable is False
    assert len(r.warnings) == 1
    assert r.decision == "PASS"  # 仅客观判


def test_programmatic_fail_overrides_comparison_pass():
    """客观回归压倒主观胜率:delta<0 直接 FAIL。"""
    r = evaluate_gate(_bench("-0.10", _cs(6, 2)), min_win_rate=0.5)
    assert r.decision == "FAIL" and r.exit_code == 1


def test_no_programmatic_guard_only_comparison():
    r = evaluate_gate(_bench("-0.10", _cs(6, 2)), min_win_rate=0.5, programmatic_guard=False)
    assert r.axes["programmatic"].applicable is False
    assert r.decision == "PASS"  # 只看主观,客观回归被关


def test_no_engaged_axis_is_na():
    """客观 null + 守卫关 + 无主观请求 → 无适用轴 → N/A。"""
    r = evaluate_gate(_bench(None), programmatic_guard=False)
    assert r.decision == "N/A" and r.exit_code == 2


def test_candidate_override():
    r = evaluate_gate(_bench("+0.27", _cs(6, 2, label_a="v1", label_b="v2")), min_win_rate=0.5, candidate="v2")
    assert r.candidate == "v2"
    assert r.baseline == "v1"


def test_candidate_must_match_benchmark_side():
    with pytest.raises(ValueError, match="must match a benchmark side"):
        evaluate_gate(_bench("+0.27", _cs(6, 2)), candidate="custom-cand")


def test_candidate_resolution_from_side_keys():
    """无 comparison_summary → 候选/基线从 run_summary side key 取。"""
    r = evaluate_gate(_bench("+0.27"))  # 无 cs
    assert r.candidate == "v1" and r.baseline == "v2"


def test_thresholds_recorded():
    r = evaluate_gate(_bench("+0.27", _cs(6, 2)), min_win_rate=0.6, min_valid=5)
    assert r.thresholds == {
        "min_win_rate": 0.6,
        "min_valid": 5,
        "programmatic_guard": True,
        "must_pass_ids": None,
        "baseline_ids": None,
        "strict_drift": False,
    }


# ---- _eval_calibration:HIGH_SCORE_SUSPECT soft warning ----


def _bench_with_cal(suspects_with):
    """造 benchmark dict:客观轴 PASS(delta=0)+ calibration_summary(with 侧 suspects)。"""
    return {
        "run_summary": {"delta": {"pass_rate_programmatic": 0.0}, "with_skill": {}, "without_skill": {}},
        "comparison_summary": {"label_a": "with_skill", "label_b": "without_skill"},
        "calibration_summary": {
            "sides": {
                "with_skill": {
                    "total": 2,
                    "verdict_counts": {"HIGH_SCORE_SUSPECT": len(suspects_with)},
                    "high_score_suspects": suspects_with,
                },
                "without_skill": None,
            }
        },
    }


def test_calibration_warning_triggered_for_high_score_suspect():
    suspects = [
        {
            "eval": 1,
            "run": 2,
            "category": "prohibition",
            "instruction_text": "引用日志原文行",
            "evidence": "未引用",
            "compliance_rate": 0.875,
        }
    ]
    res = evaluate_gate(_bench_with_cal(suspects), programmatic_guard=True)
    assert res.decision == "PASS"  # 不改 decision
    assert res.exit_code == 0
    assert res.calibration is not None
    assert res.calibration["sides"]["with_skill"]["triggered"] is True
    assert res.calibration["sides"]["with_skill"]["count"] == 1
    assert any("HIGH_SCORE_SUSPECT" in w or "高分低遵循" in w for w in res.warnings)
    assert any("[candidate" in w for w in res.warnings)  # candidate 侧(with_skill==cand)标记排前


def test_calibration_warning_not_triggered_when_clean():
    res = evaluate_gate(_bench_with_cal([]), programmatic_guard=True)
    assert res.calibration["sides"]["with_skill"]["triggered"] is False
    assert not any("高分低遵循" in w for w in res.warnings)


def test_calibration_missing_summary_is_none():
    bench = {"run_summary": {"delta": {"pass_rate_programmatic": 0.0}, "with_skill": {}}}
    res = evaluate_gate(bench, programmatic_guard=True)
    assert res.calibration is None  # 旧 benchmark 无 calibration_summary → 降级 None


def test_calibration_without_skill_side_is_none():
    """with/without 模式:without 侧 None(无 compliance)。"""
    res = evaluate_gate(_bench_with_cal([]), programmatic_guard=True)
    assert res.calibration["sides"]["without_skill"]["triggered"] is False
    assert res.calibration["sides"]["without_skill"]["count"] == 0


# ---- _eval_must_pass(必过题)+ _eval_drift(题集漂移)----


def _run(eval_id, config, passed, total, run_number=1):
    """造 RunResultEntry dict(某 eval 某 config 某 run 的结果)。"""
    return {
        "eval_id": eval_id,
        "configuration": config,
        "run_number": run_number,
        "result": {
            "pass_rate": passed / total if total else 0.0,
            "passed": passed,
            "failed": total - passed,
            "total": total,
        },
    }


def _bench_runs(runs, delta_prog="+0.27"):
    b = _bench(delta_prog)
    b["runs"] = runs
    return b


def test_must_pass_empty_ids_not_applicable():
    r = evaluate_gate(_bench_runs([_run(1, "v1", 2, 2)]))
    assert r.axes["must_pass"].applicable is False
    assert r.decision == "PASS"  # 客观 PASS,must_pass 未指定不介入


def test_must_pass_all_pass():
    runs = [_run(1, "v1", 2, 2), _run(3, "v1", 3, 3)]
    r = evaluate_gate(_bench_runs(runs), must_pass_ids=[1, 3])
    assert r.axes["must_pass"].verdict == "PASS"
    assert r.decision == "PASS" and r.exit_code == 0


def test_must_pass_one_failed():
    runs = [_run(1, "v1", 2, 2), _run(3, "v1", 1, 3)]  # eval 3: 1/3 有 failed
    r = evaluate_gate(_bench_runs(runs), must_pass_ids=[1, 3])
    assert r.axes["must_pass"].verdict == "FAIL"
    assert r.axes["must_pass"].failed_ids == [3]
    assert r.decision == "FAIL" and r.exit_code == 1


def test_must_pass_id_not_in_runs_warns_and_fails():
    r = evaluate_gate(_bench_runs([_run(1, "v1", 2, 2)]), must_pass_ids=[1, 99])
    assert r.axes["must_pass"].verdict == "FAIL"
    assert 99 in r.axes["must_pass"].failed_ids
    assert any("99" in w for w in r.warnings)


def test_must_pass_multi_run_one_fails():
    # eval 1 两个 run:run1 全过、run2 挂 → 整体没必过(strict:所有 run 都得过)
    runs = [_run(1, "v1", 2, 2, run_number=1), _run(1, "v1", 1, 2, run_number=2)]
    r = evaluate_gate(_bench_runs(runs), must_pass_ids=[1])
    assert r.axes["must_pass"].verdict == "FAIL"
    assert r.axes["must_pass"].failed_ids == [1]


def test_must_pass_only_checks_candidate_side():
    # eval 1 在 baseline 侧(v2)挂、candidate 侧(v1)全过 → 仍 PASS(只看 candidate)
    runs = [_run(1, "v1", 2, 2), _run(1, "v2", 0, 2)]
    r = evaluate_gate(_bench_runs(runs), must_pass_ids=[1])
    assert r.axes["must_pass"].verdict == "PASS"


def test_drift_no_baseline_not_engaged():
    r = evaluate_gate(_bench_runs([_run(1, "v1", 2, 2)]))
    assert "drift" not in r.axes


def test_drift_clean_passes():
    runs = [_run(1, "v1", 2, 2), _run(3, "v1", 2, 2)]
    r = evaluate_gate(_bench_runs(runs), baseline_ids={1, 3})
    assert r.axes["drift"].verdict == "PASS"
    assert r.decision == "PASS"


def test_drift_default_warning_not_in_decision():
    runs = [_run(1, "v1", 2, 2)]  # baseline {1,3},runs 只 1 → 删 3
    r = evaluate_gate(_bench_runs(runs), baseline_ids={1, 3})
    assert "drift" not in r.axes  # 非 strict → 不进 axes
    assert any("漂移" in w for w in r.warnings)
    assert r.decision == "PASS"  # 不影响 decision


def test_drift_strict_fails():
    runs = [_run(1, "v1", 2, 2)]  # 删 3
    r = evaluate_gate(_bench_runs(runs), baseline_ids={1, 3}, strict_drift=True)
    assert r.axes["drift"].verdict == "FAIL"
    assert r.decision == "FAIL" and r.exit_code == 1


def test_drift_added_and_removed_reported():
    runs = [_run(1, "v1", 2, 2), _run(5, "v1", 2, 2)]  # baseline {1,3}:删 3 增 5
    r = evaluate_gate(_bench_runs(runs), baseline_ids={1, 3})
    msg = [w for w in r.warnings if "漂移" in w][0]
    assert "增" in msg and "5" in msg
    assert "删" in msg and "3" in msg
