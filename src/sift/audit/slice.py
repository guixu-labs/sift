"""按声明单元(skill / agent / root)切 cc/structured transcript 段——kind-parametric。

三套锚点(由 kind 选其一,**不并集**):
  - ``kind="root"``:顶层作用域锚点。target 忽略——主作用域全部 record(parent_tool_use_id
    为空),单 span。主 agent 自己的 turn + 它发起的 Agent dispatch tool_use/tool_result(都在
    主作用域);子 agent 内部(parent_tool_use_id 指向某 Agent 调用)排除,归各自 ``kind="agent"``
    单元。审主 agent / 编排者打底(其声明常是 workflow 级 SKILL.md)。
  - ``kind="skill"``:Skill 调用锚点。每条 record 按 parent_tool_use_id 归作用域(主/某子 agent);
    作用域内 last-invoked-owns(每个 Skill 调用起段,到同作用域下一个 Skill 调用或作用域结束)。
  - ``kind="agent"``:agent 归属锚点。record.agent == target 的连续 run(被 dispatch 的 agent 的执行段)。

为什么必须按 kind 分路、不并集:一个名字可能既是 skill(被 Skill 调用)又是 agent(被 dispatch)、
两份不同声明正文(dual-nature,如 cannbot 的 st-verifier——同名 skill 是参考材料、agent 是角色声明)。
并集会把 agent 的执行段塞进 skill 正文的对账 → 对象错位。kind 让调用方明确审的是哪一面,各切各的。

opencode session **不走本模块**(它无 Skill tool_use、单锚点 info.agent),见
``evals/opencode.py`` 的 ``slice_opencode_by_agent``——opencode 上 skill/agent 都按 agent 字段切,
kind 对它无意义(CLI 的 ``--kind`` 对 opencode transcript 是 no-op,照常按 agent 字段切)。

判定层(rebuild/judge/aggregate)不 import 本模块——本模块只产 record 段列表。
"""
from __future__ import annotations
import json
from pathlib import Path

from sift.evals.transcript import _summary_limit_for, stamp_turn_indices
from sift.models import ToolCallRecord


def _records(path: Path) -> list[dict]:
    records: list[dict] = []
    for l in path.read_text(encoding="utf-8").splitlines():
        l = l.strip()
        if not l:
            continue
        try:
            records.append(json.loads(l))
        except json.JSONDecodeError:
            continue
    # stamp 全局 _turn_index:切片产出 dict 子集会被 JSON 序列化进 slice 文件,
    # rebuild 读回时 _turn_index 经 JSON 往返保留 → 保住原全局位置(后续 [T{idx}] 引用关键)。
    stamp_turn_indices(records)
    return records


def _content_blocks(rec: dict) -> list:
    """取 record.message.content 为 list;字符串或缺失→空列表。"""
    content = (rec.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _skill_name_of(rec: dict) -> str | None:
    """若 rec 含 Skill tool_use,返回 input.skill(剥命名空间前缀),否则 None。"""
    for b in _content_blocks(rec):
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
            full = (b.get("input") or {}).get("skill") or ""
            return full.rsplit(":", 1)[-1]  # "superpowers:writing-skills" → "writing-skills"
    return None


def _tool_result_ids(rec: dict) -> set[str]:
    """rec 内所有 tool_result 块的 tool_use_id 集合。"""
    return {
        b.get("tool_use_id", "")
        for b in _content_blocks(rec)
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")
    }


def _find_agent_ids(records: list[dict]) -> set[str]:
    """收集所有 Agent tool_use 的 id(子 agent 作用域标记)。"""
    ids: set[str] = set()
    for rec in records:
        for b in _content_blocks(rec):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent":
                tid = b.get("id", "")
                if tid:
                    ids.add(tid)
    return ids


def _scope_of(rec: dict) -> str | None:
    """record 的作用域:parent_tool_use_id 非空→该子 agent;否则 None(主)。"""
    ptid = rec.get("parent_tool_use_id")
    return ptid if ptid else None


def invoked_skills(transcript_path: Path) -> list[str]:
    """transcript 里被调用的所有 skill 名(剥命名空间前缀、去重、按首次出现顺序)。

    供多 skill 检测:init --from-transcript 不切分时,若 transcript 涉及多 skill 会串味
    (把别 skill 的活动当本 skill 挖)→ CLI 层据此警告、建议 --kind skill。
    """
    seen: list[str] = []
    for rec in _records(transcript_path):
        name = _skill_name_of(rec)
        if name and name not in seen:
            seen.append(name)
    return seen


def invoked_agents(transcript_path: Path) -> list[str]:
    """transcript 里出现过的所有 agent 名(record.agent、去重、按首次出现顺序)。

    供多 agent 检测:init --from-transcript 不切分时,若 transcript 涉及多 agent
    (主 agent dispatch 了子 agent、或多个 agent 各自的段)会串味(把别 agent 的活动
    当本 agent 挖)→ CLI 层据此警告、建议 --kind agent。与 ``invoked_skills`` 对称。
    """
    seen: list[str] = []
    for rec in _records(transcript_path):
        name = rec.get("agent")
        if name and name not in seen:
            seen.append(name)
    return seen


def scope_signals(transcript_paths: list[Path], name: str) -> set[str]:
    """扫 cc/structured transcript(非 opencode),返回可收窄的作用域锚点集合。

    供 audit CLI 在 ``--kind`` 未指定时**对称**提示:transcript 里有哪些可切的锚点,
    让用户知道带 ``--kind`` 能精确收窄(skill/agent/root 三者一视同仁)。opencode 不走本模块
    ——单锚点 ``info.agent``、无父子作用域,``--kind skill/agent`` 对它无意义、照按 agent 字段切。

    - ``"skill"``:有 Skill tool_use 且其名(剥命名空间后)== name(--kind skill 会切到它的活跃段)
    - ``"agent"``:有 ``record.agent == name`` 的段(--kind agent 切被 dispatch 的该 agent 执行段)
    - ``"root"``:有子 agent 作用域(任意 ``parent_tool_use_id`` 非空 → 主 agent 派发过子 agent;
      --kind root 切主作用域、排除子 agent 内部)

    name 为空时(skill/agent 信号无意义)只可能检出 ``"root"``。单扫一趟三锚点同检;多 transcript
    取并集。读失败 / 空 transcript → 贡献空(不影响其它 transcript 的信号)。
    """
    signals: set[str] = set()
    for tp in transcript_paths:
        try:
            records = _records(tp)
        except OSError:
            continue
        for rec in records:
            if name and _skill_name_of(rec) == name:
                signals.add("skill")
            if name and rec.get("agent") == name:
                signals.add("agent")
            if _scope_of(rec) is not None:
                signals.add("root")
    return signals


def _slice_skill_invoke(records: list[dict], target: str) -> list[list[dict]]:
    """kind='skill':按 Skill 调用切——scope-aware last-invoked-owns。

    每个 Skill(target) 调用起段,到同作用域下一个 Skill 调用或作用域结束;按 parent_tool_use_id
    归作用域。主作用域的 Agent tool_result 触发 depth-1 closure(子 agent 作用域结束)。
    """
    agent_ids = _find_agent_ids(records)

    # 作用域内状态:active = 当前生效 skill 名;cur = 当前段的 record 下标列表。
    scope_state: dict[str | None, dict] = {}
    skill_spans: list[list[int]] = []

    def _flush(state: dict) -> None:
        if state.get("active") == target and state.get("cur"):
            skill_spans.append(state["cur"])

    for i, rec in enumerate(records):
        # Unmatched capture has no reliable Skill invocation association.  Keep
        # it for loose/root/agent audit, but do not silently assign it to the
        # last active skill merely because it was serialized nearby.
        if rec.get("_sift_standalone_capture"):
            continue
        scope = _scope_of(rec)
        state = scope_state.setdefault(scope, {"active": None, "cur": []})

        name = _skill_name_of(rec)
        if name is not None:
            # 同作用域新 Skill 调用:关掉当前 target 段(如有),再切 active
            if state["active"] == target and state["cur"]:
                _flush(state)
            state["active"] = name
            state["cur"] = [i] if name == target else []
            continue

        # 非 Skill record:若当前作用域 active 是 target,归入段
        if state["active"] == target:
            state["cur"].append(i)

        # depth-1 closure only:主作用域里的 Agent tool_result 结束对应子 agent 作用域。
        # 嵌套子 agent(深度≥2)的 Agent tool_result 出现在父作用域(scope≠None),不在此
        # 触发;它们在 transcript 末统一 flush——无害,因为已返回 agent 的 scope 下不再
        # 出现 record(后续 record 归父作用域)。
        if scope is None:
            for tid in _tool_result_ids(rec):
                if tid in agent_ids:
                    sub = scope_state.get(tid)
                    if sub:
                        _flush(sub)
                        sub["active"] = None
                        sub["cur"] = []

    # transcript 末:刷掉所有仍活跃的 skill 段
    for state in scope_state.values():
        _flush(state)

    return [[records[k] for k in span] for span in skill_spans]


def _slice_agent(records: list[dict], target: str) -> list[list[dict]]:
    """kind='agent':record.agent == target 的**连续** run(被 dispatch 的 agent 的执行段)。

    纯 agent(无 Skill 调用锚点,如 developer)靠此切;insight 给子 agent turn 吐 agent=agentName,
    structured reader(``structured.py``)透传到 cc-record 的 agent 字段。

    **扁平切、嵌套断段**(有意,非 bug;边界见 ``tests/test_slice.py`` 的 agent-kind 用例):

    - 只按 ``agent == target`` 取连续 record,**不读 parent_tool_use_id**。target 的 run 中若嵌套
      了别的 agent 的 turn(A dispatch B 后 resume A,record 序 A…B…A),A 的 run 被 B 那段断成
      **两个 span**——每段仍是 A 的真实活动,只被 B 的 turn 隔开。
    - 这是 insight 数据模型下的**被迫选择**:`buildStructuredRecords` 不合成 Agent tool_use(只给
      turn 打 agent + parent_tool_use_id),故无「agent 名 → dispatch id」映射,无法像 skill 路
      那样按作用域归段。可选优化:在 ``agent==target`` 的 record 里按 parent_tool_use_id groupby,
      把同一次 dispatch 的 A-pre/A-post 合回一段——但**结果等价**(见下)。
    - **断段无害**:每段写独立 slice 文件喂 auditor,findings 全量 pool 后 ``aggregate_by_instruction``
      按指令聚合(``auditor.py``)。A 的真实活动照样全审、agent .md 声明又是外部常量,故 pool 后
      的 verdict 与合成 1 段等价。
    """
    agent_spans: list[list[int]] = []
    cur: list[int] = []
    for i, rec in enumerate(records):
        if rec.get("agent") == target:
            cur.append(i)
        elif cur:
            agent_spans.append(cur)
            cur = []
    if cur:
        agent_spans.append(cur)
    return [[records[k] for k in span] for span in agent_spans]


def _slice_root(records: list[dict]) -> list[list[dict]]:
    """kind='root':主作用域全部 record(parent_tool_use_id 为空),单 span。

    root = 顶层 agent 的完整执行流:主 agent 自己的 turn + 它发起的 Agent dispatch
    tool_use / tool_result(都在主作用域),**不含**子 agent 内部 record(那些
    parent_tool_use_id 指向某 Agent 调用,归各自 ``kind="agent"`` 单元审)。

    语义:审主 agent 的**编排**——它 dispatch 了谁、何时、怎么接返回,而非子 agent 干了啥。
    单 span:主 record 在原序列里可能被子 agent record 物理夹断,但过滤后是连贯的主执行流,
    作为一个 span 喂 auditor(区别于 ``_slice_agent`` 的同名 run 断段)。

    主作用域一定非空(至少起头 user turn 在主);防御性:全子 agent → [](audit 层 exit 2)。
    """
    root_recs = [r for r in records if _scope_of(r) is None]
    return [root_recs] if root_recs else []


def build_subagent_tool_trace(transcript_path: Path, *, locate_turns: bool = True) -> list[ToolCallRecord]:
    """A1 旁路证据源:子代理作用域全部工具调用(root 判定的"缺席≠未发生"洗冤通道)。

    只收集**子代理作用域**的 tool_use(parent_tool_use_id 非空),root 作用域已在
    判定主路径(rebuild 的 root trace)里,不重复。消费方:auditor._judge_conditional
    的第二遍确定性匹配——条件类条目 root trace 未命中时在这里再找一遍,命中 →
    PASS(evidence 标注"子代理 <role> 经派发执行")。**只做洗清嫌疑的确定性核对**:
    不进 LLM prompt、不改 root 切片语义、不喂步骤/禁令判官(那些仍只看 root 视角)。

    执行者角色名(agent 字段)归属优先级:
    1. ``rec.agent``——insight structured 路径按 turn 吐的角色名(verifier/developer…);
    2. 派发映射——cc 原生路径,主作用域 Agent/Task tool_use 的 ``id → subagent_type``
       (子代理 record 的 parent_tool_use_id 指向该 tool_use);
    3. 都无 → 空串(evidence 显示"子代理"不带角色)。

    ``turn`` 为全量 stamp 的全局 turnIndex(与 root 切片同一体系,aux 命中证据可
    用 [T{idx}] 对回 insight turnIndex)。
    """
    records = _records(transcript_path)
    # cc 原生路径的派发映射:主作用域 Agent/Task tool_use 的 id → 角色(subagent_type)。
    # insight structured 路径的 parent_tool_use_id 是 subagentSessionId(非 tool_use id),
    # 映射查不到——但该路径 rec.agent 已带角色名,走优先级 1。
    role_by_id: dict[str, str] = {}
    for rec in records:
        if _scope_of(rec) is not None:
            continue
        for b in _content_blocks(rec):
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and str(b.get("name") or "").lower() in ("agent", "task")):
                tid = b.get("id", "")
                role = ((b.get("input") or {}).get("subagent_type")
                        or (b.get("input") or {}).get("agent") or "")
                if tid and role:
                    role_by_id[tid] = str(role)
    trace: list[ToolCallRecord] = []
    for rec in records:
        ptid = _scope_of(rec)
        if ptid is None:
            continue
        agent = rec.get("agent") or role_by_id.get(ptid, "")
        turn_idx = rec.get("_turn_index")
        for b in _content_blocks(rec):
            if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                continue
            name = b.get("name", "")
            inp = b.get("input") or {}
            trace.append(ToolCallRecord(
                name=name,
                input_summary=json.dumps(inp, ensure_ascii=False)[:_summary_limit_for(name)],
                input=inp or None,
                turn=turn_idx if locate_turns else None,
                agent=str(agent or ""),
            ))
    return trace


def slice_transcript(transcript_path: Path, target: str, kind: str) -> list[list[dict]]:
    """切 target 声明单元的执行段,按 kind 选锚点(**不并集**,dual-nature 靠 kind 分路)。

    - ``kind="root"``:顶层作用域(parent_tool_use_id 为空的全部 record,单 span;target 忽略)。
    - ``kind="skill"``:Skill 调用锚点(scope-aware last-invoked-owns;每个 Skill(target) 调用起段,
      到同作用域下一个 Skill 调用或作用域结束)。
    - ``kind="agent"``:agent 归属锚点(record.agent == target 的连续 run)。

    target 从未以所选锚点出现 → 返回 [](root 不按 target 切、作用域通常非空;全子 agent session 时
    _slice_root 亦返 [],见上)。
    """
    records = _records(transcript_path)
    if not records:
        return []
    if kind == "root":
        segments = _slice_root(records)
    elif kind == "skill":
        segments = _slice_skill_invoke(records, target)
    elif kind == "agent":
        segments = _slice_agent(records, target)
    else:
        raise ValueError(f"kind 必须是 'root' / 'skill' / 'agent',收到 {kind!r}")

    # Structured v2 keeps bundle metadata once on the first canonical record.
    # Agent/skill slicing can exclude that record, so copy it once to the first
    # record of every emitted segment.  This preserves input hash, coverage and
    # warnings without repeating them on every record.
    bundle_meta = next(
        (record.get("_sift_bundle_meta") for record in records if record.get("_sift_bundle_meta")),
        None,
    )
    if bundle_meta:
        for segment in segments:
            if segment:
                segment[0]["_sift_bundle_meta"] = bundle_meta
    return segments
