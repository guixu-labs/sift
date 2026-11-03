# tests/test_init_cli.py
from typer.testing import CliRunner

from sift.cli.main import app
from sift.models import EvalCase, EvalSet

runner = CliRunner()


def _fake_evalset():
    return EvalSet(skill_name="s", evals=[EvalCase(id=1, name="n", prompt="p", level="L1")])


def _skill_dir(tmp_path):
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# b\n", encoding="utf-8")
    return sp


def test_init_without_flag_calls_generate_with_llm(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"),
        lambda self, sp, **k: called.__setitem__("default", True) or _fake_evalset(),
    )
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_from_transcript"),
        lambda self, sp, tp, on_heartbeat=None, kind=None: called.__setitem__("transcript", True) or _fake_evalset(),
    )
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert called.get("default") is True and "transcript" not in called


def test_init_with_from_transcript_routes(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_from_transcript"),
        lambda self, sp, tp, on_heartbeat=None, kind=None: called.__setitem__("tp", tp) or _fake_evalset(),
    )
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"),
        lambda self, sp, **k: called.__setitem__("default", True) or _fake_evalset(),
    )
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert called.get("tp") == tp and "default" not in called


def test_init_kind_agent_calls_generate_for_agent(monkeypatch, tmp_path):
    """init --kind agent 走 generate_for_agent(不走 skill 的 generate_with_llm)。"""
    called = {}
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_for_agent"),
        lambda self, sp, **k: called.__setitem__("agent", True) or _fake_evalset(),
    )
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"),
        lambda self, sp, **k: called.__setitem__("skill", True) or _fake_evalset(),
    )
    agent_md = tmp_path / "reviewer.md"
    agent_md.write_text(("---\nname: reviewer\ndescription: 审查代码\n---\n# 你是审查者\n只读不改\n"), encoding="utf-8")
    r = runner.invoke(app, ["init", str(agent_md), "--kind", "agent", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert called.get("agent") is True and "skill" not in called


def test_init_empty_result_does_not_clobber_existing(monkeypatch, tmp_path):
    """生成 0 题(LLM 失败耗尽重试)+ 非追加 + 已有考卷 → exit 1,既有文件字节不动。

    覆盖式 init 的语义是「换新考卷」,不是「失败时清空」——空考卷覆盖会把
    agent 既有题毁在瞬时故障手里。
    """

    agent_md = tmp_path / "reviewer.md"
    agent_md.write_text(("---\nname: reviewer\ndescription: 审查代码\n---\n# 你是审查者\n只读不改\n"), encoding="utf-8")
    out = tmp_path / "evals" / "evals.json"
    out.parent.mkdir(parents=True)
    prior = EvalSet(skill_name="reviewer", evals=[EvalCase(id=1, name="旧题", level="L1", prompt="旧 prompt")])
    out.write_text(prior.model_dump_json(indent=2), encoding="utf-8")
    before = out.read_bytes()

    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_for_agent"),
        lambda self, sp, **k: EvalSet(skill_name="reviewer", evals=[]),
    )
    r = runner.invoke(app, ["init", str(agent_md), "--kind", "agent", "-o", str(out.parent)])
    assert r.exit_code == 1, r.output
    assert "保留既有" in r.output
    assert out.read_bytes() == before, "既有考卷字节必须原样保留"


def test_init_kind_root_rejected(tmp_path):
    """--kind root 在 init 不接受(只 skill/agent)→ 非零退出。"""
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "--kind", "root", "-o", str(tmp_path / "out")])
    assert r.exit_code != 0
    assert "skill / agent" in r.output


def _fake_evalset_degraded():
    return EvalSet(
        skill_name="s",
        evals=[EvalCase(id=1, name="transcript-turn-1", prompt="p", level="L1", flags=["degraded:no-llm"])],
    )


def test_init_warns_on_fallback_flag(monkeypatch, tmp_path):
    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_from_transcript"),
        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset_degraded(),
    )
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("no-llm" in p for p in printed), printed


def test_init_no_warning_without_fallback_flag(monkeypatch, tmp_path):
    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_from_transcript"),
        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset(),
    )
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert not any("no-llm" in p for p in printed), printed


def test_init_threads_model_flag_to_generator(monkeypatch, tmp_path):
    """init --model 接线:flag 须透到 EvalInitGenerator(model=...)。曾硬编码 model=None,
    flag 是静默 no-op 死标志(对照 add 已正确接线)。
    """
    captured = {}

    def fake_init(self, model=None, timeout=300, **_kw):
        captured["model"] = model

    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.__init__"), fake_init)
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"), lambda self, sp, **k: _fake_evalset()
    )
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "--model", "sonnet", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert captured.get("model") == "sonnet", f"--model 没透到 generator: {captured}"


def test_init_threads_timeout_flag_to_generator(monkeypatch, tmp_path):
    """init --timeout 接线:flag 须透到 EvalInitGenerator(timeout=...);默认 600。"""
    captured = {}

    def fake_init(self, model=None, timeout=600, **_kw):
        captured["timeout"] = timeout

    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.__init__"), fake_init)
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"), lambda self, sp, **k: _fake_evalset()
    )
    sp = _skill_dir(tmp_path)
    # 显式传 --timeout
    r = runner.invoke(app, ["init", str(sp), "--timeout", "300", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert captured.get("timeout") == 300, f"--timeout 没透到 generator: {captured}"
    # 默认(不传)→ 600
    captured.clear()
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out2")])
    assert r.exit_code == 0, r.output
    assert captured.get("timeout") == 600, f"默认 timeout 应为 600: {captured}"


def test_init_kind_threads_flag(monkeypatch, tmp_path):
    """init --kind skill 接线:flag 须透到 generate_from_transcript(kind="skill")。"""
    captured = {}

    def fake(self, sp, tp, on_heartbeat=None, kind=None):
        captured["kind"] = kind
        return _fake_evalset()

    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.generate_from_transcript"), fake)
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"), lambda self, sp, **k: _fake_evalset()
    )
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(
        app, ["init", str(sp), "--from-transcript", str(tp), "--kind", "skill", "-o", str(tmp_path / "out")]
    )
    assert r.exit_code == 0, r.output
    assert captured.get("kind") == "skill", f"--kind 没透到 generator: {captured}"


def test_init_kind_agent_from_transcript_threads_kind(monkeypatch, tmp_path):
    """init --kind agent --from-transcript:kind=agent 透到 generate_from_transcript(agent mining 路由)。

    之前只测 skill 版透传;agent 版是 agent mining 入口,补覆盖。
    """
    captured = {}

    def fake(self, sp, tp, on_heartbeat=None, kind=None):
        captured["kind"] = kind
        return _fake_evalset()

    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.generate_from_transcript"), fake)
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(
        app, ["init", str(sp), "--from-transcript", str(tp), "--kind", "agent", "-o", str(tmp_path / "out")]
    )
    assert r.exit_code == 0, r.output
    assert captured.get("kind") == "agent", f"--kind agent 没透到 generator: {captured}"


def _multi_agent_transcript(tmp_path):
    import json

    tp = tmp_path / "session.jsonl"
    transcript_records = [
        {
            "agent": "orchestrator",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "派发子 agent"}]},
        },
        {
            "agent": "reviewer",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "审代码"}]},
        },
    ]
    tp.write_text(
        "\n".join(json.dumps(r) for r in transcript_records),
        encoding="utf-8",
    )
    return tp


def test_init_warns_multi_agent_without_kind(monkeypatch, tmp_path):
    """init --from-transcript 不带 --kind、transcript 涉及多 agent → 警告建议 --kind agent。

    对称多 skill 警告:agent mining 的切段机制(slice_transcript kind=agent)本身齐全,
    缺的是这道「忘带 --kind」的提醒——不切分会拿整段 transcript 挖、串味。
    """
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_from_transcript"),
        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset(),
    )
    agent_md = tmp_path / "reviewer.md"
    agent_md.write_text(("---\nname: reviewer\ndescription: 审查代码\n---\n# 审查\n只读\n"), encoding="utf-8")
    tp = _multi_agent_transcript(tmp_path)
    r = runner.invoke(app, ["init", str(agent_md), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "--kind agent" in r.output, f"多 agent 应警告建议 --kind agent:{r.output!r}"


def test_init_no_multi_agent_warning_with_kind(monkeypatch, tmp_path):
    """带了 --kind agent → 不触发多 agent 串味警告(会切段,不串味)。"""
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_from_transcript"),
        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset(),
    )
    agent_md = tmp_path / "reviewer.md"
    agent_md.write_text(("---\nname: reviewer\ndescription: 审查代码\n---\n# 审查\n只读\n"), encoding="utf-8")
    tp = _multi_agent_transcript(tmp_path)
    r = runner.invoke(
        app, ["init", str(agent_md), "--from-transcript", str(tp), "--kind", "agent", "-o", str(tmp_path / "out")]
    )
    assert r.exit_code == 0, r.output
    assert "可能串味" not in r.output, f"带 --kind agent 不该再警告串味:{r.output!r}"


def test_init_agent_md_default_output_writes_to_agent_dir(monkeypatch, tmp_path):
    """init <agent.md> 不传 -o → 默认写 agent 目录(.md 所在目录)的 evals/evals.json。

    像 skill(<skill>/evals/evals.json):agent .md 经 resolve_skill_dir 保持文件,
    init 默认 evals_dir 取 .md 的 parent(而非 .md/evals 会崩)。
    """
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_for_agent"), lambda self, sp, **k: _fake_evalset()
    )
    agent_dir = tmp_path / "pr-reviewer"
    agent_dir.mkdir()
    ap = agent_dir / "pr-reviewer.md"
    ap.write_text(("---\nname: pr-reviewer\ndescription: d\n---\n# body\n"), encoding="utf-8")
    r = runner.invoke(app, ["init", str(ap), "--kind", "agent"])
    assert r.exit_code == 0, r.output
    assert (agent_dir / "evals" / "evals.json").exists(), "evals.json 应默认写 agent 目录"


def test_init_prints_unknown_field_warning(monkeypatch, tmp_path):
    """LLM 输出拼错字段(expected_output→expected)→ init 终端打印 unknown-field warn。

    端到端:init → generate_with_llm → call_claude(mock)→ _parse_llm_response 收集
    → gen.init_warnings → init 命令读去打印。pydantic extra=ignore 仍解析成功,但字段
    被静默丢;warn 让人看见"考卷缺字段"。
    """

    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    fake = _init_output_field_response("expected")
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: fake)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("未知字段" in p and "expected" in p for p in printed), printed


def test_init_no_unknown_warning_when_fields_clean(monkeypatch, tmp_path):
    """LLM 输出全合法字段 → 不打印 unknown-field warn(避免噪声)。"""

    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    fake = _init_output_field_response("expected_output")
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: fake)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert not any("未知字段" in p for p in printed), printed


def test_init_warns_on_multiskill_without_kind(monkeypatch, tmp_path):
    """多 skill transcript + 不传 --kind → 警告(防静默串味:把别 skill 活动当本 skill 挖)。"""
    import json

    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_from_transcript"),
        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset(),
    )
    sp = _skill_dir(tmp_path)  # name "s"
    tp = tmp_path / "multi.jsonl"
    transcript_records = [
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": "s"}}],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "s2", "name": "Skill", "input": {"skill": "other"}}],
            }
        },
    ]
    tp.write_text(
        "\n".join(json.dumps(r) for r in transcript_records),
        encoding="utf-8",
    )
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("涉及" in p and "skill" in p for p in printed), printed


def test_init_no_warning_on_single_skill(monkeypatch, tmp_path):
    """单 skill transcript + 不传 --kind → 不警告。"""
    import json

    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_from_transcript"),
        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset(),
    )
    sp = _skill_dir(tmp_path)  # name "s"
    tp = tmp_path / "single.jsonl"
    transcript_records = [
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": "s"}}],
            }
        },
    ]
    tp.write_text(
        "\n".join(json.dumps(r) for r in transcript_records),
        encoding="utf-8",
    )
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert not any("涉及" in p and "skill" in p for p in printed), printed


def test_init_notes_same_prompt_group(monkeypatch, tmp_path):
    """生成出同 prompt 多 scenario 题(带 split flag)→ init 打 ℹ 提示「非重复」。
    防读者把同 prompt 多道误读成 bug(复现 log-error-analyzer-zh 那次误读场景)。
    断言用「同 prompt」+「非重复」整串——distinctive,避开 tmp_path 假通过陷阱。
    """
    evalset = EvalSet(
        skill_name="s",
        evals=[
            EvalCase(id=i, name=f"scenario-{i}", prompt="分析 logs/app.log 里的错误", flags=["split:3-evals-conflict"])
            for i in (1, 2, 3)
        ],
    )
    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.generate_with_llm"), lambda self, sp, **k: evalset)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("同 prompt" in p and "非重复" in p for p in printed), printed


def test_init_no_note_when_prompts_unique(monkeypatch, tmp_path):
    """每道 prompt 都不同 → 不打同 prompt 提示(免噪音)。"""
    evalset = EvalSet(
        skill_name="s", evals=[EvalCase(id=1, name="a", prompt="题一"), EvalCase(id=2, name="b", prompt="题二")]
    )
    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.generate_with_llm"), lambda self, sp, **k: evalset)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert not any("同 prompt" in p for p in printed), printed


def test_init_lists_programmatic_count_per_eval(monkeypatch, tmp_path):
    """init 逐条输出须显示客观题(assertions)数量,不只主观 expectations。
    曾只印 expectations+fixtures、漏 assertions(客观题是确定性最强的部分)。
    断言用「N assertions, M expectations」整串——避免测试名进 tmp_path 造成假绿
    (假通过陷阱:断言词与 tmp_path 来源重合会恒真,见记忆 test_init_warns_on_degraded)。
    """
    from sift.models import Assertion, AssertionType

    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    evalset = EvalSet(
        skill_name="s",
        evals=[
            EvalCase(
                id=1,
                name="n",
                prompt="p",
                level="L1",
                expectations=["e1", "e2", "e3"],
                assertions=[
                    Assertion(name="a", type=AssertionType.FILE_EXISTS),
                    Assertion(name="b", type=AssertionType.CONTAINS_PATTERN),
                ],
            )
        ],
    )
    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.generate_with_llm"), lambda self, sp, **k: evalset)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("2 assertions, 3 expectations" in p for p in printed), printed


def test_init_lists_rationale_per_eval(monkeypatch, tmp_path):
    """init 逐条输出显示 rationale(审阅抓手:这题对应 SKILL.md 哪段)+ confidence。
    断言用 rationale 内容整串,避开 tmp_path 假通过。
    """
    evalset = EvalSet(
        skill_name="s",
        evals=[
            EvalCase(id=1, name="n", prompt="p", level="L1", rationale="对应 ## 决策逻辑 bullet 2", confidence=0.85)
        ],
    )
    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.generate_with_llm"), lambda self, sp, **k: evalset)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("对应 ## 决策逻辑 bullet 2" in p for p in printed), printed
    assert any("conf 0.85" in p for p in printed), printed


def test_init_append_extends_existing_evals(monkeypatch, tmp_path):
    """--append:已有 evals.json(id 1,2)+ 新生成 2 道 → 合并 4 道,新 eval id 接续 3,4(不覆盖旧题)。"""
    import json

    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "evals.json").write_text(
        json.dumps(
            {
                "skill_name": "s",
                "evals": [
                    {"id": 1, "prompt": "old1", "level": "L1", "expectations": ["e"], "assertions": []},
                    {"id": 2, "prompt": "old2", "level": "L1", "expectations": ["e"], "assertions": []},
                ],
            }
        ),
        encoding="utf-8",
    )
    _mock_two_generated_evals(monkeypatch)
    r = runner.invoke(app, ["init", str(sp), "--append", "-o", str(out)])
    assert r.exit_code == 0, r.output
    merged = json.loads((out / "evals.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in merged["evals"]] == [1, 2, 3, 4]  # 旧 1,2 + 新接续 3,4
    prompts = [e["prompt"] for e in merged["evals"]]
    assert "old1" in prompts and "new1" in prompts  # 新旧都在(不覆盖)


def test_init_append_feeds_existing_evals_to_generation(monkeypatch, tmp_path):
    """--append + 已有 evals.json → 生成阶段喂现有题(定向补题 + 去重),不再盲生成。
    治半聚焦/不去重:init --append --focus 应让 LLM 看到现有题、只补 focus 且不重叠。
    """
    import json

    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "evals.json").write_text(
        json.dumps(
            {
                "skill_name": "s",
                "evals": [_saved_eval(name="OLD", rationale="## covered-point")],
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def fake_gen(self, sp, existing_evals=None, on_heartbeat=None):
        captured["existing"] = existing_evals
        return EvalSet(skill_name="s", evals=[EvalCase(id=1, prompt="new", level="L1", expectations=["e"])])

    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.generate_with_llm"), fake_gen)
    r = runner.invoke(app, ["init", str(sp), "--append", "--focus", "read-log-file", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert captured["existing"] is not None
    assert len(captured["existing"]) == 1
    assert captured["existing"][0].name == "OLD"  # 现有题喂进去了


def test_init_transcript_append_focus_uses_existing_cases_and_skips_exact_duplicates(monkeypatch, tmp_path):
    import json

    sp = _skill_dir(tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"message": {"role": "user", "content": "检查边界情况"}}) + "\n")
    out = tmp_path / "out"
    out.mkdir()
    existing = EvalCase(id=1, name="旧题", prompt="旧请求", rationale="已覆盖正常情况")
    (out / "evals.json").write_text(
        EvalSet(skill_name="s", evals=[existing]).model_dump_json(indent=2), encoding="utf-8"
    )
    prompts = []

    def fake_llm(self, prompt):
        prompts.append(prompt)
        duplicate = existing.model_dump(mode="json")
        duplicate["id"] = 99
        fresh = EvalCase(id=2, name="新题", prompt="新边界请求", rationale="检查边界情况")
        return json.dumps({"skill_name": "s", "evals": [duplicate, fresh.model_dump(mode="json")]})

    monkeypatch.setattr("sift.evals.generator.EvalInitGenerator._llm_call", fake_llm)
    command = [
        "init", str(sp), "--from-transcript", str(transcript),
        "--append", "--focus", "edge-cases", "-o", str(out),
    ]
    first = runner.invoke(app, command)
    assert first.exit_code == 0, first.output
    assert "edge-cases" in prompts[0]
    assert "旧请求" in prompts[0] and "已覆盖正常情况" in prompts[0]
    assert "本次新增 1" in first.output
    assert [case["prompt"] for case in json.loads((out / "evals.json").read_text())["evals"]] == [
        "旧请求", "新边界请求",
    ]

    second = runner.invoke(app, command)
    assert second.exit_code == 0, second.output
    assert "新边界请求" in prompts[1]
    assert "本次新增 0" in second.output
    assert len(json.loads((out / "evals.json").read_text())["evals"]) == 2


def test_init_append_no_existing_feeds_none(monkeypatch, tmp_path):
    """--append 但无已有 evals.json → 喂 None(退回软 focus directive,覆盖式行为不变)。"""

    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    captured = {}

    def fake_gen(self, sp, existing_evals=None, on_heartbeat=None):
        captured["existing"] = existing_evals
        return EvalSet(skill_name="s", evals=[EvalCase(id=1, prompt="only", level="L1", expectations=["e"])])

    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.generate_with_llm"), fake_gen)
    r = runner.invoke(app, ["init", str(sp), "--append", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert captured["existing"] is None


def test_init_append_shows_new_and_merged_count(monkeypatch, tmp_path):
    """append 模式:汇总行显「本次新增 N · 合并后 M」,不把合并总数当生成数(原 'Generated 12' 误导)。"""

    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    _write_existing_eval(out)
    _mock_two_generated_evals(monkeypatch)
    r = runner.invoke(app, ["init", str(sp), "--append", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "本次新增" in r.output and "合并后" in r.output


def test_init_without_append_shows_generated_count(monkeypatch, tmp_path):
    """非 append(覆盖式)→ 仍显 'Generated N eval cases'(措辞不变)。"""

    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"),
        lambda self, sp, **k: EvalSet(
            skill_name="s",
            evals=[
                EvalCase(id=1, prompt="only", level="L1", expectations=["e"]),
                EvalCase(id=2, prompt="two", level="L1", expectations=["e"]),
            ],
        ),
    )
    r = runner.invoke(app, ["init", str(sp), "-o", str(out)])  # 无 --append
    assert r.exit_code == 0, r.output
    assert "Generated" in r.output and "本次新增" not in r.output


def test_init_append_no_existing_writes_fresh(monkeypatch, tmp_path):
    """--append 但无已有 evals.json → 等同首次生成(id 从 1)。"""
    import json

    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"),
        lambda self, sp, **k: EvalSet(
            skill_name="s",
            evals=[
                EvalCase(id=1, prompt="only", level="L1", expectations=["e"]),
            ],
        ),
    )
    r = runner.invoke(app, ["init", str(sp), "--append", "-o", str(out)])
    assert r.exit_code == 0, r.output
    data = json.loads((out / "evals.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in data["evals"]] == [1]
    assert data["evals"][0]["prompt"] == "only"


def test_init_without_append_overwrites_existing(monkeypatch, tmp_path):
    """默认(无 --append)→ 覆盖:已有 evals.json 被新生成替换(旧题丢失)。"""
    import json

    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    _write_existing_eval(out)
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"),
        lambda self, sp, **k: EvalSet(
            skill_name="s",
            evals=[
                EvalCase(id=1, prompt="new", level="L1", expectations=["e"]),
            ],
        ),
    )
    r = runner.invoke(app, ["init", str(sp), "-o", str(out)])  # 无 --append
    assert r.exit_code == 0, r.output
    data = json.loads((out / "evals.json").read_text(encoding="utf-8"))
    assert len(data["evals"]) == 1
    assert data["evals"][0]["prompt"] == "new"  # "old" 被覆盖


def test_init_threads_focus_flag_to_generator(monkeypatch, tmp_path):
    """init --focus 接线:flag 须透到 EvalInitGenerator(focus=...)。"""
    from unittest.mock import create_autospec

    from sift.evals.generator import EvalInitGenerator

    captured = create_autospec(EvalInitGenerator.__init__, return_value=None)
    monkeypatch.setattr(EvalInitGenerator, "__init__", captured)
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"), lambda self, sp, **k: _fake_evalset()
    )
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "--focus", "negative-cases", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert captured.call_args.kwargs.get("focus") == "negative-cases", f"--focus 没透到 generator: {captured}"


def test_init_focus_directive_reaches_prompt(monkeypatch, tmp_path):
    """--focus negative-cases → 透到 generator → 发给 LLM 的 prompt 含反面场景指令。"""
    captured = {}

    def fake_call(prompt, *a, **k):
        captured["prompt"] = prompt
        return '{"skill_name": "s", "evals": [{"id": 1, "prompt": "p", "expectations": ["e"], "assertions": []}]}'

    monkeypatch.setattr("sift.evals.generator.call_claude", fake_call)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "--focus", "negative-cases", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "## Focus" in captured.get("prompt", ""), "focus 指令没进 prompt"
    assert "NOT" in captured.get("prompt", "")  # negative-cases 精调指令


def test_init_trigger_probes_appends_to_trigger_probes_section(tmp_path, monkeypatch):
    """--trigger-probes:保留原 scoring evals、追加 trigger_probes 段、id 接续。"""
    import json

    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(("---\nname: my\ndescription: does X\n---\n# My\nbody\n"))
    out = tmp_path / "out"
    out.mkdir()
    # 已有 evals.json:1 scoring eval、无 trigger_probes
    (out / "evals.json").write_text(('{"skill_name":"my","evals":[{"id":1,"prompt":"p"}]}'))

    monkeypatch.setattr(
        "sift.evals.generator.call_claude",
        lambda *a, **k: (
            '{"skill_name":"my","trigger_probes":[{"id":1,"prompt":"q1","expect_trigger"'
            ':true,"rationale":"r"},{"id":2,"prompt":"q2","expect_trigger":false,"ration'
            'ale":"r"}]}'
        ),
    )

    r = CliRunner().invoke(app, ["init", str(skill), "-o", str(out), "--trigger-probes"])
    assert r.exit_code == 0, r.output
    data = json.loads((out / "evals.json").read_text())
    assert len(data["evals"]) == 1  # 原 scoring eval 保留(不重生成)
    assert len(data["trigger_probes"]) == 2  # 新 trigger 段
    assert data["trigger_probes"][0]["expect_trigger"] is True


def test_init_trigger_probes_warns_on_literal_leakage(tmp_path, monkeypatch):
    """should-trigger 探题照抄 description 措辞 → init 打印词面命中警告(anti-leakage 反馈,不 retry)。"""

    skill = tmp_path / "skill"
    skill.mkdir()
    desc = "generates structured incident reports from raw text"
    (skill / "SKILL.md").write_text((f"---\nname: my\ndescription: {desc}\n---\n# body\n"))

    # should-trigger 探题逐字抄了 description 子串 → literal;should-not 无关 → 不计
    monkeypatch.setattr(
        "sift.evals.generator.call_claude",
        lambda *a, **k: (
            '{"skill_name":"my","trigger_probes":[{"id":1,"prompt":"please generates '
            "structured incident reports from raw text "
            'now","expect_trigger":true,"rationale":"r"},{"id":2,"prompt":"unrelated '
            'task here","expect_trigger":false,"rationale":"r"}]}'
        ),
    )

    r = CliRunner().invoke(app, ["init", str(skill), "-o", str(tmp_path / "out"), "--trigger-probes"])
    assert r.exit_code == 0, r.output
    assert "词面命中" in r.output
    assert "1/1" in r.output  # 1 道 literal / 1 道该触发探题(分母=该触发数,should-not 不计)


def test_init_trigger_probes_with_from_transcript_mines_negatives(tmp_path, monkeypatch):
    """--trigger-probes --from-transcript:生成探题 + 挖 trace 负例,source/消息/id 接续齐全。"""
    import json

    from sift.evals.generator import EvalInitGenerator
    from sift.models import TriggerProbe

    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(("---\nname: my\ndescription: does X\n---\n# body\n"))
    tp = tmp_path / "session.jsonl"
    tp.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "领域相邻的真实请求,足够长"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        EvalInitGenerator,
        "generate_from_transcript",
        lambda self, sp, t, **k: EvalSet(skill_name="my", evals=[{"id": 1, "prompt": "p", "expectations": ["e"]}]),
    )
    monkeypatch.setattr(
        EvalInitGenerator,
        "generate_trigger_probes",
        lambda self, sp, num_each=5: [TriggerProbe(id=0, prompt="gen-q", expect_trigger=True, rationale="r")],
    )
    monkeypatch.setattr(
        "sift.evals.generator.call_claude",
        lambda *a, **k: json.dumps({"verdicts": [{"index": 1, "verdict": "near_miss", "reason": "same domain"}]}),
    )

    out = tmp_path / "out"
    out.mkdir()
    r = CliRunner().invoke(app, ["init", str(skill), "-o", str(out), "--from-transcript", str(tp), "--trigger-probes"])
    assert r.exit_code == 0, r.output
    data = json.loads((out / "evals.json").read_text())
    probes = data["trigger_probes"]
    assert [p["prompt"] for p in probes] == ["gen-q", "领域相邻的真实请求,足够长"]
    assert [p["source"] for p in probes] == ["generated", "trace-mined"]
    assert probes[1]["expect_trigger"] is False  # 挖的负例是 should-not
    assert [p["id"] for p in probes] == [1, 2]  # id 接续
    assert "Mined" in r.output and "trace 负例" in r.output


def test_init_trigger_probe_mining_dedupes_against_existing(tmp_path, monkeypatch):
    """挖出的 query 与已有 trigger_probes 同文(lower+strip)→ 不重复入段。"""
    import json

    from sift.evals.generator import EvalInitGenerator

    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(("---\nname: my\ndescription: does X\n---\n# body\n"))
    tp = tmp_path / "session.jsonl"
    tp.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "Same Query Text"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    (out / "evals.json").write_text(
        json.dumps(
            {
                "skill_name": "my",
                "evals": [{"id": 1, "prompt": "p"}],
                "trigger_probes": [{"id": 1, "prompt": "same query text", "expect_trigger": False}],
            }
        )
    )

    monkeypatch.setattr(
        EvalInitGenerator,
        "generate_from_transcript",
        lambda self, sp, t, **k: EvalSet(skill_name="my", evals=[{"id": 1, "prompt": "p", "expectations": ["e"]}]),
    )
    monkeypatch.setattr(EvalInitGenerator, "generate_trigger_probes", lambda self, sp, num_each=5: [])
    monkeypatch.setattr(
        "sift.evals.generator.call_claude",
        lambda *a, **k: json.dumps({"verdicts": [{"index": 1, "verdict": "near_miss", "reason": "same domain"}]}),
    )

    r = CliRunner().invoke(app, ["init", str(skill), "-o", str(out), "--from-transcript", str(tp), "--trigger-probes"])
    assert r.exit_code == 0, r.output
    data = json.loads((out / "evals.json").read_text())
    assert len(data["trigger_probes"]) == 1  # 大小写差异视为同文,不重复入段
    assert "Mined" not in r.output  # 全被去重 → 不打 Mined 行


def test_init_trigger_probe_unverified_note(tmp_path, monkeypatch):
    """LLM 过滤失败 → 候选仍收(unverified)、终端打 unverified 计数。"""
    import json

    from sift.evals.generator import EvalInitGenerator

    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(("---\nname: my\ndescription: does X\n---\n# body\n"))
    tp = tmp_path / "session.jsonl"
    tp.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "一条足够长的请求"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        EvalInitGenerator, "generate_from_transcript", lambda self, sp, t, **k: EvalSet(skill_name="my", evals=[])
    )
    monkeypatch.setattr(EvalInitGenerator, "generate_trigger_probes", lambda self, sp, num_each=5: [])

    def boom(*a, **k):
        raise RuntimeError("claude down")

    monkeypatch.setattr("sift.evals.generator.call_claude", boom)

    out = tmp_path / "out"
    out.mkdir()
    r = CliRunner().invoke(app, ["init", str(skill), "-o", str(out), "--from-transcript", str(tp), "--trigger-probes"])
    assert r.exit_code == 0, r.output
    data = json.loads((out / "evals.json").read_text())
    assert len(data["trigger_probes"]) == 1
    assert data["trigger_probes"][0]["source"] == "trace-mined"
    assert data["trigger_probes"][0]["rationale"].startswith("[unverified")
    assert "unverified" in r.output


def test_init_prints_token_and_duration(monkeypatch, tmp_path):
    """generate_with_llm 走真 call_claude(mock 带 on_usage)→ gen 累加 token/耗时 → init 终端显示。
    对照 coverage/trigger/audit 都显示 LLM 消耗,init 之前漏了。
    """
    import json

    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    fake = json.dumps(
        {
            "skill_name": "s",
            "evals": [{"id": 1, "name": "n", "prompt": "p", "expectations": ["e"], "assertions": [], "fixtures": []}],
        }
    )

    def fake_call(prompt, *a, **k):
        on_usage = k.get("on_usage")
        if on_usage:
            # 真实 claude json 的 usage 含非数字字段(server_tool_use dict / service_tier
            # str)——累加只取数字,否则 int+dict 会 TypeError。
            on_usage(
                {
                    "input_tokens": 200,
                    "output_tokens": 80,
                    "server_tool_use": {"web_search": 3},
                    "service_tier": "standard",
                }
            )
        return fake

    monkeypatch.setattr("sift.evals.generator.call_claude", fake_call)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("耗时" in p and "tok" in p for p in printed), printed
    assert any("280" in p for p in printed), printed  # 200 + 80


def test_init_lists_eval_id_per_eval(monkeypatch, tmp_path):
    """逐条输出显示 eval id(对照 evals.json 用,不用数位置猜;id 本就按显示顺序递增)。"""
    evalset = EvalSet(skill_name="s", evals=[EvalCase(id=7, name="n", prompt="p", level="L1")])
    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr(("sift.evals.generator.EvalInitGenerator.generate_with_llm"), lambda self, sp, **k: evalset)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("[7]" in p for p in printed), printed


def test_init_append_preserves_existing_unknown_fields(monkeypatch, tmp_path):
    """A:append 不重序列化原有题——原有 evals.json 的未知字段(EvalCase 不认)须保留,
    不被 model_dump 丢。读 raw dict 追加新题,原有字节不动(否则 pydantic extra=ignore 会丢)。
    """
    import json

    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "evals.json").write_text(
        json.dumps(
            {
                "skill_name": "s",
                "evals": [
                    _saved_eval(name="old", custom_unknown_field="MUST_PRESERVE"),  # EvalCase 不认的未知字段
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"),
        lambda self, sp, **k: EvalSet(
            skill_name="s", evals=[EvalCase(id=1, name="new", prompt="new", level="L1", expectations=["e"])]
        ),
    )
    r = runner.invoke(app, ["init", str(sp), "--append", "-o", str(out)])
    assert r.exit_code == 0, r.output
    merged = json.loads((out / "evals.json").read_text(encoding="utf-8"))
    assert merged["evals"][0]["custom_unknown_field"] == "MUST_PRESERVE"  # 原有未知字段保留
    assert [e["id"] for e in merged["evals"]] == [1, 2]  # 原有 1 + 新接续 2
    assert merged["evals"][1]["prompt"] == "new"


def test_init_append_lists_only_new_evals(monkeypatch, tmp_path):
    """B:append 模式终端只列新增 eval,不全量重列原有(聚焦这次补了啥)。"""
    import json

    printed = []
    monkeypatch.setattr("sift.cli.commands.init.console.print", lambda *a, **k: printed.append(str(a)))
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "evals.json").write_text(
        json.dumps(
            {
                "skill_name": "s",
                "evals": [
                    _saved_eval(name="OLD_NAME_KEEP"),
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"),
        lambda self, sp, **k: EvalSet(
            skill_name="s", evals=[EvalCase(id=1, name="NEW_NAME_SHOW", prompt="new", level="L1", expectations=["e"])]
        ),
    )
    r = runner.invoke(app, ["init", str(sp), "--append", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert any("NEW_NAME_SHOW" in p for p in printed), printed  # 新增列了
    assert not any("OLD_NAME_KEEP" in p for p in printed), printed  # 原有不全量重列


def _mock_two_generated_evals(monkeypatch):
    monkeypatch.setattr(
        ("sift.evals.generator.EvalInitGenerator.generate_with_llm"),
        lambda self, sp, **k: EvalSet(
            skill_name="s",
            evals=[
                EvalCase(id=1, prompt="new1", level="L1", expectations=["e"]),
                EvalCase(id=2, prompt="new2", level="L1", expectations=["e"]),
            ],
        ),
    )


def _write_existing_eval(out):
    import json

    (out / "evals.json").write_text(
        json.dumps(
            {
                "skill_name": "s",
                "evals": [_saved_eval()],
            }
        ),
        encoding="utf-8",
    )


def _init_output_field_response(output_field):
    import json

    return json.dumps(
        {
            "skill_name": "s",
            "evals": [
                {
                    "id": 1,
                    "name": "n",
                    "prompt": "p",
                    output_field: "x",
                    "expectations": ["e"],
                    "assertions": [],
                    "fixtures": [],
                }
            ],
        }
    )


def _saved_eval(**extra):
    record = {"id": 1}
    if "name" in extra:
        record["name"] = extra.pop("name")
    record.update(prompt="old", level="L1", expectations=["e"], assertions=[])
    record.update(extra)
    return record
