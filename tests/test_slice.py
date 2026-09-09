# tests/test_slice.py
import json
from pathlib import Path
from sift.audit.slice import build_subagent_tool_trace, slice_transcript, scope_signals

# kind='skill'(按 Skill 调用切)与 kind='agent'(按 agent 归属切)各一组薄封装,省得每处传 kind。
def _slice_skill(path, name):
    return slice_transcript(path, name, "skill")


def _slice_agent(path, name):
    return slice_transcript(path, name, "agent")

def _write(tmp_path: Path, recs: list) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    return p

def _skill(tid, name):
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "Skill", "input": {"skill": name}}]}}

def _asst_text(t):
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}

def _tool_use(tid, name, inp):
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}

def _tool_result(tid, text):
    return {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}

def _agent(tid):
    """主作用域里发起子 agent 的 Agent tool_use。"""
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "Agent", "input": {"subagent_type": "general-purpose"}}]}}

def _in_sub(parent, rec):
    """给 record 打 parent_tool_use_id,归入子 agent 作用域。"""
    rec["parent_tool_use_id"] = parent
    return rec


def _ag(agent, text, parent=None):
    """带 top-level agent 字段的 record(structured reader 展开后的 cc-record 形)。

    agent 归属切分只读 rec.agent;parent(给 parent_tool_use_id)仅用于构造嵌套场景,
    验证扁平切片器**不读** parent_tool_use_id。
    """
    rec: dict = {"agent": agent, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    if parent is not None:
        rec["parent_tool_use_id"] = parent
    return rec

def test_namespaced_skill_name_stripped_to_match(tmp_path):
    """真 transcript 的 input.skill 是命名空间名(如 plugin:skill),slicer 剥前缀后匹配 target。

    防护:所有别的测试都用裸名,若 rsplit 剥前缀逻辑坏了,全测试照绿但真 transcript
    (命名空间名)会静默得 0 span → 假 exit 2。这条钉死剥前缀路径。
    """
    recs = [
        _skill("s1", "ops-team:log-error-analyzer"),   # 命名空间名(非裸名)
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _tool_result("r1", "ERR"),
    ]
    spans = _slice_skill(_write(tmp_path, recs), "log-error-analyzer")
    assert len(spans) == 1                              # 剥掉 ops-team: 前缀后命中
    assert any("app.log" in json.dumps(r, ensure_ascii=False) for r in spans[0])

def test_single_main_skill_span_to_end(tmp_path):
    recs = [
        _asst_text("pre-skill 主 agent 干的活,不该进段"),
        _skill("s1", "log-error-analyzer"),
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _tool_result("r1", "ERR boom"),
        _asst_text("classify + report"),
    ]
    spans = _slice_skill(_write(tmp_path, recs), "log-error-analyzer")
    assert len(spans) == 1
    texts = [json.dumps(r, ensure_ascii=False) for r in spans[0]]
    assert any("log-error-analyzer" in t for t in texts)
    assert any("app.log" in t for t in texts)
    assert not any("pre-skill" in t for t in texts)


def test_main_multi_skill_interleaved_no_cross_contamination(tmp_path):
    recs = [
        _skill("s1", "log-error-analyzer"),
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _skill("s2", "code-review-auditor"),
        _tool_use("r2", "Read", {"file_path": "src/c.py"}),
        _skill("s3", "release-checklist-zh"),
        _tool_use("r3", "Bash", {"command": "git tag"}),
        _skill("s4", "log-error-analyzer"),
        _tool_use("r4", "Read", {"file_path": "app2.log"}),
    ]
    a = _slice_skill(_write(tmp_path, recs), "log-error-analyzer")
    assert len(a) == 2
    a_texts = [json.dumps(r, ensure_ascii=False) for seg in a for r in seg]
    assert any("app.log" in t for t in a_texts) and any("app2.log" in t for t in a_texts)
    assert not any("src/c.py" in t or "git tag" in t for t in a_texts)


def test_back_to_back_same_skill(tmp_path):
    recs = [
        _skill("s1", "log-error-analyzer"),
        _asst_text("first A work"),
        _skill("s2", "log-error-analyzer"),
        _asst_text("second A work"),
    ]
    spans = _slice_skill(_write(tmp_path, recs), "log-error-analyzer")
    assert len(spans) == 2


def test_target_never_invoked_returns_empty(tmp_path):
    recs = [_skill("s1", "code-review-auditor"), _asst_text("x")]
    assert _slice_skill(_write(tmp_path, recs), "log-error-analyzer") == []


def test_subagent_skill_scoped_to_return_no_main_pollution(tmp_path):
    agent_id = "call_agent1"
    recs = [
        _skill("s1", "log-error-analyzer"),
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": agent_id, "name": "Agent", "input": {"subagent_type": "general-purpose"}}]}},
        {"parent_tool_use_id": agent_id, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "sb", "name": "Skill", "input": {"skill": "code-review-auditor"}}]}},
        {"parent_tool_use_id": agent_id, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "rr", "name": "Read", "input": {"file_path": "src/c.py"}}]}},
        _tool_result(agent_id, "subagent done"),
        _tool_use("r2", "Read", {"file_path": "app2.log"}),
    ]
    a = _slice_skill(_write(tmp_path, recs), "log-error-analyzer")
    a_texts = [json.dumps(r, ensure_ascii=False) for seg in a for r in seg]
    assert any("app2.log" in t for t in a_texts)      # 返回后主 agent 仍归 A(不被 B 污染)
    assert not any("src/c.py" in t for t in a_texts)   # 子 agent 内部不进主段
    b = _slice_skill(_write(tmp_path, recs), "code-review-auditor")
    assert len(b) == 1
    b_texts = [json.dumps(r, ensure_ascii=False) for r in b[0]]
    assert any("src/c.py" in t for t in b_texts)


def test_nested_subagent_depth2_skill_scoped(tmp_path):
    """depth-2:main→A→B,B 内调 Skill;B 的段不泄漏到 main/A,main 的 skill 不受影响。"""
    a_id = "call_agentA"
    b_id = "call_agentB"
    recs = [
        _skill("s1", "log-error-analyzer"),                       # main 调 X
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _agent(a_id),                                            # main 起 A
        _in_sub(a_id, _agent(b_id)),                             # A 起 B
        _in_sub(b_id, _skill("sb", "code-review-auditor")),      # B 调 Y
        _in_sub(b_id, _tool_use("rb", "Read", {"file_path": "src/c.py"})),
        _in_sub(b_id, _asst_text("B analyze")),                  # B 干活
        _in_sub(a_id, _tool_result(b_id, "B done")),             # B 返回(在 A 作用域)
        _in_sub(a_id, _asst_text("A summarize")),                # A 干活
        _tool_result(a_id, "A done"),                            # A 返回(主作用域)
        _tool_use("r2", "Read", {"file_path": "app2.log"}),      # 主继续
    ]
    p = _write(tmp_path, recs)

    # (a) B 的 skill(code-review-auditor)段限定在 B 作用域
    b_spans = _slice_skill(p, "code-review-auditor")
    assert len(b_spans) == 1
    b_texts = [json.dumps(r, ensure_ascii=False) for r in b_spans[0]]
    assert any("src/c.py" in t for t in b_texts)
    assert all(r.get("parent_tool_use_id") == b_id for r in b_spans[0])

    # (b) B 的活不泄漏到 main/A:target=log-error-analyzer 不含 src/c.py / B analyze
    a_spans = _slice_skill(p, "log-error-analyzer")
    a_texts = [json.dumps(r, ensure_ascii=False) for seg in a_spans for r in seg]
    assert not any("src/c.py" in t or "B analyze" in t for t in a_texts)

    # (c) main 里调的 skill 不受影响:含 app.log / app2.log
    assert any("app.log" in t for t in a_texts)
    assert any("app2.log" in t for t in a_texts)


def test_same_skill_main_and_subagent_two_spans(tmp_path):
    """同 skill 在 main 和子 agent 各调一次→2 段(按作用域),互不污染。"""
    a_id = "call_agentA"
    recs = [
        _skill("s1", "log-error-analyzer"),                       # main 调 X
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _agent(a_id),                                            # main 起 A
        _in_sub(a_id, _skill("s2", "log-error-analyzer")),       # A 也调 X
        _in_sub(a_id, _tool_use("r2", "Read", {"file_path": "src/c.py"})),
        _in_sub(a_id, _asst_text("A analyze")),
        _tool_result(a_id, "A done"),                            # A 返回
        _tool_use("r3", "Read", {"file_path": "app2.log"}),      # main 继续
    ]
    p = _write(tmp_path, recs)
    spans = _slice_skill(p, "log-error-analyzer")
    assert len(spans) == 2
    # 按 parent_tool_use_id 区分主段 / 子段(不依赖 spans 顺序)
    main_span = next(s for s in spans if {r.get("parent_tool_use_id") for r in s} == {None})
    sub_span = next(s for s in spans if {r.get("parent_tool_use_id") for r in s} == {a_id})

    main_texts = [json.dumps(r, ensure_ascii=False) for r in main_span]
    assert any("app.log" in t for t in main_texts)
    assert any("app2.log" in t for t in main_texts)
    assert not any("src/c.py" in t or "A analyze" in t for t in main_texts)

    sub_texts = [json.dumps(r, ensure_ascii=False) for r in sub_span]
    assert any("src/c.py" in t for t in sub_texts)
    assert not any("app.log" in t or "app2.log" in t for t in sub_texts)


# ── agent-kind:按 agent 归属切(_slice_agent)。钉「扁平切、嵌套断段」行为 + 边界。 ──


def test_agent_kind_never_dispatched_returns_empty(tmp_path):
    """target 从未以 agent 字段出现 → [](audit 层 audit.py:192 据此 exit 2)。"""
    recs = [_ag("developer", "干活"), _ag("reviewer", "审")]
    assert _slice_agent(_write(tmp_path, recs), "ghost-agent") == []


def test_agent_kind_interleaved_breaks_run_into_two_spans(tmp_path):
    """扁平切、嵌套断段(钉 _slice_agent 核心行为):A 的 run 被 B 的 turn 隔开 → A 断成 2 段。

    断段无害:每段写独立 slice 文件,auditor pool findings 后 aggregate_by_instruction,
    verdict 与合成 1 段等价(见 _slice_agent docstring)。此处只钉切段行为本身。
    """
    recs = [
        {"message": {"role": "user", "content": [{"type": "text", "text": "main 起手"}]}},  # 无 agent → 不进任何段
        _ag("A", "A 第一次"),
        _ag("B", "B 插进"),
        _ag("A", "A 第二次"),
    ]
    p = _write(tmp_path, recs)

    a = _slice_agent(p, "A")
    assert len(a) == 2                                   # A 的 run 被 B 断成两段
    assert "A 第一次" in json.dumps(a[0], ensure_ascii=False)
    assert "A 第二次" in json.dumps(a[1], ensure_ascii=False)
    assert "B 插进" not in json.dumps(a, ensure_ascii=False)     # B 的内容不泄进 A 的段
    assert "main 起手" not in json.dumps(a, ensure_ascii=False)  # 无 agent 的 record 不进段

    assert len(_slice_agent(p, "B")) == 1                # B 连续 → 1 段


def test_agent_kind_nested_subagent_splits_outer_run(tmp_path):
    """嵌套:A dispatch B 后 resume A(A-pre…[B]…A-post)。A-pre/A-post 虽共享 parent_tool_use_id,
    扁平切片器不看 parent_tool_use_id,仍按连续性切 → A 断 2 段。钉「不读 parent_tool_use_id」。"""
    pA, pB = "call_A", "call_B"
    recs = [
        _ag("A", "A-pre", parent=pA),
        _ag("B", "B 嵌套", parent=pB),
        _ag("A", "A-post", parent=pA),
    ]
    p = _write(tmp_path, recs)

    a = _slice_agent(p, "A")
    assert len(a) == 2                                   # 共享 pA 也被断开(扁平,不看 parent)
    assert "A-pre" in json.dumps(a[0], ensure_ascii=False)
    assert "A-post" in json.dumps(a[1], ensure_ascii=False)
    assert "B 嵌套" not in json.dumps(a, ensure_ascii=False)


# ── root-kind:主作用域(顶层 agent)切(_slice_root)。 ──

def _slice_root(path):
    return slice_transcript(path, "", "root")   # target 忽略(root 不按名)


def test_root_kind_returns_main_scope_excluding_subagent_inside(tmp_path):
    """root = 主作用域全部 record;子 agent 内部(parent_tool_use_id≠None)排除。

    主 agent 的 turn + 它发起的 Agent dispatch tool_use/tool_result(主作用域)全收;
    子 agent 内部的 Read/文本(parent_tool_use_id=a_id)排除(那些归 kind=agent 单元)。
    """
    a_id = "call_agentA"
    recs = [
        _asst_text("MAIN_START"),                                 # 主
        _tool_use("r1", "Read", {"file_path": "app.log"}),        # 主
        _agent(a_id),                                             # 主(dispatch 调用,主作用域)
        _in_sub(a_id, _tool_use("r2", "Read", {"file_path": "src/c.py"})),   # 子内部
        _in_sub(a_id, _asst_text("SUB_MARKER")),                             # 子内部
        _tool_result(a_id, "sub done"),                           # 主(dispatch 返回,主作用域)
        _asst_text("MAIN_END"),                                   # 主
    ]
    spans = _slice_root(_write(tmp_path, recs))
    assert len(spans) == 1
    texts = json.dumps(spans[0], ensure_ascii=False)
    # 主活动全在(含 dispatch 调用与返回——主 agent 的编排动作)
    assert "MAIN_START" in texts and "app.log" in texts and "MAIN_END" in texts
    assert "sub done" in texts
    # 子 agent 内部排除
    assert "SUB_MARKER" not in texts
    assert "src/c.py" not in texts
    # 段内 record 都在主作用域(parent_tool_use_id 为空)
    assert all(_scope_of_clean(r) is None for r in spans[0])


def _scope_of_clean(rec):
    ptid = rec.get("parent_tool_use_id")
    return ptid if ptid else None


def test_root_kind_single_span_despite_subagent_interleaving(tmp_path):
    """主 record 物理上被子 agent 夹断 → root 仍单 span(过滤后连贯),非断段。"""
    a_id = "call_agentA"
    recs = [
        _asst_text("MAIN_A"),
        _agent(a_id),
        _in_sub(a_id, _asst_text("SUB_ONLY")),
        _tool_result(a_id, "done"),
        _asst_text("MAIN_B"),   # 主,物理上在子 agent 之后
    ]
    spans = _slice_root(_write(tmp_path, recs))
    assert len(spans) == 1                       # 单 span(非断段,区别于 _slice_agent)
    texts = json.dumps(spans[0], ensure_ascii=False)
    assert "MAIN_A" in texts and "MAIN_B" in texts
    assert "SUB_ONLY" not in texts


def test_root_kind_all_subagent_returns_empty(tmp_path):
    """全部 record 都在子 agent 作用域(无主作用域)→ [](防御;现实不会发生)。"""
    a_id = "call_agentA"
    recs = [
        _in_sub(a_id, _asst_text("only sub")),
        _in_sub(a_id, _tool_use("r1", "Read", {"file_path": "x"})),
    ]
    assert _slice_root(_write(tmp_path, recs)) == []


# ── scope_signals:--kind 未指定时的对称收窄锚点检测(三者一视同仁)。 ──


def test_scope_signals_skill_when_target_invoked(tmp_path):
    """transcript 调用了名为 target 的 Skill → 含 "skill" 锚点。"""
    recs = [_skill("s1", "log-error-analyzer"), _tool_use("r1", "Read", {"file_path": "app.log"})]
    assert "skill" in scope_signals([_write(tmp_path, recs)], "log-error-analyzer")


def test_scope_signals_no_skill_when_other_skill_invoked(tmp_path):
    """transcript 调的是别的 skill(非 target)→ 不含 "skill"(--kind skill 会跑空)。"""
    recs = [_skill("s1", "other-skill"), _tool_use("r1", "Read", {"file_path": "x"})]
    assert "skill" not in scope_signals([_write(tmp_path, recs)], "log-error-analyzer")


def test_scope_signals_agent_when_target_agent_present(tmp_path):
    """transcript 有 record.agent == target → 含 "agent" 锚点。"""
    recs = [
        {"message": {"role": "user", "content": [{"type": "text", "text": "main 起手"}]}},
        _ag("developer", "干活"),
    ]
    assert "agent" in scope_signals([_write(tmp_path, recs)], "developer")


def test_scope_signals_root_when_subagent_dispatched(tmp_path):
    """transcript 有子 agent 作用域(parent_tool_use_id 非空)→ 含 "root" 锚点。"""
    a_id = "call_A"
    recs = [
        _asst_text("main"), _agent(a_id),
        _in_sub(a_id, _asst_text("sub")), _tool_result(a_id, "done"),
    ]
    assert "root" in scope_signals([_write(tmp_path, recs)], "whatever-name-ignored-for-root")


def test_scope_signals_plain_session_no_anchors(tmp_path):
    """纯主 agent、没调 target skill、没 target agent、没子 agent → 空集(无收窄锚点)。"""
    recs = [
        _asst_text("main 干活"),
        _tool_use("r1", "Read", {"file_path": "x"}),
        _tool_result("r1", "y"),
    ]
    assert scope_signals([_write(tmp_path, recs)], "my-skill") == set()


def test_scope_signals_empty_name_only_root_possible(tmp_path):
    """name 空(skill/agent 信号无意义)→ 即便调了 skill / 有 agent 段也只检出 root。"""
    a_id = "call_A"
    recs = [_skill("s1", "foo"), _ag("bar", "x"), _agent(a_id), _in_sub(a_id, _asst_text("sub"))]
    assert scope_signals([_write(tmp_path, recs)], "") == {"root"}


def test_scope_signals_multi_transcript_union(tmp_path):
    """多 transcript 取并集:任一含锚点即提示。"""
    a_id = "call_A"
    t1 = tmp_path / "a.jsonl"
    t1.write_text(json.dumps(_skill("s1", "foo")), encoding="utf-8")
    t2 = tmp_path / "b.jsonl"
    t2.write_text("\n".join(json.dumps(r) for r in [_agent(a_id), _in_sub(a_id, _asst_text("sub"))]),
                  encoding="utf-8")
    sig = scope_signals([t1, t2], "foo")
    assert "skill" in sig and "root" in sig and "agent" not in sig


def test_scope_signals_unreadable_file_skipped(tmp_path):
    """读失败的 transcript 不炸、贡献空(不影响其它 transcript 信号)。"""
    a_id = "call_A"
    good = tmp_path / "good.jsonl"
    good.write_text("\n".join(json.dumps(r) for r in [_agent(a_id), _in_sub(a_id, _asst_text("sub"))]),
                    encoding="utf-8")
    ghost = tmp_path / "ghost.jsonl"   # 不存在
    sig = scope_signals([ghost, good], "nope")
    assert sig == {"root"}


# ---- build_subagent_tool_trace: A1 旁路证据源(root 判定的"缺席≠未发生"洗冤通道) ----

def test_build_subagent_tool_trace_agent_attribution_via_dispatch(tmp_path):
    """cc 原生路径:子代理 record 的角色名从主作用域 Agent/Task tool_use 的
    id → subagent_type 映射取(其 parent_tool_use_id 指向该 tool_use)。"""
    a_id = "agent-1"
    recs = [
        _agent(a_id),  # input.subagent_type = "general-purpose"(默认 helper)
        _in_sub(a_id, _tool_use("tu1", "Bash", {"command": "nm -D libdemo_op.so"})),
        _asst_text("主作用域文本"),  # 非 tool_use,不入 trace
    ]
    p = _write(tmp_path, recs)
    trace = build_subagent_tool_trace(p)
    assert len(trace) == 1
    assert trace[0].name == "Bash"
    assert trace[0].agent == "general-purpose"  # 派发映射归属
    assert trace[0].turn is not None  # 全局 turnIndex 已 stamp


def test_build_subagent_tool_trace_rec_agent_wins(tmp_path):
    """insight structured 路径:rec.agent(角色名)优先于派发映射
    (其 parent_tool_use_id 是 subagentSessionId,查映射本就落空)。"""
    a_id = "ses_sub_123"
    recs = [
        # 主作用域有一个 Task 派发(id 与 a_id 无关——insight 路径的 ptid 非 tool_use id)
        _tool_use("tu_root", "task", {"subagent_type": "verifier", "prompt": "验收"}),
        {"agent": "code-verifier", "parent_tool_use_id": a_id,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu2", "name": "Read",
              "input": {"filePath": "docs/x/references/demo-contract.md"}}]}},
    ]
    p = _write(tmp_path, recs)
    trace = build_subagent_tool_trace(p)
    assert len(trace) == 1
    assert trace[0].agent == "code-verifier"  # rec.agent 优先
    assert trace[0].input == {"filePath": "docs/x/references/demo-contract.md"}


def test_build_subagent_tool_trace_empty_when_no_subagents(tmp_path):
    """纯主作用域 transcript(无 parent_tool_use_id 非空 record)→ 空 trace。"""
    p = _write(tmp_path, [_asst_text("只有主 agent"), _tool_use("tu1", "Bash", {"command": "ls"})])
    assert build_subagent_tool_trace(p) == []


def test_build_subagent_tool_trace_task_lowercase_dispatch(tmp_path):
    """派发工具名大小写不敏感:cc 代理捕获用小写 task、canonical 用 Task/Agent,全认。"""
    recs = [
        _tool_use("d1", "task", {"subagent_type": "developer", "prompt": "干活"}),
        _in_sub("d1", _tool_use("tu1", "Bash", {"command": "mkdir -p docs/x"})),
    ]
    p = _write(tmp_path, recs)
    trace = build_subagent_tool_trace(p)
    assert trace and trace[0].agent == "developer"


# ---- 层3:派发-回报对(digest 收集 + 段落渲染) ----

def test_digest_collects_dispatch_returns(tmp_path):
    """task tool_use 与 tool_result 配对收集进 digest.dispatch_returns(层3 证据单元)。"""
    from sift.evals.transcript import _digest_from_records, stamp_turn_indices
    records = [
        _tool_use("tu_1", "task", {"subagent_type": "verifier", "prompt": "验收 Task 47"}),
        _tool_result("tu_1", "FAILED: 254/275"),
        _tool_use("tu_2", "task", {"subagent_type": "developer", "prompt": "建目录"}),
        # tu_2 无 tool_result(回报缺失也收,result_len=0)
    ]
    stamp_turn_indices(records)
    digest = _digest_from_records(records, locate_turns=True)
    assert len(digest.dispatch_returns) == 2
    d1, d2 = digest.dispatch_returns
    assert d1.role == "verifier" and d1.result_head.startswith("FAILED") and d1.result_len == len("FAILED: 254/275")
    assert d1.dispatch_turn is not None and d1.result_turn is not None
    assert d2.role == "developer" and d2.result_len == 0 and d2.result_head == ""
    # 非 dispatch 工具不入
    recs2 = [_tool_use("tu_3", "Bash", {"command": "ls"}), _tool_result("tu_3", "out")]
    stamp_turn_indices(recs2)
    assert _digest_from_records(recs2).dispatch_returns == []


def test_dispatch_pairs_section_render_and_truncate():
    """段落渲染:统计行 + 失败标记计数 + trivial 压缩;超限裁剪保失败行。"""
    from sift.evals.transcript import DispatchReturn
    from sift.audit.auditor import _DISPATCH_PAIRS_LIMIT, _dispatch_pairs_section

    def _p(role, prompt, result, dt=1, rt=2):
        return DispatchReturn(dispatch_turn=dt, result_turn=rt, role=role,
                              prompt_head=prompt, result_head=result, result_len=len(result))

    # 小规模:全渲染
    pairs = [_p("verifier", "验收 Task 47", "FAILED: 254/275"),
             _p("developer", "建目录", "DONE")]
    sec = _dispatch_pairs_section(pairs)
    assert "统计:派发 2 次(verifier 1 / developer 1)" in sec
    assert "回报含失败字样 1 份" in sec
    assert "[T1→T2] verifier" in sec and "FAILED: 254/275" in sec
    assert "(子代理产出)" in sec

    # 大规模(超限):失败行保留、trivial 压计数、中段省略
    many = ([_p("verifier", f"验收 Task {i}", f"FAILED: 用例 {i} 超阈值", dt=i, rt=i + 1)
             for i in range(5)]
            + [_p("developer", f"开发任务 {i} 长prompt" * 10, "x" * 150, dt=100 + i, rt=101 + i)
               for i in range(120)])
    sec2 = _dispatch_pairs_section(many)
    assert len(sec2) <= _DISPATCH_PAIRS_LIMIT + 600  # 封顶(允许头部/省略行余量)
    assert "FAILED: 用例 0 超阈值" in sec2           # 失败行全保
    assert "省略" in sec2 or "另有" in sec2           # 压缩标记存在
    assert sec2.count("[T1") >= 1

    # 评审修复①:失败判定只看回报窗口——prompt 含「失败」但回报成功的长文本派发
    # (修 bug 工作流常态)不算失败行,不绕过预算 → 封顶不被打破(评审实测旧版
    # 100 条此形态派发可把段落撑到 19295 字符,超限 2.4 倍)。
    bugfix = [_p("developer", f"修复失败的用例 {i} " * 10, "全部通过 " * 30, dt=i, rt=i + 1)
              for i in range(100)]
    sec3 = _dispatch_pairs_section(bugfix)
    assert "回报含失败字样 0 份" in sec3              # prompt 的「失败」不计入
    assert len(sec3) <= _DISPATCH_PAIRS_LIMIT + 600  # 封顶守住

    # 评审修复②:失败行也守预算——超量失败回报压「另有 M 份」计数,不无条件全留
    failed_many = [_p("verifier", f"验收 Task {i}", f"FAILED: 用例 {i} 超阈值 " * 20,
                      dt=i, rt=i + 1) for i in range(60)]
    sec4 = _dispatch_pairs_section(failed_many)
    assert len(sec4) <= _DISPATCH_PAIRS_LIMIT + 600  # 封顶是不变式
    assert "份含失败字样的回报未展开" in sec4         # 超预算失败行压计数
    assert "FAILED: 用例 0 超阈值" in sec4           # 预算内失败行仍在(优先保留)

    # 空 → 空段(整段不进 prompt)
    assert _dispatch_pairs_section(None) == ""
    assert _dispatch_pairs_section([]) == ""


def test_result_windows_head_tail():
    """层3-改进2:回报头+尾窗口——剥带属性的 <task>/<task_result> 包装,长文本取
    头+尾(验收结论惯例在尾部),短文本全进 head、tail 为空。"""
    from sift.evals.transcript import _result_windows
    long_ret = ('<task id="ses_x" state="completed">\n<task_result>\n准备工作中。'
                + "过程细节" * 40 + "\n状态:❌失败,7/23 用例达标\n</task_result>\n</task>")
    h, t = _result_windows(long_ret)
    assert "task" not in h and "task" not in t          # 包装已剥(含带属性形态)
    assert h.startswith("准备工作中。")
    assert "❌失败,7/23" in t                            # 尾部结论必然可见
    h2, t2 = _result_windows("DONE")
    assert (h2, t2) == ("DONE", "")                     # 短文本不劈两半
