"""coverage grid 单测(analyzer + CLI)。"""

import json

from typer.testing import CliRunner

import sift.evals.coverage as cov
from sift.cli.commands import coverage as cov_cmd
from sift.evals.coverage import _parse_coverage_response, analyze_coverage
from sift.llm_transport import LLMCallConfig
from sift.models import EvalCase, EvalSet
from sift.models.coverage_report import CoveragePoint, CoverageReport


def test_coverage_point_defaults():
    p = CoveragePoint(label="安全事件升级")
    assert p.label == "安全事件升级"
    assert p.source_quote == ""
    assert p.covered_by == []


def test_report_gaps_are_zero_coverage_points():
    """gaps = points 里 covered_by 为空的(自动派生,不是独立字段)。"""
    rep = CoverageReport(
        skill_name="my-skill",
        points=[
            CoveragePoint(label="已覆盖点", source_quote="q1", covered_by=[1, 3]),
            CoveragePoint(label="漏测点", source_quote="q2", covered_by=[]),
        ],
    )
    assert len(rep.gaps) == 1
    assert rep.gaps[0].label == "漏测点"


def test_report_roundtrip():
    rep = CoverageReport(**_coverage_report_values(points=[CoveragePoint(label="p", source_quote="q", covered_by=[2])]))
    js = rep.model_dump_json()
    assert '"gaps"' not in js  # gaps 是派生属性,不进 JSON(防止将来误序列化)
    rep2 = CoverageReport.model_validate_json(js)
    assert rep2.points[0].covered_by == [2]
    assert rep2.gaps == []


def _fake_response_two_points():
    return (
        '{"points": [{"label": "分类错误码", "source_quote": "映射到已知码", '
        '"covered_by": [1, 6]}, {"label": "安全事件升级", "source_quote": '
        '"若日志提到安全事件", "covered_by": []}]}'
    )


def test_parse_response_builds_points_and_gaps():
    rep = _parse_coverage_response(_fake_response_two_points(), "my-skill")
    assert len(rep.points) == 2
    assert rep.points[0].covered_by == [1, 6]
    assert [g.label for g in rep.gaps] == ["安全事件升级"]


def test_parse_response_malformed_returns_empty():
    rep = _parse_coverage_response("not json {", "my-skill")
    assert rep.points == [] and rep.gaps == []


def test_analyze_coverage_calls_claude_and_maps(monkeypatch, tmp_path):
    skill = tmp_path / "sk"
    skill.mkdir()
    (skill / "SKILL.md").write_text(("---\nname: sk\ndescription: d\n---\n# S\n规则X\n"), encoding="utf-8")
    captured = {}

    def fake_call(prompt, on_usage=None, config: LLMCallConfig = LLMCallConfig()):
        captured["prompt"] = prompt
        if on_usage:
            on_usage({"input_tokens": 100, "output_tokens": 50})
        return _fake_response_two_points()

    monkeypatch.setattr(cov, "call_claude", fake_call)

    eval_set = EvalSet(
        skill_name="sk",
        evals=[
            EvalCase(id=1, prompt="分析日志", rationale="映射到已知码"),
            EvalCase(id=6, prompt="多错误", rationale="line_number"),
        ],
    )
    rep = analyze_coverage(skill, eval_set, model=None, timeout=120)

    assert "规则X" in captured["prompt"]  # SKILL.md 正文喂进去了
    assert "分析日志" in captured["prompt"]  # eval prompt+rationale 喂进去了
    assert len(rep.points) == 2
    assert rep.token_usage.get("input_tokens") == 100  # on_usage 收了 token
    assert rep.duration_seconds >= 0.0


def test_analyze_coverage_agent_kind_uses_agent_wording(monkeypatch, tmp_path):
    """kind='agent' 的 coverage prompt 用 agent 措辞,非 'SKILL.md'。

    内容点提取吃 parse 后的 body(agent.md 走得通),只改措辞 + 漏测处方带 --kind agent。
    """
    agent = tmp_path / "pr-reviewer.md"
    agent.write_text(
        ("---\nname: pr-reviewer\ndescription: 审 PR diff\ntools: [Read, Grep]\n---\n# 审查者\n硬编码密钥必拦\n"),
        encoding="utf-8",
    )
    captured = {}

    def fake_call(prompt, on_usage=None, config: LLMCallConfig = LLMCallConfig()):
        captured["prompt"] = prompt
        if on_usage:
            on_usage({"input_tokens": 1, "output_tokens": 1})
        return '{"points": []}'

    monkeypatch.setattr(cov, "call_claude", fake_call)
    eval_set = EvalSet(skill_name="pr-reviewer", evals=[EvalCase(id=1, prompt="审 diff", rationale="硬编码密钥")])
    analyze_coverage(agent, eval_set, model=None, timeout=120, kind="agent")

    assert "Agent" in captured["prompt"]
    assert "SKILL.md" not in captured["prompt"]


def test_coverage_agent_md_default_path(monkeypatch, tmp_path):
    """Agent coverage defaults both input and reports to the declaration's parent/evals."""
    runner = CliRunner()
    adir = tmp_path / "pr-reviewer"
    adir.mkdir()
    agent = adir / "pr-reviewer.md"
    agent.write_text(("---\nname: pr-reviewer\ndescription: 审 PR diff\n---\n# 审查者\n规则\n"), encoding="utf-8")
    (adir / "evals").mkdir()
    (adir / "evals" / "evals.json").write_text(
        json.dumps({"skill_name": "pr-reviewer", "evals": [{"id": 1, "prompt": "审 diff"}]}), encoding="utf-8"
    )

    def fake_analyze(skill_path, eval_set, *, model=None, timeout=120, kind=None):
        return CoverageReport(skill_name="pr-reviewer", points=[])

    monkeypatch.setattr(cov_cmd, "analyze_coverage", fake_analyze)

    r = runner.invoke(cov_cmd.app, ["coverage", str(agent), "--kind", "agent"])
    assert r.exit_code == 0, r.output
    assert "不存在" not in r.output  # 没因路径踩坑
    assert (adir / "evals" / "coverage-report.json").exists()
    assert (adir / "evals" / "coverage-report.html").exists()


def test_coverage_skill_default_output_stays_in_evals(monkeypatch, tmp_path):
    """Directory-based skill remains the positive control for the default report path."""
    runner = CliRunner()
    skill, edir = _coverage_skill(tmp_path, "---\nname: sk\ndescription: d\n---\n# S\n规则X\n")
    (edir / "evals.json").write_text(
        json.dumps({"skill_name": "sk", "evals": [{"id": 1, "prompt": "p"}]}), encoding="utf-8"
    )

    monkeypatch.setattr(
        cov_cmd,
        "analyze_coverage",
        lambda skill_path, eval_set, *, model=None, timeout=120, kind=None: CoverageReport(
            skill_name="sk", points=[]
        ),
    )

    r = runner.invoke(cov_cmd.app, ["coverage", str(skill)])
    assert r.exit_code == 0, r.output
    assert (edir / "coverage-report.json").exists()
    assert (edir / "coverage-report.html").exists()


def test_coverage_augment_agent_routes_generate_for_agent(monkeypatch, tmp_path):
    """--kind agent --augment 路由到 generate_for_agent(非 skill 的 generate_with_llm)。"""
    from sift.evals.generator import EvalInitGenerator

    runner = CliRunner()
    adir = tmp_path / "pr-reviewer"
    adir.mkdir()
    agent = adir / "pr-reviewer.md"
    agent.write_text(("---\nname: pr-reviewer\ndescription: 审 PR diff\n---\n# 审查者\n规则\n"), encoding="utf-8")
    (adir / "evals").mkdir()
    (adir / "evals" / "evals.json").write_text(
        json.dumps({"skill_name": "pr-reviewer", "evals": [{"id": 1, "prompt": "p", "rationale": "r"}]}),
        encoding="utf-8",
    )

    def fake_analyze(skill_path, eval_set, *, model=None, timeout=120, kind=None):
        return CoverageReport(
            skill_name="pr-reviewer", points=[CoveragePoint(label="漏测点X", source_quote="q", covered_by=[])]
        )

    monkeypatch.setattr(cov_cmd, "analyze_coverage", fake_analyze)

    calls = {"agent": 0, "skill": 0}

    def fake_for_agent(self, skill_path, existing_evals=None):
        calls["agent"] += 1
        return EvalSet(skill_name="pr-reviewer", evals=[EvalCase(id=0, name="new", prompt="新题", rationale="auto")])

    monkeypatch.setattr(EvalInitGenerator, "generate_for_agent", fake_for_agent)

    def fake_with_llm(self, skill_path, existing_evals=None):
        calls["skill"] += 1
        return EvalSet(skill_name="pr-reviewer", evals=[])

    monkeypatch.setattr(EvalInitGenerator, "generate_with_llm", fake_with_llm)

    r = runner.invoke(
        cov_cmd.app, ["coverage", str(agent), "-o", str(tmp_path / "out"), "--kind", "agent", "--augment"]
    )
    assert r.exit_code == 0, r.output
    assert calls["agent"] == 1 and calls["skill"] == 0  # 走 agent 路,没走 skill 路


def test_parse_response_strips_json_fence():
    fenced = '```json\n{"points": [{"label": "x", "source_quote": "q", "covered_by": ["2"]}]}\n```'
    rep = _parse_coverage_response(fenced, "my-skill")
    assert len(rep.points) == 1
    assert rep.points[0].covered_by == [2]  # string id "2" coerced + fence stripped


def test_analyze_coverage_call_failure_returns_empty(monkeypatch, tmp_path):
    skill = tmp_path / "sk"
    skill.mkdir()
    (skill / "SKILL.md").write_text(("---\nname: sk\ndescription: d\n---\n# S\n规则X\n"), encoding="utf-8")

    monkeypatch.setattr(cov, "call_claude", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    eval_set = EvalSet(skill_name="sk", evals=[EvalCase(id=1, prompt="分析日志", rationale="r")])
    rep = analyze_coverage(skill, eval_set, model=None, timeout=120)

    assert rep.points == []
    assert rep.duration_seconds >= 0.0


def test_coverage_default_reports_gaps_and_writes_json(monkeypatch, tmp_path):
    runner = CliRunner()

    skill, edir = _coverage_skill(tmp_path, "---\nname: sk\ndescription: d\n---\n# S\n规则X\n")
    (edir / "evals.json").write_text(
        json.dumps(
            {
                "skill_name": "sk",
                "evals": [{"id": 1, "prompt": "分析日志", "rationale": "映射到已知码"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_analyze(skill_path, eval_set, *, model=None, timeout=120, kind=None):
        return CoverageReport(
            **_coverage_report_values(
                skill_name="sk",
                points=[
                    CoveragePoint(label="已覆盖", source_quote="q1", covered_by=[1]),
                    CoveragePoint(label="安全事件升级", source_quote="若日志提到安全事件", covered_by=[]),
                ],
                token_usage={"input_tokens": 100, "output_tokens": 50},
                duration_seconds=2.5,
            )
        )

    monkeypatch.setattr(cov_cmd, "analyze_coverage", fake_analyze)

    out = tmp_path / "out"
    r = runner.invoke(cov_cmd.app, ["coverage", str(skill), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "[cyan]" not in r.output  # 富文本标记不能漏到纯文本输出(markup=False)
    assert "安全事件升级" in r.output  # 漏测点上报
    assert "init" in r.output and "--focus" in r.output  # 给了补题处方
    assert (out / "coverage-report.json").exists()
    js = json.loads((out / "coverage-report.json").read_text(encoding="utf-8"))
    assert any(p["covered_by"] == [] for p in js["points"])


def test_coverage_no_gaps_reports_all_covered(monkeypatch, tmp_path):
    runner = CliRunner()
    skill, edir = _coverage_skill(tmp_path, "---\nname: sk\ndescription: d\n---\n# S\n规则X\n")
    (edir / "evals.json").write_text(
        json.dumps({"skill_name": "sk", "evals": [{"id": 1, "prompt": "p"}]}), encoding="utf-8"
    )

    def fake_analyze(skill_path, eval_set, *, model=None, timeout=120, kind=None):
        return CoverageReport(skill_name="sk", points=[CoveragePoint(label="全覆盖", source_quote="q", covered_by=[1])])

    monkeypatch.setattr(cov_cmd, "analyze_coverage", fake_analyze)

    r = runner.invoke(cov_cmd.app, ["coverage", str(skill), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0
    assert "无漏测" in r.output or "全覆盖" in r.output


def test_coverage_missing_evals_json_warns(tmp_path):
    """无 evals.json(用户没跑 init)→ 黄字提示 + exit 0,不抛 traceback。"""
    runner = CliRunner()
    skill = tmp_path / "sk"
    skill.mkdir()
    (skill / "SKILL.md").write_text(("---\nname: sk\ndescription: d\n---\n# S\n规则X\n"), encoding="utf-8")

    r = runner.invoke(cov_cmd.app, ["coverage", str(skill)])
    assert r.exit_code == 0
    assert "不存在" in r.output or "先跑 init" in r.output


def test_coverage_augment_appends_flagged_dedup_evals(monkeypatch, tmp_path):
    from sift.evals.generator import EvalInitGenerator

    runner = CliRunner()

    skill, edir = _coverage_skill(tmp_path, "---\nname: sk\ndescription: d\n---\n# S\n规则X\n")
    (edir / "evals.json").write_text(
        json.dumps(
            {
                "skill_name": "sk",
                "evals": [{"id": 1, "prompt": "已存在 prompt", "rationale": "r"}],
                "trigger_probes": [{"id": 1, "prompt": "tp", "expect_trigger": True}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_analyze(skill_path, eval_set, *, model=None, timeout=120, kind=None):

        return CoverageReport(
            skill_name="sk", points=[CoveragePoint(label="安全事件升级", source_quote="q", covered_by=[])]
        )  # 1 个漏测点

    monkeypatch.setattr(cov_cmd, "analyze_coverage", fake_analyze)

    # fake generate_with_llm:返回 2 道新题,其中一道 prompt 与已有重复(应被去重)
    def fake_gen(self, skill_path, existing_evals=None):

        return EvalSet(
            skill_name="sk",
            evals=[
                EvalCase(id=0, name="new1", prompt="测安全事件", rationale="auto"),
                EvalCase(id=0, name="dup", prompt="已存在 prompt", rationale="auto"),  # 重复→跳过
            ],
        )

    monkeypatch.setattr(EvalInitGenerator, "generate_with_llm", fake_gen)

    r = runner.invoke(cov_cmd.app, ["coverage", str(skill), "-o", str(tmp_path / "out"), "--augment"])
    assert r.exit_code == 0, r.output
    assert "补 1" in r.output and "跳过 1" in r.output  # 去重:1 补 1 跳
    assert "1/1" in r.output  # per-gap 进度行(1 个漏测点)

    saved = json.loads((edir / "evals.json").read_text(encoding="utf-8"))
    assert len(saved["evals"]) == 2  # 原 1 + 新 1
    new_e = [e for e in saved["evals"] if e["prompt"] == "测安全事件"][0]
    assert new_e["id"] == 2  # id 接续 max+1
    assert "review:coverage-augment" in new_e["flags"]  # 带 review flag
    assert len(saved["trigger_probes"]) == 1  # 顶层字段写回保全(防回归)
    assert saved["trigger_probes"][0]["prompt"] == "tp"


def test_coverage_augment_feeds_existing_evals(monkeypatch, tmp_path):
    """--augment 调 generate_with_llm 时喂现有 evals(共享内核定向补题 + 去重),不盲生成。"""
    from sift.evals.generator import EvalInitGenerator

    runner = CliRunner()

    skill, edir = _coverage_skill(tmp_path, "---\nname: sk\ndescription: d\n---\n# S\n规则X\n")
    (edir / "evals.json").write_text(
        json.dumps(
            {
                "skill_name": "sk",
                "evals": [{"id": 1, "name": "EXISTING", "prompt": "p", "rationale": "## r"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_analyze(skill_path, eval_set, *, model=None, timeout=120, kind=None):

        return CoverageReport(skill_name="sk", points=[CoveragePoint(label="漏测点A", source_quote="q", covered_by=[])])

    monkeypatch.setattr(cov_cmd, "analyze_coverage", fake_analyze)

    captured = {}

    def fake_gen(self, skill_path, existing_evals=None):
        captured["existing"] = existing_evals

        return EvalSet(skill_name="sk", evals=[EvalCase(id=0, name="new", prompt="补题", rationale="r")])

    monkeypatch.setattr(EvalInitGenerator, "generate_with_llm", fake_gen)

    r = runner.invoke(cov_cmd.app, ["coverage", str(skill), "-o", str(tmp_path / "out"), "--augment"])
    assert r.exit_code == 0, r.output
    assert captured["existing"] is not None
    assert any(getattr(e, "name", None) == "EXISTING" for e in captured["existing"])


def _coverage_report_values(**changes):
    data = {"skill_name": "my-skill", "points": None, "token_usage": {"total_tokens": 500}, "duration_seconds": 3.2}
    data.update(changes)
    return data


def _coverage_skill(tmp_path, text):
    skill = tmp_path / "sk"
    edir = skill / "evals"
    edir.mkdir(parents=True)
    (skill / "SKILL.md").write_text(text, encoding="utf-8")
    return skill, edir
