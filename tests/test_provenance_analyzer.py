"""Tests for the deterministic provenance behaviour analyser."""

import pytest

from sift.audit.provenance import analyze_provenance
from sift.evals.transcript import SessionDigest
from sift.models import FileEffectRecord, ProvenanceEvent, ToolCallRecord


@pytest.mark.parametrize("value,expected", [(None, 0), ("3", 0), (3.5, 0), (True, 0), (-1, 0), ([], 0), (0, 0), (3, 3)])
def test_summary_counts_do_not_coerce_invalid_statistics(value, expected):
    from sift.models import ProvenanceAuditResult

    result = ProvenanceAuditResult(statistics={"capture_count": value})
    assert result.get_count("capture_count") == expected
    assert result.get_count("effect_count") == 0
    assert result.statistics["capture_count"] == value


def _effect(capture: str, path: str, *, quality: str = "observed", after: str | None = None):
    return FileEffectRecord(
        path=path,
        evidence_ref=f"capture:{capture}:{path}",
        quality=quality,
        capture_id=capture,
        actor="developer",
        tool_name="Write",
        before_blob=f"before-{capture}",
        after_blob=after or f"after-{capture}",
        additions=1,
        deletions=1,
    )


def _event(capture: str, *, at: str | None, tool: str = "Write", agent: str = "developer",
           quality: str = "observed", association: str = "exact", effects=None,
           is_error: bool | None = False, succeeded: bool | None = True,
           tool_id: str | None = None):
    return ProvenanceEvent(
        sequence=1,
        timestamp=at,
        capture_id=capture,
        tool_use_id=tool_id or f"tool-{capture}",
        association_quality=association,
        quality=quality,
        agent=agent,
        tool_name=tool,
        is_error=is_error,
        succeeded=succeeded,
        effects=effects or [],
    )


def _digest(events, calls=None, **kwargs):
    return SessionDigest(
        evidence_metadata={"version": 2},
        provenance_timeline=events,
        tool_trace=calls or [],
        **kwargs,
    )


def test_v1_or_ordinary_digest_is_unavailable():
    result = analyze_provenance(SessionDigest())
    assert result.available is False
    assert result.findings == []


def test_observed_effect_builds_timeline_and_final_writer():
    event = _event("c1", at="2026-09-01T10:00:00Z", effects=[_effect("c1", "src/a.py")])
    result = analyze_provenance(_digest([event]))
    assert result.available is True
    assert result.statistics["trusted_effect_count"] == 1
    assert result.statistics["final_writers"]["src/a.py"] == "developer"
    assert result.file_timelines["src/a.py"][0].trusted is True
    assert any(f.code == "FINAL_MODIFICATION_UNVERIFIED" for f in result.findings)


def test_unmatched_and_untrusted_effects_are_not_deterministic():
    event = _event(
        "c1", at="2026-09-01T10:00:00Z", quality="incomplete", association="unmatched",
        effects=[_effect("c1", "src/a.py", quality="incomplete")],
    )
    result = analyze_provenance(_digest([event]))
    assert result.statistics["trusted_effect_count"] == 0
    assert any(f.code == "UNMATCHED_CAPTURE" for f in result.findings)
    assert result.file_timelines["src/a.py"][0].trusted is False


def test_trusted_and_untrusted_effects_partition_all_effects():
    trusted = _effect("trusted", "src/trusted.py")
    conflicted = _effect("conflicted", "src/conflicted.py")
    conflicted.conflict = True
    events = [
        _event("trusted", at="2026-09-01T10:00:00Z", effects=[trusted]),
        _event("conflicted", at="2026-09-01T10:01:00Z", effects=[conflicted]),
    ]
    result = analyze_provenance(_digest(events))
    assert result.statistics["effect_count"] == (
        result.statistics["trusted_effect_count"]
        + result.statistics["untrusted_effect_count"]
    )


def test_evidence_health_covers_all_quality_and_reference_findings():
    unmatched = _event(
        "unmatched", at="2026-09-01T10:00:00Z", association="unmatched",
        effects=[_effect("unmatched", "src/unmatched.py")],
    )
    ambiguous = _event(
        "ambiguous", at="2026-09-01T10:01:00Z", quality="ambiguous",
        effects=[_effect("ambiguous", "src/ambiguous.py", quality="ambiguous")],
    )
    conflicted = _event(
        "conflicted", at="2026-09-01T10:02:00Z", quality="conflicted",
        effects=[_effect("conflicted", "src/conflicted.py", quality="conflicted")],
    )
    conflicted.conflicted = True
    incomplete = _event(
        "incomplete", at="2026-09-01T10:03:00Z", quality="incomplete",
        effects=[_effect("incomplete", "src/incomplete.py", quality="incomplete")],
    )
    untrusted_effect_event = _event(
        "untrusted-effect", at="2026-09-01T10:04:00Z",
        effects=[_effect("untrusted-effect", "src/untrusted.py", quality="incomplete")],
    )
    missing_call = ToolCallRecord(
        name="Write", capture_id="missing", tool_use_id="missing-tool", turn=7,
    )
    digest = _digest(
        [unmatched, ambiguous, conflicted, incomplete, untrusted_effect_event],
        calls=[missing_call],
        evidence_warnings=["capture 覆盖不完整"],
        unreconstructed=["src/unknown.py"],
    )
    result = analyze_provenance(digest)
    codes = {finding.code for finding in result.findings}
    untrusted_result = analyze_provenance(_digest([untrusted_effect_event]))
    codes.update(finding.code for finding in untrusted_result.findings)
    assert {
        "EVIDENCE_WARNINGS", "UNMATCHED_CAPTURE", "MISSING_CAPTURE_EVENT",
        "AMBIGUOUS_CAPTURE", "CONFLICTED_CAPTURE", "INCOMPLETE_CAPTURE",
        "UNTRUSTED_EFFECT", "UNRECONSTRUCTED_FILE",
    } <= codes


def test_attribution_integrity_detects_error_and_read_mutation_and_empty_write():
    error = _event("err", at="2026-09-01T10:00:00Z", is_error=True,
                   effects=[_effect("err", "src/a.py")])
    read = _event("read", at="2026-09-01T10:01:00Z", tool="Read",
                  effects=[_effect("read", "src/b.py")])
    empty = _event("empty", at="2026-09-01T10:02:00Z", effects=[])
    result = analyze_provenance(_digest([error, read, empty]))
    codes = {f.code for f in result.findings}
    assert {"ERROR_WITH_MUTATION", "NON_WRITE_MUTATION", "WRITE_WITHOUT_EFFECT"} <= codes


def test_error_status_takes_precedence_over_success_status():
    event = _event("contradictory", at="2026-09-01T10:00:00Z", effects=[_effect("contradictory", "src/a.py")])
    event.succeeded = True
    event.is_error = True
    result = analyze_provenance(_digest([event]))
    assert any(f.code == "ERROR_WITH_MUTATION" for f in result.findings)


def test_read_and_non_mutating_bash_are_normal():
    read = _event("read", at="2026-09-01T10:00:00Z", tool="Read", effects=[])
    shell = _event("shell", at="2026-09-01T10:01:00Z", tool="Bash", effects=[])
    shell.tool_input = {"command": "ls -la src"}
    result = analyze_provenance(_digest([read, shell]))
    assert not any(f.code == "WRITE_WITHOUT_EFFECT" for f in result.findings)
    assert not any(f.code == "NON_WRITE_MUTATION" for f in result.findings)


def test_churn_detects_revert_and_actor_handoff_gap():
    first = _event("one", at="2026-09-01T10:00:00Z", effects=[_effect("one", "src/a.py", after="state-a")])
    second = _event("two", at="2026-09-01T10:01:00Z", agent="other",
                    effects=[_effect("two", "src/a.py", after="state-b")])
    second.parent_agent = "unrelated"
    third = _event("three", at="2026-09-01T10:02:00Z", agent="developer",
                   effects=[_effect("three", "src/a.py", after="state-a")])
    result = analyze_provenance(_digest([first, second, third]))
    codes = {f.code for f in result.findings}
    assert "REPEATED_FILE_MODIFICATION" in codes
    assert "REVERTED_FILE_STATE" in codes
    assert "CROSS_ACTOR_WITHOUT_HANDOFF" in codes


def test_churn_marks_full_write_overwrite_as_information():
    effect = _effect("overwrite", "src/a.py")
    effect.before_content = "old one\nold two\n"
    effect.after_content = "new\n"
    effect.deletions = 3
    event = _event("overwrite", at="2026-09-01T10:00:00Z", effects=[effect])
    result = analyze_provenance(_digest([event]))
    finding = next(f for f in result.findings if f.code == "FULL_FILE_OVERWRITE")
    assert finding.severity == "info"


def test_verification_closure_detects_unverified_change_after_failed_test():
    test_event = _event("test", at="2026-09-01T10:01:00Z", tool="Bash", effects=[], tool_id="test-tool")
    test_event.tool_input = {"command": "pytest -q"}
    test_event.is_error = True
    change = _event("change", at="2026-09-01T10:02:00Z", effects=[_effect("change", "src/a.py")])
    calls = [
        ToolCallRecord(name="Bash", input={"command": "pytest -q"}, capture_id="test",
                       tool_use_id="test-tool", is_error=True),
    ]
    result = analyze_provenance(_digest([test_event, change], calls))
    codes = {f.code for f in result.findings}
    assert "FINAL_MODIFICATION_UNVERIFIED" in codes
    assert "TEST_FAILURE_THEN_MODIFY" not in codes


def test_verification_closure_accepts_test_after_final_modification():
    change = _event("change", at="2026-09-01T10:00:00Z", effects=[_effect("change", "src/a.py")])
    test_event = _event("test", at="2026-09-01T10:01:00Z", tool="Bash", effects=[], tool_id="test-tool")
    test_event.tool_input = {"command": "pytest -q"}
    calls = [ToolCallRecord(name="Bash", input={"command": "pytest -q"}, capture_id="test", tool_use_id="test-tool")]
    result = analyze_provenance(_digest([change, test_event], calls))
    assert not any(f.code == "FINAL_MODIFICATION_UNVERIFIED" for f in result.findings)


def test_markdown_changes_are_exempt_from_post_change_test_requirement():
    change = _event("docs", at="2026-09-01T10:00:00Z", effects=[_effect("docs", "README.md")])
    result = analyze_provenance(_digest([change]))
    assert not any(f.code == "FINAL_MODIFICATION_UNVERIFIED" for f in result.findings)


def test_invalid_timestamp_events_are_kept_for_health_but_excluded_from_state_reasoning():
    change = _event("z-write", at=None, effects=[_effect("z-write", "src/a.py")])
    test_event = _event("a-test", at=None, tool="Bash", effects=[])
    test_event.tool_input = {"command": "pytest -q"}
    calls = [
        ToolCallRecord(name="Bash", input={"command": "pytest -q"}, capture_id="a-test"),
    ]
    result = analyze_provenance(_digest([change, test_event], calls))
    codes = {finding.code for finding in result.findings}
    assert result.statistics["effect_count"] == 1
    assert result.statistics["trusted_effect_count"] == 1
    assert "src/a.py" in result.file_timelines
    assert "src/a.py" not in result.statistics.get("final_writers", {})
    assert "VERIFICATION_ORDER_UNKNOWN" not in codes
    assert "FINAL_MODIFICATION_UNVERIFIED" not in codes


def test_bash_heuristics_ignore_stream_redirection_and_commit_message():
    change = _event("change", at="2026-09-01T10:00:00Z", effects=[_effect("change", "src/a.py")])
    grep_event = _event("grep", at="2026-09-01T10:01:00Z", tool="Bash", effects=[])
    grep_event.tool_input = {"command": "grep foo bar.txt 2>&1"}
    commit_event = _event("commit", at="2026-09-01T10:02:00Z", tool="Bash", effects=[])
    commit_event.tool_input = {"command": 'git commit -m "test"'}
    calls = [
        ToolCallRecord(name="Bash", input=grep_event.tool_input, capture_id="grep", succeeded=True),
        ToolCallRecord(name="Bash", input=commit_event.tool_input, capture_id="commit", succeeded=True),
    ]
    result = analyze_provenance(_digest([change, grep_event, commit_event], calls))
    codes = {finding.code for finding in result.findings}
    assert "WRITE_WITHOUT_EFFECT" not in codes
    assert "FINAL_MODIFICATION_UNVERIFIED" in codes
