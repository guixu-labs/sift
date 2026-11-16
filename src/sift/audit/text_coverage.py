"""text_coverage: 声明文本覆盖视图——对账结果投影回声明原文。

coverage 视图(aggregate.aggregate_coverage)的分母是**抽取出的指令**;指令是工程
中间物,客户视角是文档。本模块把覆盖状态沿「指令 → 声明原文单元」回指上卷,产
「拿着原文看覆盖」的数据面:

- **单元切分**(``split_declaration_units``):fence-aware 把正文切成四类单元
  (heading / bullet / paragraph / codeblock)。bullet 续行规则与
  extractor._bullet_excerpt 逐字一致(缩进 > 基准缩进延续、空行终止、嵌套列表项
  归外层 bullet)——切分与抽取同规,source_excerpt 回指命中率才有根。
- **回指匹配**(``_match_units``):source_excerpt 归一化后先精确等值、再双向包含;
  启发式回指(精确等值/双向包含)**单点消歧**——一条指令只挂一个单元,取归一化
  后最长命中,避免同一条 covered 指令把多个单元同时染绿、虚高双指标。只有
  确定性回退(REFERENCE_USAGE 路径子串 / OUTPUT_FORMAT 标题下代码块)才允许
  一条指令对应多处文本。匹配不上 → 计入 unmapped_instructions(诚实计数,
  不静默吞)。
- **单元状态**(保守最低态):covered(全部指令 covered)/ uncovered(有指令但存在
  未触发或判不成都拉黄)/ unmodeled(没有指令回指——非规则文本或抽取盲区,**不猜**)。
  heading 是结构行,不进分母、无色。
- **双指标**:coverage_rate=covered/modeled(可评文本覆盖,未建模不进分母——单一
  数字不混「执行触达」与「抽取召回」两种含义);modeled_ratio=modeled/total_units
  (建模完整度,抽取质量的自我坦白)。

纯函数、零 LLM、零缓存(与 boundary 同款「每次全量重算,毫秒级」哲学)。三态判定
复用 aggregate.coverage_status 单一真身;指令分组复用 aggregate._group_coverage_findings
(derived/synthetic 天然排除——反推/合成指令不是声明规则,不参与文本覆盖)。
"""

from __future__ import annotations

import re
from typing import NamedTuple

from sift.audit.aggregate import _group_coverage_findings, coverage_status
from sift.compliance.extractor import _REF_PATH_RE
from sift.models.audit_models import (
    AuditFinding,
    TextCoverageSummary,
    TextCoverageUnit,
    TextUnitInstruction,
)

# bullet 起始行(与 _bullet_excerpt 的 item_starts 同一模式):行首可选缩进 + 列表
# 标记(- * + 或 1.) + 空白。与 extractor 两处约定永远一致,改一处必改另一处。
_BULLET_START_RE = re.compile(r"^[ \t]*(?:[-*+]\s+|\d+\.\s+)")

# 归一化:去行首列表标记、去空白/反引号/星号(markdown 包裹与排版噪声),供回指匹配。
# 不去 # / >(标题/引用不参与 bullet 匹配)、不去连字符(工具名含 -,去了会误伤等值)。
_MARK_NOISE_RE = re.compile(r"[\s`*]")

# 双向包含匹配的最短归一长度:太短的串(如「必须存在」归一后 4 字)在多篇单元里
# 都出现,包含匹配会大面积误指;短于阈值的 excerpt 只走精确等值。
_CONTAIN_MIN_LEN = 8


class DeclarationUnit(NamedTuple):
    """声明正文的一个切分单元(内部结构;报告序列化用 models 的 TextCoverageUnit)。"""

    kind: str  # "heading" | "bullet" | "paragraph" | "codeblock"
    start: int  # 在 skill_body 里的字符区间(诊断/定位;渲染用 text)
    end: int
    text: str
    heading: str = ""  # codeblock 专属:fence 开块时所处的标题(OUTPUT_FORMAT 回指键)


def _normalize(text: str) -> str:
    """回指匹配的归一化:剥 markdown 排版噪声,只留内容字符。"""
    if not text:
        return ""
    lines = [re.sub(r"^[ \t]*(?:[-*+]\s+|\d+\.\s+)", "", ln) for ln in text.split("\n")]
    return _MARK_NOISE_RE.sub("", "\n".join(lines))


class _UnitScanner:
    """fence-aware 正文扫描状态机:``split_declaration_units`` 的实现载体。

    逐行喂入(``_feed``),按 fence / 标题 / 空行 / 列表标记分派;开放中的
    bullet / paragraph 由 ``_close_text`` 收口,codeblock 整块归属开 fence 时的
    标题。切分规则与 extractor 的 _split_sections/_bullet_excerpt/extract_output_format
    同一套约定(嵌套列表项归外层 bullet 续行、fence 未闭合到文末按块收尾不丢内容)。
    """

    def __init__(self, body: str) -> None:
        self._body = body
        self._units: list[DeclarationUnit] = []
        self._open_kind: str | None = None  # "bullet" | "paragraph"
        self._open_start = 0
        self._open_base_indent = 0
        self._in_fence = False
        self._fence_start = 0
        self._fence_heading = ""
        self._current_heading = ""

    def scan(self) -> list[DeclarationUnit]:
        offset = 0
        for line in self._body.split("\n"):
            self._feed(line, offset, offset + len(line))
            offset += len(line) + 1
        return self._finish()

    def _feed(self, line: str, start: int, end: int) -> None:
        stripped = line.strip()
        if stripped.startswith("```"):
            self._on_fence(start, end)
            return
        if self._in_fence:
            return  # fence 内容行:整块归 codeblock,不当标题/正文处理
        if line.startswith("#"):
            self._on_heading(line, start, end)
            return
        if not stripped:
            self._close_text(start)  # 空行终止 bullet/段落,不并进任何单元
            return
        self._on_text_line(line, start)

    def _on_fence(self, start: int, end: int) -> None:
        if not self._in_fence:
            self._close_text(start)
            self._in_fence = True
            self._fence_start = start
            self._fence_heading = self._current_heading
            return
        self._in_fence = False
        text = self._body[self._fence_start:end]
        unit = DeclarationUnit("codeblock", self._fence_start, end, text, self._fence_heading)
        self._units.append(unit)

    def _on_heading(self, line: str, start: int, end: int) -> None:
        self._close_text(start)
        self._current_heading = line.lstrip("#").strip()
        self._units.append(DeclarationUnit("heading", start, end, line))

    def _on_text_line(self, line: str, start: int) -> None:
        indent = len(line) - len(line.lstrip(" \t"))
        if _BULLET_START_RE.match(line):
            self._on_bullet_start(start, indent)
            return
        if self._open_kind == "bullet" and indent > self._open_base_indent:
            return  # bullet 续行(嵌套列表项同规,归外层)
        if self._open_kind == "bullet":
            self._close_text(start)
            self._open_kind = "paragraph"
            self._open_start = start
        elif self._open_kind is None:
            self._open_kind = "paragraph"
            self._open_start = start

    def _on_bullet_start(self, start: int, indent: int) -> None:
        if self._open_kind == "bullet" and indent > self._open_base_indent:
            return  # 嵌套列表项:归外层 bullet 的续行(_bullet_excerpt 同规)
        self._close_text(start)
        self._open_kind = "bullet"
        self._open_start = start
        self._open_base_indent = indent

    def _close_text(self, end: int) -> None:
        if self._open_kind is None:
            return
        text = self._body[self._open_start:end].rstrip()
        unit = DeclarationUnit(self._open_kind, self._open_start, end, text)
        self._units.append(unit)
        self._open_kind = None

    def _finish(self) -> list[DeclarationUnit]:
        if self._in_fence:
            # fence 未闭合到文末也按块收尾(不丢内容)
            text = self._body[self._fence_start:]
            unit = DeclarationUnit("codeblock", self._fence_start, len(self._body), text, self._fence_heading)
            self._units.append(unit)
        else:
            self._close_text(len(self._body))
        return self._units


def split_declaration_units(body: str) -> list[DeclarationUnit]:
    """fence-aware 把声明正文切成文档序单元序列。

    规则(与 extractor 的 _split_sections/_bullet_excerpt/extract_output_format 三处
    同一套约定):
    - 代码块:``` 开/闭 fence,块内一切行(含 # 行)是块内容,整块一个单元,归属
      开 fence 时的标题;fence 未闭合到文末也按块收尾(不丢内容)。
    - 标题:行首 # 开头(不容缩进,同 _heading_map);单行一个 heading 单元,结构行。
    - bullet:列表标记行起,续行 = 缩进 > 该项基准缩进(**嵌套列表项也是续行**——
      _bullet_excerpt 同规),空行/缩进回落/标题/fence 终止。
    - paragraph:非上述的连续非空行。
    - 空行:终止 bullet/段落(不并进任何单元)。
    """
    return _UnitScanner(body).scan()


def _match_units(group: dict, units: list[DeclarationUnit]) -> list[int]:
    """一条指令分组 → 回指的单元下标(可空 = unmapped)。

    优先级:source_excerpt 精确等值 > 双向包含 > 指令类型专属回退
    (REFERENCE_USAGE 路径子串 / OUTPUT_FORMAT 标题下代码块)。
    启发式回指(精确等值/双向包含,见 _match_by_excerpt)单点消歧:一条指令
    只挂一个单元;只有确定性回退允许一条指令对应多处文本。
    """
    excerpt = group.get("source_excerpt") or ""
    if excerpt:
        hits = _match_by_excerpt(excerpt, units)
        if hits:
            return hits
    source = group.get("source")
    if source == "REFERENCE_USAGE":
        hits = _match_reference_paths(group.get("instruction_text") or "", units)
        if hits:
            return hits
    if source == "OUTPUT_FORMAT":
        hits = _match_section_codeblocks(group.get("section") or "", units)
        if hits:
            return hits
    return []


def _match_by_excerpt(excerpt: str, units: list[DeclarationUnit]) -> list[int]:
    """source_excerpt 回指:精确等值优先,双向包含兜底(bullet/paragraph 参与)。"""
    norm_excerpt = _normalize(excerpt)
    # 1) 精确等值(heading/codeblock 不参与——excerpt 是列表项/行原文)
    for i, u in enumerate(units):
        if u.kind in ("bullet", "paragraph") and _normalize(u.text) == norm_excerpt:
            return [i]
    # 2) 双向包含(短边 >= 阈值;声明被轻微编辑/缓存条目旧形态时兜底)
    if len(norm_excerpt) < _CONTAIN_MIN_LEN:
        return []
    # 单点消歧:一条指令只挂一个单元——若把同一条 covered 指令同时挂到多个
    # 单元,会把它们全部染绿,虚高 coverage_rate/modeled_ratio。取归一化后最长
    # 的命中单元(最接近「完整原文」),并列取文档序首个。
    best = -1
    best_len = -1
    for i, u in enumerate(units):
        if u.kind not in ("bullet", "paragraph"):
            continue
        norm_unit = _normalize(u.text)
        if len(norm_unit) < _CONTAIN_MIN_LEN:
            continue
        if norm_excerpt in norm_unit or norm_unit in norm_excerpt:
            if len(norm_unit) > best_len:
                best, best_len = i, len(norm_unit)
    return [best] if best >= 0 else []


def _match_reference_paths(instruction_text: str, units: list[DeclarationUnit]) -> list[int]:
    """REFERENCE_USAGE 回退:按 instruction_text 里的 references/X | scripts/X 路径找单元。

    extractor 写的是 "Use reference file: {ref}";类别级("Consult reference
    material (N …)")无具体路径 → 不中,如实进 unmapped。
    """
    hits: set[int] = set()
    for path in _REF_PATH_RE.findall(instruction_text):
        for i, u in enumerate(units):
            if path in u.text:
                hits.add(i)
    return sorted(hits)


def _match_section_codeblocks(section: str, units: list[DeclarationUnit]) -> list[int]:
    """OUTPUT_FORMAT 回退:对 section(fence 归属标题)下的 codeblock 回指。

    同节多模板共享一组块(与 extract_output_format 同键)。extractor 对每个 section
    只采样前 3 个 codeblock 当输出模板(_append_output_template 的 per_section_count
    < 3),回指同样只取前 3 个(文档序前 3 个,与 extractor 采样一致)——第 4+ 块
    extractor 并未抽成 OUTPUT_FORMAT 指令,一并标 covered 会过标;保守对齐只取 3。
    """
    hits = []
    for i, u in enumerate(units):
        if u.kind == "codeblock" and u.heading == section:
            hits.append(i)
            if len(hits) == 3:
                break
    return hits


def _unit_state(kind: str, instrs: list[TextUnitInstruction]) -> str:
    """单元三态判定:heading 是结构行返回 "";无指令 = unmodeled;有指令取保守最低态。

    保守最低态:全部指令 covered 才绿;存在未触发或判不成都拉黄。
    """
    if kind == "heading":
        return ""
    if not instrs:
        return "unmodeled"
    if all(x.status == "covered" for x in instrs):
        return "covered"
    return "uncovered"


def build_text_coverage(skill_body: str, findings: list[AuditFinding]) -> TextCoverageSummary:
    """对账结果投影回声明原文(纯函数;空 body/空 findings → 全 0 保守值)。"""
    units = split_declaration_units(skill_body or "")
    groups = _group_coverage_findings(findings)
    unit_instructions: dict[int, list[TextUnitInstruction]] = {}
    unmapped: list[str] = []
    for g in groups.values():
        status = coverage_status(g["pass"], g["fail"], g["na"], g["unresolved"], g["indeterminate"])
        hits = _match_units(g, units)
        if not hits:
            unmapped.append(g["instruction_text"])
            continue
        entry = TextUnitInstruction(
            instruction_text=g["instruction_text"], headline=g.get("headline", ""), status=status
        )
        for i in hits:
            unit_instructions.setdefault(i, []).append(entry)

    models: list[TextCoverageUnit] = []
    counters = {"covered": 0, "uncovered": 0, "unmodeled": 0}
    for i, u in enumerate(units):
        state = _unit_state(u.kind, unit_instructions.get(i, []))
        if state:
            counters[state] += 1
        models.append(
            TextCoverageUnit(
                kind=u.kind, start=u.start, end=u.end, text=u.text, state=state, heading=u.heading,
                instructions=unit_instructions.get(i, []),
            )
        )
    total = sum(counters.values())
    modeled = counters["covered"] + counters["uncovered"]
    return TextCoverageSummary(
        total_units=total,
        covered=counters["covered"],
        uncovered=counters["uncovered"],
        unmodeled=counters["unmodeled"],
        coverage_rate=counters["covered"] / modeled if modeled else 0.0,
        modeled_ratio=modeled / total if total else 0.0,
        unmapped_instructions=unmapped,
        units=models,
    )
