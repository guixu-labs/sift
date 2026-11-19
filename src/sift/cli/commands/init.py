from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional

import typer

from sift.cli.main import app, console
from sift.cli.options import options_command
from sift.errors import SiftError
from sift.evals.inspect import same_prompt_groups
from sift.evals.run_bundle_mining import RunBundleMiningInputs, prepare_run_bundle_mining_inputs
from sift.parser import parse_skill_md, resolve_skill_dir

_INIT_KIND_HELP = (
    "声明类型:生成时 agent=按 agent 角色出题(默认/skill 走 skill 出题);配 "
    "--from-transcript 选挖掘作用域(skill=按 Skill 调用切 / agent=按 agent "
    "归属切);配 --from-run 时省略则挖命中 session 的完整记录"
)

_INIT_TIMEOUT_HELP = "LLM 调用超时秒数(mining 是单次大 prompt,默认 600;撞超时 → degraded fallback)"

_INIT_APPEND_HELP = "追加到已有 evals.json(不覆盖):新 eval 的 id 接续已有 max+1。常配 --focus 定向补薄弱面。"

_INIT_FOCUS_HELP = (
    "定向生成某一类 eval 补盲区:预设 negative-cases(反面场景)/ "
    "edge-cases(边界/错误),或任意自由文本(skill 特定方向)。常配 --append "
    "增量补到已有 evals.json。"
)

_INIT_TRIGGER_PROBES_HELP = (
    "生成 trigger-accuracy 探题(should/should-not 各 N 道)追加到 evals.json 的 trigger_probes 段。"
)


_INIT_SKILL_PATH_ARGUMENT = typer.Argument(..., help="Path to skill directory")

_INIT_OUTPUT_OPTION = typer.Option(None, "--output", "-o", help="Output directory for evals.json")

_INIT_GENERATE_PROMPTS_OPTION = typer.Option(
    True, "--generate-prompts/--no-generate-prompts", help="Use LLM to generate L1 unit-level evals"
)

_INIT_MODEL_OPTION = typer.Option(None, "--model", help="Model for LLM generation")

_INIT_NUM_EVALS_OPTION = typer.Option(None, "--num-evals", help="Max number of eval cases to generate")

_INIT_FROM_TRANSCRIPT_OPTION = typer.Option(
    None,
    "--from-transcript",
    help=("从真实 session transcript 挖 evals(加料 SKILL.md,非替代)"),
)

_INIT_FROM_RUN_OPTION = typer.Option(
    None,
    "--from-run",
    help="从导出的 run 包中选择目标 skill/agent 的执行，逐 session 挖 evals；与 --from-transcript 互斥",
)

_INIT_KIND_OPTION = typer.Option(
    None,
    "--kind",
    help=(_INIT_KIND_HELP),
)

_INIT_TIMEOUT_OPTION = typer.Option(
    600,
    "--timeout",
    help=(_INIT_TIMEOUT_HELP),
)

_INIT_APPEND_OPTION = typer.Option(
    False,
    "--append",
    help=(_INIT_APPEND_HELP),
)

_INIT_FOCUS_OPTION = typer.Option(
    None,
    "--focus",
    help=(_INIT_FOCUS_HELP),
)

_INIT_TRIGGER_PROBES_OPTION = typer.Option(
    False,
    "--trigger-probes",
    help=(_INIT_TRIGGER_PROBES_HELP),
)


class _InitGenerationOptions(NamedTuple):
    """选择出题来源和声明类型。"""

    from_transcript: Path | None
    kind: str | None
    generate_prompts: bool
    run_inputs: RunBundleMiningInputs | None


class _InitProbeOptions(NamedTuple):
    """触发探题生成、挖掘及追加的配置。"""

    output: Path
    append: bool
    skill_path: Path
    num_evals: int | None
    from_transcript: Path | None
    run_inputs: RunBundleMiningInputs | None


@dataclass
class InitCommandOptions:
    """Command option declarations; parsed with Typer before execution."""

    skill_path: Path = _INIT_SKILL_PATH_ARGUMENT
    output: Optional[Path] = _INIT_OUTPUT_OPTION
    generate_prompts: bool = _INIT_GENERATE_PROMPTS_OPTION
    model: Optional[str] = _INIT_MODEL_OPTION
    num_evals: Optional[int] = _INIT_NUM_EVALS_OPTION
    from_transcript: Optional[Path] = _INIT_FROM_TRANSCRIPT_OPTION
    from_run: Optional[Path] = _INIT_FROM_RUN_OPTION
    kind: Optional[str] = _INIT_KIND_OPTION
    timeout: int = _INIT_TIMEOUT_OPTION
    append: bool = _INIT_APPEND_OPTION
    focus: Optional[str] = _INIT_FOCUS_OPTION
    trigger_probes: bool = _INIT_TRIGGER_PROBES_OPTION


@options_command(app, InitCommandOptions, rich_help_panel="出题")
def init(cli_options: InitCommandOptions):
    """生成 evals.json 考卷(LLM 出题 / transcript 挖题 / 纯模板兜底)。

    Generates L1 unit test cases from SKILL.md content.
    """
    skill_path = cli_options.skill_path
    output = cli_options.output
    skill_path = resolve_skill_dir(skill_path)
    from sift.evals.generator import EvalInitGenerator

    temporary_run: tempfile.TemporaryDirectory | None = None
    try:
        if cli_options.from_run is not None and cli_options.from_transcript is not None:
            console.print("[red]Error:[/red] --from-run 与 --from-transcript 互斥")
            raise typer.Exit(code=2)
        if cli_options.from_run is not None and cli_options.trigger_probes and cli_options.kind != "skill":
            console.print("[red]Error:[/red] run 的 trigger 探题需要 --kind skill")
            raise typer.Exit(code=2)
        gen = EvalInitGenerator(model=cli_options.model, timeout=cli_options.timeout, focus=cli_options.focus)

        # 提前算 output 路径:append 模式要在生成前读已有 evals.json(喂定向补题 + 去重)
        output = _resolve_evals_output(skill_path, output)

        # append 模式:提前读现有 evals。生成阶段喂 _existing_evals(定向补题 + 语义去重,
        # 治半聚焦/不去重);合并阶段复用 _existing_raw 保原题字节。读失败 → 降级覆盖写。
        _existing_raw, _existing_evals = _load_existing_evals(cli_options.append, output)

        # --kind 通验(声明类型:生成时选 skill/agent 出题路径;配 --from-transcript 选挖掘作用域)
        if cli_options.kind is not None and cli_options.kind not in ("skill", "agent"):
            console.print((f"[red]Error:[/red] --kind 只接受 skill / agent,收到 {cli_options.kind!r}"))
            raise typer.Exit(code=2)

        run_inputs = None
        if cli_options.from_run is not None:
            temporary_run = tempfile.TemporaryDirectory(prefix="sift-init-run-")
            name = parse_skill_md(skill_path)[0]
            run_inputs = prepare_run_bundle_mining_inputs(
                cli_options.from_run,
                name,
                cli_options.kind,
                Path(temporary_run.name),
                include_unmatched=cli_options.trigger_probes,
            )
            calls = sum(len(session.call_ids) for session in run_inputs.sessions)
            console.print(f"Run {run_inputs.run_id}: 命中 {len(run_inputs.sessions)} 个 session、{calls} 次调用")
            for warning in run_inputs.warnings:
                console.print(f"[yellow]⚠ {warning}[/yellow]")

        eval_set = _generate_init_evals(
            gen,
            skill_path,
            _existing_evals,
            _InitGenerationOptions(
                cli_options.from_transcript, cli_options.kind, cli_options.generate_prompts, run_inputs
            ),
        )

        _trim_init_evals(eval_set, cli_options.num_evals, cli_options.append, output)

        eval_set, appended, new_evals_display, _write_raw = _append_generated_evals(
            cli_options.append, _existing_raw, eval_set
        )

        if cli_options.trigger_probes:
            eval_set = _generate_init_probes(
                gen,
                eval_set,
                _write_raw,
                _InitProbeOptions(
                    output, cli_options.append, skill_path, cli_options.num_evals, cli_options.from_transcript,
                    run_inputs,
                ),
            )

        # append 模式写 _write_raw(原有字节保留 + 新追加);否则 model_dump 全量。
        data = _write_raw if _write_raw is not None else eval_set.model_dump(mode="json")
        output.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _print_generated_evals(eval_set, gen, output, appended, new_evals_display)
    except SiftError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
    finally:
        if temporary_run is not None:
            temporary_run.cleanup()


def _resolve_evals_output(skill_path, output):
    """Resolve the eval file path while preserving the existing directory behavior."""
    if output is None:
        # skill_path 经 resolve_skill_dir:SKILL.md/目录 → 目录;agent .md → 文件(原样)。
        # .md 文件取 parent(agent 目录),让 agent 考卷默认写 <agent>/evals/(像 skill)。
        base = skill_path if skill_path.is_dir() else skill_path.parent
        evals_dir = base / "evals"
        evals_dir.mkdir(exist_ok=True)
        output = evals_dir / "evals.json"
    elif output.is_dir():
        output = output / "evals.json"
    return output


def _load_existing_evals(append, output):
    """Load append input, allowing generation to continue when it is unreadable."""
    _existing_raw = None
    _existing_evals = None
    if append and output.exists():
        from sift.models import EvalSet

        try:
            _existing_raw = json.loads(output.read_text(encoding="utf-8"))
            _existing_evals = EvalSet.model_validate(_existing_raw).evals
        except Exception:
            console.print((f"[yellow]⚠️ 读已有 {output} 失败,改为覆盖写入。[/yellow]"))
    return _existing_raw, _existing_evals


def _append_generated_evals(append, _existing_raw, eval_set):
    """Append new cases with continued IDs and preserve the original raw fields."""
    appended = 0
    new_evals_display: list = []  # append 模式:终端只列这些新增(B);非 append 为空 → 列全量
    _write_raw = None  # append 模式:保留原有字节的 raw dict(A);非 append 为 None → model_dump 写
    if append and _existing_raw is not None:
        from sift.models import EvalSet

        existing = EvalSet.model_validate(_existing_raw)
        next_id = max((e.id for e in existing.evals), default=0) + 1
        new_evals = list(eval_set.evals)
        for e in new_evals:
            e.id = next_id
            next_id += 1
        appended = len(new_evals)
        # A:原有 raw evals 字节保留,只追加新题 model_dump(不重序列化原有 →
        # diff 干净、不丢 EvalCase 不认的未知字段)。
        _existing_raw.setdefault("evals", []).extend(e.model_dump(mode="json") for e in new_evals)
        eval_set = existing
        eval_set.evals.extend(new_evals)
        _write_raw = _existing_raw
        new_evals_display = new_evals
    return eval_set, appended, new_evals_display, _write_raw


def _print_eval_diagnostics(display_evals, gen, eval_set):
    """Show generation warnings, case details, and intentionally shared prompts."""
    degraded = _degraded_cases(display_evals)
    if degraded:
        console.print(
            (
                "[yellow]⚠️  "
                f"{len(degraded)}"
                " 个 eval 为 degraded fallback(LLM "
                "失败,已降级模板/兜底挖取——题面为模板句、assertions 可能为空):[/yellow]"
            )
        )
        for e, f in degraded:
            console.print(f"[yellow]   - {e.name}: {f}[/yellow]")
        console.print("[yellow]建议重跑,或检查 claude CLI 可用性。[/yellow]")
    unknown_warns = gen.init_warnings
    if unknown_warns:
        console.print(
            (
                f"[yellow]⚠️  {len(unknown_warns)} 条 eval 含未知字段"
                f"(LLM 写了 EvalCase 不认的字段,已忽略;可能拼错、考卷可能缺字段):[/yellow]"
            )
        )
        for w in unknown_warns:
            console.print(f"[yellow]   - {w}[/yellow]")
    for e in display_evals:
        level = getattr(e, "level", "L1")
        line = (
            f"  [{e.id}] [{level}] {e.name}: {len(e.assertions)} assertions, {len(e.expectations)} expectations"
        ) + (f", {len(e.fixtures)} fixtures" if e.fixtures else "")
        if e.confidence is not None:
            line += f" · conf {e.confidence:.2f}"
        console.print(line)
        rat = (e.rationale or "").strip()
        if not (rat):
            continue
        console.print(f"      ↳ {rat[:140]}")
    for g in same_prompt_groups(eval_set):
        prompt_short = g["prompt"][:60] + ("…" if len(g["prompt"]) > 60 else "")
        flag_note = ",带 split flag" if g["split_flag"] else ""
        console.print(
            (f"[cyan]ℹ[/cyan] 含同 prompt 题组:「{prompt_short}」× {g['count']} 道(不同 scenario{flag_note},非重复)")
        )


def _append_trigger_probes(eval_set, generated_probes, mined_probes, _write_raw):
    """Deduplicate mined probes, allocate IDs, and preserve existing raw probe fields."""
    from sift.evals.trigger_mining import probe_dedup_key

    _seen_keys = {probe_dedup_key(p.prompt) for p in eval_set.trigger_probes}
    _seen_keys.update(probe_dedup_key(p.prompt) for p in generated_probes)
    mined_kept: list = []
    for p in mined_probes:
        if probe_dedup_key(p.prompt) in _seen_keys:
            continue
        _seen_keys.add(probe_dedup_key(p.prompt))
        generated_probes.append(p)
        mined_kept.append(p)
    new_probes = generated_probes
    # id 接续已有 trigger_probes 的 max id + 1
    existing_probes = list(eval_set.trigger_probes)
    next_id = max((p.id for p in existing_probes), default=0) + 1
    for p in new_probes:
        p.id = next_id
        next_id += 1
    eval_set.trigger_probes = existing_probes + new_probes
    if _write_raw is not None:  # append 模式:新 probe 也追加到 raw(保留原有 probe 字节)
        _write_raw.setdefault("trigger_probes", []).extend(p.model_dump(mode="json") for p in new_probes)
    return new_probes, mined_kept


def _print_trigger_probe_summary(new_probes, mined_kept, skill_path):
    """Report generated and mined probes, including unverified and leakage warnings."""
    if new_probes:
        console.print((f"[green]Generated[/green] {len(new_probes) - len(mined_kept)} trigger probes"))
        if mined_kept:
            n_unverified = sum(1 for p in mined_kept if p.rationale.startswith("[unverified"))
            note = (f",{n_unverified} 条 unverified(LLM 过滤失败,建议人审)") if n_unverified else ""
            console.print((f"[green]Mined[/green] {len(mined_kept)} 条 trace 负例(真实会话领域相邻未触发{note})"))
        # anti-leakage:should-trigger 探题若照抄 description 措辞,cc 可能靠字面匹配
        # 触发而非语义理解 → recall 虚高。只反馈(词面命中探题仍有诊断价值),不 retry。
        from sift.execution.trigger_runner import (
            PHRASE_LEAK_MIN_CHARS,
            classify_phrase,
        )
        from sift.parser import parse_skill_md

        _desc = parse_skill_md(skill_path)[1]
        if _desc:
            n_generated_st = sum(1 for p in new_probes if p.expect_trigger)
            leaks = [p for p in new_probes if p.expect_trigger and classify_phrase(p.prompt, _desc) == "literal"]
            if leaks:
                console.print(
                    (
                        f"[yellow]⚠ {len(leaks)}/{n_generated_st} 道该触发探题为词面命中"
                        f"(照抄 description ≥{PHRASE_LEAK_MIN_CHARS} 字符),"
                        f"建议人工复核或收紧 description。[/yellow]"
                    )
                )
    else:
        console.print(("[yellow]⚠️  未生成 trigger 探题(LLM 失败或解析空)。[/yellow]"))


def _degraded_cases(evals):
    """Retain each degraded flag in case order for the generation diagnostics."""
    degraded = []
    for eval_case in evals:
        for flag in eval_case.flags or []:
            if flag.startswith("degraded:"):
                degraded.append((eval_case, flag))
    return degraded


def _generate_init_evals(gen, skill_path: Path, _existing_evals, options: _InitGenerationOptions):
    """按 transcript、LLM 或模板路径生成考题。"""
    from_transcript, kind, generate_prompts, run_inputs = options
    if run_inputs is not None:
        eval_set = gen.generate_from_run(
            skill_path,
            [(session.session_id, session.scoped_path) for session in run_inputs.sessions],
            kind,
            on_heartbeat=lambda m: console.print(m),
        )
    elif from_transcript:
        if not kind:
            _warn_transcript_generation_scope(from_transcript)
        console.print((f"Mining evals from transcript [bold]{from_transcript}[/bold]..."))
        eval_set = gen.generate_from_transcript(
            skill_path,
            from_transcript,
            on_heartbeat=lambda m: console.print(m),
            kind=kind,
        )
    elif generate_prompts:
        with console.status("正在用 LLM 生成 evals…", spinner="dots"):
            if kind == "agent":
                eval_set = gen.generate_for_agent(
                    skill_path, existing_evals=_existing_evals, on_heartbeat=lambda m: console.print(m)
                )
            else:
                eval_set = gen.generate_with_llm(
                    skill_path, existing_evals=_existing_evals, on_heartbeat=lambda m: console.print(m)
                )
    else:
        console.print("Generating template evals...")
        eval_set = gen.generate_template(skill_path)

    return eval_set


def _trim_init_evals(eval_set, num_evals: int | None, append: bool, output: Path) -> None:
    """限制题数，并防止空生成结果覆盖已有考卷。"""
    if num_evals and num_evals < len(eval_set.evals):
        console.print((f"  Trimming to {num_evals} evals (from {len(eval_set.evals)})"))
        eval_set.evals = eval_set.evals[:num_evals]

    # 空集防覆盖:生成 0 题(LLM 失败耗尽重试;agent 路径唯一空集来源)+ 非追加 +
    # 目标已有考卷 → 不拿空考卷毁掉已有考卷,报错退出(重跑即恢复;覆盖式 init
    # 语义是"换新考卷",不是"失败时清空")。
    if not eval_set.evals and not append and output.exists():
        console.print(
            (
                f"[red]Error:[/red] 生成 0 题(LLM 失败,重试耗尽),已保留既有 {output} 不覆盖。"
                "大概率瞬态故障,建议直接重跑。"
            )
        )
        raise typer.Exit(code=1)


def _generate_init_probes(gen, eval_set, _write_raw, options: _InitProbeOptions):
    """追加触发探题，保留已有评分题和 raw 字段。"""
    output, append, skill_path, num_evals, from_transcript, run_inputs = options
    if output.exists() and not append:
        from sift.models import EvalSet

        try:
            eval_set = EvalSet.model_validate(json.loads(output.read_text(encoding="utf-8")))
        except Exception:
            console.print((f"[yellow]⚠️ 读已有 {output} 失败,trigger 探题将附加到新生成的 evals。[/yellow]"))
    with console.status("正在生成 trigger 探题…", spinner="dots"):
        generated_probes = gen.generate_trigger_probes(skill_path, num_each=(num_evals or 5))
    # trace-mined 负例:配历史会话输入才挖(真实会话里领域相邻且未触发本
    # skill 的用户 query → should-not 真题;单给 --trigger-probes 维持纯生成)。
    mined_probes = []
    if run_inputs is not None:
        with console.status(f"正在从 run {run_inputs.run_id} 挖 trigger 负例…", spinner="dots"):
            mined_probes = gen.mine_trigger_probes_from_transcripts(skill_path, list(run_inputs.probe_paths))
    elif from_transcript:
        with console.status(f"正在从 {from_transcript} 挖 trigger 负例…", spinner="dots"):
            mined_probes = gen.mine_trigger_probes(skill_path, from_transcript)
    if num_evals:  # 每方向上限 trim(不补下限);只 trim 生成的——真题不受生成上限约束
        per = max(1, num_evals)
        should = [p for p in generated_probes if p.expect_trigger][:per]
        should_not = [p for p in generated_probes if not p.expect_trigger][:per]
        generated_probes = should + should_not
    # 归一去重(prompt lower+strip 精确匹配):挖的 vs 已有 + 生成的;挖的内部
    # 事实层已去重。语义去重不做——量小人审兜底。
    new_probes, mined_kept = _append_trigger_probes(eval_set, generated_probes, mined_probes, _write_raw)
    _print_trigger_probe_summary(new_probes, mined_kept, skill_path)
    return eval_set


def _print_generated_evals(eval_set, gen, output: Path, appended: int, new_evals_display: list) -> None:
    """分别显示新增题数、合并总数、模型消耗和考题诊断。"""
    # B:append 模式终端只列新增(degraded / per-eval);非 append 列全量。
    display_evals = new_evals_display if new_evals_display else eval_set.evals

    if appended:
        # append 模式:显示「本次新增 N · 合并后 M」,不把合并总数当生成数(原 "Generated 12" 误导)
        console.print(
            (
                "本次新增 [green]"
                f"{appended}"
                "[/green] eval · 合并后共 [bold]"
                f"{len(eval_set.evals)}"
                "[/bold] 道 for [bold]"
                f"{eval_set.skill_name}"
                "[/bold]"
            )
        )
    else:
        console.print(
            (f"Generated [green]{len(eval_set.evals)}[/green] eval cases for [bold]{eval_set.skill_name}[/bold]")
        )
    tok = gen.llm_usage.get("input_tokens", 0) + gen.llm_usage.get("output_tokens", 0)
    if gen.llm_duration > 0 or tok:
        console.print((f"耗时 [bold]{gen.llm_duration:.1f}s[/bold] · {tok} tok"))
    if appended:
        console.print((f"[green]Appended[/green] {appended} new evals to existing {output}(id 接续、原题保留)"))
    _print_eval_diagnostics(display_evals, gen, eval_set)
    console.print(f"Written to {output}")


def _warn_transcript_generation_scope(from_transcript: Path) -> None:
    """未限制作用域时，提示跨 skill 或 agent 挖题的影响。"""
    from sift.audit.slice import invoked_agents, invoked_skills

    skills = invoked_skills(from_transcript)
    if len(skills) > 1:
        console.print(
            (
                f"[yellow]⚠️  transcript 涉及 {len(skills)} 个 skill"
                f"({', '.join(skills)}),会把全部当本 skill 挖、可能混入其他任务内容;"
                f"加 --kind skill 只挖本 skill 被调用期间段。[/yellow]"
            )
        )
    agents = invoked_agents(from_transcript)
    if len(agents) > 1:
        console.print(
            (
                f"[yellow]⚠️  transcript 涉及 {len(agents)} 个 agent"
                f"({', '.join(agents)}),会把全部当本声明挖、可能混入其他任务内容;"
                f"加 --kind agent 只挖本 agent 被 dispatch 期间段。[/yellow]"
            )
        )
