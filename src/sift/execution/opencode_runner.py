"""OpenCodeRunner — 路径1 真任务模式的 opencode 执行器(与 ClaudeRunner 对偶)。

同签名 ``run(prompt, skill_path, model, timeout, cwd, max_turns, on_progress) -> RunOutput``。
spawn ``opencode run <prompt> --standalone --format json --dangerously-skip-permissions
[-m provider/model]``,在隔离 XDG_DATA_HOME 下跑(会话落一次性 db、不污染真库;
``--standalone`` 私有 server 是隔离在 opencode 2.x 生效的前提 —— 共享 service 持有
操作员真实 env,客户端 XDG 被完全忽略),跑完 export 刚建的 session(**不带
--sanitize**,audit 要原文)→ ``RunOutput.stdout``。目录经进程 cwd 控制(opencode 2.x
已移除 ``--dir`` flag)。

实战模式抄 opsbot src/opsbot/driver/opencode.py(spike-derived):
- ``_ensure_api_key``:opencode @ai-sdk/anthropic 只读 ANTHROPIC_API_KEY,本机 GLM
  Coding Plan 给的是 ANTHROPIC_AUTH_TOKEN → alias 过来。
- model 含 ``/`` 才传 ``-m``(裸名 opencode API 不认 → 跳过走 config 默认)。
- stdin 关 EOF 避免 non-TTY 挂起;``--dangerously-skip-permissions`` headless 必带
  (opencode 无 --permission-mode);进程组 + killpg 超时清理。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from sift.errors import ExecutionError
from sift.evals.opencode_db import isolated_xdg_env_seeded, opencode_version, run_opencode
from sift.execution.cli_runner import build_live_prompt
from sift.execution.options import ExecutionOptions
from sift.models import RunOutput
from sift.utils import claude_cli_env


def _ensure_api_key(env: dict[str, str]) -> dict[str, str]:
    """opencode @ai-sdk/anthropic 只读 ANTHROPIC_API_KEY;本机 GLM Coding Plan 给的是
    ANTHROPIC_AUTH_TOKEN → 缺 API_KEY 时 alias 过来(抄 opsbot driver/opencode.py:221)。
    """
    if not env.get("ANTHROPIC_API_KEY") and env.get("ANTHROPIC_AUTH_TOKEN"):
        env["ANTHROPIC_API_KEY"] = env["ANTHROPIC_AUTH_TOKEN"]
    return env


def _progress_from_opencode_line(line: str) -> str | None:
    """opencode --format json 一行 → 进度串(供 on_progress 实时吐),无足轻重返 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    typ = ev.get("type")
    part = ev.get("part") or {}
    if typ == "text":
        t = part.get("text", "")
        return f"💬 {t[:80]}" if t else None
    if typ == "tool_use":
        return f"🔧 {part.get('tool', '?')}"
    if typ == "step_finish":
        tok = part.get("tokens") or {}
        return f"✓ {part.get('reason', '')}, {tok.get('total', 0)} tok"
    return None


def _parse_opencode_stream(stdout: str) -> tuple[int, dict[str, int], str, str, int]:
    """opencode --format json 流 → (total_tokens, tool_calls, result_text, stop_reason, num_turns)。best-effort。

    transcript 走 export(权威);这里只抽 run_debug.json 用的 operational metrics。
    事件形状:text / tool_use(call+result 合并)/ step_finish(tokens+reason)。
    num_turns = step_finish 事件计数(每个 ≈ 一轮 agent turn)。
    """
    total_tokens = 0
    tool_calls: dict[str, int] = {}
    result_text = ""
    stop_reason = ""
    num_turns = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            logging.getLogger(__name__).debug("Optional operation failed; continuing with fallback", exc_info=True)
            continue
        typ = ev.get("type")
        part = ev.get("part") or {}
        if typ == "text":
            t = part.get("text", "")
            if t:
                result_text = t  # 末条 assistant text 胜出
        elif typ == "tool_use":
            name = part.get("tool", "")
            if name:
                tool_calls[name] = tool_calls.get(name, 0) + 1
        elif typ == "step_finish":
            num_turns += 1
            tok = part.get("tokens") or {}
            tot = tok.get("total") or ((tok.get("input") or 0) + (tok.get("output") or 0))
            if tot:
                total_tokens = tot
            r = part.get("reason")
            if r:
                stop_reason = r
    return total_tokens, tool_calls, result_text, stop_reason, num_turns


class OpenCodeRunner:
    @staticmethod
    def run(prompt, skill_path=None, options: ExecutionOptions = ExecutionOptions()) -> RunOutput:
        model = options.model
        timeout = options.timeout
        cwd = options.cwd
        on_progress = options.on_progress
        if shutil.which("opencode") is None:
            raise ExecutionError(
                "opencode CLI 未找到;--executor opencode 需先装 opencode 并确保在 PATH(https://opencode.ai)"
            )
        injected = build_live_prompt(prompt, skill_path)
        workspace = cwd or Path.cwd()

        args = _opencode_run_command(injected, model, workspace)

        start = time.time()
        # 隔离 XDG seed 版:会话落一次性 db 不碰真库,同时 cp 真实 auth.json ——
        # opencode 2.x 凭据存 XDG_DATA_HOME,隔离后不 seed 则凭据型 provider 全断。
        with isolated_xdg_env_seeded() as xdg:
            # PWD 必须重写为 workspace:opencode server 用 env 的 PWD(不是进程 cwd)做项目
            # 发现 —— 残留 PWD 会把任务解析到 sift 自己的仓而非目标 workspace(opencode
            # 2.x 移除 --dir 后,PWD+cwd 是目录控制的唯一通道;spike 实证)。
            env = _ensure_api_key(
                {**claude_cli_env(), "XDG_DATA_HOME": str(xdg), "PWD": str(workspace)}
            )
            env.pop("OLDPWD", None)
            try:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=str(workspace),
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise ExecutionError(f"opencode CLI 启动失败: {exc}") from exc

            stdout, stderr = _collect_opencode_output(proc, timeout, on_progress)
            if proc.returncode != 0:
                raise ExecutionError(f"opencode run 失败(exit {proc.returncode}): {stderr[:500]}")

            transcript_json = _export_opencode_session(xdg, stderr)

            total_tokens, tool_calls, result_text, stop_reason, num_turns = _parse_opencode_stream(stdout)
            duration_ms = int((time.time() - start) * 1000)
            return RunOutput(
                exit_code=proc.returncode,
                stdout=transcript_json,
                duration_ms=duration_ms,
                total_tokens=total_tokens,
                tool_calls=tool_calls,
                result_text=result_text,
                stop_reason=stop_reason,
                num_turns=num_turns,
            )


def _collect_opencode_output(proc, timeout: int, on_progress: Callable[[str], None] | None) -> tuple[str, str]:
    """并行读取输出，并在超时后终止进程组、回收 reader 线程。"""
    # stdin 关 EOF:避免 opencode non-TTY stdin 挂起(opsbot spike finding)
    if proc.stdin is None:
        raise RuntimeError("Required process pipe is unavailable")
    proc.stdin.close()

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    t_out = threading.Thread(target=_read_opencode_stdout, args=(proc, stdout_chunks, on_progress), daemon=True)
    t_err = threading.Thread(target=_read_opencode_stderr, args=(proc, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait()
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    if timed_out:
        raise ExecutionError(f"opencode run 超时({timeout}s)")

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    return stdout, stderr


def _export_opencode_session(xdg: Path, stderr: str) -> str:
    """从本次隔离数据库导出原始会话，保留缺失会话时的诊断。

    选会话:**先剔除子会话**(带 ``parentID`` 的 subagent 子会话 —— 本机真库 API 实证
    字段名;子会话可能比主会话更晚 updated,按 updated 取最新会误选 child,后续审计
    拿不到完整主任务记录),再按 ``updated`` desc 取最新。

    命令面按版本分叉(``opencode_version`` 判定):

    - **2.x**:列 session 走 server API(``api --standalone GET /api/session``,不按
      调用方 cwd 项目过滤 —— CLI ``session list`` 会过滤,session 的 directory=
      workspace 与隔离 cwd 不匹配会列空),导出走 ``session export --standalone``
      (顶层 ``export`` 子命令已删)。全部 ``--standalone``:否则连后台共享 service
      读真库,隔离 db 读不到(service 持有操作员 env,客户端 XDG 被忽略;spike 实证)。
    - **1.x**:CLI ``session list --format json`` + 顶层 ``export <sid>``
      (main 原形态,1.19.5 验证;无后台 service,XDG env 直接生效)。
    """
    major = opencode_version()[0]
    if major >= 2:
        try:
            out = run_opencode(xdg, ["api", "--standalone", "GET", "/api/session"])
            data = json.loads(out.strip() or "{}")
        except json.JSONDecodeError:
            data = {}
        items = (data.get("data") if isinstance(data, dict) else None) or []
    else:
        try:
            items = json.loads(
                run_opencode(xdg, ["session", "list", "--format", "json"]).strip() or "[]"
            )
        except json.JSONDecodeError:
            items = []
    # 子会话剔除(带 parentID 跳过;字段不存在时 .get 为 None,1.x 无此字段不受影响)。
    # 极端情形(列出的全是子会话)不空手而归:如实导最新一条,错误信息可见好排。
    top_level = [s for s in items if not s.get("parentID")] or items
    if not top_level:
        raise ExecutionError(f"opencode run 未持久化 session(无法 export;stderr: {stderr[:300]})")
    # 隔离 XDG db 是 fresh 的,只含本次 opencode run 刚建的会话 → 剔除子会话后按 updated
    # desc 取最新即它;若 opencode 将来会额外建辅助会话,按 timestamp 过滤仍稳。
    if major >= 2:
        sid = max(top_level, key=lambda s: ((s.get("time") or {}).get("updated") or 0)).get("id", "")
        export_args = ["session", "export", "--standalone", sid]
    else:
        sid = max(top_level, key=lambda s: (s.get("updated") or 0)).get("id", "")
        export_args = ["export", sid]
    transcript_json = run_opencode(xdg, export_args)  # 原文,不 sanitize

    return transcript_json


def _read_opencode_stdout(proc, stdout_chunks: list[str], on_progress: Callable[[str], None] | None) -> None:
    """读取进程管道，按原顺序收集解码后的文本。"""
    if proc.stdout is None:
        raise RuntimeError("Required process pipe is unavailable")
    for raw in iter(proc.stdout.readline, b""):
        text = raw.decode("utf-8", errors="replace")
        stdout_chunks.append(text)
        if not (on_progress):
            continue
        msg = _progress_from_opencode_line(text)
        if msg:
            on_progress(msg)


def _read_opencode_stderr(proc, stderr_chunks: list[str]) -> None:
    """读取进程管道，按原顺序收集解码后的文本。"""
    if proc.stderr is None:
        raise RuntimeError("Required process pipe is unavailable")
    for raw in iter(proc.stderr.readline, b""):
        stderr_chunks.append(raw.decode("utf-8", errors="replace"))


def _opencode_run_command(injected: str, model: str | None, workspace: Path) -> list[str]:
    """按 opencode 版本构造 run 参数(1.x/2.x 命令面分叉,``opencode_version`` 判定)。

    **1.x**(≤1.19 形态,main 原样,1.19.5 验证):

    - 有 ``--dir``(目录 flag 尚在),无 ``--standalone``(1.x 无后台共享 service,
      ``XDG_DATA_HOME`` 等客户端 env 直接生效,隔离不靠私有 server)。

    **2.x**(2026-11-19 spike 实证适配,本机 2.0.24):

    - **无 ``--dir`` flag**:2.x 已移除(传了直接 usage 报错 exit 1)——目录经进程
      cwd(``Popen(cwd=workspace)``)控制,opencode 从 cwd 做项目发现。
    - **``--standalone``(私有 server)**:2.x 默认连后台共享 service,service 持有
      操作员真实 env → 客户端 ``XDG_DATA_HOME`` 被完全忽略、session 落真库、隔离目录
      里连 db 都不出现(隔离 + export 机制整个失效,还会污染操作员会话库)。
      standalone 每次私有 server + 客户端 env 生效 + session 落隔离一次性 db。
    - ``--dangerously-skip-permissions`` 两代均兼容(2.x 为隐藏别名,实测 exit 0)。
    - model 含 ``/`` 才传 ``-m``(裸名 opencode API 不认 → 跳过走 config 默认)。
    """
    if opencode_version()[0] >= 2:
        args = ["opencode", "run", "--standalone", "--format", "json", "--dangerously-skip-permissions"]
    else:
        args = ["opencode", "run", "--format", "json", "--dangerously-skip-permissions", "--dir", str(workspace)]
    if model and "/" in model:
        args.extend(["-m", model])
    args.append(injected)
    return args
