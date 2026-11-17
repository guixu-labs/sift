"""gate 命令 CLI 测试(块 C)。"""

import json

from typer.testing import CliRunner

from sift.cli.main import app

cli = CliRunner()


def _write_bench(path, delta_prog="+0.27", cs=None, runs=None):
    bench = {
        "metadata": {"skill_name": "sk"},
        "runs": runs or [],
        "run_summary": {
            "v1": {"pass_rate": {"mean": 0.8}},
            "v2": {"pass_rate": {"mean": 0.6}},
            "delta": {"pass_rate_programmatic": delta_prog},
        },
    }
    if cs is not None:
        bench["comparison_summary"] = cs
    path.write_text(json.dumps(bench), encoding="utf-8")
    return path


def _comparison_summary():
    return {
        "a_wins": 2,
        "b_wins": 6,
        "ties": 0,
        "failed": 0,
        "valid": 8,
        "win_rate": None,
        "label_a": "v1",
        "label_b": "v2",
    }


def test_gate_pass_exit0(tmp_path):
    bp = _write_bench(tmp_path / "benchmark.json", "+0.27")
    r = cli.invoke(app, ["gate", str(bp), "-o", str(tmp_path / "gate.json")])
    assert r.exit_code == 0, r.output
    g = json.loads((tmp_path / "gate.json").read_text())
    assert g["decision"] == "PASS" and g["exit_code"] == 0
    assert g["candidate"] == "v1" and g["baseline"] == "v2"


def test_gate_fail_regression_exit1(tmp_path):
    bp = _write_bench(tmp_path / "benchmark.json", "-0.10")
    r = cli.invoke(app, ["gate", str(bp), "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 1


def test_gate_na_exit2(tmp_path):
    """纯主观题(programmatic null)+ 无主观请求 → N/A。"""
    bp = _write_bench(tmp_path / "benchmark.json", None)
    r = cli.invoke(app, ["gate", str(bp), "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 2
    g = json.loads((tmp_path / "g.json").read_text())
    assert g["decision"] == "N/A"


def test_gate_comparison_threshold(tmp_path):
    bp = _write_bench(tmp_path / "benchmark.json", "+0.27", _comparison_summary())
    r = cli.invoke(app, ["gate", str(bp), "--min-win-rate", "0.5", "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 1  # 主观 0.25 < 0.5 → FAIL
    g = json.loads((tmp_path / "g.json").read_text())
    assert g["axes"]["comparison"]["win_rate"] == 0.25


def test_gate_candidate_b_comparison_threshold(tmp_path):
    bp = _write_bench(
        tmp_path / "benchmark.json", "+0.27", _comparison_summary()
    )
    r = cli.invoke(
        app,
        [
            "gate",
            str(bp),
            "--candidate",
            "v2",
            "--no-programmatic-guard",
            "--min-win-rate",
            "0.5",
            "-o",
            str(tmp_path / "g.json"),
        ],
    )
    assert r.exit_code == 0, r.output
    g = json.loads((tmp_path / "g.json").read_text())
    assert g["candidate"] == "v2" and g["baseline"] == "v1"
    comparison = g["axes"]["comparison"]
    assert comparison["win_rate"] == 0.75
    assert comparison["candidate_wins"] == 6
    assert comparison["a_wins"] == 2 and comparison["b_wins"] == 6


def test_gate_invalid_candidate_exit2_without_gate_json(tmp_path):
    cs = {"label_a": "v1", "label_b": "v2", "a_wins": 1, "b_wins": 1, "ties": 0}
    bp = _write_bench(tmp_path / "benchmark.json", "+0.27", cs)
    r = cli.invoke(app, ["gate", str(bp), "--candidate", "custom", "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 2
    assert "must match a benchmark side" in r.output
    assert not (tmp_path / "g.json").exists()


def test_gate_missing_file_exit2_no_gatejson(tmp_path):
    """文件不存在 → exit 2 且不写 gate.json(与 N/A 的 exit 2 由 gate.json 是否存在区分)。"""
    r = cli.invoke(app, ["gate", str(tmp_path / "nope.json"), "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 2
    assert not (tmp_path / "g.json").exists()


def test_gate_corrupt_json_exit2(tmp_path):
    """benchmark.json 解析失败 → exit 2 且不写 gate.json(使用错误,非判决)。"""
    bp = tmp_path / "benchmark.json"
    bp.write_text("{bad json", encoding="utf-8")
    r = cli.invoke(app, ["gate", str(bp), "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 2
    assert not (tmp_path / "g.json").exists()


def test_gate_default_out_path(tmp_path):
    """不给 -o → gate.json 落 benchmark.json 同目录。"""
    bp = _write_bench(tmp_path / "benchmark.json", "+0.27")
    cli.invoke(app, ["gate", str(bp)])
    assert (tmp_path / "gate.json").exists()


def _run_entry(eval_id, config, passed, total):
    return {
        "eval_id": eval_id,
        "configuration": config,
        "run_number": 1,
        "result": {
            "pass_rate": passed / total if total else 0.0,
            "passed": passed,
            "failed": total - passed,
            "total": total,
        },
    }


def test_gate_must_pass_failed_exit1(tmp_path):
    bp = _write_bench(tmp_path / "benchmark.json", "+0.27", runs=[_run_entry(1, "v1", 1, 2)])  # eval 1 挂
    r = cli.invoke(app, ["gate", str(bp), "--must-pass", "1", "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 1


def test_gate_must_pass_parse_error_exit2(tmp_path):
    bp = _write_bench(tmp_path / "benchmark.json", "+0.27")
    r = cli.invoke(app, ["gate", str(bp), "--must-pass", "1,bad", "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 2
    assert not (tmp_path / "g.json").exists()


def test_gate_baseline_strict_drift_exit1(tmp_path):
    bp = _write_bench(tmp_path / "benchmark.json", "+0.27", runs=[_run_entry(1, "v1", 2, 2)])  # runs 只有 eval 1
    basep = tmp_path / "baseline.json"
    basep.write_text(json.dumps({"skill_name": "sk", "evals": [{"id": 1}, {"id": 3}]}))
    r = cli.invoke(app, ["gate", str(bp), "--baseline", str(basep), "--strict-drift", "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 1  # 删 eval 3 → strict FAIL


def test_gate_baseline_warning_keeps_pass(tmp_path):
    bp = _write_bench(tmp_path / "benchmark.json", "+0.27", runs=[_run_entry(1, "v1", 2, 2)])
    basep = tmp_path / "baseline.json"
    basep.write_text(json.dumps({"skill_name": "sk", "evals": [{"id": 1}, {"id": 3}]}))
    r = cli.invoke(app, ["gate", str(bp), "--baseline", str(basep), "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 0  # 默认 warning,不 FAIL
    g = json.loads((tmp_path / "g.json").read_text())
    assert any("漂移" in w for w in g["warnings"])


def test_gate_baseline_missing_exit2(tmp_path):
    bp = _write_bench(tmp_path / "benchmark.json", "+0.27")
    r = cli.invoke(app, ["gate", str(bp), "--baseline", str(tmp_path / "nope.json"), "-o", str(tmp_path / "g.json")])
    assert r.exit_code == 2
    assert not (tmp_path / "g.json").exists()
