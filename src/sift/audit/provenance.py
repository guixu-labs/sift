"""文件归因证据的确定性分析。

分析器消费已经重建的 :class:`SessionDigest`，不调用 LLM，也不会修改既有的
指令审计 finding。即使 v2 证据包没有文件变更，仍视为可用，因为“工具执行但
没有 mutation”本身也是有价值的证据。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import re
from typing import Iterable

from sift.evals.transcript import SessionDigest
from sift.models import (
    FileEffectRecord,
    ProvenanceAuditResult,
    ProvenanceFinding,
    ProvenanceEvent,
    ProvenanceTimelineEntry,
    ToolCallRecord,
)
from sift.models.provenance_audit_models import ProvenanceDimension, ProvenanceSeverity


_READ_TOOLS = {
    "read", "glob", "grep", "search", "websearch", "list", "ls", "cat",
    "find", "stat", "head", "tail", "pwd",
}
_BASH_MUTATION_COMMANDS = {
    "cp", "mv", "rm", "mkdir", "touch", "truncate", "apply_patch",
}
_BASH_TEST_PATTERNS = (
    re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+)?(?:pytest|tox|nose|unittest)(?:\s|$)"),
    re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:npm|yarn|pnpm|make|cargo|go|mix|mvn|gradle)\s+[^;&|]*\btest(?:s)?\b"),
)
_BASH_REDIRECT_RE = re.compile(r"(?:^|[\s;|])(?:\d+)?>{1,2}\s*([^\s;&|]+)")
_TEST_TOOL_NAME_RE = re.compile(r"(?:^|[_-])tests?(?:$|[_-])")
_PROVENANCE_SCHEMA_VERSION = 2
_VERIFICATION_EXEMPT_SUFFIXES = {".md", ".markdown", ".mdx"}


def _parse_timestamp(value: str | None) -> datetime | None:
    """解析时间戳；缺失或畸形值返回 ``None``。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _collect_events_with_valid_timestamps(
    events: Iterable[ProvenanceEvent],
) -> list[ProvenanceEvent]:
    """收集并按合法时间戳排序的事件。

    无效时间戳事件不会参与文件状态或验证闭环推理；同一时间戳下保留
    原始 sequence 和输入顺序，避免把 capture ID 当作时间依据。
    """
    timed_events: list[tuple[datetime, int, int, ProvenanceEvent]] = []
    for index, event in enumerate(events):
        timestamp = _parse_timestamp(event.timestamp)
        if timestamp is not None:
            timed_events.append((timestamp, event.sequence, index, event))
    timed_events.sort(key=lambda item: (item[0], item[1], item[2]))
    return [event for _, _, _, event in timed_events]


def _get_valid_timestamp(event: ProvenanceEvent) -> datetime:
    """获取已筛选事件的合法时间戳。"""
    timestamp = _parse_timestamp(event.timestamp)
    if timestamp is None:
        raise ValueError("event does not contain a valid timestamp")
    return timestamp


def _normalize_tool_name(name: str | None) -> str:
    return (name or "").strip().lower()


def _is_test_call(call: ToolCallRecord, name: str | None = None) -> bool:
    normalized_name = name if name is not None else _normalize_tool_name(call.name)
    if normalized_name in {"pytest", "tox", "unittest", "check"} or _TEST_TOOL_NAME_RE.search(normalized_name):
        return True
    if normalized_name in {"bash", "shell", "terminal", "exec", "run"}:
        command = str((call.input or {}).get("command", "")).lower()
        return any(pattern.search(command) for pattern in _BASH_TEST_PATTERNS)
    return False


def _contains_bash_mutation(command: str) -> bool:
    """仅识别明确的 Bash 变更命令，忽略 2>&1 和 /dev/null 等重定向。"""
    for match in _BASH_REDIRECT_RE.finditer(command):
        target = match.group(1)
        if target.startswith("&") or target.startswith("/dev/"):
            continue
        return True
    for segment in re.split(r"[;&|]+", command):
        words = segment.strip().split()
        if not words:
            continue
        executable = words[0].rsplit("/", 1)[-1]
        if executable == "tee":
            targets = [word for word in words[1:] if not word.startswith("-")]
            if any(not target.startswith("/dev/") for target in targets):
                return True
        if executable in _BASH_MUTATION_COMMANDS:
            return True
        if executable == "sed" and "-i" in words:
            return True
        if executable == "perl" and any(word.startswith("-i") for word in words):
            return True
        if executable == "git" and len(words) > 1 and words[1] == "apply":
            return True
    return False


def _is_write_call(event: ProvenanceEvent, name: str | None = None) -> bool:
    """识别明确声明写入的工具调用，不把普通 Bash 一律视为写入。"""
    normalized_name = name if name is not None else _normalize_tool_name(event.tool_name)
    if normalized_name in {"write", "edit", "multiedit", "apply_patch", "patch"}:
        return True
    if normalized_name == "bash":
        command = str((event.tool_input or {}).get("command", "")).lower()
        return _contains_bash_mutation(command)
    return False


def _is_event_failed(event: ProvenanceEvent) -> bool:
    """将明确错误或明确失败状态视为调用失败。"""
    return event.is_error is True or event.succeeded is False


def _is_event_successful(event: ProvenanceEvent) -> bool:
    """只有明确成功且没有错误标记时才视为调用成功。"""
    return event.succeeded is True and event.is_error is not True


def _requires_verification(path: str) -> bool:
    """判断变更路径通常是否需要修改后的测试。"""
    normalized_path = path.lower()
    return not any(normalized_path.endswith(suffix) for suffix in _VERIFICATION_EXEMPT_SUFFIXES)


def _deduplicate_values(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _deduplicate_events(events: Iterable[ProvenanceEvent]) -> list[ProvenanceEvent]:
    """去重事件，同时保留时间线顺序。"""
    seen: set[str] = set()
    result: list[ProvenanceEvent] = []
    for event in events:
        key = event.event_id or event.capture_id
        if key not in seen:
            seen.add(key)
            result.append(event)
    return result


def _is_trusted_effect(event: ProvenanceEvent, effect: FileEffectRecord) -> bool:
    """判断 effect 是否足够可信，可以参与文件状态推理。"""
    return (
        event.quality == "observed"
        and event.association_quality == "exact"
        and effect.quality == "observed"
        and not event.conflicted
        and not effect.conflict
    )


def _flatten_effects(events: Iterable[ProvenanceEvent]) -> list[FileEffectRecord]:
    return [effect for event in events for effect in event.effects]


def _collect_unreconstructed_paths(
    digest: SessionDigest,
    untrusted_effects: Iterable[tuple[ProvenanceEvent, FileEffectRecord]],
) -> list[str]:
    untrusted_paths = {effect.path for _, effect in untrusted_effects}
    return sorted(set(digest.unreconstructed) | untrusted_paths)


def _build_finding(
    *,
    dimension: ProvenanceDimension,
    severity: ProvenanceSeverity,
    code: str,
    summary: str,
    explanation: str = "",
    events: Iterable[ProvenanceEvent] = (),
    paths: Iterable[str] = (),
    turn_refs: Iterable[int] = (),
    capture_ids: Iterable[str] = (),
    tool_use_ids: Iterable[str] = (),
    evidence_refs: Iterable[str] = (),
    effects: Iterable[FileEffectRecord] | None = None,
) -> ProvenanceFinding:
    """构造 finding；必要时复用已经展开的 effect 记录。"""
    events = list(events)
    effect_records = (
        list(effects) if effects is not None else _flatten_effects(events)
    )
    return ProvenanceFinding(
        dimension=dimension,
        severity=severity,
        code=code,
        summary=summary,
        explanation=explanation,
        paths=_deduplicate_values(paths)
        or _deduplicate_values(effect.path for effect in effect_records),
        actors=_deduplicate_values(event.agent or event.actor_kind or "" for event in events),
        capture_ids=_deduplicate_values(capture_ids)
        or _deduplicate_values(event.capture_id for event in events),
        tool_use_ids=_deduplicate_values(tool_use_ids)
        or _deduplicate_values(event.tool_use_id or "" for event in events),
        evidence_refs=_deduplicate_values(evidence_refs)
        or _deduplicate_values(effect.evidence_ref for effect in effect_records),
        turn_refs=sorted(set(turn_refs)),
    )


def _collect_event_turns(
    events: Iterable[ProvenanceEvent],
    calls_by_capture: dict[str, list[ToolCallRecord]],
) -> list[int]:
    turn_refs: list[int] = []
    for event in events:
        turn_refs.extend(
            call.turn for call in calls_by_capture.get(event.capture_id, [])
            if call.turn is not None
        )
    return turn_refs


def _build_timeline_summary(
    events: list[ProvenanceEvent],
) -> dict[str, list[ProvenanceTimelineEntry]]:
    out: dict[str, list[ProvenanceTimelineEntry]] = defaultdict(list)
    for event in events:
        for effect in event.effects:
            out[effect.path].append(ProvenanceTimelineEntry(
                sequence=event.sequence,
                timestamp=event.timestamp,
                capture_id=event.capture_id,
                tool_use_id=event.tool_use_id,
                actor=event.agent or event.actor_kind or "",
                tool_name=event.tool_name or "",
                quality=event.quality,
                association_quality=event.association_quality,
                trusted=_is_trusted_effect(event, effect),
                evidence_ref=effect.evidence_ref,
                before_blob=effect.before_blob,
                after_blob=effect.after_blob,
                additions=effect.additions,
                deletions=effect.deletions,
                conflict=effect.conflict,
            ))
    return dict(out)


def analyze_provenance(digest: SessionDigest) -> ProvenanceAuditResult:
    """在不调用 LLM 的前提下分析证据健康度和执行行为。"""
    metadata = digest.evidence_metadata or {}
    has_provenance_data = bool(digest.provenance_timeline or digest.evidence_refs)
    available = metadata.get("version") == _PROVENANCE_SCHEMA_VERSION or has_provenance_data
    if not available:
        return ProvenanceAuditResult(available=False)

    # 全部事件用于证据健康检查；只有带合法时间戳的事件参与状态推理。
    all_events = list(digest.provenance_timeline)
    timed_events = _collect_events_with_valid_timestamps(all_events)
    calls_by_capture: dict[str, list[ToolCallRecord]] = defaultdict(list)
    for call in digest.tool_trace:
        if call.capture_id:
            calls_by_capture[call.capture_id].append(call)

    quality_counts = Counter(event.quality for event in all_events)
    association_counts = Counter(event.association_quality for event in all_events)
    effects: list[FileEffectRecord] = []
    trusted_effects: list[tuple[ProvenanceEvent, FileEffectRecord]] = []
    untrusted_effects: list[tuple[ProvenanceEvent, FileEffectRecord]] = []
    for event in all_events:
        for effect in event.effects:
            effects.append(effect)
            target = trusted_effects if _is_trusted_effect(event, effect) else untrusted_effects
            target.append((event, effect))
    timed_trusted_effects = [
        (event, effect)
        for event in timed_events
        for effect in event.effects
        if _is_trusted_effect(event, effect)
    ]
    findings: list[ProvenanceFinding] = []
    warnings = _deduplicate_values(digest.evidence_warnings)

    # 证据健康度 ----------------------------------------------------------
    stats = {
        "capture_count": len(all_events),
        "effect_count": len(effects),
        "trusted_effect_count": len(trusted_effects),
        "untrusted_effect_count": len(untrusted_effects),
        "quality_counts": dict(quality_counts),
        "association_counts": dict(association_counts),
        "file_count": len({effect.path for effect in effects}),
        "trusted_file_count": len({effect.path for _, effect in trusted_effects}),
        "actor_count": len({
            event.agent or event.actor_kind
            for event in all_events
            if event.agent or event.actor_kind
        }),
        "unreconstructed_count": len(digest.unreconstructed),
        "unreconstructed_files": _collect_unreconstructed_paths(digest, untrusted_effects),
    }
    if warnings:
        findings.append(_build_finding(
            dimension="evidence_health", severity="warning", code="EVIDENCE_WARNINGS",
            summary=f"证据包含 {len(warnings)} 条覆盖或质量警告",
            explanation="; ".join(warnings), events=all_events, effects=effects,
        ))
    unmatched: list[ProvenanceEvent] = []
    ambiguous: list[ProvenanceEvent] = []
    conflicted: list[ProvenanceEvent] = []
    incomplete: list[ProvenanceEvent] = []
    for event in all_events:
        if event.association_quality == "unmatched":
            unmatched.append(event)
        if event.association_quality == "ambiguous" or event.quality == "ambiguous":
            ambiguous.append(event)
        if event.quality == "conflicted" or event.conflicted:
            conflicted.append(event)
        if event.quality == "incomplete" or event.incomplete:
            incomplete.append(event)
    if unmatched:
        findings.append(_build_finding(
            dimension="evidence_health", severity="warning", code="UNMATCHED_CAPTURE",
            summary=f"{len(unmatched)} 个 capture 未能唯一关联工具调用",
            explanation="这些 capture 会保留在时间线中，但不能证明具体对应哪一次工具调用。",
            events=unmatched, turn_refs=_collect_event_turns(unmatched, calls_by_capture),
        ))
    event_capture_ids = {event.capture_id for event in all_events}
    missing_capture_calls = [
        call for call in digest.tool_trace
        if call.capture_id and call.capture_id not in event_capture_ids
    ]
    if missing_capture_calls:
        findings.append(_build_finding(
            dimension="evidence_health", severity="warning", code="MISSING_CAPTURE_EVENT",
            summary=f"{len(missing_capture_calls)} 个工具调用引用了缺失的 capture 事件",
            explanation="工具调用仍保留在原有轨迹中，但无法关联文件归因时间线。",
            capture_ids=[call.capture_id or "" for call in missing_capture_calls],
            tool_use_ids=_deduplicate_values(call.tool_use_id or "" for call in missing_capture_calls),
            turn_refs=sorted({call.turn for call in missing_capture_calls if call.turn is not None}),
        ))
    if ambiguous:
        findings.append(_build_finding(
            dimension="evidence_health", severity="indeterminate", code="AMBIGUOUS_CAPTURE",
            summary=f"{len(ambiguous)} 个 capture 的文件归因不确定",
            explanation="存在重复关联、冲突或质量不确定的文件变更证据，不能覆盖可信文件状态。",
            events=ambiguous, turn_refs=_collect_event_turns(ambiguous, calls_by_capture),
        ))
    if conflicted:
        findings.append(_build_finding(
            dimension="evidence_health", severity="indeterminate", code="CONFLICTED_CAPTURE",
            summary=f"{len(conflicted)} 个 capture 存在冲突",
            explanation="冲突 capture 的文件变更证据不能覆盖可信文件状态，需要人工复核冲突范围。",
            events=conflicted, turn_refs=_collect_event_turns(conflicted, calls_by_capture),
        ))
    if incomplete:
        findings.append(_build_finding(
            dimension="evidence_health", severity="warning", code="INCOMPLETE_CAPTURE",
            summary=f"{len(incomplete)} 个 capture 的生命周期或快照不完整",
            explanation="不完整 capture 会保留作上下文，但不能单独证明最终文件状态。",
            events=incomplete, turn_refs=_collect_event_turns(incomplete, calls_by_capture),
        ))
    untrusted_events = _deduplicate_events(event for event, _ in untrusted_effects)
    if untrusted_effects and not ambiguous:
        findings.append(_build_finding(
            dimension="evidence_health", severity="indeterminate", code="UNTRUSTED_EFFECT",
            summary=f"{len(untrusted_effects)} 个文件变更证据不满足可信条件",
            explanation="质量不完整、存在冲突或关联不明确的文件变更只能作为提示，不能覆盖可信文件状态。",
            events=untrusted_events,
            effects=[effect for _, effect in untrusted_effects],
            turn_refs=_collect_event_turns(untrusted_events, calls_by_capture),
        ))
    if digest.unreconstructed:
        findings.append(_build_finding(
            dimension="evidence_health", severity="warning", code="UNRECONSTRUCTED_FILE",
            summary=f"{len(digest.unreconstructed)} 个文件无法从证据重建",
            explanation="这些路径来自不完整的工具重建，不能作为最终文件状态。",
            paths=digest.unreconstructed,
        ))

    # 归因完整性 ----------------------------------------------------------
    for event in all_events:
        name = _normalize_tool_name(event.tool_name)
        linked_calls = calls_by_capture.get(event.capture_id, [])
        mismatched_calls = [
            call for call in linked_calls
            if (event.tool_use_id and call.tool_use_id and event.tool_use_id != call.tool_use_id)
            or (name and call.name and name != _normalize_tool_name(call.name))
        ]
        if mismatched_calls:
            findings.append(_build_finding(
                dimension="attribution_integrity", severity="indeterminate",
                code="TRAJECTORY_FIELD_MISMATCH", summary="capture 与工具轨迹字段不一致",
                explanation="capture 的工具 ID 或工具名与主轨迹不一致，不能确定文件变更的精确归属。",
                events=[event], turn_refs=sorted({call.turn for call in mismatched_calls if call.turn is not None}),
            ))
        if event.effects and _is_event_failed(event):
            findings.append(_build_finding(
                dimension="attribution_integrity", severity="indeterminate",
                code="ERROR_WITH_MUTATION", summary="错误工具调用产生了文件变更",
                explanation="工具结果标记为错误，无法确定这次文件变更是否完整或可准确归因。",
                events=[event], turn_refs=_collect_event_turns([event], calls_by_capture),
            ))
        if event.effects and name in _READ_TOOLS:
            findings.append(_build_finding(
                dimension="attribution_integrity", severity="warning",
                code="NON_WRITE_MUTATION", summary=f"只读工具 {event.tool_name} 产生了文件变更",
                explanation="读取或搜索工具通常不应产生文件变更；请检查证据生产端的工具分类或 capture 关联。",
                events=[event], turn_refs=_collect_event_turns([event], calls_by_capture),
            ))
        if (
            not event.effects
            and _is_write_call(event, name)
            and _is_event_successful(event)
        ):
            findings.append(_build_finding(
                dimension="attribution_integrity", severity="warning",
                code="WRITE_WITHOUT_EFFECT", summary=f"写工具 {event.tool_name} 成功但没有文件变更",
                explanation="这可能是空写入或未被证据覆盖的变更；不能据此断言写入失败。",
                events=[event], turn_refs=_collect_event_turns([event], calls_by_capture),
            ))

    # 文件变更与执行者归属 ------------------------------------------------
    by_path: dict[str, list[tuple[ProvenanceEvent, FileEffectRecord]]] = defaultdict(list)
    for event, effect in timed_trusted_effects:
        by_path[effect.path].append((event, effect))
    for path, entries in by_path.items():
        if len(entries) > 1:
            events_for_path = [event for event, _ in entries]
            findings.append(_build_finding(
                dimension="file_churn", severity="info", code="REPEATED_FILE_MODIFICATION",
                summary=f"{path} 被反复修改了 {len(entries)} 次",
                explanation="重复修改本身不是违规，但可用于识别绕路、反复覆盖或流程优化机会。",
                events=events_for_path, paths=[path], turn_refs=_collect_event_turns(events_for_path, calls_by_capture),
            ))
            prior_hashes: set[str] = set()
            for index, (_, effect) in enumerate(entries):
                if effect.after_blob and effect.after_blob in prior_hashes:
                    events_for_revert = [entries[index][0]]
                    findings.append(_build_finding(
                        dimension="file_churn", severity="warning", code="REVERTED_FILE_STATE",
                        summary=f"{path} 恢复到较早的文件状态",
                        explanation="变更后的内容哈希与更早状态相同，说明中间修改被回退。",
                        events=events_for_revert, paths=[path], turn_refs=_collect_event_turns(events_for_revert, calls_by_capture),
                    ))
                for key in (effect.before_blob, effect.after_blob):
                    if key:
                        prior_hashes.add(key)
        for event, effect in entries:
            event_name = _normalize_tool_name(event.tool_name)
            if (
                event_name == "write"
                and effect.before_content is not None
                and effect.after_content is not None
                and effect.deletions >= effect.before_content.count("\n") + 1
                and effect.before_content != effect.after_content
            ):
                findings.append(_build_finding(
                    dimension="file_churn", severity="info", code="FULL_FILE_OVERWRITE",
                    summary=f"{path} 被 Write 完整覆盖",
                    explanation="文件快照的全部行被删除，说明文件被完全重写。",
                    events=[event], paths=[path], turn_refs=_collect_event_turns([event], calls_by_capture),
                ))
        actors = _deduplicate_values(event.agent or event.actor_kind or "" for event, _ in entries)
        if len(actors) > 1:
            for previous, current in zip(entries, entries[1:]):
                previous_event, _ = previous
                current_event, _ = current
                previous_actor = previous_event.agent or previous_event.actor_kind or ""
                current_actor = current_event.agent or current_event.actor_kind or ""
                if (
                    current_actor
                    and current_actor != previous_actor
                    and current_event.parent_agent is not None
                    and current_event.parent_agent != previous_actor
                ):
                    findings.append(_build_finding(
                        dimension="actor_ownership", severity="warning", code="CROSS_ACTOR_WITHOUT_HANDOFF",
                        summary=f"{path} 在执行者之间切换，但 parent lineage 指向其他执行者",
                        explanation="只有证据生产端明确提供了不一致的 parent_agent 时才提示；普通多执行者协作不会触发该 finding。",
                        events=[previous_event, current_event], paths=[path],
                        turn_refs=_collect_event_turns([previous_event, current_event], calls_by_capture),
                    ))
        for event, _ in entries:
            if not event.agent and not event.actor_kind:
                findings.append(_build_finding(
                    dimension="actor_ownership", severity="warning", code="LINEAGE_GAP",
                    summary=f"{path} 的写入缺少执行者信息",
                    explanation="没有执行者信息时无法判断文件归属。",
                    events=[event], paths=[path], turn_refs=_collect_event_turns([event], calls_by_capture),
                ))

    # 验证闭环 ------------------------------------------------------------
    test_calls = [
        call for call in digest.tool_trace
        if _is_test_call(call, _normalize_tool_name(call.name))
    ]
    test_events = [
        event for event in timed_events
        if any(call.capture_id == event.capture_id for call in test_calls)
    ]
    for path, entries in by_path.items():
        if not _requires_verification(path):
            continue
        last_event, _ = entries[-1]
        last_timestamp = _get_valid_timestamp(last_event)
        post_tests = [
            event for event in test_events
            if _get_valid_timestamp(event) > last_timestamp
        ]
        if not post_tests:
            findings.append(_build_finding(
                dimension="verification_closure", severity="warning", code="FINAL_MODIFICATION_UNVERIFIED",
                summary=f"{path} 的最终可信修改后没有测试证据",
                explanation="未发现发生在最终修改之后的测试/验证工具调用；请确认最终文件状态是否经过验证。",
                events=[last_event], paths=[path], turn_refs=_collect_event_turns([last_event], calls_by_capture),
            ))

    for path, entries in by_path.items():
        if entries:
            stats.setdefault("final_writers", {})[path] = entries[-1][0].agent or entries[-1][0].actor_kind or ""
    raw_coverage = metadata.get("coverage")
    coverage: dict = raw_coverage if isinstance(raw_coverage, dict) else {}
    return ProvenanceAuditResult(
        available=True,
        coverage=coverage,
        statistics=stats,
        findings=findings,
        file_timelines=_build_timeline_summary(all_events),
        warnings=warnings,
    )
