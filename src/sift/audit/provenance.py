"""Deterministic analysis of file-provenance evidence.

The analyser consumes an already reconstructed :class:`SessionDigest`.  It
does not call an LLM and it never changes the existing instruction-audit
findings.  A v2 bundle is considered available even when it contains no file
effects, because an empty capture is useful evidence that a tool ran without a
mutation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
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
_BASH_MUTATION_MARKERS = (
    ">", "tee ", "sed -i", "perl -i", "cp ", "mv ", "rm ", "mkdir ",
    "touch ", "truncate ", "git apply", "apply_patch",
)
_PROVENANCE_SCHEMA_VERSION = 2
_TIME_VALID = 0
_TIME_MALFORMED = 1
_TIME_MISSING = 2
_VERIFICATION_EXEMPT_SUFFIXES = {".md", ".markdown", ".mdx"}


def _parse_time_key(value: str | None) -> tuple[int, float, str]:
    """Return a total ordering key; malformed timestamps remain deterministic."""
    if not value:
        return (_TIME_MISSING, 0.0, "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (_TIME_VALID, parsed.timestamp(), "")
    except (TypeError, ValueError):
        return (_TIME_MALFORMED, 0.0, str(value))


def _build_event_key(event: ProvenanceEvent) -> tuple[int, float, str, str]:
    return (*_parse_time_key(event.timestamp), event.event_id or event.capture_id)


def _normalize_tool_name(name: str | None) -> str:
    return (name or "").strip().lower()


def _is_test_call(call: ToolCallRecord, name: str | None = None) -> bool:
    normalized_name = name if name is not None else _normalize_tool_name(call.name)
    if "test" in normalized_name or normalized_name in {"pytest", "tox", "unittest", "check"}:
        return True
    if normalized_name in {"bash", "shell", "terminal", "exec", "run"}:
        command = str((call.input or {}).get("command", "")).lower()
        return any(token in command for token in ("pytest", "unittest", " tox", " test", "make test"))
    return False


def _is_write_call(event: ProvenanceEvent, name: str | None = None) -> bool:
    """Recognise a call that claims to write, without treating every Bash as one."""
    normalized_name = name if name is not None else _normalize_tool_name(event.tool_name)
    if normalized_name in {"write", "edit", "multiedit", "apply_patch", "patch"}:
        return True
    if normalized_name == "bash":
        command = str((event.tool_input or {}).get("command", "")).lower()
        return any(marker in command for marker in _BASH_MUTATION_MARKERS)
    return False


def _is_event_failed(event: ProvenanceEvent) -> bool:
    """Treat an explicit error or unsuccessful status as a failed call."""
    return event.is_error is True or event.succeeded is False


def _is_event_successful(event: ProvenanceEvent) -> bool:
    """Require an explicit success and reject contradictory error flags."""
    return event.succeeded is True and event.is_error is not True


def _requires_verification(path: str) -> bool:
    """Return whether a changed path normally needs a post-change test."""
    normalized_path = path.lower()
    return not any(normalized_path.endswith(suffix) for suffix in _VERIFICATION_EXEMPT_SUFFIXES)


def _deduplicate_values(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _deduplicate_events(events: Iterable[ProvenanceEvent]) -> list[ProvenanceEvent]:
    """Deduplicate events while preserving timeline order."""
    seen: set[str] = set()
    result: list[ProvenanceEvent] = []
    for event in events:
        key = event.event_id or event.capture_id
        if key not in seen:
            seen.add(key)
            result.append(event)
    return result


def _is_trusted_effect(event: ProvenanceEvent, effect: FileEffectRecord) -> bool:
    """Whether an effect is strong enough to participate in state reasoning."""
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
    """Build a finding, optionally reusing already flattened effect records."""
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
    """Analyse evidence health and execution behaviour without an LLM."""
    metadata = digest.evidence_metadata or {}
    has_provenance_data = bool(digest.provenance_timeline or digest.evidence_refs)
    available = metadata.get("version") == _PROVENANCE_SCHEMA_VERSION or has_provenance_data
    if not available:
        return ProvenanceAuditResult(available=False)

    events = sorted(digest.provenance_timeline, key=_build_event_key)
    calls_by_capture: dict[str, list[ToolCallRecord]] = defaultdict(list)
    for call in digest.tool_trace:
        if call.capture_id:
            calls_by_capture[call.capture_id].append(call)

    quality_counts = Counter(event.quality for event in events)
    association_counts = Counter(event.association_quality for event in events)
    effects: list[FileEffectRecord] = []
    trusted_effects: list[tuple[ProvenanceEvent, FileEffectRecord]] = []
    untrusted_effects: list[tuple[ProvenanceEvent, FileEffectRecord]] = []
    for event in events:
        for effect in event.effects:
            effects.append(effect)
            target = trusted_effects if _is_trusted_effect(event, effect) else untrusted_effects
            target.append((event, effect))
    findings: list[ProvenanceFinding] = []
    warnings = _deduplicate_values(digest.evidence_warnings)

    # Evidence health -----------------------------------------------------
    stats = {
        "capture_count": len(events),
        "effect_count": len(effects),
        "trusted_effect_count": len(trusted_effects),
        "untrusted_effect_count": len(untrusted_effects),
        "quality_counts": dict(quality_counts),
        "association_counts": dict(association_counts),
        "file_count": len({effect.path for effect in effects}),
        "trusted_file_count": len({effect.path for _, effect in trusted_effects}),
        "actor_count": len({
            event.agent or event.actor_kind
            for event in events
            if event.agent or event.actor_kind
        }),
        "unreconstructed_count": len(digest.unreconstructed),
        "unreconstructed_files": _collect_unreconstructed_paths(digest, untrusted_effects),
    }
    if warnings:
        findings.append(_build_finding(
            dimension="evidence_health", severity="warning", code="EVIDENCE_WARNINGS",
            summary=f"证据包含 {len(warnings)} 条覆盖或质量警告",
            explanation="; ".join(warnings), events=events, effects=effects,
        ))
    unmatched: list[ProvenanceEvent] = []
    ambiguous: list[ProvenanceEvent] = []
    conflicted: list[ProvenanceEvent] = []
    incomplete: list[ProvenanceEvent] = []
    for event in events:
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
    event_capture_ids = {event.capture_id for event in events}
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

    # Attribution integrity ----------------------------------------------
    for event in events:
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

    # File churn and ownership -------------------------------------------
    by_path: dict[str, list[tuple[ProvenanceEvent, FileEffectRecord]]] = defaultdict(list)
    for event, effect in trusted_effects:
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

    # Verification closure -----------------------------------------------
    test_calls = [
        call for call in digest.tool_trace
        if _is_test_call(call, _normalize_tool_name(call.name))
    ]
    test_events = [
        event for event in events
        if any(call.capture_id == event.capture_id for call in test_calls)
    ]
    for path, entries in by_path.items():
        if not _requires_verification(path):
            continue
        last_event, _ = entries[-1]
        post_tests = [
            event for event in test_events
            if _build_event_key(event) > _build_event_key(last_event)
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
        file_timelines=_build_timeline_summary(events),
        warnings=warnings,
    )
