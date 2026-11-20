"""opencode session(JSON)→ SessionDigest + agent 切分 单测。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sift.evals.opencode import (
    _records_from_opencode,
    build_digest_from_opencode,
    is_opencode_session,
    load_opencode_session,
)
from sift.evals.transcript import build_digest

_FIX = Path(__file__).parents[2] / "fixtures" / "example-skills"
_LEA_OC = _FIX / "log-error-analyzer-zh" / "opencode-sessions" / "synthetic-violation.json"
_LEA_CC = _FIX / "log-error-analyzer-zh" / "transcripts" / "synthetic-violation.jsonl"


def test_is_opencode_session_by_extension_and_shape(tmp_path):
    # .json + 两键(info/messages)→ True;.jsonl / 缺键 / 坏 JSON → False
    assert is_opencode_session(_LEA_OC) is True
    assert is_opencode_session(_LEA_CC) is False  # .jsonl
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a session"}', encoding="utf-8")
    assert is_opencode_session(bad) is False  # 缺 info/messages
    ugly = tmp_path / "ugly.json"
    ugly.write_text("{not valid", encoding="utf-8")
    assert is_opencode_session(ugly) is False  # 坏 JSON(JSONDecodeError 分支)


def test_build_digest_from_opencode_shape():
    d = build_digest_from_opencode(_LEA_OC)
    # skill_used 采用会话中的 info.agent
    assert d.skill_used == "log-error-analyzer"
    # user_turn 保留原始 prompt
    assert d.user_turns == ["分析 logs/app.log 里的错误,写 error-report.json"]
    # input_files 含 app.log,内容是 log 正文(去行号后)
    # 注:单路径 fixture 下 _detect_root 取父目录(.../logs),rel 结果是 "app.log"
    # ——与 cc build_digest 对同名 .jsonl 的输出一致(镜像 cc,不发散)。
    assert len(d.input_files) == 1
    assert d.input_files[0].path.endswith("app.log")
    assert "E-AUTH-001" in d.input_files[0].content
    # 无 write → produced_files 空(镜像 cc 没写报告)
    assert d.produced_files == []
    # tool_trace 有 Read(经 _records_from_opencode 的 _norm_tool 归一为 cc 形大写,
    # 与 cc build_digest 同款;旧 inline 重建保小写,接共享重建器后统一)
    assert any(tc.name.lower() == "read" for tc in d.tool_trace)
    # transcript_text 含编造的 E-FAKE-999
    assert "E-FAKE-999" in d.transcript_text


def test_cc_and_opencode_digests_equivalent():
    """同场景 cc .jsonl 与 opencode .json 重建出的 digest 关键字段一致(重建等价门)。"""
    cc = build_digest(_LEA_CC)
    oc = build_digest_from_opencode(_LEA_OC)

    assert cc.user_turns == oc.user_turns
    # input_files:同 path 同内容(app.log 去行号后)
    assert [f.path for f in cc.input_files] == [f.path for f in oc.input_files]
    assert cc.input_files[0].content == oc.input_files[0].content
    # 都没写报告
    assert cc.produced_files == oc.produced_files == []
    # tool_trace 工具名归一后一致(cc Read / opencode read)
    assert [t.name.lower() for t in cc.tool_trace] == [t.name.lower() for t in oc.tool_trace]
    # transcript_text 都含编造码
    assert "E-FAKE-999" in cc.transcript_text and "E-FAKE-999" in oc.transcript_text


def test_rebuild_dispatches_opencode():
    from sift.audit.rebuild import rebuild

    tool_trace, text, digest = rebuild(_LEA_OC)
    assert digest.skill_used == "log-error-analyzer"  # 走了 opencode 分支
    assert any(tc.name.lower() == "read" for tc in tool_trace)
    assert "E-FAKE-999" in text


def test_rebuild_still_handles_cc_jsonl():
    from sift.audit.rebuild import rebuild

    tool_trace, text, digest = rebuild(_LEA_CC)  # cc 路径不被破坏
    assert any(tc.name.lower() == "read" for tc in tool_trace)


def test_tool_match_handles_opencode_lowercase_names():
    """tool_match 对工具名大小写不敏感:trace 里小写 read,SKILL 指令工具名 Read → 应命中。
    (历史上 opencode tool_trace 是小写;现 opencode 经 _norm_tool 已归一为大写,但 tool_match
    仍按大小写不敏感设计,小写 trace 依旧命中——测的是 tool_match 鲁棒性,不改 tool_match.py。)
    """
    from sift.audit.tool_match import match_tool_call
    from sift.models import ToolCallRecord

    trace = [ToolCallRecord(name="read", input_summary='{"filePath": "logs/app.log"}')]
    # 条件1:工具名大小写不敏感命中(Read vs read)
    assert match_tool_call("Read", "", trace) is True
    # 条件2:script_path 子串命中(opencode filePath 也含路径)
    assert match_tool_call("", "logs/app.log", trace) is True
    # 未出现的工具不命中
    assert match_tool_call("Bash", "", trace) is False


_MULTI_OC = _FIX / "multi-skill-demo" / "multi-skill.json"


def test_slice_opencode_by_agent_segments():
    from sift.evals.opencode import slice_opencode_by_agent

    lea = slice_opencode_by_agent(_MULTI_OC, "log-error-analyzer")
    cra = slice_opencode_by_agent(_MULTI_OC, "code-review-auditor")
    rcz = slice_opencode_by_agent(_MULTI_OC, "release-checklist-zh")
    # LEA 2 段(run1+run4)、CRA 2 段(run2+run5)、RCZ 1 段(run3)
    assert len(lea) == 2
    assert len(cra) == 2
    assert len(rcz) == 1
    # target 未出现 → []
    assert slice_opencode_by_agent(_MULTI_OC, "nope") == []


def test_slice_opencode_purity_and_skill_used():
    """每段只含 target agent 的 message;子 info.agent = target(rebuild 取 skill_used)。

    CRA(非顶层 agent)锁 override:fixture 顶层 info.agent 是 LEA,若 slicer 没把
    子段 info.agent 改写为 target,CRA 段会继承 LEA → skill_used 错。所以必须用 CRA
    验 override,光验 LEA(顶层本就是 LEA)是 no-op 断言。
    """
    import tempfile

    from sift.evals.opencode import slice_opencode_by_agent

    def _skill_used_of(seg: dict) -> str:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "seg.json"
            p.write_text(json.dumps(seg, ensure_ascii=False), encoding="utf-8")
            return build_digest_from_opencode(p).skill_used

    for target in ("log-error-analyzer", "code-review-auditor"):
        segs = slice_opencode_by_agent(_MULTI_OC, target)
        assert segs  # 两 agent 都有段
        for seg in segs:
            # 纯度:段内 message.info.agent 全是 target
            assert {m.get("info", {}).get("agent") for m in seg["messages"]} == {target}
            # override:子 info.agent 改写为 target(CRA 才真验)
            assert seg["info"]["agent"] == target
            # 端到端：重建后的 skill_used 应记录目标名称。
            assert _skill_used_of(seg) == target

    # 段自带 user 子任务 prompt + read 工具(用 LEA 段验产物重建)
    lea0 = slice_opencode_by_agent(_MULTI_OC, "log-error-analyzer")[0]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "seg.json"
        p.write_text(json.dumps(lea0, ensure_ascii=False), encoding="utf-8")
        d = build_digest_from_opencode(p)
        assert d.user_turns
        assert any(tc.name.lower() == "read" for tc in d.tool_trace)


def test_root_target_is_session_info_agent():
    """root 在 opencode = 切 session.info.agent(声明的主 agent)。

    audit.py 的 --kind root 依赖这条契约:opencode 无 parent_tool_use_id,root 靠
    session.info.agent 定主 agent。multi-skill.json 的主 agent 是 log-error-analyzer
    (另含 code-review-auditor / release-checklist-zh 两个子 agent)。断言
    slice_opencode_by_agent 以 info.agent 为 target 返回非空、每段 info.agent 回填
    主 agent、且段内不含别的 agent 的 message。
    """
    from sift.evals.opencode import slice_opencode_by_agent

    sess = load_opencode_session(_MULTI_OC)
    main_agent = (sess.get("info") or {}).get("agent")
    assert main_agent == "log-error-analyzer"  # multi-skill.json 声明的主 agent
    assert main_agent, "multi-skill.json 应带 info.agent(主 agent)"

    segs = slice_opencode_by_agent(_MULTI_OC, main_agent)
    assert segs, "主 agent 段非空"
    # 每段 info.agent 回填为 main_agent(slice_opencode_by_agent 契约)
    assert all((s.get("info") or {}).get("agent") == main_agent for s in segs)
    # 主 agent 段不含别的 agent 的 message(排除 code-review-auditor / release-checklist-zh)
    other_agents = {(m.get("info") or {}).get("agent") for s in segs for m in s["messages"]} - {main_agent}
    assert not other_agents, f"主 agent 段混入了别的 agent: {other_agents}"


# 真 `opencode export` 产出的会话(本地 /tmp/oc_export.json,缺席则 skip)。
# 证明 build_digest_from_opencode 吃的是真实生产格式,非自造。生成:
#   opencode session list  # 找 sessionID
#   opencode export <sessionID> > /tmp/oc_export.json
_REAL_EXPORT = Path("/tmp/oc_export.json")


def test_real_opencode_export_consumed():
    """真 opencode export 输出能被 build_digest_from_opencode 消费(非自造格式证明)。"""
    if not _REAL_EXPORT.exists():
        pytest.skip(f"无真 export fixture:({_REAL_EXPORT};生成见上注释)")
    assert is_opencode_session(_REAL_EXPORT) is True
    d = build_digest_from_opencode(_REAL_EXPORT)
    # 真 session 有 tool_trace(该 session 跑了 read/bash/write 等)或至少有内容
    assert d.tool_trace or d.transcript_text or d.user_turns
    assert d.skill_used  # info.agent 非空


# 真 opencode .db 端到端:audit --db 路径的 list→export→digest 闭环(需 opencode CLI + 真 .db)。
# 生成:cp <你的 opencode.db> /tmp/oc_export.db
#   (opencode db 默认在 ~/.local/share/opencode/opencode.db,或 $XDG_DATA_HOME/opencode/ 下)
_REAL_DB = Path("/tmp/oc_export.db")


def test_real_opencode_db_export_consumed():
    """真 .db → list_sessions/export_session → build_digest_from_opencode 端到端通(audit --db 闭环证明)。"""
    import shutil
    import tempfile

    if not _REAL_DB.exists():
        pytest.skip(f"无真 .db fixture:({_REAL_DB};生成见上注释)")
    if not shutil.which("opencode"):
        pytest.skip("opencode CLI 未装")
    from sift.evals.opencode_db import export_session, list_sessions, resolve_session

    sessions = list_sessions(_REAL_DB)
    if not sessions:
        pytest.skip("真 .db 内无 session")
    # 取最新那条(list_sessions 已按 updated desc 排)
    target = resolve_session(sessions, sessions[0]["id"])
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(export_session(_REAL_DB, target["id"]))
        p = Path(f.name)
    try:
        assert is_opencode_session(p) is True
        d = build_digest_from_opencode(p)
        assert d.tool_trace or d.transcript_text or d.user_turns
    finally:
        p.unlink(missing_ok=True)


def _oc_session(parts_user, parts_asst):
    """造最小 opencode export 形 session JSON。"""
    return {
        "info": {"agent": "log-error-analyzer", "time": {"created": 1}},
        "messages": [
            {"info": {"role": "user"}, "parts": parts_user},
            {"info": {"role": "assistant"}, "parts": parts_asst},
        ],
    }


def test_records_from_opencode_basic_shape(tmp_path):
    sess = _oc_session(
        [{"type": "text", "text": "do it"}],
        [
            {"type": "text", "text": "thinking"},
            {
                "type": "tool",
                "tool": "read",
                "state": {
                    "input": {"filePath": "/p/a.py"},
                    "output": "1  hello\n2  world",
                },
            },
        ],
    )
    p = tmp_path / "s.json"
    p.write_text(json.dumps(sess))
    recs = _records_from_opencode(p)
    # 第一条是用户 prompt；保留 text，供摘要重建抽取 user_turns，
    #     与 cc 路径经 _records_from_cc 保留 user 消息对齐→digest 等价),
    # 第二条是助手的文本及工具调用，第三条是用户角色承载的工具结果。
    assert recs[0]["message"]["role"] == "user"
    assert recs[1]["message"]["role"] == "assistant"
    types = [b["type"] for b in recs[1]["message"]["content"]]
    assert types == ["text", "tool_use"]
    tu = next(b for b in recs[1]["message"]["content"] if b["type"] == "tool_use")
    assert tu["name"] == "Read"  # 小写归大写
    assert tu["input"]["file_path"] == "/p/a.py"  # filePath → file_path
    assert recs[2]["message"]["role"] == "user"
    tr = next(b for b in recs[2]["message"]["content"] if b["type"] == "tool_result")
    assert "hello" in tr["content"]


def test_records_from_opencode_edit_field_mapping(tmp_path):
    sess = _oc_session(
        [{"type": "text", "text": "go"}],
        [
            {
                "type": "tool",
                "tool": "edit",
                "state": {
                    "input": {
                        "filePath": "/p/a.py",
                        "oldString": "x",
                        "newString": "y",
                        "replaceAll": True,
                    },
                    "output": "ok",
                },
            }
        ],
    )
    p = tmp_path / "e.json"
    p.write_text(json.dumps(sess))
    recs = _records_from_opencode(p)
    # 第一条是用户 prompt，第二条是助手的工具调用。
    asst = next(r for r in recs if r["message"]["role"] == "assistant")
    tu = next(b for b in asst["message"]["content"] if b["type"] == "tool_use")
    assert tu["name"] == "Edit"
    assert tu["input"] == {
        "file_path": "/p/a.py",
        "old_string": "x",
        "new_string": "y",
        "replace_all": True,
    }


def test_opencode_edit_reconstructs(tmp_path):
    """read→edit 命中 → 经 canonical 重建器产出 edit 后内容(opencode Task 3-4 落地能力验证)。

    旧 opencode 路径只有 read/write/bash,edit 不重建;接共享重建器后应自动获得。
    本测端到端验证:read 拿到 base → edit 把 beta→BETA → produced_files 含 BETA。
    """
    base = "1  alpha\n2  beta\n"
    sess = {
        "info": {"agent": "x", "time": {"created": 1}},
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "edit it"}]},
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "text", "text": "ok"},
                    {
                        "type": "tool",
                        "tool": "read",
                        "state": {"input": {"filePath": "/p/a.py"}, "output": base},
                    },
                    {
                        "type": "tool",
                        "tool": "edit",
                        "state": {
                            "input": {
                                "filePath": "/p/a.py",
                                "oldString": "beta",
                                "newString": "BETA",
                            },
                            "output": "ok",
                        },
                    },
                ],
            },
        ],
    }
    p = tmp_path / "e.json"
    p.write_text(json.dumps(sess))
    d = build_digest_from_opencode(p)
    # edit 命中 base → produced_files 含 edit 后内容
    assert any("BETA" in f.content for f in d.produced_files)
    # 不应残留未 edit 的 base 副本(有 beta\n 且无 BETA 的 produced_file 即残留)
    assert not any("beta\n" in f.content and "BETA" not in f.content for f in d.produced_files)


def test_opencode_edit_missing_old_demotes(tmp_path):
    """edit oldString 在 read base 里找不到 → demoted → 进 unreconstructed(此前 opencode 无此能力)。

    验证共享重建器的 demote 推理在 opencode 路径上端到端工作。
    """
    sess = {
        "info": {"agent": "x", "time": {"created": 1}},
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "edit"}]},
            {
                "info": {"role": "assistant"},
                "parts": [
                    {
                        "type": "tool",
                        "tool": "read",
                        "state": {
                            "input": {"filePath": "/p/a.py"},
                            "output": "1  alpha\n",
                        },
                    },
                    {
                        "type": "tool",
                        "tool": "edit",
                        "state": {
                            "input": {
                                "filePath": "/p/a.py",
                                "oldString": "GHOST",
                                "newString": "x",
                            },
                            "output": "ok",
                        },
                    },
                ],
            },
        ],
    }
    p = tmp_path / "g.json"
    p.write_text(json.dumps(sess))
    d = build_digest_from_opencode(p)
    # oldString 不在 base → demoted → 进 unreconstructed
    assert any("a.py" in u for u in d.unreconstructed)


def test_cc_opencode_digest_symmetric(tmp_path):
    """同一组工具调用(read /p/a.py → write /p/b.py),cc-record 形与 opencode 形
    经同一重建器 _digest_from_records → 重建相关字段等价(canonical 路径一致性)。
    """
    from sift.evals.transcript import _digest_from_records

    # —— cc-record 形:直接构造 records 喂重建器 ——
    cc_records = _cc_read_write_records()
    d_cc = _digest_from_records(cc_records)

    # —— opencode 形:造 export JSON 喂 build_digest_from_opencode(内部转 records 再喂同一重建器)——
    sess = _tool_session(
        [
            {"type": "text", "text": "ok"},
            {
                "type": "tool",
                "tool": "read",
                "state": {
                    "input": {"filePath": "/p/a.py"},
                    "output": "1  hi\n",
                },
            },
            {
                "type": "tool",
                "tool": "write",
                "state": {
                    "input": {"filePath": "/p/b.py", "content": "out\n"},
                    "output": "wrote",
                },
            },
        ]
    )
    p = tmp_path / "sym.json"
    p.write_text(json.dumps(sess))
    d_oc = build_digest_from_opencode(p)

    # 重建相关字段等价(canonical 路径一致性)
    assert [f.path for f in d_cc.produced_files] == [f.path for f in d_oc.produced_files]
    assert [f.content for f in d_cc.produced_files] == [f.content for f in d_oc.produced_files]
    assert [f.path for f in d_cc.input_files] == [f.path for f in d_oc.input_files]
    assert [f.content for f in d_cc.input_files] == [f.content for f in d_oc.input_files]
    assert d_cc.user_turns == d_oc.user_turns
    assert d_cc.unreconstructed == d_oc.unreconstructed
    assert [c.cmd for c in d_cc.commands] == [c.cmd for c in d_oc.commands]
    # tool_trace 工具名序列等价(两边都经 _norm_tool 归一)
    assert [t.name for t in d_cc.tool_trace] == [t.name for t in d_oc.tool_trace]


def _cc_read_write_records():
    cc_records = [
        {
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
            "parent_tool_use_id": None,
        },
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "ok"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Read",
                        "input": {"file_path": "/p/a.py"},
                    },
                ],
            },
            "parent_tool_use_id": None,
        },
        {
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "1  hi\n"}],
            },
            "parent_tool_use_id": None,
        },
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_2",
                        "name": "Write",
                        "input": {"file_path": "/p/b.py", "content": "out\n"},
                    }
                ],
            },
            "parent_tool_use_id": None,
        },
    ]
    return cc_records


def test_opencode_real_format_read_cleaned_and_edit_reconstructs(tmp_path):
    """真 opencode read 格式(<content> 包裹 + N: 冒号空格行号)→ (1) 只读文件入 input_files
    内容干净(无包裹、无 N:);(2) 多行 edit 命中(reviewer M1:修复前行号未剥,'alpha\\nbeta'
    在 '1: alpha\\n2: beta' 里找不到 → edit demote)。
    """

    sess = _opencode_read_edit_session()
    p = tmp_path / "real.json"
    p.write_text(json.dumps(sess))
    d = build_digest_from_opencode(p)
    # (1) 只读的 notes.md 入 input_files,内容干净
    notes = next((f for f in d.input_files if f.path.endswith("notes.md")), None)
    assert notes is not None, f"notes.md 应在 input_files;got {[f.path for f in d.input_files]}"
    assert "<content>" not in notes.content and "<path>" not in notes.content
    assert not any(re.match(r"^[ \t]*\d+:[ \t]", ln) for ln in notes.content.split("\n")), "N: 行号未剥"
    assert notes.content == "alpha\nbeta\ngamma\n"
    # (2) app.py 多行 edit 命中 → produced_files 含替换后内容(修复前 demote)
    assert any("ALPHA" in f.content and "BETA" in f.content for f in d.produced_files), (
        f"多行 edit 应重建(M1 回归);produced={[f.path for f in d.produced_files]} unrecon={d.unreconstructed}"
    )
    assert not any("app.py" in u for u in d.unreconstructed), "edit 不该 demote"


def _opencode_read_edit_session():
    def _read_out(body_lines):
        return (
            "<path>/p/f</path>\n<type>file</type>\n<content>\n"
            + "\n".join(f"{i}: {ln}" for i, ln in enumerate(body_lines, 1))
            + "\n</content>"
        )

    sess = _tool_session(
        [
            {"type": "text", "text": "ok"},
            # 只读 → 入 input_files,验内容干净
            {
                "type": "tool",
                "tool": "read",
                "state": {
                    "input": {"filePath": "/p/notes.md"},
                    "output": _read_out(["alpha", "beta", "gamma"]),
                },
            },
            # 读 + 多行 edit → 验 edit 命中(修复前 demote)
            {
                "type": "tool",
                "tool": "read",
                "state": {
                    "input": {"filePath": "/p/app.py"},
                    "output": _read_out(["alpha", "beta", "gamma"]),
                },
            },
            {
                "type": "tool",
                "tool": "edit",
                "state": {
                    "input": {
                        "filePath": "/p/app.py",
                        "oldString": "alpha\nbeta",
                        "newString": "ALPHA\nBETA",
                    },
                    "output": "ok",
                },
            },
        ]
    )
    return sess


def _tool_session(parts):
    return {
        "info": {"agent": "x", "time": {"created": 1}},
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "go"}]},
            {"info": {"role": "assistant"}, "parts": parts},
        ],
    }


# ---- opencode 2.x export 形状垫片(load_opencode_session 单点归一) ----


def _v2_export() -> dict:
    """opencode 2.0.24 实测形状(2026-11-19 spike):消息顶层 type/text/content,
    tool part 名在 name、输出在 state.content[].text、无 session 级 agent、含 idle。"""
    return {
        "info": {
            "id": "ses_v2",
            "projectID": "p1",
            "model": {"id": "glm-5.3", "providerID": "gateway", "variant": "default"},
            "outcome": "succeeded",
            "time": {"created": 1, "updated": 2},
            "location": {"directory": "/ws"},
        },
        "messages": [
            {
                "id": "m1",
                "time": {"created": 10},
                "type": "user",
                # 2.x 实测怪癖:user text 双重 JSON 编码(首尾带 ")
                "text": '"read app.log and report"',
                "files": [],
            },
            {
                "id": "m2",
                "time": {"created": 20},
                "type": "assistant",
                "agent": "log-error-analyzer",
                "model": {"id": "glm-5.3", "providerID": "gateway"},
                "finish": "tool-calls",
                "content": [
                    {"type": "reasoning", "text": "先读文件"},
                    {
                        "type": "tool",
                        "id": "call_x1",
                        "name": "read",
                        "executed": False,
                        "state": {
                            "status": "completed",
                            "input": {"path": "/ws/logs/app.log"},
                            "content": [
                                {"type": "text", "text": "Read file /ws/logs/app.log, lines 1-1\n1: E-AUTH-001 token expired"}
                            ],
                            "metadata": {"truncated": False},
                        },
                        "time": {"created": 20, "ran": 21, "completed": 22},
                    },
                ],
            },
            {
                "id": "m3",
                "time": {"created": 30},
                "type": "assistant",
                "agent": "log-error-analyzer",
                "finish": "stop",
                "content": [{"type": "text", "text": "发现 E-FAKE-999(编造)"}],
            },
            {"id": "m4", "time": {"created": 40}, "type": "idle", "outcome": "succeeded"},
        ],
    }


def _write_v2(tmp_path: Path) -> Path:
    p = tmp_path / "v2-session.json"
    p.write_text(json.dumps(_v2_export(), ensure_ascii=False), encoding="utf-8")
    return p


def test_load_normalizes_v2_shape_and_drops_idle(tmp_path):
    sess = load_opencode_session(_write_v2(tmp_path))
    # idle 丢弃;roles 从顶层 type 归一;主 agent 由 assistant 众数推导
    assert [m["info"]["role"] for m in sess["messages"]] == ["user", "assistant", "assistant"]
    assert all(m["info"]["role"] != "idle" for m in sess["messages"])
    assert sess["info"]["agent"] == "log-error-analyzer"
    # user 文本 → parts;tool part 名(name→tool)、输出(state.content→state.output)
    user_parts = sess["messages"][0]["parts"]
    assert user_parts[0]["text"] == "read app.log and report"
    tool_part = sess["messages"][1]["parts"][0]
    assert tool_part["type"] == "tool" and tool_part["tool"] == "read"
    assert tool_part["state"]["input"] == {"path": "/ws/logs/app.log"}
    assert tool_part["state"]["output"] == "Read file /ws/logs/app.log, lines 1-1\n1: E-AUTH-001 token expired"
    # reasoning part 不进 parts(1.x 解析器本就不读思考类)
    assert [p["type"] for p in sess["messages"][1]["parts"]] == ["tool"]


def test_load_v2_user_message_gets_next_assistant_agent(tmp_path):
    """评审复现:2.x user 消息无 agent 字段(export 实测),按 agent/root 切片只保留
    info.agent 匹配的消息 → 用户请求被整段丢掉、digest.user_turns 空。归属规则:
    用户消息归「下一条 assistant」的 agent(回应者即归属者)。"""
    sess = load_opencode_session(_write_v2(tmp_path))
    user = sess["messages"][0]
    assert user["info"]["role"] == "user"
    assert user["info"]["agent"] == "log-error-analyzer", "user 须归到下一条 assistant 的 agent"


def test_slice_v2_user_request_not_dropped_from_agent_slice(tmp_path):
    """评审原始复现闭环:「用户请求 + 目标 agent 回复」→ 切片必须同时保留 user 与
    assistant(修复前切片只剩 assistant,digest.user_turns 为空 → 遵循性失判)。"""
    from sift.evals.opencode import slice_opencode_by_agent

    segments = slice_opencode_by_agent(_write_v2(tmp_path), "log-error-analyzer")
    assert len(segments) == 1
    roles = [m["info"]["role"] for m in segments[0]["messages"]]
    assert roles == ["user", "assistant", "assistant"], "用户请求必须留在 agent 切片里"
    # 切片重建 digest:user_turns 非空(修复前为空)
    seg_path = tmp_path / "seg.json"
    seg_path.write_text(json.dumps(segments[0], ensure_ascii=False), encoding="utf-8")
    d = build_digest_from_opencode(seg_path)
    assert d.user_turns == ["read app.log and report"]


def test_load_v2_trailing_user_without_assistant_has_no_agent(tmp_path):
    """尾部悬空 user(无后续 assistant)→ 保持无 agent,不属于任何段(不硬造归属)。"""
    sess = _v2_export()
    sess["messages"].append(
        {"id": "m5", "time": {"created": 50}, "type": "user", "text": '"orphan"', "files": []}
    )
    p = tmp_path / "trailing-user.json"
    p.write_text(json.dumps(sess, ensure_ascii=False), encoding="utf-8")
    loaded = load_opencode_session(p)
    trailing = loaded["messages"][-1]
    assert trailing["info"]["role"] == "user"
    assert not trailing["info"].get("agent")


def test_build_digest_from_v2_export(tmp_path):
    d = build_digest_from_opencode(_write_v2(tmp_path))
    assert d.skill_used == "log-error-analyzer"  # 众数 agent → skill_used
    assert d.user_turns == ["read app.log and report"]
    assert any(tc.name.lower() == "read" for tc in d.tool_trace)
    # Read 输出进 input_files(共享重建器清洗:去行号前缀)
    assert d.input_files and d.input_files[0].path.endswith("app.log")
    assert "E-AUTH-001" in d.input_files[0].content
    assert "1: E-AUTH-001" not in d.input_files[0].content  # 行号前缀已清
    # assistant 文本进 transcript_text
    assert "E-FAKE-999" in d.transcript_text


def test_load_v2_quoted_user_text_preserved(tmp_path):
    """原文首尾带引号(任务指令本身是引号串)时,剥一层双重编码后引号保留、不误剥。"""
    sess = _v2_export()
    raw = '"verify the fix"'  # 任务指令原文本身首尾带引号
    sess["messages"][0]["text"] = json.dumps(raw)  # opencode 2.x 双重编码(json.dumps 后的原文)
    p = tmp_path / "quoted-session.json"
    p.write_text(json.dumps(sess, ensure_ascii=False), encoding="utf-8")
    loaded = load_opencode_session(p)
    assert loaded["messages"][0]["parts"][0]["text"] == '"verify the fix"'  # 引号保留


def test_load_v1_shape_passthrough_untouched():
    """1.x 形状原样透传(幂等):既有 fixture 不经垫片改动。"""
    sess = load_opencode_session(_LEA_OC)
    assert sess["info"]["agent"] == "log-error-analyzer"  # 原生 info.agent,非推导
    for m in sess["messages"]:
        assert "type" not in m  # 未被改写
