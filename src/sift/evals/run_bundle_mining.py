"""Prepare run bundle member sessions for transcript-based eval mining."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sift.audit.run import convert_run_member, find_target_calls, load_run_bundle
from sift.audit.slice import slice_transcript
from sift.errors import InputError
from sift.evals.structured import load_structured_records


@dataclass(frozen=True)
class RunBundleMiningSession:
    session_id: str
    scoped_path: Path
    full_path: Path
    call_ids: tuple[str, ...]


@dataclass(frozen=True)
class RunBundleMiningInputs:
    run_id: str
    sessions: tuple[RunBundleMiningSession, ...]
    probe_paths: tuple[Path, ...]
    warnings: tuple[str, ...]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(rec, ensure_ascii=False) for rec in records) + "\n",
        encoding="utf-8",
    )


def _user_text(rec: dict) -> str:
    if (
        rec.get("parent_tool_use_id")
        or (rec.get("message") or {}).get("role") != "user"
    ):
        return ""
    content = rec["message"].get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    return ""


def _dispatch_prompt(records: list[dict], tool_id: str) -> str:
    if not tool_id:
        return ""
    for rec in records:
        for block in (rec.get("message") or {}).get("content") or []:
            if (
                not isinstance(block, dict)
                or block.get("type") != "tool_use"
                or block.get("id") != tool_id
            ):
                continue
            inp = block.get("input") or {}
            if isinstance(inp, dict):
                return str(inp.get("prompt") or inp.get("description") or "").strip()
    return ""


def _segment_context(
    records: list[dict], segment: list[dict], kind: str
) -> tuple[str, int | None]:
    first = segment[0]
    parent_tool_id = str(first.get("parent_tool_use_id") or "")
    if parent_tool_id:
        prompt = _dispatch_prompt(records, parent_tool_id)
        label = "派发给该 agent 的任务" if kind == "agent" else "派发给该子 agent 的任务"
        return (f"{label}：{prompt}" if prompt else "", None)
    if kind == "agent":
        return "", None
    first_turn = int(first.get("_turn_index") or 0)
    for rec in reversed(records[: max(first_turn - 1, 0)]):
        text = _user_text(rec)
        if text and not text.startswith("<system-reminder>"):
            return text, rec.get("_turn_index")
    return "", None


def _scoped_records(
    records: list[dict], segments: list[list[dict]], kind: str
) -> list[dict]:
    scoped: list[dict] = []
    contexts = [_segment_context(records, segment, kind) for segment in segments]
    for index, segment in enumerate(segments):
        context, _ = contexts[index]
        if context:
            scoped.append({"message": {"role": "user", "content": context}})
        # A request that precedes the next invocation is that invocation's
        # context, even if the audit slicer still includes it in this span.
        later_requests = {turn for _, turn in contexts[index + 1 :] if turn is not None}
        scoped.extend(
            rec for rec in segment if rec.get("_turn_index") not in later_requests
        )
    return scoped


def prepare_run_bundle_mining_inputs(
    run_path: Path,
    name: str,
    kind: str | None,
    temporary_dir: Path,
    *,
    include_unmatched: bool = False,
) -> RunBundleMiningInputs:
    """Validate a run, select target executions, and retain full sessions for probes."""
    bundle = load_run_bundle(run_path)
    sessions: list[RunBundleMiningSession] = []
    probe_paths: list[Path] = []
    warnings = list(bundle.warnings)
    for index, member in enumerate(bundle.members):
        calls = find_target_calls(member, name, kind)
        if kind is None:
            agent_calls = find_target_calls(member, name, "agent")
            calls = list(dict.fromkeys([*calls, *agent_calls]))
        if not calls and not include_unmatched:
            continue
        data, member_warnings = convert_run_member(member)
        warnings.extend(member_warnings)
        structured = temporary_dir / f"member-{index}.json"
        structured.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        records = load_structured_records(structured)
        full = temporary_dir / f"member-{index}.jsonl"
        _write_jsonl(full, records)
        if include_unmatched:
            probe_paths.append(full)
        had_calls = bool(calls)
        converted_ids = {
            str(call.get("id") or "")
            for record in data["records"]
            for call in record.get("tool_calls", [])
        }
        calls = [tool_id for tool_id in calls if tool_id in converted_ids]
        if not calls:
            if had_calls:
                warnings.append(f"{member.session_id}: 找到目标调用，但执行记录无法可靠导入，已跳过")
            continue
        if kind is None:
            sessions.append(RunBundleMiningSession(member.session_id, full, full, tuple(dict.fromkeys(calls))))
            continue
        segments = slice_transcript(full, name, kind)
        if not segments:
            warnings.append(
                f"{member.session_id}: 找到目标派发或调用，但没有可归属的执行记录，已跳过"
            )
            continue
        if kind == "skill":
            scoped_ids = [
                str(segment[0].get("_sift_slice_anchor_id") or "")
                for segment in segments
            ]
        else:
            scoped_ids = [
                str(rec.get("parent_tool_use_id"))
                for segment in segments
                for rec in segment
                if rec.get("parent_tool_use_id")
            ]
        call_ids = tuple(
            dict.fromkeys(tool_id for tool_id in scoped_ids if tool_id in calls)
        )
        scoped = temporary_dir / f"member-{index}-scoped.jsonl"
        _write_jsonl(scoped, _scoped_records(records, segments, kind))
        sessions.append(RunBundleMiningSession(member.session_id, scoped, full, call_ids))
    if not sessions:
        detail = f"；{warnings[-1]}" if warnings else ""
        raise InputError(
            f"run {bundle.run_id}: 未找到 {name!r} 可用于挖题的 {kind or 'skill／agent'} 执行{detail}"
        )
    return RunBundleMiningInputs(
        bundle.run_id, tuple(sessions), tuple(probe_paths), tuple(warnings)
    )
