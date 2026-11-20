# tests/test_opencode_db.py
"""opencode_db:从 opencode .db 文件 list/export session 的 plumbing 单测。

不真调 opencode(``subprocess.run`` 全 mock),验:
- ``run_opencode``:env 注入 XDG_DATA_HOME、cwd=隔离目录、命令拼接、失败 → OpencodeDbError
- ``isolated_xdg_env``:cp db 到 ``<tmp>/opencode/opencode.db``、退出清理、db 缺失报错
- ``list_sessions`` / ``export_session`` / ``resolve_session``(见各自 task 补的测)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import sift.evals.opencode_db as ocdb
from sift.errors import ExecutionError, OpencodeDbError

# 版本探测默认写死 2.0.24(经进程内缓存注入,不打真 CLI):list/export 的 1.x/2.x
# 分支测试各自覆写缓存;1.x/2.x 分叉是本模块的被测行为,不能依赖本机装的是哪代。
@pytest.fixture(autouse=True)
def _pin_version_cache(monkeypatch):
    monkeypatch.setattr(ocdb, "_opencode_version_cache", (2, 0, 24))


# ---- opencode_version(版本探测)----


def test_opencode_version_parses_v_prefix(monkeypatch):
    monkeypatch.setattr(ocdb, "_opencode_version_cache", None)
    monkeypatch.setattr(ocdb.shutil, "which", lambda name: "/fake/opencode")
    monkeypatch.setattr(
        ocdb.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="opencode v2.0.24\n", stderr=""),
    )
    assert ocdb.opencode_version() == (2, 0, 24)


def test_opencode_version_parses_bare_triplet(monkeypatch):
    monkeypatch.setattr(ocdb, "_opencode_version_cache", None)
    monkeypatch.setattr(ocdb.shutil, "which", lambda name: "/fake/opencode")
    monkeypatch.setattr(
        ocdb.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="opencode 1.19.5", stderr=""),
    )
    assert ocdb.opencode_version() == (1, 19, 5)


def test_opencode_version_caches_across_calls(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="opencode v2.0.24", stderr="")

    monkeypatch.setattr(ocdb, "_opencode_version_cache", None)
    monkeypatch.setattr(ocdb.shutil, "which", lambda name: "/fake/opencode")
    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)
    assert ocdb.opencode_version() == (2, 0, 24)
    assert ocdb.opencode_version() == (2, 0, 24)
    assert calls == [["opencode", "--version"]], "进程内只探一次"


def test_opencode_version_missing_cli_raises(monkeypatch):
    monkeypatch.setattr(ocdb, "_opencode_version_cache", None)
    monkeypatch.setattr(ocdb.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="opencode CLI 未找到"):
        ocdb.opencode_version()


def test_opencode_version_unparseable_output_raises(monkeypatch):
    """探不出 → 报原文,不猜默认(猜错分支比报错贵)。"""
    monkeypatch.setattr(ocdb, "_opencode_version_cache", None)
    monkeypatch.setattr(ocdb.shutil, "which", lambda name: "/fake/opencode")
    monkeypatch.setattr(
        ocdb.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="garbage", stderr="boom"),
    )
    with pytest.raises(RuntimeError, match="无法解析"):
        ocdb.opencode_version()


# ---- OpencodeDbError 层级 ----


def test_opencode_db_error_is_execution_error_exit3():
    assert issubclass(OpencodeDbError, ExecutionError)
    assert OpencodeDbError("x").exit_code == 3


# ---- run_opencode ----


def test_run_opencode_passes_xdg_env_cwd_and_cmd(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)

    out = ocdb.run_opencode(tmp_path, ["session", "list", "--standalone", "--format", "json"])
    assert out == "OK"
    assert captured["cmd"] == ["opencode", "session", "list", "--standalone", "--format", "json"]
    # XDG_DATA_HOME 指向隔离目录(核心:opencode 凭此找到那个 db,不碰本机真库)
    assert captured["env"]["XDG_DATA_HOME"] == str(tmp_path)
    # cwd=隔离目录(非仓库)→ 规避 session list 的 cwd-scoping 过滤
    assert captured["cwd"] == str(tmp_path)


def test_run_opencode_nonzero_raises_with_stderr(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom!")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)
    with pytest.raises(OpencodeDbError, match="boom!"):
        ocdb.run_opencode(tmp_path, ["session", "export", "--standalone", "ses_x"])


def test_run_opencode_missing_binary_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file", cmd[0])

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)
    with pytest.raises(OpencodeDbError, match="opencode"):
        ocdb.run_opencode(tmp_path, ["session", "list", "--standalone"])


def test_run_opencode_timeout_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)
    with pytest.raises(OpencodeDbError, match="超时"):
        ocdb.run_opencode(tmp_path, ["session", "export", "--standalone", "ses_x"], timeout=5)


# ---- isolated_xdg_env ----


def test_isolated_xdg_env_copies_db_and_cleans_up(tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"SQLITE-CONTENT")

    with ocdb.isolated_xdg_env(db) as xdg:
        copied = xdg / "opencode" / "opencode.db"
        assert copied.exists()
        assert copied.read_bytes() == b"SQLITE-CONTENT"
        # 退出前目录还在
        assert xdg.exists()

    # 退出后临时目录清理干净
    assert not xdg.exists()


def test_isolated_xdg_env_missing_db_raises(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(OpencodeDbError, match="不存在"):
        with ocdb.isolated_xdg_env(missing):
            pass


# ---- isolated_xdg_env_empty(写侧,与拷贝版对偶)----


def test_isolated_xdg_env_empty_yields_opencode_dir_and_cleans_up():
    """空隔离 XDG:yield 时 <tmp>/opencode/ 在;退出 rmtree。不拷 db(与拷贝版对偶)。"""
    from contextlib import ExitStack

    stack = ExitStack()
    xdg = stack.enter_context(ocdb.isolated_xdg_env_empty())
    assert (Path(xdg) / "opencode").is_dir()  # yield 时 opencode/ 目录在
    captured_xdg = Path(xdg)
    stack.close()
    assert not captured_xdg.exists()  # 退出后整个临时目录消失


# ---- isolated_xdg_env_seeded(seed 版,保凭据型 provider)----


def test_seed_auth_copies_real_auth(monkeypatch, tmp_path):
    """真实 auth.json 存在 → 拷贝;缺失 → 静默跳过返 False。"""
    fake_data = tmp_path / "realdata"
    (fake_data / "opencode").mkdir(parents=True)
    (fake_data / "opencode" / "auth.json").write_text('{"zhipuai-coding-plan": {}}')
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_data))

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    dest = dest_dir / "auth.json"
    assert ocdb.seed_auth(dest) is True
    assert dest.read_text() == '{"zhipuai-coding-plan": {}}'

    empty = tmp_path / "emptydata"
    empty.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(empty))
    dest2 = empty / "auth.json"
    assert ocdb.seed_auth(dest2) is False  # 真实 auth 不存在 → 跳过
    assert not dest2.exists()


def test_isolated_xdg_env_seeded_yields_auth_and_cleans_up(monkeypatch, tmp_path):
    """seed 版隔离:yield 时 <tmp>/opencode/auth.json 已就位;退出 rmtree。"""
    fake_data = tmp_path / "realdata"
    (fake_data / "opencode").mkdir(parents=True)
    (fake_data / "opencode" / "auth.json").write_text("{}")
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_data))

    from contextlib import ExitStack

    stack = ExitStack()
    xdg = Path(stack.enter_context(ocdb.isolated_xdg_env_seeded()))
    assert (xdg / "opencode" / "auth.json").is_file()  # 凭据已 seed
    stack.close()
    assert not xdg.exists()  # 退出后整个临时目录消失


# ---- list_sessions ----
# opencode 2.x 全量通道:server API session.list 的单页形状(不按 cwd 项目过滤,
# CLI session list 会过滤 → 外来 db 列空;见 list_sessions docstring)。
_API_PAGE_NEW = json.dumps(
    {
        "data": [
            {
                "id": "ses_new",
                "projectID": "p1",
                "time": {"created": 190, "updated": 200},
                "location": {"directory": "/orig/machine/proj"},
            },
            {
                "id": "ses_old",
                "projectID": "p1",
                "time": {"created": 90, "updated": 100},
                "location": {"directory": "/orig/machine/proj"},
            },
        ],
        "cursor": {"previous": None, "next": None},
    }
)


def test_list_sessions_parses_sorts_and_normalizes(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_API_PAGE_NEW, stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)

    sessions = ocdb.list_sessions(db)
    # 按 updated desc(新→旧)
    assert [s["id"] for s in sessions] == ["ses_new", "ses_old"]
    # 形状规整:API 嵌套字段摊平到下游消费形状(title 无则空串、directory 出 location)
    assert sessions[0]["updated"] == 200 and sessions[0]["created"] == 190
    assert sessions[0]["projectId"] == "p1"
    assert sessions[0]["directory"] == "/orig/machine/proj"
    assert sessions[0]["title"] == ""
    # 命令是全量 API 通道(CLI session list 按项目过滤不可用)
    assert cmds == [["opencode", "api", "--standalone", "GET", "/api/session"]]


def test_list_sessions_paginates_via_cursor(monkeypatch, tmp_path):
    """cursor.next 非空 → 带 ?cursor= 翻页取全量。"""
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    cmds = []
    pages = [
        json.dumps({"data": [{"id": "ses_b", "time": {"updated": 50}, "location": {}}], "cursor": {"next": "PAGE2"}}),
        json.dumps({"data": [{"id": "ses_a", "time": {"updated": 100}, "location": {}}], "cursor": {"next": None}}),
    ]

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=pages[len(cmds) - 1], stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)

    sessions = ocdb.list_sessions(db)
    assert [s["id"] for s in sessions] == ["ses_a", "ses_b"]
    assert cmds == [
        ["opencode", "api", "--standalone", "GET", "/api/session"],
        ["opencode", "api", "--standalone", "GET", "/api/session?cursor=PAGE2"],
    ]


def test_list_sessions_empty_data_is_empty_list(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(
        ocdb.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"data": [], "cursor": {"next": None}}), stderr=""),
    )
    assert ocdb.list_sessions(db) == []


def test_list_sessions_garbage_raises(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(
        ocdb.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr=""),
    )
    with pytest.raises(OpencodeDbError, match="解析"):
        ocdb.list_sessions(db)


def test_list_sessions_v1_uses_cli_session_list(monkeypatch, tmp_path):
    """1.x:无 server API,CLI ``session list --format json`` 即全量(main 原形态);
    行为平铺(updated 直读,非 2.x 的 time.updated 嵌套)。"""
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    cmds = []
    rows = [{"id": "ses_a", "updated": 100, "title": "A"}, {"id": "ses_b", "updated": 200}]

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)
    monkeypatch.setattr(ocdb, "_opencode_version_cache", (1, 19, 5))

    sessions = ocdb.list_sessions(db)
    assert [s["id"] for s in sessions] == ["ses_b", "ses_a"]  # updated desc
    assert cmds == [["opencode", "session", "list", "--format", "json"]]


def test_list_sessions_v1_empty_output_returns_empty(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(ocdb, "_opencode_version_cache", (1, 19, 5))
    monkeypatch.setattr(
        ocdb.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    assert ocdb.list_sessions(db) == []


# ---- export_session ----

_EXPORT_JSON = json.dumps({"info": {"id": "ses_x", "agent": "build"}, "messages": []})


def test_export_session_default_no_sanitize_returns_json(monkeypatch, tmp_path):
    """默认不带 --sanitize:audit 靠 build_digest_from_opencode 读 part.text 判遵循性,
    --sanitize 会把文本脱敏成 [redacted:text:…] → 审计降级。
    """
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_EXPORT_JSON, stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)

    out = ocdb.export_session(db, "ses_x")
    assert json.loads(out)["info"]["id"] == "ses_x"
    # 默认 export <id>,无 --sanitize
    assert cmds == [["opencode", "session", "export", "--standalone", "ses_x"]]


def test_export_session_sanitize_opt_in(monkeypatch, tmp_path):
    """sanitize=True 才带 --sanitize(分享脱敏版;audit 不走这条)。"""
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_EXPORT_JSON, stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)

    ocdb.export_session(db, "ses_y", sanitize=True)
    assert cmds == [["opencode", "session", "export", "--standalone", "ses_y", "--sanitize"]]


def test_export_session_non_session_shape_raises(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(
        ocdb.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout='{"not": "a session"}', stderr=""),
    )
    with pytest.raises(OpencodeDbError, match="合法"):
        ocdb.export_session(db, "ses_x")


def test_export_session_v1_uses_top_level_export(monkeypatch, tmp_path):
    """1.x:顶层 ``export <sid>``(main 原形态;2.x 已删挪到 session export)。"""
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_EXPORT_JSON, stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)
    monkeypatch.setattr(ocdb, "_opencode_version_cache", (1, 19, 5))

    out = ocdb.export_session(db, "ses_x")
    assert json.loads(out)["info"]["id"] == "ses_x"
    assert cmds == [["opencode", "export", "ses_x"]]


# ---- resolve_session(纯策略,无 mock)----


def _sess(i: str, title: str = "t") -> dict:
    return {"id": i, "title": title, "updated": 0, "created": 0}


def test_resolve_session_id_match_returns_it():
    sessions = [_sess("ses_a"), _sess("ses_b")]
    assert ocdb.resolve_session(sessions, "ses_b")["id"] == "ses_b"


def test_resolve_session_id_not_found_lists_existing():
    sessions = [_sess("ses_a"), _sess("ses_b")]
    with pytest.raises(OpencodeDbError, match="ses_b") as exc:  # 错误信息含被找的 id
        ocdb.resolve_session(sessions, "ses_missing")
    assert "ses_a" in str(exc.value) and "ses_b" in str(exc.value)  # 且列出已有


def test_resolve_session_empty_raises():
    with pytest.raises(OpencodeDbError, match="无 session"):
        ocdb.resolve_session([], None)


def test_resolve_session_single_autopicks():
    sessions = [_sess("ses_only")]
    assert ocdb.resolve_session(sessions, None)["id"] == "ses_only"


def test_resolve_session_multi_returns_none():
    """多 session + 未指定 → 返回 None(信号歧义,交给 CLI 列清单 + exit 2)。"""
    sessions = [_sess("ses_a"), _sess("ses_b"), _sess("ses_c")]
    assert ocdb.resolve_session(sessions, None) is None
