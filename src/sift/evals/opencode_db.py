"""opencode .db → 单 session export 的 plumbing:把「拿到一个 opencode .db 文件 → audit」
的手工三步(XDG_DATA_HOME 隔离 → 列 session → 导出原文)包成函数,供 audit 命令编排。

**不读 SQLite**:列 session 靠 server API(``opencode api --standalone GET /api/session``,
全量 + cursor 翻页),导出靠 ``opencode session export --standalone <id>``(默认原文;
``--sanitize`` 可选,但脱敏后别喂 audit —— auditor 读 ``part.text`` 判遵循性);
opencode schema 变是 opencode 的事,sift 不背 SQLite 解析(与 b9ef5be「用真格式、
不重造 export」决议一致)。

**opencode 2.x 适配(2026-11-19 spike 实证,本机 2.0.24)**:
- 顶层 ``opencode export`` 子命令已删(挪到 ``session export``),旧形态直接 usage 报错。
- **所有 opencode 子进程必须 ``--standalone``**:后台共享 service 持有操作员真实 env,
  客户端 ``XDG_DATA_HOME`` 被完全忽略 → 连 service 读的是真库,隔离 db 读不到。
- ``opencode session list``(CLI)按调用方 cwd 项目过滤 —— 外来 db 的 session
  directory 是原机路径,本机任何 cwd 都列空;全量通道只有 server API(不按项目过滤)。
- 凭据存 ``$XDG_DATA_HOME/opencode/auth.json``:隔离 XDG 的写侧路径(执行器/判官)
  必须 seed 一份真实 auth(``seed_auth`` / ``isolated_xdg_env_seeded``),否则凭据型
  provider(zhipuai-coding-plan 等)全断。

**实证(2026-07-25 探 ``~/.local/share/opencode/opencode.db``)**:库是**事件溯源**结构 ——
单表 ``event(id, aggregate_id, seq, type, data)``,按 ``aggregate_id``(=session)+ ``seq``
重放 6 种带版本后缀(``.1``)的内部事件(``message.part.updated`` / ``message.updated`` /
``session.*`` 等,单 session ~284 事件、part 增量更新要按 part_id 取末态)才能拼出 session。
直读 = 自己实现事件重放 + 绑 opencode 内部版本化 schema,维护锚重;且 read 输出在**源存储**
里就是 ``<content>``+``N: `` 格式(与 export 同),直读也绕不开清洗(见 ``opencode._clean_opencode_read_output``)。
唯一好处(无 opencode CLI 读别处拷来的 .db)场景窄,不值。

opencode 无 ``--db`` 参数,但认 ``XDG_DATA_HOME``(db 放
``$XDG_DATA_HOME/opencode/opencode.db``)→ 设该变量即可隔离指向任意 .db、不碰本机真库
(**前提 ``--standalone``**,见上)。

本模块纯逻辑(无 CLI/IO:不打表、不 ``typer.Exit``)。失败统一抛 ``OpencodeDbError``
(exit_code=3,被 audit 的 ``except SiftError`` 兜住)。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sift.errors import OpencodeDbError

#: ``opencode --version`` 进程内缓存(版本不变,探一次就够;测试 monkeypatch 本函数,
#: 缓存由 ``monkeypatch.setattr(opencode_db, "_opencode_version_cache", None)`` 复位)。
_opencode_version_cache: tuple[int, int, int] | None = None


def opencode_version() -> tuple[int, int, int]:
    """``opencode --version`` → ``(major, minor, patch)``,进程内缓存一次。

    1.x/2.x 命令面分叉的判定基础(runner 的 run/export、db 的 list/export、判官
    transport 的最低版本门)。可见原则:探不出 → RuntimeError 报原文,**不猜默认**
    —— 猜错分支比报错贵(1.x 机器跑 2.x 命令是 usage 报错,2.x 机器跑 1.x 命令
    是共享 service 吞掉隔离 env、会话落真库)。
    """
    global _opencode_version_cache
    if _opencode_version_cache is not None:
        return _opencode_version_cache
    if shutil.which("opencode") is None:
        raise RuntimeError(
            "opencode CLI 未找到:opencode 路径(执行器 / audit --db / 判官 transport)需先装 opencode 并确保在 PATH(https://opencode.ai)"
        )
    try:
        proc = subprocess.run(
            ["opencode", "--version"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"opencode --version 执行失败: {exc}") from exc
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", out)
    if proc.returncode != 0 or not m:
        raise RuntimeError(
            f"opencode --version 输出无法解析(exit {proc.returncode}): {out[:200]!r}"
        )
    _opencode_version_cache = (int(m[1]), int(m[2]), int(m[3]))
    return _opencode_version_cache


def run_opencode(xdg_dir: Path, args: list[str], timeout: int = 120) -> str:
    """在 ``XDG_DATA_HOME=xdg_dir``、``cwd=xdg_dir`` 下跑 ``opencode <args>``,返回 stdout。

    cwd 用隔离目录(非仓库)规避 ``session list`` 的 cwd-scoping 过滤。失败
    (binary 缺失 / 非零退出 / 超时)→ ``OpencodeDbError``。
    """
    cmd = ["opencode", *args]
    # PWD 重写为 cwd(opencode 用 env 的 PWD 做项目发现,残留 PWD 会把 cwd-scoping
    # 解析到调用方的仓 → session list 过滤列空;与进程 cwd 对齐才稳)。
    env = {**os.environ, "XDG_DATA_HOME": str(xdg_dir), "PWD": str(xdg_dir)}
    env.pop("OLDPWD", None)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(xdg_dir),
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OpencodeDbError("opencode CLI 未找到;--db 需先装 opencode 并确保在 PATH(https://opencode.ai)") from exc
    except subprocess.TimeoutExpired as exc:
        raise OpencodeDbError(f"opencode {' '.join(args)} 超时({timeout}s)") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise OpencodeDbError(f"opencode {' '.join(args)} 失败(exit {proc.returncode}): {detail[:500]}")
    return proc.stdout


@contextmanager
def isolated_xdg_env(db_path: Path):
    """搭隔离 XDG 环境:``mkdtemp`` → cp db_path 到 ``<tmp>/opencode/opencode.db`` → yield tmp。

    yield 的目录即作 ``XDG_DATA_HOME`` 和子进程 cwd(opencode 在此找到 db、且 cwd 非仓库
    保证 session list 列全)。退出 ``rmtree`` 清理。db 不存在 → ``OpencodeDbError``。
    """
    if not db_path.exists():
        raise OpencodeDbError(f"opencode db 文件不存在: {db_path}")
    tmp = Path(tempfile.mkdtemp(prefix="sift-opencode-"))
    try:
        (tmp / "opencode").mkdir(parents=True)
        shutil.copy2(db_path, tmp / "opencode" / "opencode.db")
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def isolated_xdg_env_empty():
    """空隔离 XDG(写侧):mkdtemp → mkdir opencode/ → yield → rmtree。

    给 ``opencode run`` 用:会话落一次性 ``<tmp>/opencode/opencode.db``、不碰真库。
    与 ``isolated_xdg_env(db_path)``(拷贝版,audit --db 读侧)对偶 —— 那个拷一个已有
    db 进去,这个建空目录让 opencode 自己写。

    .. deprecated:: XDG 隔离后 opencode 2.x 的凭据型 provider(zhipuai-coding-plan、
       opencode free 等,凭据存 ``XDG_DATA_HOME/opencode/auth.json``)会断,新代码用
       ``isolated_xdg_env_seeded``(seed 一份真实 auth.json)。
    """
    tmp = Path(tempfile.mkdtemp(prefix="sift-opencode-run-"))
    try:
        (tmp / "opencode").mkdir(parents=True)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def seed_auth(dest: Path) -> bool:
    """把操作员真实 ``auth.json`` seed 到隔离 XDG 布局,返回是否拷了。

    opencode 2.x 把凭据存 ``$XDG_DATA_HOME/opencode/auth.json``(spike 实证:
    zhipuai-coding-plan 等登录态都在这),XDG 隔离(判官 transport / 执行器)后不
    seed 则凭据型 provider 全断、只剩 env/config 型(ANTHROPIC_API_KEY /
    opencode.json 内嵌 apiKey)可用。真实 auth 不存在(从未登录任何 provider)→
    静默跳过返 False(env/config 型不受影响);拷贝失败也跳过(不因 seed 砸主路径,
    缺凭据的错误由 opencode 自己报、比这里的推断准)。
    """
    real_data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    src = real_data / "opencode" / "auth.json"
    try:
        if not src.is_file():
            return False
        shutil.copy2(src, dest)
        return True
    except OSError:
        return False


@contextmanager
def isolated_xdg_env_seeded():
    """seed 版空隔离 XDG(写侧):mkdtemp → opencode/ + cp 真实 auth.json → yield → rmtree。

    ``isolated_xdg_env_empty`` 的继任:会话仍落一次性 ``<tmp>/opencode/opencode.db``
    不碰真库,同时 seed 一份真实 ``auth.json`` 保住凭据型 provider(opencode 2.x
    凭据随 XDG_DATA_HOME 迁移)。给需要跑真实 LLM 调用的隔离路径用(执行器
    ``OpenCodeRunner``;判官 transport 在 ``execution/opencode_llm.py`` 自管临时
    目录、只复用 ``seed_auth``)。
    """
    tmp = Path(tempfile.mkdtemp(prefix="sift-opencode-run-"))
    try:
        (tmp / "opencode").mkdir(parents=True)
        seed_auth(tmp / "opencode" / "auth.json")
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _normalize_api_session(raw: dict) -> dict:
    """``session.list`` API 单条 → 下游消费的规整形状 ``{id, title, updated, created, projectId, directory}``。

    API 形状(2.x):``{id, projectID, time:{created, updated}, location:{directory}, …}``
    —— 无 title(模型摘要只在 CLI 命令里给,而 CLI 按 cwd 项目过滤不可用)、directory
    嵌 location、时间嵌 time。title 规整为空串(CLI 表格空列,非关键信息)。
    """
    time = raw.get("time") or {}
    return {
        "id": raw.get("id", ""),
        "title": raw.get("title") or "",
        "updated": time.get("updated") or 0,
        "created": time.get("created") or 0,
        "projectId": raw.get("projectID", ""),
        "directory": (raw.get("location") or {}).get("directory", ""),
    }


def list_sessions(db_path: Path) -> list[dict]:
    """列出 opencode db 里**所有** session,按 ``updated`` desc(新→旧)返回。

    命令面按版本分叉(``opencode_version`` 判定):

    - **2.x**:CLI ``session list`` 按调用方 cwd 项目过滤 —— 外来 db 的 session
      directory 是原机路径,任何本机 cwd 都列不出来(spike 实证)。全量通道是
      server API:``opencode api --standalone GET /api/session``(不按项目过滤,带
      cursor 翻页,畸形 cursor → HTTP 400 可检测),输出经 ``_normalize_api_session``
      规整成下游消费形状(audit --db 的表列 id/title/updated)。
    - **1.x**:CLI ``session list --format json`` 即全量(无 server API/cursor;
      main 原形态,1.19.5 验证),行为 ``{id, title, updated, created, projectId, directory}``。
    """
    if opencode_version()[0] < 2:
        with isolated_xdg_env(db_path) as xdg:
            out = run_opencode(xdg, ["session", "list", "--format", "json"])
        stripped = out.strip()
        if not stripped:
            return []
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise OpencodeDbError(f"opencode session list 输出无法解析为 JSON: {stripped[:200]!r}") from exc
        if not isinstance(data, list):
            raise OpencodeDbError(f"opencode session list 输出非数组: {str(data)[:200]}")
        return sorted(data, key=lambda s: s.get("updated", 0) or 0, reverse=True)

    items: list[dict] = []
    with isolated_xdg_env(db_path) as xdg:
        cursor: str | None = None
        while True:
            path = "/api/session" + (f"?cursor={cursor}" if cursor else "")
            out = run_opencode(xdg, ["api", "--standalone", "GET", path])
            try:
                data = json.loads(out)
            except json.JSONDecodeError as exc:
                raise OpencodeDbError(f"opencode api session.list 输出无法解析为 JSON: {out[:200]!r}") from exc
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise OpencodeDbError(f"opencode api session.list 输出非预期形状: {str(data)[:200]}")
            items.extend(_normalize_api_session(s) for s in data["data"])
            cursor = (data.get("cursor") or {}).get("next")
            if not cursor:
                break
    return sorted(items, key=lambda s: s.get("updated", 0) or 0, reverse=True)


def export_session(db_path: Path, session_id: str, sanitize: bool = False) -> str:
    """导出单 session,返回 opencode export 格式 JSON 串(``{info, messages}``)。

    ``sanitize`` 默认 **False**(导原文):audit 靠 ``build_digest_from_opencode`` 读
    ``part.text`` 判遵循性,``--sanitize`` 会把文本脱敏成 ``[redacted:text:…]`` → auditor
    看不到任务/回复 → 审计降级(只剩工具骨架)。要脱敏分享请在 sift 外手动
    ``opencode session export --sanitize``。

    输出非合法 session JSON → ``OpencodeDbError``。
    """
    if opencode_version()[0] < 2:
        # 1.x:顶层 ``export`` 子命令(main 原形态;2.x 已删,挪到 session export)。
        args = ["export", session_id]
    else:
        # opencode 2.x:export 顶层子命令已删(挪到 ``session export``),必须 ``--standalone``
        # —— 否则连后台共享 service 读真库,隔离 db 完全读不到(服务持有操作员 env)。
        args = ["session", "export", "--standalone", session_id]
    if sanitize:
        args.append("--sanitize")
    with isolated_xdg_env(db_path) as xdg:
        out = run_opencode(xdg, args)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise OpencodeDbError(f"opencode export 输出无法解析为 JSON(session={session_id}): {out[:200]!r}") from exc
    if not (isinstance(data, dict) and "messages" in data):
        raise OpencodeDbError(f"opencode export 输出非合法 session(缺 messages;session={session_id})")
    return out


def resolve_session(sessions: list[dict], session_id: str | None) -> dict | None:
    """从 list_sessions 的结果里选出要审的 session(纯策略)。

    - ``session_id`` 给定 → 返回匹配项;不在列 → ``OpencodeDbError``(列出已有 id 帮用户挑)。
    - 0 条 → ``OpencodeDbError``("db 内无 session")。
    - 恰 1 条 → 自动返回它(用户无需 ``--session``)。
    - 多条 → 返回 ``None``(歧义信号:交给 CLI 打表 + exit 2,让用户带 ``--session`` 重跑)。
    """
    if session_id is not None:
        for s in sessions:
            if not (s.get("id") == session_id):
                continue
            return s
        have = ", ".join(s.get("id", "?") for s in sessions) or "(无)"
        raise OpencodeDbError(f"session {session_id} 不在此 db;现有: {have}")
    if not sessions:
        raise OpencodeDbError("db 内无 session")
    if len(sessions) == 1:
        return sessions[0]
    return None
