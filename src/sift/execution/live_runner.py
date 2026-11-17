"""路径1 真任务模式:不读 evals.json,直接 spawn 执行器在真实工作目录跑真实任务。

执行器多态(默认 cc,opt-in opencode/dsh 经由 make_live_runner / executors 参数):
cc spawn `claude -p` 真跑,opencode spawn `opencode run` 真跑,dsh spawn
`dsh --profile <headless> --json -` 真跑 —— 同一考场、同一任务、同一 skill,
各跑 N 次,公平对照。

区别于:
- 路径3(evals.json fixture 剧本 spawn + scoring):任务源是 init 编的剧本 + fixture 数据;
- 路径2(audit 审历史 trace):不 spawn,审已有 trace。

路径1 = 真实工作目录(--cwd 任务材料)+ 真实任务 prompt(--task)+ 被测 skill(--skill)
→ spawn 执行器真跑 → dump 真任务 trace → audit 审 skill 遵循性。

设计要点:
- invocation 开始时将 --cwd(或无 --cwd 时的 skill)冻结到 pristine snapshot;
- 每次 run 从 snapshot 重建 output_dir/workspace,避免跨 run 污染;
- skill references/ 拷进 workspace(SKILL.md 里 references/xxx 可读);
- N 次 spawn,每次 dump 会话流成 transcript(cc .jsonl / opencode .json),打通 audit 闭环。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sift.errors import InputError
from sift.execution.cli_runner import ClaudeRunner
from sift.execution.options import ExecutionOptions, LiveTaskOptions
from sift.execution.runner import _copy_skill_references
from sift.models import RunOutput

ProgressCallback = Callable[..., None]

# 拷贝 --cwd 时忽略的大目录(.git/.venv 等),避免拷贝爆炸
_IGNORE_ON_COPY = shutil.ignore_patterns(
    ".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"
)


def make_live_runner(name: str):
    """执行器名 → runner 实例(路径1 多态 seam)。未知名 → InputError(exit 2)。

    OpenCodeRunner 懒 import(避免无 opencode 时也拖进依赖/启动开销)。
    """
    if name == "cc":
        return ClaudeRunner()
    if name == "opencode":
        from sift.execution.opencode_runner import OpenCodeRunner

        return OpenCodeRunner()
    if name == "dsh":
        from sift.execution.dsh_runner import DshRunner

        return DshRunner()
    raise InputError(f"未知 executor: {name}(支持: cc, opencode, dsh)")


def _workspace_for(skill_path: Path, options: LiveTaskOptions) -> Path:
    workspace = options.output_dir / "workspace"
    source_root = (options.cwd or skill_path).resolve()
    workspace_root = workspace.resolve()
    overlaps = source_root == workspace_root or source_root in workspace_root.parents
    overlaps = overlaps or workspace_root in source_root.parents
    if overlaps:
        raise InputError(
            "--output/workspace must not overlap the run input; choose an output directory outside it"
        )
    return workspace


def _reset_workspace(snapshot: Path, workspace: Path) -> None:
    if workspace.is_symlink() or workspace.is_file():
        workspace.unlink()
    elif workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(snapshot, workspace)


@dataclass(frozen=True)
class _LiveRun:
    skill_path: Path
    task: str
    options: LiveTaskOptions
    workspace: Path
    runner: object
    run_dir: Path
    run_num: int
    exec_name: str
    snapshot: Path


def _run_one(run: _LiveRun) -> RunOutput:
    skill_path = run.skill_path
    task = run.task
    options = run.options
    workspace = run.workspace
    runner = run.runner
    run_dir = run.run_dir
    run_num = run.run_num
    exec_name = run.exec_name
    _reset_workspace(run.snapshot, workspace)
    _copy_skill_references(skill_path, workspace)
    if options.on_run_progress:
        options.on_run_progress(f"▶ {exec_name} run {run_num}/{options.runs}")
    during = (
        (lambda msg: options.on_run_progress(f"  {msg}"))
        if options.on_run_progress
        else None
    )
    output: RunOutput = runner.run(
        prompt=task,
        skill_path=skill_path,
        options=ExecutionOptions(
            model=options.model,
            timeout=options.timeout,
            cwd=workspace,
            max_turns=options.max_turns,
            on_progress=during,
        ),
    )
    if output.stdout:
        ext = ".json" if exec_name == "opencode" else ".jsonl"
        (run_dir / f"transcript{ext}").write_text(output.stdout, encoding="utf-8")
    debug = {
        "run_num": run_num,
        "executor": exec_name,
        "task": task[:500],
        "duration_ms": output.duration_ms,
        "total_tokens": output.total_tokens,
        "num_turns": output.num_turns,
        "stop_reason": output.stop_reason,
        "tool_calls": output.tool_calls,
        "result_text": output.result_text[:2000] if output.result_text else "",
    }
    (run_dir / "run_debug.json").write_text(
        json.dumps(debug, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return output


def _run_executors(
    skill_path: Path,
    task: str,
    options: LiveTaskOptions,
    workspace: Path,
    snapshot: Path,
) -> None:
    executors = list(options.executors) if options.executors else ["cc"]
    multi = len(executors) > 1
    for exec_name in executors:
        runner = make_live_runner(exec_name)
        for run_num in range(1, options.runs + 1):
            suffix = f"-{exec_name}" if multi else ""
            run_dir = options.output_dir / f"run-{run_num}{suffix}"
            run_dir.mkdir(parents=True, exist_ok=True)
            output = _run_one(
                _LiveRun(
                    skill_path=skill_path,
                    task=task,
                    options=options,
                    workspace=workspace,
                    runner=runner,
                    run_dir=run_dir,
                    run_num=run_num,
                    exec_name=exec_name,
                    snapshot=snapshot,
                )
            )
            if options.on_progress:
                options.on_progress(run_num, options.runs, output)


def _run_from_snapshot(
    skill_path: Path,
    task: str,
    options: LiveTaskOptions,
    workspace: Path,
) -> None:
    source = options.cwd or skill_path
    with tempfile.TemporaryDirectory(
        prefix=".sift-live-snapshot-", dir=options.output_dir
    ) as temp:
        snapshot = Path(temp) / "pristine"
        shutil.copytree(source, snapshot, ignore=_IGNORE_ON_COPY)
        _run_executors(skill_path, task, options, workspace, snapshot)


def run_live_task(
    skill_path: Path, task: str, options: LiveTaskOptions = LiveTaskOptions()
) -> Path:
    """跑路径1 真任务模式,返回 output_dir。"""
    workspace = _workspace_for(skill_path, options)
    options.output_dir.mkdir(parents=True, exist_ok=True)
    _run_from_snapshot(skill_path, task, options, workspace)
    return options.output_dir
