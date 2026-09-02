"""结构化 records 输入:友好的 JSON → cc 记录形,复用现有 cc digest / slice 管线。

producer(insight 等)按 turn 吐::

    {"format": "sift-records",
     "records": [
        {"role": "user", "text": "..."},
        {"role": "assistant", "agent": "...", "parent_tool_use_id": null,
         "text": "...",
         "tool_calls": [{"name": "skill", "input": {"name": "x"}, "result": "..."},
                        {"name": "read",  "input": {"file_path": "..."}, "result": "..."}]}
     ]}

producer 不必拼 cc-jsonl 的 tool_use / tool_result 拆分、不必管 ``Skill`` 大小写 ——
本模块展开成 cc 记录(``{message:{role,content}, parent_tool_use_id[, agent]}``),
``build_digest`` / ``audit`` 零改动;``slice_transcript`` 按 ``--kind`` 选锚点切本单元段。

**``agent`` 字段透传到 cc-record**:producer(insight 等)按 turn 吐 ``agent``(= 该 turn 归属
的 agent 名),本 reader 把它挂到每条展开记录(主消息 + tool_result 回流记录都带)。切片器
``slice_transcript`` 的 ``kind="agent"`` 路据此切 agent 归属段——支持审"被 dispatch 的 agent"
(无 Skill 调用的纯 agent,如 developer)。**kind 分路、不并集**:同名既是 skill 又是 agent
(dual-nature,如 st-verifier)时,skill 面(``kind="skill"`` 按 Skill 调用切)与 agent 面
(``kind="agent"``)各审各的声明正文,互不串味。之前 v1 不消费(commit fdad6bf 标注的默契)是因
还没有 per-agent 切分的消费者;agent-audit 接通即消费。

识别:顶层 dict ``format == "sift-records"``(与 opencode 的 ``{info,messages}`` 区分)。
设计动机见 transcript-driven / 结构化输入;此处只做纯展开,无 LLM。
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from sift.errors import InputError
from sift.evals.transcript import _norm_tool, stamp_turn_indices
from sift.models.sift_records_v2 import (
    SiftRecordsV2,
    SiftRecordsV2Blob,
    SiftRecordsV2Capture,
    SiftRecordsV2FileEffect,
    SiftRecordsV2Record,
    SiftRecordsV2Session,
)

_FORMAT = "sift-records"


_OLD_FORMAT = "skill-eval-records"


def is_structured_records(path: Path) -> bool:
    """``.json`` 且顶层 ``format == "sift-records"`` → True(区分 opencode 形)。

    旧 id(``skill-eval-records``)不静默当 False:那会让旧 records 掉进 cc-jsonl
    解析、报「transcript 无可对账内容」误导诊断——显式报改名错误(cryptic → honest)。
    """
    if path.suffix != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("format") == _OLD_FORMAT:
        raise InputError(
            f"{path}: 旧格式 skill-eval-records 已改名 sift-records(工具 skill-eval → sift),"
            "请用新版 producer 重新导出后再喂 audit"
        )
    return data.get("format") == _FORMAT


def _rec(base: dict, agent) -> dict:
    """cc-record 带可选 ``agent``:有则挂上(切片器按 agent 归属切 agent 段),无则不挂(免每条都
    带个 null)。``agent`` = 该 turn 归属的 agent 名(insight 的 agentName),producer 吐、reader 透传。"""
    if agent:
        base["agent"] = agent
    return base


def load_structured_records(path: Path) -> list[dict]:
    """结构化 JSON → canonical cc records，按协议版本分派 loader。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version", 1)
    if (
        version is None
        or version == "1"
        or (type(version) is int and version == 1)
    ):
        return _load_v1_records(data)
    if type(version) is int and version == 2:
        return _load_v2_records(data, path)
    raise InputError(f"{path}: 不支持 sift-records version {version}")


def _build_native_session_agent_map(session: SiftRecordsV2Session) -> dict[str, str]:
    return {
        item.id: item.agent
        for item in session.native_sessions
        if item.id and item.agent
    }


def _blob_details(blob: SiftRecordsV2Blob | None) -> dict[str, object]:
    if blob is None:
        return {"content": None, "binary": False, "size": None}
    return {
        "content": blob.content if blob.encoding == "utf-8" else None,
        "binary": blob.encoding == "base64",
        "size": blob.size,
    }


def _build_capture_effects(
    capture_id: str,
    capture: SiftRecordsV2Capture,
    blobs: dict[str, SiftRecordsV2Blob],
) -> list[SiftRecordsV2FileEffect]:
    effects: list[SiftRecordsV2FileEffect] = []
    for raw_effect in capture.effects:
        effect = raw_effect.model_dump(mode="python", exclude_unset=True)
        for stage in ("before", "after"):
            ref = getattr(raw_effect, f"{stage}_blob")
            details = _blob_details(blobs.get(ref) if ref else None)
            effect[f"{stage}_content"] = details["content"]
            effect[f"{stage}_binary"] = details["binary"]
            effect[f"{stage}_size"] = details["size"]
        effect.setdefault("evidence_ref", f"capture:{capture_id}:{raw_effect.path}")
        effects.append(SiftRecordsV2FileEffect.model_validate(effect))
    return effects


def _normalize_capture(
    capture_id: str,
    capture: SiftRecordsV2Capture,
    blobs: dict[str, SiftRecordsV2Blob],
    session_agents: dict[str, str],
    association_quality: str,
) -> SiftRecordsV2Capture:
    parent_native_session_id = capture.parent_native_session_id
    attributes = dict(capture.attributes)
    raw_reasons = capture.mutation_reasons
    reasons = [raw_reasons] if isinstance(raw_reasons, str) else [str(value) for value in raw_reasons]
    if capture.incomplete and "incomplete" not in reasons:
        reasons.append("incomplete")
    if capture.conflicted and "conflicted" not in reasons:
        reasons.append("conflicted")
    if association_quality != "exact" and association_quality not in reasons:
        reasons.append(association_quality)
    normalized = capture.model_dump(mode="python", exclude_unset=True)
    normalized.update({
        "capture_id": capture_id,
        "association_quality": association_quality,
        "actor": _capture_actor(capture),
        "agent": capture.agent_name or capture.agent,
        "parent_agent": session_agents.get(str(parent_native_session_id))
            if parent_native_session_id else None,
        "iteration": capture.iteration if capture.iteration is not None
            else attributes.get("iteration"),
        "parent_tool_use_id": capture.parent_tool_use_id
            if capture.parent_tool_use_id is not None
            else attributes.get("parent_tool_use_id"),
        "mutation_reasons": reasons,
        "attributes": attributes,
        "effects": _build_capture_effects(capture_id, capture, blobs),
    })
    return SiftRecordsV2Capture.model_validate(normalized)


def _capture_actor(capture: SiftRecordsV2Capture, fallback: str | None = None) -> str:
    """按统一优先级归一 capture actor，避免 reader/digest 各自回退漂移。"""
    return str(
        capture.actor
        or capture.agent_name
        or capture.agent
        or capture.actor_kind
        or fallback
        or ""
    )


def _build_v2_context(
    bundle: SiftRecordsV2, path: Path, session_agents: dict[str, str]
) -> tuple[dict[str, SiftRecordsV2Capture], dict]:
    """Build v2 evidence context using the caller-provided session index."""
    captures = bundle.captures
    blobs = bundle.blobs
    records = bundle.records
    references: Counter[str] = Counter()
    for rec in records:
        for tool_call in rec.tool_calls:
            capture_id = tool_call.capture_id
            if capture_id is not None:
                references[capture_id] += 1

    session = bundle.session
    normalized: dict[str, SiftRecordsV2Capture] = {}
    for capture_id, capture in captures.items():
        count = references[capture_id]
        association = (
            "unmatched" if count == 0
            else "ambiguous" if count > 1 or capture.quality == "ambiguous"
            else "exact"
        )
        normalized[capture_id] = _normalize_capture(
            capture_id, capture, blobs, session_agents, association
        )

    raw = path.read_bytes()
    meta = {
        "source": "opsbot",
        "format": _FORMAT,
        "version": 2,
        "producer": bundle.producer,
        "exported_at": bundle.exported_at,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "input_name": path.name,
        "session_id": session.id,
        "session": session.model_dump(mode="python", exclude_unset=True),
        "coverage": bundle.coverage,
        "warnings": list(bundle.warnings),
        "unmatched_captures": [
            capture for capture in normalized.values()
            if capture.association_quality == "unmatched"
        ],
    }
    return normalized, meta


def _normalize_tool_input(name: str, value: object) -> tuple[str, dict]:
    """归一 v2 工具名和参数；v1 loader 保持历史实现不动。"""
    normalized_name = _norm_tool(name or "")
    inp = dict(value or {}) if isinstance(value, dict) else {}
    if normalized_name == "Skill" and "skill" not in inp and inp.get("name"):
        # producer 用 input.name(insight 形)→ input.skill(cc slicer 认)
        inp["skill"] = inp["name"]
    return normalized_name, inp


def _load_v1_records(data: dict) -> list[dict]:
    """每个 friendly record 展开为:一条主消息(text + tool_use 块);若有 result,
    再跟一条 user 消息装 tool_result(按 tool_use_id 回流,cc digest pass1 只从 user 收)。
    skill 调用归一:``name="skill"`` → ``"Skill"``、``input.name`` → ``input.skill``。
    """
    out: list[dict] = []
    counter = 0
    for rec in data.get("records") or []:
        role = rec.get("role") or "assistant"
        ptid = rec.get("parent_tool_use_id")
        agent = rec.get("agent")
        blocks: list[dict] = []
        results: list[tuple[str, object]] = []
        text = rec.get("text")
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in rec.get("tool_calls") or []:
            counter += 1
            tid = f"tu_{counter}"
            name = _norm_tool(tc.get("name") or "")
            inp = dict(tc.get("input") or {})
            if name == "Skill" and "skill" not in inp:
                # producer 用 input.name(insight 形)→ 归一到 input.skill(cc slicer 认)
                if inp.get("name"):
                    inp["skill"] = inp["name"]
            blocks.append({"type": "tool_use", "id": tid, "name": name, "input": inp})
            if tc.get("result") is not None:
                results.append((tid, tc.get("result")))
        out.append(_rec({"message": {"role": role, "content": blocks}, "parent_tool_use_id": ptid}, agent))
        if results:
            rblocks = [{"type": "tool_result", "tool_use_id": tid, "content": r} for tid, r in results]
            out.append(_rec({"message": {"role": "user", "content": rblocks}, "parent_tool_use_id": ptid}, agent))
    stamp_turn_indices(out)
    return out


def _extract_v2_record_metadata(rec: SiftRecordsV2Record, session_agents: dict[str, str]) -> dict:
    """提取 v2 record 的 session/iteration/时间字段，供主消息和 result 复用。"""
    parent_native = rec.parent_native_session_id
    return {
        "session_id": rec.session_id,
        "native_session_id": rec.native_session_id,
        "parent_native_session_id": parent_native,
        "parent_agent": session_agents.get(str(parent_native)) if parent_native else None,
        "event_id": rec.event_id,
        "iteration": rec.iteration,
        "timestamp": rec.timestamp,
    }


def _load_v2_records(data: dict, path: Path) -> list[dict]:
    """展开 v2 records，并附加 evidence、lineage 和 bundle metadata。"""
    try:
        bundle = SiftRecordsV2.model_validate(data)
    except ValidationError as exc:
        details_list: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            message = error.get("msg", "")
            if location.endswith(".input") and "dictionary" in message:
                message = "tool call input 必须是对象"
            elif ".effects." in location and location.endswith((".additions", ".deletions")):
                message = "文件 effect 字段类型无效"
            details_list.append(f"{location}: {message}")
        raise InputError(
            f"{path}: sift-records v2 schema 校验失败: {'; '.join(details_list)}"
        ) from exc
    session_agents = _build_native_session_agent_map(bundle.session)
    try:
        captures, meta = _build_v2_context(bundle, path, session_agents)
    except InputError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError(f"{path}: sift-records v2 字段类型无效: {exc}") from exc
    out: list[dict] = []
    counter = 0
    for rec in bundle.records:
        role = rec.role or "assistant"
        ptid = rec.parent_tool_use_id
        blocks: list[dict] = []
        results: list[tuple[str, object]] = []
        text = rec.text
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in rec.tool_calls:
            counter += 1
            tid = str(tc.id or f"tu_{counter}")
            name, inp = _normalize_tool_input(tc.name, tc.input)
            capture_id = tc.capture_id
            evidence = captures.get(str(capture_id)) if capture_id else None
            blocks.append({
                "type": "tool_use",
                "id": tid,
                "name": name,
                "input": inp,
                "_sift_evidence": (
                    evidence.model_dump(mode="python", exclude_unset=True)
                    if evidence else None
                ),
                "_sift_timestamp": tc.timestamp,
                "_sift_is_error": tc.is_error,
                "_sift_result": tc.result,
            })
            if tc.result is not None:
                results.append((tid, tc.result))
        metadata = _extract_v2_record_metadata(rec, session_agents)
        base = {"message": {"role": role, "content": blocks}, "parent_tool_use_id": ptid}
        base.update(metadata)
        out.append(_rec(base, rec.agent))
        if results:
            result_base = {
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tid, "content": result}
                        for tid, result in results
                    ],
                },
                "parent_tool_use_id": ptid,
            }
            result_base.update(metadata)
            out.append(_rec(result_base, rec.agent))

    # Keep the full unmatched capture payload only as standalone canonical
    # records.  Do not put it in bundle metadata: slice_transcript copies that
    # metadata to every emitted segment, which would multiply large snapshots.
    unmatched_captures = meta.pop("unmatched_captures", [])
    for capture in unmatched_captures:
        out.append(_rec({
            "message": {"role": "assistant", "content": []},
            "parent_tool_use_id": capture.parent_tool_use_id,
            "session_id": capture.session_id,
            "native_session_id": capture.native_session_id,
            "parent_native_session_id": capture.parent_native_session_id,
            "parent_agent": capture.parent_agent,
            "iteration": capture.iteration,
            "timestamp": capture.started_at,
            "_sift_standalone_capture": capture.model_dump(
                mode="python", exclude_unset=True
            ),
        }, capture.agent))
    if out:
        out[0]["_sift_bundle_meta"] = meta
    stamp_turn_indices(out)
    return out
