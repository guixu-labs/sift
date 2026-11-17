"""gate — post-hoc 对已有 benchmark.json 出通过/失败判决。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer

from sift.cli.main import app, console
from sift.cli.options import options_command
from sift.gating.models import GateResult
from sift.gating.verdict import evaluate_gate


@dataclass
class GateCommandOptions:
    """Command option declarations; parsed with Typer before execution."""

    benchmark_path: Path = typer.Argument(..., help="Path to benchmark.json")
    min_win_rate: Optional[float] = typer.Option(
        None, "--min-win-rate", help="主观 gate 阈值(候选 tie-排除胜率);不给则只走客观回归守卫"
    )
    min_valid: int = typer.Option(3, "--min-valid", help="主观 gate 最少决定性比较数")
    no_programmatic_guard: bool = typer.Option(False, "--no-programmatic-guard", help="关掉客观回归守卫")
    candidate: Optional[str] = typer.Option(
        None, "--candidate", help="候选侧名(默认 comparison_summary.label_a / 首个 side key)"
    )
    must_pass: Optional[str] = typer.Option(
        None, "--must-pass", help="必过题 eval id(逗号分隔,如 3,5,7);candidate 侧这些题任一挂 → FAIL"
    )
    baseline: Optional[Path] = typer.Option(None, "--baseline", help="冻结基线 evals.json(freeze 产);启用题集漂移守卫")
    strict_drift: bool = typer.Option(False, "--strict-drift", help="题集漂移升 FAIL(默认只 warning)")
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="gate.json 输出路径(默认 benchmark.json 同目录)")


@options_command(app, GateCommandOptions, rich_help_panel="判决")
def gate(cli_options: GateCommandOptions):
    """对已有 benchmark.json 出通过/失败判决(post-hoc,毫秒级,可反复改阈值重判)。

    四轴:客观 programmatic 回归守卫(默认常开)+ 主观 comparison(--min-win-rate opt-in)
    + 必过题(--must-pass,candidate 侧关键题任一挂 → FAIL)
    + 题集漂移(--baseline,默认 warning / --strict-drift 升 FAIL)。
    退出码:0=PASS / 1=FAIL / 2=N/A(信号不足)。判决写 gate.json。
    """
    # exit 2 双语义:文件/解析错误(此处,不写 gate.json)与 N/A 判决(下方写 gate.json)
    # 都退 2——CI 靠 gate.json 是否存在区分「使用错误」vs「信号不足」。
    if not cli_options.benchmark_path.exists():
        console.print(f"[red]Error:[/red] not found: {cli_options.benchmark_path}")
        raise typer.Exit(code=2)
    try:
        benchmark = json.loads(cli_options.benchmark_path.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[red]Error:[/red] failed to parse benchmark.json: {exc}")
        raise typer.Exit(code=2) from exc

    must_pass_ids: list[int] = []
    if cli_options.must_pass:
        try:
            must_pass_ids = [int(x.strip()) for x in cli_options.must_pass.split(",") if x.strip()]
        except ValueError as _caught_error:
            console.print(f"[red]Error:[/red] --must-pass 解析失败(要逗号分隔整数,如 3,5,7): {cli_options.must_pass!r}")
            raise typer.Exit(code=2) from _caught_error

    baseline_ids: set[int] | None = None
    if cli_options.baseline:
        if not cli_options.baseline.exists():
            console.print(f"[red]Error:[/red] baseline not found: {cli_options.baseline}")
            raise typer.Exit(code=2)
        try:
            bdata = json.loads(cli_options.baseline.read_text(encoding="utf-8"))
        except Exception as exc:
            console.print(f"[red]Error:[/red] failed to parse baseline: {exc}")
            raise typer.Exit(code=2) from exc
        baseline_ids = {e["id"] for e in bdata.get("evals", []) if isinstance(e, dict) and "id" in e}

    try:
        result = evaluate_gate(
            benchmark,
            min_win_rate=cli_options.min_win_rate,
            min_valid=cli_options.min_valid,
            programmatic_guard=not cli_options.no_programmatic_guard,
            candidate=cli_options.candidate,
            must_pass_ids=must_pass_ids or None,
            baseline_ids=baseline_ids,
            strict_drift=cli_options.strict_drift,
        )
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    _write_gate_result(cli_options, result)


def _write_gate_result(cli_options: GateCommandOptions, result: GateResult) -> None:
    result.artifact = str(cli_options.benchmark_path)
    gate_json_path = cli_options.out or (cli_options.benchmark_path.parent / "gate.json")
    gate_json_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    color = {"PASS": "green", "FAIL": "red", "N/A": "yellow", "UNCLEAR": "yellow"}[result.decision]
    console.print(f"[{color}][{result.decision}][/{color}] {result.reason}")
    for w in result.warnings:
        console.print(f"[yellow]⚠ {w}[/yellow]")
    console.print(f"gate verdict → {gate_json_path}")
    raise typer.Exit(code=result.exit_code)
