"""text_coverage 单元测试:声明文本覆盖视图(对账结果投影回原文)。

纯函数测试,不调 LLM——直接造 AuditFinding + 声明正文,验证:
- 单元切分:heading/bullet(含嵌套)/paragraph/codeblock(fence 内 # 行不当标题);
- 回指匹配:source_excerpt 等值 > 双向包含 > REFERENCE_USAGE 路径 / OUTPUT_FORMAT 标题回退;
- 单元状态:保守最低态(uncovered/unknown 都拉黄)、unmodeled(无指令)、heading 结构行;
- 双指标:coverage_rate=covered/modeled、modeled_ratio=modeled/total;heading 不进分母;
- derived/synthetic 不参与(复用 coverage 分组同一排除)。
"""

from __future__ import annotations

import pytest

from finding_fixtures import FindingOrigin

from sift.audit.text_coverage import build_text_coverage, split_declaration_units
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditVerdict,
)

_BODY = """# 编排纪律

修复任何代码前必须先复现该 bug。

- 关键节点必须向用户确认后方可继续
- 改动前必须先读相关源码
- 输出报告前须核对 scripts/check.sh
  - 嵌套子项也算外层 bullet 的续行

正常说明文字,不该被抽成指令。

## 输出格式

```
状态:✅通过/❌失败
```
"""


def _f(
    text: str,
    verdict: AuditVerdict,
    origin: FindingOrigin,
) -> AuditFinding:
    return AuditFinding(
        instruction_text=text,
        source_excerpt=origin.excerpt,
        source=origin.source,
        verdict=verdict,
        method=AuditMethod.LLM,
        category=origin.category,
        evidence="e",
        transcript="s1",
        seq=origin.seq,
        section=origin.section,
        derived_from_transcript=origin.derived,
        synthetic=origin.synthetic,
    )


class TestSplitDeclarationUnits:
    @staticmethod
    def test_kinds_and_document_order():
        units = split_declaration_units(_BODY)
        assert [u.kind for u in units] == [
            "heading",  # # 编排纪律
            "paragraph",  # 修复任何代码前必须先复现该 bug。
            "bullet",  # - 关键节点必须向用户确认后方可继续
            "bullet",  # - 改动前必须先读相关源码
            "bullet",  # - 输出报告前须核对 scripts/check.sh(嵌套子项并入)
            "paragraph",  # 正常说明文字…
            "heading",  # ## 输出格式
            "codeblock",  # 状态:✅通过/❌失败
        ]

    @staticmethod
    def test_nested_bullet_is_continuation():
        units = [u for u in split_declaration_units(_BODY) if u.kind == "bullet"]
        merged = units[2].text
        assert "scripts/check.sh" in merged
        assert "嵌套子项" in merged

    @staticmethod
    def test_codeblock_tracks_heading():
        units = [u for u in split_declaration_units(_BODY) if u.kind == "codeblock"]
        assert len(units) == 1
        assert units[0].heading == "输出格式"
        assert "✅通过" in units[0].text

    @staticmethod
    def test_hash_inside_fence_is_not_heading():
        body = "## 输出\n```\n# 注释行不是标题\n```\n"
        units = split_declaration_units(body)
        assert [u.kind for u in units] == ["heading", "codeblock"]
        assert "# 注释行不是标题" in units[1].text

    @staticmethod
    def test_empty_body():
        assert split_declaration_units("") == []

    @staticmethod
    def test_unclosed_fence_runs_to_eof():
        units = split_declaration_units("```\nfoo\nbar")
        assert len(units) == 1
        assert units[0].kind == "codeblock"
        assert "bar" in units[0].text


class TestBuildTextCoverage:
    @staticmethod
    def test_excerpt_match_and_conservative_rollup():
        """excerpt 等值回指:covered/uncovered/unmodeled 三色各归其位。"""
        findings = [
            _f(
                "必须先复现",
                AuditVerdict.PASS,
                FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP, excerpt="修复任何代码前必须先复现该 bug。"),
            ),
            _f(
                "必须向用户确认",
                AuditVerdict.PASS,
                FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION, excerpt="关键节点必须向用户确认后方可继续"),
            ),
            _f(
                "必须先读相关源码",
                AuditVerdict.NA,
                FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION, excerpt="改动前必须先读相关源码"),
            ),
        ]
        cov = build_text_coverage(_BODY, findings)
        bullet_confirm = next(u for u in cov.units if u.kind == "bullet" and "向用户确认" in u.text)
        bullet_read = next(u for u in cov.units if u.kind == "bullet" and "先读相关源码" in u.text)
        para_repro = next(u for u in cov.units if u.kind == "paragraph" and "复现" in u.text)
        prose = next(u for u in cov.units if "正常说明文字" in u.text)
        assert bullet_confirm.state == "covered"
        assert para_repro.state == "covered"
        assert bullet_read.state == "uncovered"  # 全 NA → 指令 uncovered → 单元黄
        assert prose.state == "unmodeled"

    @staticmethod
    def test_same_unit_mixed_statuses_pull_yellow():
        """同 bullet 两条指令(covered + unknown)→ 单元黄(保守最低态)。"""
        findings = [
            _f(
                "确认",
                AuditVerdict.PASS,
                FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION, excerpt="关键节点必须向用户确认后方可继续"),
            ),
            _f(
                "确认2",
                AuditVerdict.UNRESOLVED,
                FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP, excerpt="关键节点必须向用户确认后方可继续"),
            ),
        ]
        cov = build_text_coverage(_BODY, findings)
        bullet = next(u for u in cov.units if u.kind == "bullet" and "向用户确认" in u.text)
        assert bullet.state == "uncovered"
        assert len(bullet.instructions) == 2

    @staticmethod
    def test_reference_usage_path_fallback():
        findings = [
            _f(
                "Use reference file: scripts/check.sh",
                AuditVerdict.PASS,
                FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            ),
        ]
        cov = build_text_coverage(_BODY, findings)
        bullet = next(u for u in cov.units if u.kind == "bullet" and "scripts/check.sh" in u.text)
        assert bullet.state == "covered"
        assert bullet.instructions[0].status == "covered"

    @staticmethod
    def test_category_reference_is_unmapped():
        findings = [
            _f(
                "Consult reference material (2 alternative category references)",
                AuditVerdict.PASS,
                FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            ),
        ]
        cov = build_text_coverage(_BODY, findings)
        assert cov.unmapped_instructions == ["Consult reference material (2 alternative category references)"]
        assert all(u.state in ("", "unmodeled") for u in cov.units)

    @staticmethod
    def test_output_format_maps_codeblock_by_heading():
        findings = [
            _f(
                "Output should follow template format",
                AuditVerdict.FAIL,
                FindingOrigin(source="OUTPUT_FORMAT", category=AuditCategory.OUTPUT, section="输出格式"),
            ),
        ]
        cov = build_text_coverage(_BODY, findings)
        block = next(u for u in cov.units if u.kind == "codeblock")
        # FAIL 也算触达(会话碰过这条模板规则)→ 指令 covered → 单元绿
        assert block.state == "covered"
        assert block.instructions[0].status == "covered"

    @staticmethod
    def test_output_format_limits_to_first_three_codeblocks():
        """OUTPUT_FORMAT 回退只取每节前 3 个 codeblock(与 extractor 采样同规)。

        回归锁:旧实现 _match_section_codeblocks 返回该标题下全部 codeblock,当一节
        有 4+ 个块时,extractor 只抽前 3 个当输出模板,第 4+ 块却被一并标 covered
        (过标);修复后第 4 块保持 unmodeled。
        """
        body = (
            "## 输出格式\n\n"
            "```\n模板A\n```\n"
            "```\n模板B\n```\n"
            "```\n模板C\n```\n"
            "```\n模板D\n```\n"
        )
        findings = [
            _f(
                "Output should follow template format",
                AuditVerdict.PASS,
                FindingOrigin(source="OUTPUT_FORMAT", category=AuditCategory.OUTPUT, section="输出格式"),
            ),
        ]
        cov = build_text_coverage(body, findings)
        blocks = [u for u in cov.units if u.kind == "codeblock"]
        assert len(blocks) == 4
        assert [b.state for b in blocks] == ["covered", "covered", "covered", "unmodeled"]

    @staticmethod
    def test_derived_and_synthetic_excluded():
        findings = [
            _f(
                "读取 x.md",
                AuditVerdict.PASS,
                FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL, derived=True),
            ),
            _f(
                "(顺序)「A」须先于「B」",
                AuditVerdict.NA,
                FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION, synthetic=True),
            ),
        ]
        cov = build_text_coverage(_BODY, findings)
        assert cov.total_units > 0
        assert cov.covered == 0
        assert all(u.state in ("", "unmodeled") for u in cov.units)

    @staticmethod
    def test_dual_metrics_and_heading_not_counted():
        """coverage_rate=covered/modeled;modeled_ratio=modeled/total;heading 不进分母。"""
        findings = [
            _f(
                "确认",
                AuditVerdict.PASS,
                FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION, excerpt="关键节点必须向用户确认后方可继续"),
            ),
            _f(
                "读源码",
                AuditVerdict.NA,
                FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION, excerpt="改动前必须先读相关源码"),
            ),
        ]
        cov = build_text_coverage(_BODY, findings)
        # 分母 = 6 个非 heading 单元(paragraph×2 + bullet×3 + codeblock×1;heading 不进分母)
        assert cov.total_units == 6
        assert cov.covered == 1
        assert cov.uncovered == 1
        assert cov.unmodeled == 4
        assert cov.coverage_rate == pytest.approx(0.5)  # 1/2:covered/(covered+uncovered),未建模不进分母
        assert cov.modeled_ratio == pytest.approx(2 / 6)

    @staticmethod
    def test_empty_body_all_zero():
        cov = build_text_coverage("", [])
        assert cov.total_units == 0
        assert cov.coverage_rate == pytest.approx(0.0)
        assert cov.units == []

    @staticmethod
    def test_containment_fallback_for_edited_excerpt():
        """excerpt 是单元文的真子串(主语被裁/续行被切)→ 双向包含兜底命中。"""
        findings = [
            _f(
                "关键节点必须向用户确认后方可继续",
                AuditVerdict.PASS,
                FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION, excerpt="必须向用户确认后方可继续"),
            ),
        ]
        cov = build_text_coverage(_BODY, findings)
        bullet = next(u for u in cov.units if u.kind == "bullet" and "向用户确认" in u.text)
        assert bullet.state == "covered"

    @staticmethod
    def test_containment_single_point_no_double_count():
        """双向包含单点消歧:同一条被轻编辑 excerpt 出现在多条 bullet 时只挂一个单元。

        回归锁:旧实现 `return hits` 会把一条 covered 指令同时挂到多个单元,把
        covered/unmodeled 计数与 modeled_ratio 同时虚高(一条指令把两个单元染绿)。
        """
        body = "# 回指歧义\n\n- 必须先读相关源码后再动手修改\n- 改动前必须先读相关源码是硬前提\n"
        findings = [
            _f(
                "先读源码再动手",
                AuditVerdict.PASS,
                FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION, excerpt="必须先读相关源码"),
            ),
        ]
        cov = build_text_coverage(body, findings)
        # 两条 bullet 都含「必须先读相关源码」,但单点消歧后只染绿一个单元
        assert cov.covered == 1
        assert cov.uncovered == 0
        assert cov.unmodeled == 1
        assert cov.total_units == 2
        assert cov.coverage_rate == pytest.approx(1.0)  # covered/modeled = 1/1
        assert cov.modeled_ratio == pytest.approx(0.5)  # modeled/total = 1/2
