"""Run-backed init keeps session boundaries and reuses the audit run importer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from sift.cli.main import app
from sift.evals.generator import EvalInitGenerator
from sift.evals.run_bundle_mining import prepare_run_bundle_mining_inputs
from sift.evals.transcript import build_digest
from sift.evals.trigger_mining import mine_untriggered_queries
from sift.models import EvalSet


def _bundle(tmp_path: Path, monkeypatch) -> Path:
    schema = {
        "$id": "urn:workflow-bundle:v1",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["id", "data"],
        "properties": {"data": {"type": "object", "required": ["members"]}},
    }
    raw = json.dumps(schema).encode()
    (tmp_path / "workflow.schema.json").write_bytes(raw)
    monkeypatch.setattr("sift.audit.run.SCHEMA_SHA256", hashlib.sha256(raw).hexdigest())
    members = [
        {
            "id": f"m{i}",
            "kind": "session",
            "sessionId": f"s{i}",
            "location": f"sessions/s{i}.native",
        }
        for i in range(1, 4)
    ]
    manifest = {
        "schema": "workflow-bundle",
        "version": 1,
        "id": "run1",
        "kind": "workflow",
        "data": {"members": members},
    }
    (tmp_path / "workflow.json").write_text(json.dumps(manifest), encoding="utf-8")

    def native(role: str, content):
        return {
            "type": role,
            "message": {"role": role, "content": content},
            "timestamp": "2026-01-01T00:00:00Z",
        }

    sessions = {
        "s1": [
            native("user", "完成第一个任务"),
            native(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": "a",
                        "name": "skill",
                        "input": {"name": "demo"},
                    }
                ],
            ),
            native(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": "read-a",
                        "name": "read",
                        "input": {"filePath": "a.txt"},
                    }
                ],
            ),
            native(
                "user",
                [{"type": "tool_result", "tool_use_id": "read-a", "content": "old"}],
            ),
            native("user", "完成第二个任务"),
            native(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": "b",
                        "name": "skill",
                        "input": {"name": "demo"},
                    }
                ],
            ),
        ],
        "s2": [
            native("user", "完成第三个任务"),
            native(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": "c",
                        "name": "skill",
                        "input": {"name": "demo"},
                    }
                ],
            ),
        ],
        "s3": [native("user", "检查第四个任务")],
    }
    for sid, rows in sessions.items():
        path = tmp_path / f"sessions/{sid}.native/{sid}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
    return tmp_path


def _skill(tmp_path: Path) -> Path:
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: demo\ndescription: 测试\n---\n# 规则\n先读文件。\n",
        encoding="utf-8",
    )
    return skill


def test_run_bundle_mining_selects_calls_and_keeps_each_session_context(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    _bundle(root, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    prepared = prepare_run_bundle_mining_inputs(
        root, "demo", "skill", work, include_unmatched=True
    )
    assert [session.session_id for session in prepared.sessions] == ["s1", "s2"]
    assert [session.call_ids for session in prepared.sessions] == [("a", "b"), ("c",)]
    assert len(prepared.probe_paths) == 3
    first = [
        json.loads(line)
        for line in prepared.sessions[0].scoped_path.read_text().splitlines()
    ]
    second = [
        json.loads(line)
        for line in prepared.sessions[1].scoped_path.read_text().splitlines()
    ]
    assert [
        row["message"]["content"]
        for row in first
        if row["message"]["role"] == "user"
        and isinstance(row["message"]["content"], str)
    ] == ["完成第一个任务", "完成第二个任务"]
    assert [
        row["message"]["content"]
        for row in second
        if row["message"]["role"] == "user"
        and isinstance(row["message"]["content"], str)
    ] == ["完成第三个任务"]
    assert build_digest(prepared.sessions[0].scoped_path).user_turns == [
        "完成第一个任务",
        "完成第二个任务",
    ]
    assert mine_untriggered_queries(prepared.probe_paths[2], "demo") == [
        "检查第四个任务"
    ]


def test_run_bundle_mining_without_kind_keeps_whole_matched_sessions(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    _bundle(root, monkeypatch)
    agent_only = root / "sessions/s3.native/s3.jsonl"
    agent_only.write_text(
        agent_only.read_text(encoding="utf-8")
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use", "id": "dispatch", "name": "task",
                        "input": {"subagent_type": "demo", "prompt": "处理第四个任务"},
                    }],
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()

    prepared = prepare_run_bundle_mining_inputs(root, "demo", None, work)

    assert [session.session_id for session in prepared.sessions] == ["s1", "s2", "s3"]
    assert [session.call_ids for session in prepared.sessions] == [("a", "b"), ("c",), ("dispatch",)]
    assert all(session.scoped_path == session.full_path for session in prepared.sessions)
    assert "完成第一个任务" in build_digest(prepared.sessions[0].scoped_path).user_turns


def test_run_bundle_mining_without_kind_skips_unlinked_child_call(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    _bundle(root, monkeypatch)
    child = root / "sessions/s3.native/subagents/unlinked.jsonl"
    child.parent.mkdir()
    child.write_text(
        json.dumps({
            "message": {"role": "assistant", "content": [{
                "type": "tool_use", "id": "hidden", "name": "skill", "input": {"name": "demo"},
            }]},
        }) + "\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()

    prepared = prepare_run_bundle_mining_inputs(root, "demo", None, work)

    assert [session.session_id for session in prepared.sessions] == ["s1", "s2"]
    assert any("s3: 找到目标调用，但执行记录无法可靠导入" in warning for warning in prepared.warnings)


def test_run_bundle_mining_child_skill_uses_dispatch_task(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    _bundle(root, monkeypatch)
    parent = root / "sessions/s3.native/s3.jsonl"
    parent.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in [
        {"message": {"role": "user", "content": "主会话的另一个请求"}, "timestamp": "2026-01-01T00:00:00Z"},
        {"message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "dispatch", "name": "Task",
            "input": {"subagent_type": "worker", "prompt": "处理子任务 Z"},
        }]}, "timestamp": "2026-01-01T00:00:01Z"},
    ]) + "\n", encoding="utf-8")
    child = root / "sessions/s3.native/subagents/child.jsonl"
    child.parent.mkdir()
    child.write_text(json.dumps({
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "child-skill", "name": "skill", "input": {"name": "demo"},
        }]}, "timestamp": "2026-01-01T00:00:02Z",
    }) + "\n", encoding="utf-8")
    child.with_suffix(".meta.json").write_text(json.dumps({"toolUseId": "dispatch"}), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    prepared = prepare_run_bundle_mining_inputs(root, "demo", "skill", work)
    session = next(item for item in prepared.sessions if item.session_id == "s3")
    digest = build_digest(session.scoped_path)

    assert session.call_ids == ("child-skill",)
    assert "派发给该子 agent 的任务：处理子任务 Z" in digest.user_turns
    assert "主会话的另一个请求" not in digest.user_turns


def test_init_from_run_generates_one_combined_evalset(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    _bundle(root, monkeypatch)
    skill = _skill(tmp_path)
    prompts = []

    def fake_llm(self, prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "skill_name": "demo",
                "evals": [
                    {
                        "id": 1,
                        "name": f"case-{len(prompts)}",
                        "prompt": f"task-{len(prompts)}",
                    }
                ],
            }
        )

    monkeypatch.setattr("sift.evals.generator.EvalInitGenerator._llm_call", fake_llm)
    output = tmp_path / "evals.json"
    result = CliRunner().invoke(
        app,
        [
            "init",
            str(skill),
            "--from-run",
            str(root),
            "--kind",
            "skill",
            "-o",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(prompts) == 2
    assert "完成第一个任务" in prompts[0] and "完成第二个任务" in prompts[0]
    assert "完成第三个任务" in prompts[1] and "完成第一个任务" not in prompts[1]
    assert [case["id"] for case in json.loads(output.read_text())["evals"]] == [1, 2]


def test_init_from_run_without_kind_generates_from_whole_sessions(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    _bundle(root, monkeypatch)
    skill = _skill(tmp_path)
    prompts = []

    def fake_llm(self, prompt):
        prompts.append(prompt)
        return json.dumps({"skill_name": "demo", "evals": [{"id": 1, "prompt": f"task-{len(prompts)}"}]})

    monkeypatch.setattr("sift.evals.generator.EvalInitGenerator._llm_call", fake_llm)
    output = tmp_path / "evals.json"
    result = CliRunner().invoke(app, ["init", str(skill), "--from-run", str(root), "-o", str(output)])

    assert result.exit_code == 0, result.output
    assert len(prompts) == 2
    assert "完成第一个任务" in prompts[0] and "完成第二个任务" in prompts[0]
    assert "完成第三个任务" in prompts[1]
    assert len(json.loads(output.read_text(encoding="utf-8"))["evals"]) == 2


def test_run_mining_discards_degraded_case_when_normal_case_covers_same_prompt(tmp_path, monkeypatch):
    skill = _skill(tmp_path)
    results = [
        {"skill_name": "demo", "evals": [{"id": 1, "prompt": "相同任务", "expected_output": "正常结果 A"}]},
        {"skill_name": "demo", "evals": [{"id": 1, "prompt": "相同任务", "expected_output": "正常结果 B"}]},
        {"skill_name": "demo", "evals": [{
            "id": 1, "prompt": "相同任务", "expected_output": "兜底结果", "flags": ["degraded:no-llm"],
        }]},
        {"skill_name": "demo", "evals": [{
            "id": 1, "prompt": "不同任务", "expected_output": "另一道兜底题", "flags": ["degraded:no-llm"],
        }]},
    ]
    generator = EvalInitGenerator()
    monkeypatch.setattr(generator, "generate_from_transcript", lambda *args, **kwargs: EvalSet.model_validate(results.pop(0)))

    combined = generator.generate_from_run(
        skill, [("s1", Path("a")), ("s2", Path("b")), ("s3", Path("c")), ("s4", Path("d"))], None
    )

    assert len(combined.evals) == 2
    assert [case.id for case in combined.evals] == [1, 2]
    assert "正常结果 A" in combined.evals[0].expected_output
    assert "正常结果 B" in combined.evals[0].expected_output
    assert "兜底结果" not in combined.evals[0].expected_output
    assert "degraded:no-llm" not in combined.evals[0].flags
    assert combined.evals[1].prompt == "不同任务"
    assert "degraded:no-llm" in combined.evals[1].flags


def test_init_run_trigger_probes_reads_unmatched_session(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    _bundle(root, monkeypatch)
    skill = _skill(tmp_path)

    def fake_llm(self, prompt):
        if "filtering user queries" in prompt:
            return json.dumps(
                {
                    "verdicts": [
                        {"index": 1, "verdict": "near_miss", "reason": "相关任务"}
                    ]
                }
            )
        return json.dumps(
            {"skill_name": "demo", "evals": [{"id": 1, "prompt": "task"}]}
        )

    monkeypatch.setattr("sift.evals.generator.EvalInitGenerator._llm_call", fake_llm)
    monkeypatch.setattr(
        "sift.evals.generator.EvalInitGenerator.generate_trigger_probes",
        lambda self, *a, **k: [],
    )
    output = tmp_path / "evals.json"
    result = CliRunner().invoke(
        app,
        [
            "init",
            str(skill),
            "--from-run",
            str(root),
            "--kind",
            "skill",
            "--trigger-probes",
            "-o",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    probes = json.loads(output.read_text())["trigger_probes"]
    assert any(
        probe["prompt"] == "检查第四个任务" and probe["source"] == "trace-mined"
        for probe in probes
    )


def test_init_from_run_rejects_untyped_trigger_probes_and_mixed_input(tmp_path):
    skill = _skill(tmp_path)
    runner = CliRunner()
    untyped_probes = runner.invoke(app, ["init", str(skill), "--from-run", "missing", "--trigger-probes"])
    assert untyped_probes.exit_code == 2 and "需要 --kind skill" in untyped_probes.output
    mixed = runner.invoke(
        app,
        ["init", str(skill), "--from-run", "missing", "--from-transcript", "x.jsonl"],
    )
    assert mixed.exit_code == 2 and "互斥" in mixed.output


def test_run_agent_mining_uses_linked_child_and_dispatch_task(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    _bundle(root, monkeypatch)
    parent = root / "sessions/s1.native/s1.jsonl"
    parent.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "dispatch-1",
                            "name": "task",
                            "input": {
                                "subagent_type": "reviewer",
                                "prompt": "审查这一段代码",
                            },
                        }
                    ],
                },
                "timestamp": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    child = root / "sessions/s1.native/subagents/c1.jsonl"
    child.parent.mkdir()
    child.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "read-1",
                            "name": "read",
                            "input": {"filePath": "code.py"},
                        }
                    ],
                },
                "timestamp": "2026-01-01T00:00:01Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    child.with_suffix(".meta.json").write_text(
        json.dumps({"toolUseId": "dispatch-1"}), encoding="utf-8"
    )
    work = tmp_path / "work"
    work.mkdir()
    prepared = prepare_run_bundle_mining_inputs(root, "reviewer", "agent", work)
    assert len(prepared.sessions) == 1
    scoped = [
        json.loads(line)
        for line in prepared.sessions[0].scoped_path.read_text().splitlines()
    ]
    assert scoped[0]["message"]["content"] == "派发给该 agent 的任务：审查这一段代码"
    assert any(rec.get("agent") == "reviewer" for rec in scoped)

    declaration = tmp_path / "reviewer.md"
    declaration.write_text(
        "---\nname: reviewer\ndescription: 审查代码\n---\n# 规则\n阅读代码。\n",
        encoding="utf-8",
    )
    prompts = []

    def fake_llm(self, prompt):
        prompts.append(prompt)
        return json.dumps(
            {"skill_name": "reviewer", "evals": [{"id": 1, "prompt": "审查代码"}]}
        )

    monkeypatch.setattr(EvalInitGenerator, "_llm_call", fake_llm)
    result = EvalInitGenerator().generate_from_run(
        declaration,
        [(prepared.sessions[0].session_id, prepared.sessions[0].scoped_path)],
        "agent",
    )
    assert len(result.evals) == 1
    assert "派发给该 agent 的任务：审查这一段代码" in prompts[0]
    assert "## Agent system prompt" in prompts[0]

    output = tmp_path / "agent-evals.json"
    cli_result = CliRunner().invoke(
        app,
        ["init", str(declaration), "--from-run", str(root), "--kind", "agent", "-o", str(output)],
    )
    assert cli_result.exit_code == 0, cli_result.output
    assert len(json.loads(output.read_text(encoding="utf-8"))["evals"]) == 1
