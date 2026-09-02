# Copyright (c) 2025-2026 Huawei Technologies Co., Ltd.
"""sift-records v2 provenance fields -> canonical records -> SessionDigest."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from sift.audit.slice import slice_transcript
from sift.audit.rebuild import rebuild
from sift.errors import InputError
from sift.evals.structured import load_structured_records
from sift.evals.transcript import _digest_from_records


def _blob(raw: bytes, *, binary: bool = False) -> tuple[str, dict]:
    digest = hashlib.sha256(raw).hexdigest()
    return digest, {
        "encoding": "base64" if binary else "utf-8",
        "content": base64.b64encode(raw).decode("ascii") if binary else raw.decode("utf-8"),
        "size": len(raw),
    }


def _capture(capture_id: str, *, started_at: str, effects: list[dict] | None = None) -> dict:
    return {
        "capture_id": capture_id,
        "quality": "observed",
        "actor_kind": "agent",
        "agent_name": "developer",
        "native_agent_id": "developer",
        "native_session_id": "child-session",
        "parent_native_session_id": "root-session",
        "runtime": "opencode",
        "model": "provider/model",
        "tool_name": "write",
        "tool_use_id": f"tool-{capture_id}",
        "tool_input": {"file_path": "/repo/foo.txt"},
        "tool_output": {"status": "ok"},
        "started_at": started_at,
        "finished_at": "2026-08-29T09:00:02Z",
        "succeeded": True,
        "is_error": False,
        "incomplete": False,
        "conflicted": False,
        "conflicts": [],
        "cwd": "/repo",
        "head_before": "before-head",
        "head_after": "after-head",
        "committed_sha": None,
        "attributes": {"parent_tool_use_id": "dispatch-1"},
        "effects": effects or [],
    }


def _bundle() -> dict:
    old_hash, old_blob = _blob(b"old\n")
    new_hash, new_blob = _blob(b"new\n")
    return {
        "format": "sift-records",
        "version": 2,
        "producer": "opsbot-test",
        "exported_at": "2026-08-29T10:00:00Z",
        "session": {
            "id": "audit-session",
            "native_sessions": [
                {"id": "root-session", "agent": "orchestrator", "parent_id": None},
                {"id": "child-session", "agent": "developer", "parent_id": "root-session"},
            ],
        },
        "records": [
            {
                "role": "assistant",
                "text": "write file",
                "agent": "developer",
                "event_id": "event-b",
                "iteration": 7,
                "session_id": "audit-session",
                "native_session_id": "child-session",
                "parent_native_session_id": "root-session",
                "parent_tool_use_id": "dispatch-1",
                "timestamp": "2026-08-29T09:00:00Z",
                "tool_calls": [{
                    "id": "tool-cap-b",
                    "name": "write",
                    "input": {"file_path": "/repo/foo.txt", "content": "new\n"},
                    "result": {"status": "ok"},
                    "is_error": False,
                    "timestamp": "2026-08-29T09:00:00.500Z",
                    "capture_id": "cap-b",
                }],
            },
        ],
        "captures": {
            "cap-b": _capture("cap-b", started_at="2026-08-29T09:00:01Z", effects=[{
                "path": "/repo/foo.txt",
                "before_blob": old_hash,
                "after_blob": new_hash,
                "additions": 1,
                "deletions": 1,
                "conflict": False,
            }]),
            # No tool call and no mutation: must remain visible as unmatched evidence.
            "cap-a": _capture("cap-a", started_at="2026-08-29T08:59:59Z"),
        },
        "blobs": {old_hash: old_blob, new_hash: new_blob},
        "coverage": {"warnings": ["readonly subagent tools unavailable"]},
        "warnings": ["partial runtime coverage"],
    }


def _write(tmp_path, data: dict):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_v2_preserves_tool_capture_lineage_and_snapshots(tmp_path):
    records = load_structured_records(_write(tmp_path, _bundle()))
    digest = _digest_from_records(records, root="/repo", locate_turns=True)

    call = digest.tool_trace[0]
    assert call.tool_use_id == "tool-cap-b"
    assert call.timestamp == "2026-08-29T09:00:00.500Z"
    assert call.record_timestamp == "2026-08-29T09:00:00Z"
    assert call.event_id == "event-b"
    assert call.iteration == 7
    assert call.result == {"status": "ok"}
    assert call.capture_id == "cap-b"
    assert call.evidence_quality == "observed"
    assert call.association_quality == "exact"
    assert call.agent == "developer"
    assert call.parent_agent == "orchestrator"
    assert call.native_session_id == "child-session"
    assert call.parent_native_session_id == "root-session"
    assert call.runtime == "opencode"
    assert call.model == "provider/model"
    assert call.started_at == "2026-08-29T09:00:01Z"
    assert call.succeeded is True

    ref = "capture:cap-b:/repo/foo.txt"
    assert call.evidence_refs == [ref]
    effect = digest.evidence_refs[ref]
    assert effect.before_content == "old\n"
    assert effect.after_content == "new\n"
    assert effect.before_binary is False
    assert effect.after_binary is False
    assert digest.evidence_metadata["session_id"] == "audit-session"
    assert digest.evidence_warnings == [
        "partial runtime coverage",
        "readonly subagent tools unavailable",
    ]


def test_v2_timeline_keeps_empty_unmatched_capture_and_sorts_stably(tmp_path):
    digest = _digest_from_records(load_structured_records(_write(tmp_path, _bundle())))

    assert [event.capture_id for event in digest.provenance_timeline] == ["cap-a", "cap-b"]
    assert [event.sequence for event in digest.provenance_timeline] == [1, 2]
    unmatched = digest.provenance_timeline[0]
    assert unmatched.association_quality == "unmatched"
    assert unmatched.effects == []
    assert unmatched.agent == "developer"
    assert unmatched.parent_agent == "orchestrator"
    assert unmatched.parent_tool_use_id == "dispatch-1"
    assert unmatched.tool_input == {"file_path": "/repo/foo.txt"}
    assert unmatched.tool_output == {"status": "ok"}


def test_v2_duplicate_capture_reference_is_ambiguous_but_not_duplicated(tmp_path):
    data = _bundle()
    duplicate = dict(data["records"][0]["tool_calls"][0])
    duplicate["id"] = "second-tool-id"
    data["records"][0]["tool_calls"].append(duplicate)
    digest = _digest_from_records(load_structured_records(_write(tmp_path, data)))

    assert len(digest.provenance_timeline) == 2
    calls = [call for call in digest.tool_trace if call.capture_id == "cap-b"]
    assert len(calls) == 2
    assert all(call.association_quality == "ambiguous" for call in calls)
    assert next(event for event in digest.provenance_timeline if event.capture_id == "cap-b").association_quality == "ambiguous"


def test_v2_equal_timestamp_uses_event_id_as_stable_tiebreaker(tmp_path):
    data = _bundle()
    data["captures"].pop("cap-a")
    data["captures"].pop("cap-b")
    first = data["records"][0]
    first["event_id"] = "event-z"
    first["tool_calls"][0]["capture_id"] = "cap-z"
    first["tool_calls"][0]["id"] = "tool-cap-z"
    data["captures"]["cap-z"] = _capture("cap-z", started_at="2026-08-29T09:00:01Z")
    data["records"].append({
        **first,
        "event_id": "event-a",
        "tool_calls": [{
            **first["tool_calls"][0],
            "id": "tool-cap-a",
            "capture_id": "cap-a",
        }],
    })
    # Same instant expressed with a different timezone offset: ordering must be
    # chronological first, then stable event ID (not raw string ordering).
    data["captures"]["cap-a"] = _capture("cap-a", started_at="2026-08-29T10:00:01+01:00")

    timeline = _digest_from_records(load_structured_records(_write(tmp_path, data))).provenance_timeline
    assert [event.event_id for event in timeline] == ["event-a", "event-z"]


def test_v2_rejects_unknown_version_missing_blob_and_bad_hash(tmp_path):
    unknown = _bundle()
    unknown["version"] = 3
    with pytest.raises(InputError, match="不支持 sift-records version 3"):
        load_structured_records(_write(tmp_path, unknown))

    missing = _bundle()
    missing["blobs"] = {}
    with pytest.raises(InputError, match="引用了缺失 blob"):
        load_structured_records(_write(tmp_path, missing))

    corrupt = _bundle()
    first_hash = next(iter(corrupt["blobs"]))
    corrupt["blobs"][first_hash]["content"] = "tampered"
    with pytest.raises(InputError, match="SHA-256 校验失败"):
        load_structured_records(_write(tmp_path, corrupt))


@pytest.mark.parametrize("version", [pytest.param(..., id="missing"), None, "1", 1])
def test_v1_version_spellings_keep_legacy_loader_path(tmp_path, version):
    data = {
        "format": "sift-records",
        "records": [{
            "role": "assistant",
            "tool_calls": [{"name": "read", "input": {"file_path": "x.py"}}],
        }],
    }
    if version is not ...:
        data["version"] = version
    records = load_structured_records(_write(tmp_path, data))
    assert records[0]["message"]["content"][0]["id"] == "tu_1"


def test_v2_non_object_tool_input_is_a_clean_input_error(tmp_path):
    data = _bundle()
    data["records"][0]["tool_calls"][0]["input"] = ["not", "an", "object"]
    with pytest.raises(InputError, match="tool call input 必须是对象"):
        load_structured_records(_write(tmp_path, data))


def test_v2_model_type_error_is_a_clean_input_error_during_load(tmp_path):
    data = _bundle()
    data["captures"]["cap-b"]["effects"][0]["additions"] = "one"
    with pytest.raises(InputError, match="文件 effect 字段类型无效"):
        load_structured_records(_write(tmp_path, data))


def test_v2_preserves_binary_blob_metadata_without_decoding_as_text(tmp_path):
    data = _bundle()
    binary_hash, binary_blob = _blob(b"\x00\xff", binary=True)
    data["blobs"][binary_hash] = binary_blob
    data["captures"]["cap-c"] = _capture("cap-c", started_at="2026-08-29T09:01:00Z", effects=[{
        "path": "/repo/asset.bin",
        "before_blob": None,
        "after_blob": binary_hash,
        "additions": 0,
        "deletions": 0,
        "conflict": False,
    }])
    data["records"][0]["tool_calls"].append({
        "id": "tool-cap-c",
        "name": "write",
        "input": {"file_path": "/repo/asset.bin"},
        "capture_id": "cap-c",
    })

    digest = _digest_from_records(load_structured_records(_write(tmp_path, data)))
    effect = digest.evidence_refs["capture:cap-c:/repo/asset.bin"]
    assert effect.after_binary is True
    assert effect.after_content is None
    assert effect.after_size == 2


def test_provenance_paths_do_not_change_existing_root_inference(tmp_path):
    data = _bundle()
    data["captures"]["cap-b"]["effects"][0]["path"] = "/outside/evidence/foo.txt"
    digest = _digest_from_records(load_structured_records(_write(tmp_path, data)))
    assert [item.path for item in digest.produced_files] == ["foo.txt"]


def test_standalone_capture_is_usable_when_records_are_empty(tmp_path):
    data = _bundle()
    data["records"] = []
    data["captures"] = {"cap-a": data["captures"]["cap-a"]}
    records = load_structured_records(_write(tmp_path, data))
    jsonl = tmp_path / "expanded.jsonl"
    jsonl.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )
    _, _, digest = rebuild(jsonl)
    assert [event.capture_id for event in digest.provenance_timeline] == ["cap-a"]


def test_v1_output_has_no_v2_metadata(tmp_path):
    data = {
        "format": "sift-records",
        "records": [{
            "role": "assistant",
            "agent": "developer",
            "tool_calls": [{"id": "native-v1-id", "name": "read", "input": {"file_path": "x.py"}}],
        }],
    }
    records = load_structured_records(_write(tmp_path, data))
    assert "_sift_bundle_meta" not in records[0]
    assert set(records[0]) == {"message", "parent_tool_use_id", "agent", "_turn_index"}
    assert records[0]["message"]["content"][0]["id"] == "tu_1"
    call = _digest_from_records(records).tool_trace[0]
    assert call.tool_use_id is None
    assert call.capture_id is None
    assert call.timestamp is None


def test_slice_propagates_v2_metadata_without_assigning_unmatched_to_skill(tmp_path):
    data = _bundle()
    data["records"][0]["tool_calls"].insert(0, {
        "id": "skill-1",
        "name": "skill",
        "input": {"name": "demo"},
    })
    records = load_structured_records(_write(tmp_path, data))
    jsonl = tmp_path / "expanded.jsonl"
    jsonl.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records), encoding="utf-8")

    segments = slice_transcript(jsonl, "demo", "skill")
    assert len(segments) == 1
    assert segments[0][0]["_sift_bundle_meta"]["version"] == 2
    assert "unmatched_captures" not in segments[0][0]["_sift_bundle_meta"]
    assert all("_sift_standalone_capture" not in record for record in segments[0])
