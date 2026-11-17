import json
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional

import typer

from sift.cli.main import app, console
from sift.cli.options import options_command
from sift.errors import SiftError
from sift.evals.coverage import analyze_coverage
from sift.models import EvalSet
from sift.parser import parse_skill_md, resolve_skill_dir


class _AugmentOptions(NamedTuple):
    """覆盖补题需要的模型和声明类型配置。"""

    model: str | None
    timeout: int
    kind: str | None


_COVERAGE_AUGMENT_HELP = (
    "自动补漏测题:每个漏测点调 init 生成逻辑补题(带 review:coverage-augment flag,按 prompt 去重),写回 evals.json。"
)

_COVERAGE_KIND_HELP = "声明类型:agent=审 agent 定义正文的内容点(skill 默认);配 --augment 时走 generate_for_agent 出题。"


@dataclass
class CoverageCommandOptions:
    """Command option declarations; parsed with Typer before execution."""

    skill_path: Path = typer.Argument(..., help="Path to skill directory")
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output dir")
    model: Optional[str] = typer.Option(None, "--model")
    timeout: int = typer.Option(120, "--timeout")
    evals_file: Optional[Path] = typer.Option(None, "--evals", help="evals.json 路径(默认 skill/evals/evals.json)")
    augment: bool = typer.Option(
        False,
        "--augment",
        help=(_COVERAGE_AUGMENT_HELP),
    )
    kind: Optional[str] = typer.Option(
        None,
        "--kind",
        help=(_COVERAGE_KIND_HELP),
    )


@options_command(app, CoverageCommandOptions, rich_help_panel="出题")
def coverage(cli_options: CoverageCommandOptions):
    """诊断 evals.json 对声明(SKILL.md / agent.md)内容点的覆盖(漏测=0 覆盖的点;纯诊断无 gate)。"""
    skill_path = cli_options.skill_path
    skill_path = resolve_skill_dir(skill_path)
    if cli_options.kind is not None and cli_options.kind not in ("skill", "agent"):
        console.print((f"[red]Error:[/red] --kind 只接受 skill / agent,收到 {cli_options.kind!r}"))
        raise typer.Exit(code=2)
    try:
        name, *_ = parse_skill_md(skill_path)
        # agent .md 是文件 → 取 parent 找 evals/(同 init);skill 目录原样。
        base = skill_path if skill_path.is_dir() else skill_path.parent
        ef = cli_options.evals_file or (base / "evals" / "evals.json")
        if not ef.exists():
            console.print((f"[yellow]⚠️ {ef} 不存在,先跑 init 生成考题。[/yellow]"))
            raise typer.Exit(code=0)
        eval_set = EvalSet.model_validate(json.loads(ef.read_text(encoding="utf-8")))
        if not eval_set.evals:
            console.print((f"[yellow]⚠️ {ef} 无 eval,先跑 init 生成考题。[/yellow]"))
            raise typer.Exit(code=0)

        # 单次阻塞 LLM 调用(call_claude 非流式),用 spinner 给「分析中」反馈;非交互终端自动降级。
        with console.status(
            (f"正在分析 {name} 的内容点 × eval 覆盖({len(eval_set.evals)} evals)…"),
            spinner="dots",
        ):
            report = analyze_coverage(
                skill_path, eval_set, model=cli_options.model, timeout=cli_options.timeout, kind=cli_options.kind
            )

        out_dir = cli_options.output or (base / "evals")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "coverage-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")

        gaps = report.gaps
        _print_coverage_summary(report, skill_path, cli_options.kind)

        if cli_options.augment and gaps:
            _augment_coverage(
                skill_path,
                eval_set,
                gaps,
                ef,
                _AugmentOptions(cli_options.model, cli_options.timeout, cli_options.kind),
            )

        _write_coverage_html(report, out_dir)
    except SiftError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=getattr(exc, "exit_code", 1)) from exc


def _print_coverage_summary(report, skill_path: Path, kind: str | None) -> None:
    """打印内容点覆盖摘要和定向补题命令。"""
    gaps = report.gaps
    tu = report.token_usage
    tok = (tu.get("input_tokens", 0) or 0) + (tu.get("output_tokens", 0) or 0)
    console.print(
        (
            f"内容点 [bold]{len(report.points)}[/bold] · 漏测 [red]{len(gaps)}[/red]"
            f" · 耗时 {report.duration_seconds:.1f}s · {tok} tok"
        )
    )
    decl_word = "agent 定义" if kind == "agent" else "SKILL.md"
    if not gaps:
        console.print((f"[green]✓ 无漏测:{decl_word} 内容点均有 eval 覆盖。[/green]"))
    else:
        console.print("[bold]漏测点(0 道题覆盖):[/bold]")
        for g in gaps:
            console.print(f"  · [red]{g.label}[/red]")
            if not (g.source_quote):
                continue
            console.print((f"      ↳ {decl_word}:「{g.source_quote[:120]}」"), markup=False)
        console.print(("[dim]补题:逐条人工 review 后,用以下命令定向补(或加 --augment 自动补):[/dim]"))
        kind_flag = " --kind agent" if kind == "agent" else ""
        for g in gaps:
            safe_label = g.label.replace('"', "'")
            console.print(
                (f'  init {skill_path}{kind_flag} --append --focus "{safe_label}"'),
                markup=False,
            )


def _augment_coverage(skill_path: Path, eval_set: EvalSet, gaps: list, ef: Path, options: _AugmentOptions) -> None:
    """逐个补齐漏测点，去重后写回并保留人工 review 标记。"""
    model, timeout, kind = options
    from sift.evals.generator import EvalInitGenerator

    existing_prompts = {e.prompt for e in eval_set.evals}
    next_id = max((e.id for e in eval_set.evals), default=0) + 1
    added, skipped = 0, 0
    n_gaps = len(gaps)
    for idx, g in enumerate(gaps, 1):
        console.print((f"[bold]补题 {idx}/{n_gaps}[/bold] · {g.label}"))
        gen = EvalInitGenerator(model=model, timeout=timeout, focus=g.label)
        new_set = _generate_gap_evals(gen, skill_path, eval_set.evals, kind, g.label)
        if new_set is None:
            continue
        for e in new_set.evals:
            if e.prompt in existing_prompts:
                skipped += 1
                continue
            e.id = next_id
            next_id += 1
            e.flags = list(e.flags or []) + ["review:coverage-augment"]
            eval_set.evals.append(e)
            existing_prompts.add(e.prompt)
            added += 1
    if added:
        ef.write_text(eval_set.model_dump_json(indent=2), encoding="utf-8")
    console.print(
        (
            f"[green]--augment:补 {added} 题[/green](带 review:coverage-augment,需人审)"
            f"{f' · 跳过 {skipped} 题(与现有 prompt 重复)' if skipped else ''}"
        )
    )
    console.print(("[dim]补题已写回 evals.json;请人审 review:coverage-augment 题后接受。[/dim]"))


def _generate_gap_evals(gen, skill_path: Path, existing_evals: list, kind: str | None, label: str):
    """生成单个漏测点的补题；模型调用失败则提示并跳过此点。"""
    try:
        with console.status(f"正在为「{label}」生成补题…", spinner="dots"):
            # 喂现有 evals(含本批已补题)→ 共享内核定向补题 + 语义去重;
            # 下面的 prompt 字面去重作兜底。agent 走 generate_for_agent。
            if kind == "agent":
                return gen.generate_for_agent(skill_path, existing_evals=existing_evals)
            else:
                return gen.generate_with_llm(skill_path, existing_evals=existing_evals)
    except RuntimeError:
        console.print(f"[dim]补「{label}」失败(限流/异常),跳过。[/dim]")
        return None


def _write_coverage_html(report, out_dir: Path) -> None:
    """写 HTML 视图并显示 JSON 与 HTML 的实际路径。"""
    from sift.reporting.coverage_viewer import CoverageReportGenerator

    html_path = out_dir / "coverage-report.html"
    CoverageReportGenerator().write_html(report, html_path)
    console.print((f"Written to {out_dir / 'coverage-report.json'} · {html_path}"))
