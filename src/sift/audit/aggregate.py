"""aggregate: 多 transcript 对账的指令级聚合视图。

audit 的 findings 默认平铺(每 transcript × 每指令 一条),多 transcript 时是一堆散点,
看不出"哪条指令系统性被违反"。本模块把 findings 按 instruction_text 跨 transcript 聚合:
``aggregate_by_instruction`` 算每条指令的 pass/fail/na 分布 + fail_rate(按 fail_rate
降序——系统性违规一眼可见);``aggregate_coverage`` 算声明覆盖视图(covered/uncovered/
unknown 三态,按声明分组——本次对账的证据面铺了声明的多大比例)。

设计决策:
- **本层不归一化**:按 instruction_text 如实分组。**同文 exact 重复**(同一句被抽成
  CONSTRAINT + ANTI_FABRICATION、同 verdict)已在上游 ``audit.dedup`` 合并,本层看不到;
  **不同 text 的扇出**(如 error-codes 抽成 REFERENCE_USAGE + HEADING_STEP 两条不同文本)
  仍是两条不同指令、在此显形为两行——这是 extractor 的毛病,聚合如实反映(倒逼治本),
  不在聚合层硬塞归一化引入新 bug。两者在**平铺 findings** 里靠 ``related`` 互标关联。
- **纯函数**:不调 LLM、不读 transcript,只对已有 findings 后处理 → 单元可测、确定。
- 判定层一行不动,只重组输出。
"""

from __future__ import annotations

from sift.models.audit_models import (
    AuditFinding,
    AuditVerdict,
    CoverageSummary,
    InstructionAggregate,
    InstructionCoverage,
    SectionCoverage,
)


def aggregate_by_instruction(
    findings: list[AuditFinding],
) -> list[InstructionAggregate]:
    """按 instruction_text 跨 transcript 聚合,返回每条指令的 verdict 分布(按 fail_rate 降序)。

    多 transcript 对账的核心价值:从"会话级 FAIL 计数"(无指导意义)转到"指令级违规率"
    (定位 skill 系统性问题,如"NEVER fabricate 在 75% 会话被违反")。单 transcript 时每
    指令 1 verdict,聚合退化为带 fail_rate 的列表,仍可用。

    Args:
        findings: audit 的平铺 findings(可能跨多 transcript)。

    Returns:
        每条不同 instruction_text 一个 ``InstructionAggregate``,按 fail_rate 降序
        (fail_rate 相同按 fail_count 降序,再按 instruction_text 升序,稳定可读)。
        空输入 → []。
    """
    if not findings:
        return []

    groups = _group_instruction_findings(findings)

    rows = _instruction_summary_rows(groups)
    return rows


def _instruction_summary_rows(groups):
    rows: list[InstructionAggregate] = []
    for g in groups.values():
        total = g["pass"] + g["fail"] + g["na"] + g["unresolved"] + g["indeterminate"]
        rows.append(
            InstructionAggregate(
                instruction_text=g["instruction_text"],
                source_excerpt=g["source_excerpt"],
                headline=g["headline"],
                section=g["section"],
                control_flow=g["control_flow"],
                source=g["source"],
                category=g["category"],
                pass_count=g["pass"],
                fail_count=g["fail"],
                na_count=g["na"],
                unresolved_count=g["unresolved"],
                indeterminate_count=g["indeterminate"],
                total=total,
                fail_rate=g["fail"] / total if total else 0.0,
                fail_in_transcripts=g["fail_in_transcripts"],
                na_in_transcripts=g["na_in_transcripts"],
                fail_seqs=g["fail_seqs"],
                na_seqs=g["na_seqs"],
            )
        )

    rows.sort(key=lambda r: (-r.fail_rate, -r.fail_count, r.instruction_text))
    return rows


def _group_instruction_findings(findings):
    groups: dict[str, dict] = {}
    for f in findings:
        if f.synthetic:
            # 合成 finding(ORDER 的“(顺序)…”)不进 by_instruction——聚合表保持
            # 「声明指令」纯度(合成指令无系统性/单发信号可言),聚合级噪声规则(noise.py
            # 按 HEADING_STEP 行判)也因无行而不误触发。过滤落点在本函数入口:auditor /
            # eval 侧遵循性聚合(viewer)/ 整段审计(slice)三处复用一次覆盖。
            continue
        # setdefault 取首次出现的 source/category/section —— 同一 instruction_text 跨
        # transcript 来自同一 SKILL.md 抽取(含 derived),取首条即可(section 同文同节,
        # dedup 后跨节合并的极端情形也 first-wins,与 source_excerpt 同约定)。
        g = groups.setdefault(
            f.instruction_text,
            {
                "instruction_text": f.instruction_text,
                "source_excerpt": f.source_excerpt,
                "headline": f.headline,
                "section": f.section or "",
                "control_flow": f.control_flow or "",
                "source": f.source,
                "category": f.category,
                "pass": 0,
                "fail": 0,
                "na": 0,
                "unresolved": 0,
                "indeterminate": 0,
                "fail_in_transcripts": [],
                "na_in_transcripts": [],
                "fail_seqs": [],
                "na_seqs": [],
            },
        )
        if f.verdict == AuditVerdict.PASS:
            g["pass"] += 1
        elif f.verdict == AuditVerdict.FAIL:
            g["fail"] += 1
            g["fail_in_transcripts"].append(f.transcript)
            g["fail_seqs"].append(f.seq)
        elif f.verdict == AuditVerdict.UNRESOLVED:
            g["unresolved"] += 1
        elif f.verdict == AuditVerdict.INDETERMINATE:
            g["indeterminate"] += 1
        else:  # AuditVerdict.NA(不适用)
            g["na"] += 1
            g["na_in_transcripts"].append(f.transcript)
            g["na_seqs"].append(f.seq)
    return groups


def coverage_status(pass_count: int, fail_count: int, na_count: int, unresolved: int, indeterminate: int) -> str:
    """指令覆盖三态判定(covered/uncovered/unknown)——覆盖视图与文本覆盖视图共用,单一真身防漂移。

    优先级(与 InstructionCoverage.status 文档一致):
    - covered:任一 transcript PASS 或 FAIL(会话碰过这条规则,守没守另算);
    - uncovered:全部 transcript 都 N/A 且无没判成/判不准(规则在、场景从未触发);
    - unknown:其余(N/A+UNRESOLVED 混合 / 纯 UNRESOLVED / 纯 INDETERMINATE)。
    """
    if pass_count or fail_count:
        return "covered"
    if na_count and not (unresolved or indeterminate):
        return "uncovered"
    return "unknown"


def aggregate_coverage(findings: list[AuditFinding]) -> CoverageSummary:
    """声明覆盖视图:按声明分组统计指令被 transcript(s) 触达多少(纯函数、不调 LLM)。

    与 ``aggregate_by_instruction`` 互为姊妹(同按 instruction_text 分键、同 verdict 计数),
    回答的问题不同:那个查"触达的守没守规"(fail_rate),本函数查"触达了多少"
    (coverage_rate)——本次对账证据面铺了声明的多大比例、哪些段从没被锻炼到。

    分母 = 声明来的去重指令;``derived_from_transcript=True`` 不计(反推指令不是声明
    规则、构造上偏 PASS——它存在正因为 trace 做了该动作,计入会虚增覆盖率)。
    状态判定(优先级,与 InstructionCoverage.status 文档一致):
    - covered:任一 transcript PASS 或 FAIL(会话碰过这条规则);
    - uncovered:全部 transcript 都 N/A(规则存在、场景从未触发);
    - unknown:其余(N/A+UNRESOLVED 混合 / 纯 UNRESOLVED / 纯 INDETERMINATE)。
    coverage_rate = covered/total,unknown 计入分母、单列——宁低估不虚高。

    排序:分组按 (-uncovered, -total, section)(未覆盖多的段排前,一眼看到证据缺口);
    组内 uncovered → unknown → covered(covered 行保留在模型里供下游,模板只渲染非
    covered),组内再按 instruction_text 升序。空输入 → 全 0 的 CoverageSummary。
    """
    groups = _group_coverage_findings(findings)

    sections = _build_coverage_sections(groups)

    coverage_order = {"uncovered": 0, "unknown": 1, "covered": 2}
    summary = CoverageSummary()
    for sec in sections.values():
        sec_total = len(sec["instructions"])
        instructions = sorted(sec["instructions"], key=lambda i: (coverage_order.get(i.status), i.instruction_text))
        summary.sections.append(
            SectionCoverage(
                section=sec["section"],
                total=sec_total,
                covered=sec["covered"],
                uncovered=sec["uncovered"],
                unknown=sec["unknown"],
                coverage_rate=sec["covered"] / sec_total if sec_total else 0.0,
                instructions=instructions,
            )
        )
        summary.total += sec_total
        summary.covered += sec["covered"]
        summary.uncovered += sec["uncovered"]
        summary.unknown += sec["unknown"]
    summary.sections.sort(key=lambda s: (-s.uncovered, -s.total, s.section))
    summary.coverage_rate = summary.covered / summary.total if summary.total else 0.0
    return summary


def _build_coverage_sections(groups):
    sections: dict[str, dict] = {}
    for g in groups.values():
        total = g["pass"] + g["fail"] + g["na"] + g["unresolved"] + g["indeterminate"]
        status = coverage_status(g["pass"], g["fail"], g["na"], g["unresolved"], g["indeterminate"])
        sec = sections.setdefault(
            g["section"],
            {"section": g["section"], "covered": 0, "uncovered": 0, "unknown": 0, "instructions": []},
        )
        sec[status] += 1
        sec["instructions"].append(
            InstructionCoverage(
                instruction_text=g["instruction_text"],
                source_excerpt=g["source_excerpt"],
                headline=g["headline"],
                section=g["section"],
                source=g["source"],
                category=g["category"],
                status=status,
                pass_count=g["pass"],
                fail_count=g["fail"],
                na_count=g["na"],
                unresolved_count=g["unresolved"],
                indeterminate_count=g["indeterminate"],
                total=total,
                seqs=sorted(set(g["seqs"])),
            )
        )
    return sections


def _group_coverage_findings(findings):
    groups: dict[str, dict] = {}
    for f in findings:
        if f.derived_from_transcript or f.synthetic:
            # synthetic(ORDER 合成文案)同 derived 不进覆盖分母——它不是声明指令,
            # 且其 na(A/B 未发生)是预期高频态,计入会虚增 unknown、压低 coverage_rate。
            continue
        g = groups.setdefault(
            f.instruction_text,
            {
                "instruction_text": f.instruction_text,
                "source_excerpt": f.source_excerpt,
                "headline": f.headline,
                "section": f.section or "",
                "source": f.source,
                "category": f.category,
                "pass": 0,
                "fail": 0,
                "na": 0,
                "unresolved": 0,
                "indeterminate": 0,
                "seqs": [],
            },
        )
        g["seqs"].append(f.seq)
        if f.verdict == AuditVerdict.PASS:
            g["pass"] += 1
        elif f.verdict == AuditVerdict.FAIL:
            g["fail"] += 1
        elif f.verdict == AuditVerdict.UNRESOLVED:
            g["unresolved"] += 1
        elif f.verdict == AuditVerdict.INDETERMINATE:
            g["indeterminate"] += 1
        else:  # AuditVerdict.NA(不适用)
            g["na"] += 1
    return groups
