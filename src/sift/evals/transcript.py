"""Transcript → SessionDigest:把会话 transcript 解析成结构化 digest。纯解析,无 LLM。

**canonical 重建路径的归宿**:本模块持有共享重建器 ``_digest_from_records`` + 工具名归一表
``_NAME_MAP``/``_norm_tool``。三种 transcript importer 都先归一到 **cc-record 形**
(``{message:{role, content:[block]}, parent_tool_use_id}`` —— 仓库的 canonical 中间表示),
再喂同一个重建器,重建逻辑单处定义、三源一致:

- cc —— ``_records_from_cc``:读 JSONL,近恒等产生 cc-record。
- structured —— ``structured.load_structured_records``:结构化 JSON → cc-record(工具名归一)。
- opencode —— ``opencode._records_from_opencode``:opencode export → cc-record(工具名归一 +
  camelCase→snake_case 字段映射 + 合成 tool_use_id)。

抹平格式差异后,Read/Write/Edit/MultiEdit 重建 + demoted/unreconstructed 推导对三源一致。
Read 结果的 opencode 形清洗(剥 ``<content>`` 包裹 + ``N: `` 行号)也归本模块 ``_clean_read_result``、
在重建器里按 ``<content>`` 标记触发——structured(insight 等)直喂 opencode 形 read 也自动洗净,
cc / 裸内容不受 ``N: `` 正则误伤。
cc 对外入口 ``build_digest`` = ``_digest_from_records(_records_from_cc(path))``(薄封装)。

**内部 sentinel ``_turn_index``**:各 importer 加载时给每条 cc-record 标全局 1-based
``_turn_index``(``stamp_turn_indices``)。下划线前缀 = 非 transcript 原生字段(纯内部标记),
经 JSON 序列化往返保留——切片产出的 dict 子集写进 slice 文件、rebuild 读回时该字段存活,
保住原全局位置。重建器 ``_digest_from_records`` 消费它:**默认(locate_turns=False)回到
no-seq 形态**——不加 ``[T{idx}] `` 前缀、``ToolCallRecord.turn`` 置 ``None``;``locate_turns=True``
时才给每条 assistant record 的文本加 ``[T{idx}] `` 前缀(per-record,非 per-block)、``turn`` 记
所属 turnIndex(后续 finding 引用 turn 的基线,切片后仍是原全局号、不重编号)。``_turn_index``
sentinel 总 stamp(切片往返用),仅控渲染。opencode 切片经 message 子集往返、需重建后 re-stamp,
见 ``opencode._records_from_opencode`` 的已知限制说明。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from sift.errors import InputError
from sift.models import FileEffectRecord, ProvenanceEvent, ToolCallRecord

_LINE_NUM_RE = re.compile(r"^\s*\d+\t", re.MULTILINE)

_BASH_WRITE_RE = re.compile(
    r"""(?:>>?\s*|tee\s+(?:-a\s+)?)['"]?([^\s'";|&]+)"""
)

# A3(dispatch prompt 截断):task/Agent 派发类 tool_call 的 input(prompt/要求清单)是
# 编排会话里主 agent 行为的核心证据,统一 [:200] 会掐掉要求清单 → 判官"看不见"派发
# 内容(缺席式误判;实测样本里开发日志追加要求位于派发 prompt 第 ~600 字符处,被
# 判"未要求")。
# 默认 2000(实验结论:11 会话 5 档位对比——2000 覆盖 92% 派发内容,token 2.2-4.0x、
# 墙钟不变;A3 型误判 500 档即翻案,"派发指令预置降级理由"类真实问题 ≥1000 才可见)。
# env SIFT_TASK_SUMMARY_LIMIT 可覆盖(=200 回旧行为,实验消融用);其他工具保持 200。
_DISPATCH_TOOL_NAMES = {"task", "agent"}
_DISPATCH_SUMMARY_LIMIT = 2000


def _summary_limit_for(tool_name: str) -> int:
    if tool_name.strip().lower() not in _DISPATCH_TOOL_NAMES:
        return 200
    raw = os.environ.get("SIFT_TASK_SUMMARY_LIMIT", "")
    try:
        return max(200, int(raw)) if raw else _DISPATCH_SUMMARY_LIMIT
    except ValueError:
        return _DISPATCH_SUMMARY_LIMIT


class FileContent(BaseModel):
    path: str
    content: str


class Command(BaseModel):
    cmd: str
    stdout: str


_DISPATCH_RESULT_TAG_RE = re.compile(r"</?task(?:_result)?[^>]*>")


def _result_windows(text: str, head: int = 80, tail: int = 60) -> tuple[str, str]:
    """回报文本的头+尾窗口(层3-改进2)。剥 ``<task>``/``<task_result>`` 包装标签,
    压平空白后取头 ``head`` + 尾 ``tail`` 字。验收类回报的结论惯例在**尾部**
    ("状态:✅通过"/"FAILED X/Y"收尾),只截头会把结论掐掉 → 判官锚不到验收结论。"""
    t = _DISPATCH_RESULT_TAG_RE.sub("", text or "")
    t = " ".join(t.split())
    if len(t) <= head + tail:
        return t, ""
    return t[:head], t[-tail:]


class DispatchReturn(BaseModel):
    """一次派发(task/Agent tool_use)及其回报(tool_result)的对——层3 证据单元。

    cc 原生 transcript 里派发的 tool_result 就是主作用域记录(主 agent 亲眼看到回报
    才能决策推进/修复),本模型把它从 id2result 里按派发配对拎出来,供 step 判官构建
    "派发-回报记录"段(A1 step 类缺席误判的洗冤证据 + "传递≠达成"的验收结论依据)。
    """
    dispatch_turn: int | None = None  # 派发 tool_use 所属全局 turnIndex
    result_turn: int | None = None    # 回报 tool_result 所属全局 turnIndex
    role: str = ""                    # 被派发角色(input.subagent_type / agent)
    prompt_head: str = ""             # 派发 prompt 头(≤200 字)
    result_head: str = ""             # 回报头(≤400 字,已剥 <task> 包装)
    result_tail: str = ""             # 回报尾(≤60 字,验收结论惯例在尾部)
    result_len: int = 0               # 回报全长(裁剪统计用)


class SessionDigest(BaseModel):
    skill_used: str | None = None
    user_turns: list[str] = Field(default_factory=list)
    input_files: list[FileContent] = Field(default_factory=list)
    produced_files: list[FileContent] = Field(default_factory=list)
    dispatch_returns: list[DispatchReturn] = Field(default_factory=list)
    unreconstructed: list[str] = Field(default_factory=list)
    commands: list[Command] = Field(default_factory=list)
    # 以下两字段为 audit 命令重建(工具指令对账 / 文本回放)预留,默认空,非侵入。
    tool_trace: list[ToolCallRecord] = Field(default_factory=list)
    transcript_text: str = ""
    evidence_refs: dict[str, FileEffectRecord] = Field(default_factory=dict)
    evidence_warnings: list[str] = Field(default_factory=list)
    evidence_metadata: dict = Field(default_factory=dict)
    provenance_timeline: list[ProvenanceEvent] = Field(default_factory=list)

    @property
    def manifest(self) -> set[str]:
        return {f.path for f in self.input_files} | {f.path for f in self.produced_files}


def _strip_line_numbers(text: str) -> str:
    return _LINE_NUM_RE.sub("", text)


# opencode / insight 实存的 read 输出行号形:"N: "(冒号空格;cc 是 "\d+\t" 见 _LINE_NUM_RE)。
# 用 [ \t] 不用 \s,以免跨行吃换行。归本模块:_clean_read_result 按 <path> 起手 + <content> 包裹触发。
_OC_LINE_NUM_RE = re.compile(r"^[ \t]*\d+:[ \t]?", re.MULTILINE)


def _clean_read_result(text: str) -> str:
    """归一 Read 工具结果到干净文件内容(三源通用,canonical 重建器 ``_digest_from_records`` 调用)。

    按输出**起手** + ``<content>`` 包裹区分格式:opencode read 恒以 ``<path>`` 起手、cc read
    恒以行号 ``\\d+\\t`` 起手——首字符就分化,比"任意位置含某字面"稳得多:
    - opencode 形(``<path>…</path>\\n<type>…</type>\\n<content>…</content>`` + ``N: `` 行号)→
      剥 ``<content>`` 取内文 + 去 ``N: `` 行号 + 去前导换行;
    - cc 形(``\\d+\\t`` 行号)或裸内容 → 只去 cc tab 行号(``_strip_line_numbers``)。

    靠"``<path>`` 起手"识别 opencode,cc / 干净内容不会被 ``N: `` 正则误伤——cc 正文里合法的
    "1: ..." 文本、乃至正文含 ``<content>``/``<path>`` 字面的文件都保留(只要不以 ``<path>``
    起手)。structured(insight 等)直喂 opencode 形 read,经此与 opencode 原生路等价;裸 ``N:``
    (无包裹)走 cc 路保留。

    格式耦合(已知局限):判定绑 opencode ``<path>`` 起手这一字面。根因是 structured 路径可能
    喂 opencode 形 read、共享层无法靠来源判定只能嗅探内容——彻底解耦要给 canonical record 加
    来源标记位(大 refactor),opencode 无改格式迹象前不值,留心。
    """
    if text.startswith("<path>") and "<content>" in text and "</content>" in text:
        i = text.find("<content>") + len("<content>")
        j = text.rfind("</content>")
        if 0 <= i <= j:
            text = text[i:j]
        return _OC_LINE_NUM_RE.sub("", text).lstrip("\n")
    return _strip_line_numbers(text)


def _result_text(result_content) -> str:
    if isinstance(result_content, str):
        return result_content
    if isinstance(result_content, list):
        return "\n".join(
            b.get("text", "") for b in result_content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _detect_root(paths: list[str]) -> str:
    paths = [p for p in paths if p]
    if not paths:
        return ""
    if len(paths) == 1:
        return str(Path(paths[0]).parent) or "/"
    try:
        cp = os.path.commonpath(paths)
    except ValueError:
        return ""
    return os.path.dirname(cp) if os.path.isfile(cp) else cp


def _rel(path: str, root: str) -> str:
    if not path or not root:
        return path
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _build_timestamp_sort_key(value: str | None) -> tuple[int, float, str]:
    """ISO-8601 chronological key; malformed/missing values sort deterministically last."""
    if not value:
        return (2, 0.0, "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (0, parsed.timestamp(), "")
    except ValueError:
        return (1, 0.0, value)


# 工具名归一表:producer(opencode / insight 等)多用小写,cc digest 的分支认大写。
# 未命中的名字原样透传(只进 tool_trace,不影响重建)。
# 归属本模块(canonical 重建器 _digest_from_records 所在),各 importer 共用一份。
_NAME_MAP = {
    "skill": "Skill",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "multiedit": "MultiEdit",
    "bash": "Bash",
}


def _norm_tool(name: str) -> str:
    return _NAME_MAP.get((name or "").lower(), name or "")


def stamp_turn_indices(records: list[dict]) -> None:
    """给每条 record 标全局 _turn_index(1-based,若未带)。

    切片/JSON 序列化往返保留:已带 _turn_index(切片文件读回)不覆盖,保原全局值;
    首次加载(原 transcript)按顺序 stamp。opencode 路径重建 dict 时须显式拷此字段
    (或在重建后 stamp 重建列表——_records_from_opencode 取后者:按 out 位置 stamp,
    与 cc/structured 同款 position 语义、且重建 dict 无字段丢失风险)。
    """
    for i, r in enumerate(records, 1):
        if "_turn_index" not in r:
            r["_turn_index"] = i


def _records_from_cc(transcript_path: Path) -> list[dict]:
    """读 Claude Code session JSONL → record 列表（跳过空行 / 坏 JSON 行）。

    canonical 记录 = cc 记录形（``{message, parent_tool_use_id?}``）。抽出来供
    canonical 路径复用：cc importer 近乎恒等；opencode / insight importer 归一成
    同形 record，再喂 ``_digest_from_records``。与 ``audit/slice.py`` 的 ``_records`` 同形。
    """
    records: list[dict] = []
    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    stamp_turn_indices(records)
    return records


def _digest_from_records(
    records: list[dict], root: str | None = None, *, locate_turns: bool = False
) -> SessionDigest:
    """cc 记录列表（canonical 记录形）→ SessionDigest。纯内存、不读文件。

    从 ``build_digest`` 抽出，使「读文件」与「records→digest」分离：canonical
    importer（cc/opencode/insight）产 records，统一走本函数重建。

    ``locate_turns`` 控 turn 定位特性的渲染(默认关):关时回到 no-seq 形态——
    ``transcript_text`` 不加 ``[T{idx}] `` 前缀、``ToolCallRecord.turn`` 置 ``None``;
    开时给每条 assistant record 的文本加 ``[T{idx}] `` 前缀(per-record)、``turn`` 记所属
    全局 turnIndex。内部 ``_turn_index`` sentinel 总是 stamp(切片往返用),关时只是不渲染。
    """
    # pass 1: tool_use_id -> result text(+ 回报所属 turn,层3 派发-回报对用)
    id2result: dict[str, str] = {}
    id2result_turn: dict[str, int | None] = {}
    for rec in records:
        msg = rec.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    id2result[b.get("tool_use_id", "")] = _result_text(b.get("content"))
                    id2result_turn[b.get("tool_use_id", "")] = rec.get("_turn_index")

    # pass 2: walk in order
    user_turns: list[str] = []
    skill_used: str | None = None
    reads: dict[str, str] = {}            # path -> stripped initial content
    read_order: list[str] = []
    files: dict[str, str] = {}            # path -> 当前重建状态
    written: set[str] = set()
    demoted: set[str] = set()
    bash_touched: set[str] = set()
    commands: list[Command] = []
    all_paths: list[str] = []
    tool_trace: list[ToolCallRecord] = []
    dispatch_returns: list[DispatchReturn] = []
    transcript_text_parts: list[str] = []
    evidence_refs: dict[str, FileEffectRecord] = {}
    evidence_warnings: list[str] = []
    evidence_metadata: dict = {}
    provenance_by_capture: dict[str, ProvenanceEvent] = {}

    def _record_capture_event(
        raw_capture: object,
        rec: dict,
        *,
        tool_use_id: str | None = None,
        tool_timestamp: str | None = None,
    ) -> ProvenanceEvent | None:
        if not isinstance(raw_capture, dict) or not raw_capture.get("capture_id"):
            return None
        capture_id = str(raw_capture["capture_id"])
        effects: list[FileEffectRecord] = []
        for raw_effect in raw_capture.get("effects") or []:
            if not isinstance(raw_effect, dict) or not raw_effect.get("path"):
                continue
            try:
                effect = FileEffectRecord.model_validate({
                    **raw_effect,
                    "evidence_ref": raw_effect.get("evidence_ref")
                        or f"capture:{capture_id}:{raw_effect['path']}",
                    "quality": raw_capture.get("quality"),
                    "capture_id": capture_id,
                    "actor": raw_capture.get("actor")
                        or rec.get("agent")
                        or "",
                    "tool_name": raw_capture.get("tool_name") or "",
                })
            except ValidationError as exc:
                raise InputError(
                    f"provenance capture {capture_id} 的文件 effect 字段类型无效: {exc}"
                ) from exc
            effects.append(effect)
            evidence_refs[effect.evidence_ref] = effect

        try:
            event = ProvenanceEvent.model_validate({
                "timestamp": raw_capture.get("started_at") or tool_timestamp or rec.get("timestamp"),
                "record_timestamp": rec.get("timestamp"),
                "tool_timestamp": tool_timestamp,
                "started_at": raw_capture.get("started_at"),
                "finished_at": raw_capture.get("finished_at"),
                "event_id": rec.get("event_id"),
                "capture_id": capture_id,
                "tool_use_id": raw_capture.get("tool_use_id") or tool_use_id,
                "association_quality": raw_capture.get("association_quality") or "unmatched",
                "quality": raw_capture.get("quality"),
                "actor_kind": raw_capture.get("actor_kind"),
                "agent": raw_capture.get("agent") or rec.get("agent"),
                "parent_agent": raw_capture.get("parent_agent") or rec.get("parent_agent"),
                "native_agent_id": raw_capture.get("native_agent_id"),
                "iteration": rec.get("iteration") if rec.get("iteration") is not None
                    else raw_capture.get("iteration"),
                "parent_tool_use_id": raw_capture.get("parent_tool_use_id")
                    or rec.get("parent_tool_use_id"),
                "session_id": raw_capture.get("session_id") or rec.get("session_id"),
                "native_session_id": raw_capture.get("native_session_id")
                    or rec.get("native_session_id"),
                "parent_native_session_id": raw_capture.get("parent_native_session_id")
                    or rec.get("parent_native_session_id"),
                "tool_name": raw_capture.get("tool_name"),
                "tool_input": raw_capture.get("tool_input"),
                "tool_output": raw_capture.get("tool_output"),
                "runtime": raw_capture.get("runtime"),
                "model": raw_capture.get("model"),
                "succeeded": raw_capture.get("succeeded"),
                "is_error": raw_capture.get("is_error"),
                "incomplete": raw_capture.get("incomplete", False),
                "conflicted": raw_capture.get("conflicted", False),
                "conflicts": raw_capture.get("conflicts") or [],
                "mutation_reasons": raw_capture.get("mutation_reasons") or [],
                "cwd": raw_capture.get("cwd"),
                "head_before": raw_capture.get("head_before"),
                "head_after": raw_capture.get("head_after"),
                "committed_sha": raw_capture.get("committed_sha"),
                "attributes": raw_capture.get("attributes") or {},
                "effects": effects,
            })
        except ValidationError as exc:
            raise InputError(
                f"provenance capture {capture_id} 字段类型无效: {exc}"
            ) from exc
        provenance_by_capture.setdefault(capture_id, event)
        return provenance_by_capture[capture_id]

    def remember(p: str) -> None:
        if p and p not in all_paths:
            all_paths.append(p)

    for rec in records:
        bundle_meta = rec.get("_sift_bundle_meta")
        if isinstance(bundle_meta, dict):
            if not evidence_metadata:
                evidence_metadata = {
                    key: value for key, value in bundle_meta.items()
                    if key != "unmatched_captures"
                }
            evidence_warnings.extend(str(w) for w in bundle_meta.get("warnings") or [])
            coverage = bundle_meta.get("coverage") or {}
            if isinstance(coverage, dict):
                evidence_warnings.extend(str(w) for w in coverage.get("warnings") or [])
        _record_capture_event(rec.get("_sift_standalone_capture"), rec)
        msg = rec.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, str):
                t = content.strip()
            elif isinstance(content, list):
                t = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
            else:
                t = ""
            if t and not t.startswith("<system-reminder>") and "Caveat: The messages below" not in t:
                user_turns.append(t)
            continue
        if role != "assistant" or not isinstance(content, list):
            continue
        # per-record 文本缓冲:同一 record 的多个 text 块合并、单次加 [T{idx}] 前缀
        # (per-record 而非 per-block——一条 assistant 消息可能含多个 text 块,重复同 [T{idx}]
        # 既吵又暗示是不同 turn;合并后前缀锚住整条 record 的全局位置)。
        rec_text_parts: list[str] = []
        had_block = False  # 本 record 是否含非空 text 或 tool_use(决定是否发 turn 锚)
        turn_idx = rec.get("_turn_index")
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                if b.get("text"):
                    rec_text_parts.append(b.get("text", ""))
                    had_block = True
                continue
            if btype != "tool_use":
                continue
            had_block = True
            name = b.get("name", "")
            inp = b.get("input", {}) or {}
            tid = b.get("id", "")
            structured_v2 = "_sift_evidence" in b
            raw_evidence = b.get("_sift_evidence")
            event = _record_capture_event(
                raw_evidence,
                rec,
                tool_use_id=str(tid) if structured_v2 and tid else None,
                tool_timestamp=b.get("_sift_timestamp"),
            )
            # 无条件收集所有 tool_use 块(在按工具名分支之前):
            # 现有分支只覆盖 Skill/Read/Write/Edit/MultiEdit/Bash,
            # Glob/Grep/WebSearch 等会被忽略。tool_trace 必须收全部,
            # audit 的工具指令对账才能按任意工具名匹配。turn=所属 record 的全局 _turn_index
            # (locate_turns=False 时置 None——turn 定位特性可选化,no-seq 形态不标 turn)。
            try:
                tool_trace.append(ToolCallRecord(
                    name=name,
                    # A3:dispatch 类(task/Agent)的 input_summary 上限放宽(默认 2000,
                    # env SIFT_TASK_SUMMARY_LIMIT 可覆盖)——派发 prompt 是编排会话里
                    # 主 agent 行为的核心证据,200 截断会掐掉要求清单(缺席式误判)。
                    input_summary=json.dumps(b.get("input") or {}, ensure_ascii=False)[:_summary_limit_for(name)],
                    input=inp or None,  # 完整结构化参数(不截断),供参数级匹配/路径规范化;inp 见上(line 274)
                    turn=turn_idx if locate_turns else None,
                    tool_use_id=str(tid) if structured_v2 and tid else None,
                    timestamp=b.get("_sift_timestamp"),
                    record_timestamp=rec.get("timestamp"),
                    event_id=rec.get("event_id"),
                    iteration=rec.get("iteration"),
                    result=b.get("_sift_result"),
                    is_error=b.get("_sift_is_error") if b.get("_sift_is_error") is not None
                        else event.is_error if event else None,
                    capture_id=event.capture_id if event else None,
                    evidence_quality=event.quality if event else None,
                    association_quality=event.association_quality if event else None,
                    evidence_refs=[effect.evidence_ref for effect in event.effects] if event else [],
                    agent=rec.get("agent") or (event.agent if event else None),
                    parent_agent=rec.get("parent_agent") or (event.parent_agent if event else None),
                    parent_tool_use_id=rec.get("parent_tool_use_id")
                        or (event.parent_tool_use_id if event else None),
                    session_id=rec.get("session_id") or (event.session_id if event else None),
                    native_session_id=rec.get("native_session_id")
                        or (event.native_session_id if event else None),
                    parent_native_session_id=rec.get("parent_native_session_id")
                        or (event.parent_native_session_id if event else None),
                    runtime=event.runtime if event else None,
                    model=event.model if event else None,
                    started_at=event.started_at if event else None,
                    finished_at=event.finished_at if event else None,
                    succeeded=event.succeeded if event else None,
                ))
            except ValidationError as exc:
                # Keep cc/opencode's historical failure behavior untouched;
                # only v2 provenance input uses the clean InputError channel.
                if not structured_v2:
                    raise
                raise InputError(f"tool call {tid} 字段类型无效: {exc}") from exc
            # 层3:派发(task/Agent tool_use)与回报(tool_result)配对。回报在 pass 1
            # 已收进 id2result(无论回报记录在派发之前/之后出现);无回报的派发也收
            # (result_len=0,判"验收是否发生"时有用)。角色取 input.subagent_type/agent
            # (rec.agent 在主作用域是任务标题,不是角色名,不用)。
            if str(name).strip().lower() in ("task", "agent"):
                _result = id2result.get(tid, "")
                _rhead, _rtail = _result_windows(_result)
                dispatch_returns.append(DispatchReturn(
                    dispatch_turn=turn_idx,
                    result_turn=id2result_turn.get(tid),
                    role=str(inp.get("subagent_type") or inp.get("agent") or ""),
                    prompt_head=str(inp.get("prompt") or inp.get("description") or "")[:200],
                    result_head=_rhead[:400],
                    result_tail=_rtail,
                    result_len=len(_result),
                ))
            if name == "Skill":
                if not skill_used:
                    skill_used = inp.get("skill") or inp.get("name")
            elif name == "Read":
                fp = inp.get("file_path", "")
                remember(fp)
                if fp and fp not in reads:
                    reads[fp] = _clean_read_result(id2result.get(tid, ""))
                    read_order.append(fp)
                if fp and fp not in files:
                    files[fp] = reads[fp]
            elif name == "Write":
                fp = inp.get("file_path", "")
                remember(fp)
                if fp:
                    files[fp] = inp.get("content", "")
                    written.add(fp)
                    demoted.discard(fp)
            elif name == "Edit":
                fp = inp.get("file_path", "")
                remember(fp)
                old, new = inp.get("old_string", ""), inp.get("new_string", "")
                if fp and fp in files and old in files[fp]:
                    if inp.get("replace_all"):
                        files[fp] = files[fp].replace(old, new)
                    else:
                        files[fp] = files[fp].replace(old, new, 1)
                    written.add(fp)
                elif fp:
                    written.discard(fp)
                    demoted.add(fp)
            elif name == "MultiEdit":
                fp = inp.get("file_path", "")
                remember(fp)
                edits = inp.get("edits", []) or []
                cur, ok = files.get(fp), fp in files
                if ok:
                    for e in edits:
                        o, n = e.get("old_string", ""), e.get("new_string", "")
                        if o not in cur:
                            ok = False
                            break
                        cur = cur.replace(o, n, 1)
                if ok and fp:
                    files[fp] = cur
                    written.add(fp)
                elif fp:
                    written.discard(fp)
                    demoted.add(fp)
            elif name == "Bash":
                cmd = inp.get("command", "")
                commands.append(Command(cmd=cmd, stdout=id2result.get(tid, "")[:2000]))
                for m in _BASH_WRITE_RE.finditer(cmd):
                    tgt = (m.group(1) or "").strip("'\"")
                    if tgt:
                        bash_touched.add(tgt)
        # flush 本 record 的 turn 锚(仅 locate_turns=True):有 text 则带 [T{idx}] 前缀;
        # text-less 的纯 tool_use record 也发裸 [T{idx}](LLM 可引用任何 turn,含纯动作 turn
        # 如 Agent dispatch)。locate_turns=False 时 text-less record 不贡献文本(无前缀可锚);
        # 有 text 的 record 贡献纯文本。_turn_index 缺失(异常/旧切片)→ "?",不崩。
        if had_block:
            body = "\n".join(rec_text_parts)
            if locate_turns:
                idx_str = str(turn_idx) if turn_idx is not None else "?"
                anchor = f"[T{idx_str}]"
                transcript_text_parts.append(f"{anchor} {body}" if body else anchor)
            elif body:
                transcript_text_parts.append(body)

    if root is None:
        root = _detect_root(all_paths)

    produced = [FileContent(path=_rel(p, root), content=files.get(p, ""))
                for p in sorted(written) if p not in demoted]
    inputs = [FileContent(path=_rel(p, root), content=reads[p])
              for p in read_order if p not in written and p not in demoted]
    produced_paths = {f.path for f in produced}
    unrecon = sorted({_rel(p, root) for p in (demoted | bash_touched) if p})
    unrecon = [u for u in unrecon if u not in produced_paths]

    timeline = sorted(
        provenance_by_capture.values(),
        key=lambda event: (
            _build_timestamp_sort_key(event.timestamp),
            event.event_id or event.capture_id,
            event.capture_id,
        ),
    )
    timeline = [event.model_copy(update={"sequence": index}) for index, event in enumerate(timeline, 1)]

    return SessionDigest(
        skill_used=skill_used, user_turns=user_turns,
        input_files=inputs, produced_files=produced,
        unreconstructed=unrecon, commands=commands,
        dispatch_returns=dispatch_returns,
        tool_trace=tool_trace, transcript_text="\n".join(transcript_text_parts),
        evidence_refs=evidence_refs,
        evidence_warnings=list(dict.fromkeys(evidence_warnings)),
        evidence_metadata=evidence_metadata,
        provenance_timeline=timeline,
    )


def build_digest(
    transcript_path: Path, root: str | None = None, *, locate_turns: bool = False
) -> SessionDigest:
    """Claude Code session JSONL → SessionDigest（读文件 → cc 记录 → digest）。

    薄封装：``_records_from_cc`` 读文件成 cc 记录（canonical 记录形），``_digest_from_records``
    重建。拆分后各 importer（cc / opencode / insight）产 records 统一走 ``_digest_from_records``。
    ``locate_turns`` 透传重建器(控 ``[T{idx}] `` 前缀与 ``ToolCallRecord.turn``,默认关)。
    """
    return _digest_from_records(_records_from_cc(transcript_path), root, locate_turns=locate_turns)
