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

import base64
import hashlib
import json
from collections import Counter
from pathlib import Path

from sift.errors import InputError
from sift.evals.transcript import _norm_tool, stamp_turn_indices

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
    if version == 1:
        return _load_v1_records(data)
    if version == 2:
        return _load_v2_records(data, path)
    raise InputError(f"{path}: 不支持 sift-records version {version}")


def _decode_blobs(data: dict, path: Path) -> dict[str, dict]:
    """校验并解码 v2 content-addressed blobs；任一损坏都拒绝整个输入。"""
    blobs = data.get("blobs")
    if not isinstance(blobs, dict):
        raise InputError(f"{path}: sift-records v2 的 blobs 必须是对象")
    decoded: dict[str, dict] = {}
    for digest, blob in blobs.items():
        if not isinstance(digest, str) or not isinstance(blob, dict):
            raise InputError(f"{path}: 非法 blob 条目")
        encoding = blob.get("encoding")
        content = blob.get("content")
        try:
            if encoding == "utf-8" and isinstance(content, str):
                raw = content.encode("utf-8")
            elif encoding == "base64" and isinstance(content, str):
                raw = base64.b64decode(content, validate=True)
            else:
                raise ValueError("unsupported encoding")
        except (ValueError, TypeError) as exc:
            raise InputError(f"{path}: blob {digest} 内容或编码无效") from exc
        expected = digest.removeprefix("sha256:")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise InputError(f"{path}: blob {digest} SHA-256 校验失败")
        if blob.get("size") is not None and blob.get("size") != len(raw):
            raise InputError(f"{path}: blob {digest} size 不匹配")
        decoded[digest] = {
            "content": content if encoding == "utf-8" else None,
            "binary": encoding == "base64",
            "size": len(raw),
        }
    return decoded


def _build_native_session_agent_map(session: dict) -> dict[str, str]:
    return {
        str(item["id"]): str(item["agent"])
        for item in session.get("native_sessions") or []
        if isinstance(item, dict) and item.get("id") and item.get("agent")
    }


def _build_capture_effects(capture_id: str, capture: dict, blobs: dict[str, dict]) -> list[dict]:
    effects: list[dict] = []
    for raw_effect in capture.get("effects") or []:
        effect = dict(raw_effect)
        for stage in ("before", "after"):
            ref = effect.get(f"{stage}_blob")
            decoded = blobs.get(ref) if ref else None
            effect[f"{stage}_content"] = decoded.get("content") if decoded else None
            effect[f"{stage}_binary"] = bool(decoded and decoded.get("binary"))
            effect[f"{stage}_size"] = decoded.get("size") if decoded else None
        effect.setdefault("evidence_ref", f"capture:{capture_id}:{effect['path']}")
        effects.append(effect)
    return effects


def _normalize_capture(
    capture_id: str,
    capture: dict,
    blobs: dict[str, dict],
    session_agents: dict[str, str],
    association_quality: str,
) -> dict:
    parent_native_session_id = capture.get("parent_native_session_id")
    attributes = dict(capture.get("attributes") or {})
    raw_reasons = capture.get("mutation_reasons") or []
    reasons = [raw_reasons] if isinstance(raw_reasons, str) else [str(value) for value in raw_reasons]
    if capture.get("incomplete") and "incomplete" not in reasons:
        reasons.append("incomplete")
    if capture.get("conflicted") and "conflicted" not in reasons:
        reasons.append("conflicted")
    if association_quality != "exact" and association_quality not in reasons:
        reasons.append(association_quality)
    return {
        "capture_id": capture_id,
        "quality": capture.get("quality"),
        "association_quality": association_quality,
        "actor_kind": capture.get("actor_kind"),
        "actor": capture.get("agent_name") or capture.get("actor_kind") or "",
        "agent": capture.get("agent_name"),
        "parent_agent": session_agents.get(str(parent_native_session_id)) if parent_native_session_id else None,
        "native_agent_id": capture.get("native_agent_id"),
        "iteration": capture.get("iteration", attributes.get("iteration")),
        "parent_tool_use_id": capture.get("parent_tool_use_id", attributes.get("parent_tool_use_id")),
        "tool_name": capture.get("tool_name") or "",
        "tool_input": capture.get("tool_input"),
        "tool_output": capture.get("tool_output"),
        "tool_use_id": capture.get("tool_use_id"),
        "session_id": capture.get("session_id"),
        "native_session_id": capture.get("native_session_id"),
        "parent_native_session_id": parent_native_session_id,
        "runtime": capture.get("runtime"),
        "model": capture.get("model"),
        "started_at": capture.get("started_at"),
        "finished_at": capture.get("finished_at"),
        "succeeded": capture.get("succeeded"),
        "is_error": capture.get("is_error"),
        "incomplete": bool(capture.get("incomplete")),
        "conflicted": bool(capture.get("conflicted")),
        "conflicts": list(capture.get("conflicts") or []),
        "mutation_reasons": reasons,
        "cwd": capture.get("cwd"),
        "head_before": capture.get("head_before"),
        "head_after": capture.get("head_after"),
        "committed_sha": capture.get("committed_sha"),
        "attributes": attributes,
        "effects": _build_capture_effects(capture_id, capture, blobs),
    }


def _build_v2_context(data: dict, path: Path) -> tuple[dict[str, dict], dict]:
    captures = data.get("captures")
    if not isinstance(captures, dict):
        raise InputError(f"{path}: sift-records v2 的 captures 必须是对象")
    blobs = _decode_blobs(data, path)
    records = data.get("records")
    if not isinstance(records, list):
        raise InputError(f"{path}: sift-records v2 的 records 必须是数组")

    references: Counter[str] = Counter()
    for rec in records:
        if not isinstance(rec, dict):
            raise InputError(f"{path}: sift-records v2 record 无效")
        for tool_call in rec.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                raise InputError(f"{path}: sift-records v2 tool call 无效")
            capture_id = tool_call.get("capture_id")
            if capture_id is not None:
                references[str(capture_id)] += 1

    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    session_agents = _build_native_session_agent_map(session)
    normalized: dict[str, dict] = {}
    for capture_id, capture in captures.items():
        if not isinstance(capture_id, str) or not isinstance(capture, dict):
            raise InputError(f"{path}: capture {capture_id} 无效")
        if capture.get("capture_id") not in (None, capture_id):
            raise InputError(f"{path}: capture key 与 capture_id 不一致: {capture_id}")
        if capture.get("quality") not in {"observed", "incomplete", "conflicted", "ambiguous"}:
            raise InputError(f"{path}: capture {capture_id} quality 无效")
        for effect in capture.get("effects") or []:
            if not isinstance(effect, dict) or not effect.get("path"):
                raise InputError(f"{path}: capture {capture_id} effect 无效")
            for key in ("before_blob", "after_blob"):
                ref = effect.get(key)
                if ref is not None and ref not in blobs:
                    raise InputError(f"{path}: capture {capture_id} 引用了缺失 blob {ref}")
        count = references[capture_id]
        association = (
            "unmatched" if count == 0
            else "ambiguous" if count > 1 or capture.get("quality") == "ambiguous"
            else "exact"
        )
        normalized[capture_id] = _normalize_capture(
            capture_id, capture, blobs, session_agents, association
        )

    for capture_id in references:
        if capture_id not in captures:
            raise InputError(f"{path}: tool call 引用了缺失 capture {capture_id}")

    raw = path.read_bytes()
    meta = {
        "source": "opsbot",
        "format": _FORMAT,
        "version": 2,
        "producer": data.get("producer"),
        "exported_at": data.get("exported_at"),
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "input_name": path.name,
        "session_id": session.get("id"),
        "session": session,
        "coverage": data.get("coverage") if isinstance(data.get("coverage"), dict) else {},
        "warnings": list(data.get("warnings") or []),
        "unmatched_captures": [
            capture for capture in normalized.values()
            if capture["association_quality"] == "unmatched"
        ],
    }
    return normalized, meta


def _normalize_tool_input(name: str, value: object) -> tuple[str, dict]:
    """归一工具名和参数；v1/v2 loader 共享这一段格式转换。"""
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


def _extract_v2_record_metadata(rec: dict, session_agents: dict[str, str]) -> dict:
    """提取 v2 record 的 session/iteration/时间字段，供主消息和 result 复用。"""
    parent_native = rec.get("parent_native_session_id")
    return {
        "session_id": rec.get("session_id"),
        "native_session_id": rec.get("native_session_id"),
        "parent_native_session_id": parent_native,
        "parent_agent": session_agents.get(str(parent_native)) if parent_native else None,
        "event_id": rec.get("event_id"),
        "iteration": rec.get("iteration"),
        "timestamp": rec.get("timestamp"),
    }


def _load_v2_records(data: dict, path: Path) -> list[dict]:
    """展开 v2 records，并附加 evidence、lineage 和 bundle metadata。"""
    captures, meta = _build_v2_context(data, path)
    session_agents = _build_native_session_agent_map(meta.get("session", {}))
    out: list[dict] = []
    counter = 0
    for rec in data.get("records") or []:
        role = rec.get("role") or "assistant"
        ptid = rec.get("parent_tool_use_id")
        blocks: list[dict] = []
        results: list[tuple[str, object]] = []
        text = rec.get("text")
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in rec.get("tool_calls") or []:
            counter += 1
            tid = str(tc.get("id") or f"tu_{counter}")
            name, inp = _normalize_tool_input(tc.get("name") or "", tc.get("input"))
            capture_id = tc.get("capture_id")
            blocks.append({
                "type": "tool_use",
                "id": tid,
                "name": name,
                "input": inp,
                "_sift_evidence": captures.get(str(capture_id)) if capture_id else None,
                "_sift_timestamp": tc.get("timestamp"),
                "_sift_is_error": tc.get("is_error"),
                "_sift_result": tc.get("result"),
            })
            if tc.get("result") is not None:
                results.append((tid, tc.get("result")))
        metadata = _extract_v2_record_metadata(rec, session_agents)
        base = {"message": {"role": role, "content": blocks}, "parent_tool_use_id": ptid}
        base.update(metadata)
        out.append(_rec(base, rec.get("agent")))
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
            out.append(_rec(result_base, rec.get("agent")))
    # Captures that cannot be linked to a unique tool call remain first-class
    # evidence, but are not fabricated into tool_use blocks.
    for capture in meta["unmatched_captures"]:
        out.append(_rec({
            "message": {"role": "assistant", "content": []},
            "parent_tool_use_id": capture.get("parent_tool_use_id"),
            "session_id": capture.get("session_id"),
            "native_session_id": capture.get("native_session_id"),
            "parent_native_session_id": capture.get("parent_native_session_id"),
            "parent_agent": capture.get("parent_agent"),
            "iteration": capture.get("iteration"),
            "timestamp": capture.get("started_at"),
            "_sift_standalone_capture": capture,
        }, capture.get("agent")))
    if out:
        out[0]["_sift_bundle_meta"] = meta
    stamp_turn_indices(out)
    return out
