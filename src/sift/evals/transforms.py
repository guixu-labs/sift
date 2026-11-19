
"""Pure evaluation prompt preparation and result normalization."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import NamedTuple

from sift.evals.prompt_sections import (
    AGENT_SOURCE_HEADER,
    EVAL_JSON_HEADER,
    EVAL_RATIONALE_RULE,
    SKILL_SOURCE_SECTION,
)
from sift.models import (
    Assertion,
    AssertionType,
    EvalCase,
    EvalSet,
)


class PromptSource(NamedTuple):
    """供不同出题方式共用的声明内容；content 保留调用方选择的全文或正文。"""

    name: str
    description: str
    content: str
    frontmatter: dict | None = None


_SKILL_OUTPUT_SECTION_RE = re.compile((r"(输出|output|产物|产出|生成物|交付物|deliverable|result|结果)"), re.IGNORECASE)


_SKILL_STEP_SECTION_RE = re.compile(r"(步骤|step|流程|workflow|procedure|过程)", re.IGNORECASE)


_SKILL_WRITE_VERB_RE = re.compile(
    (
        "(写出|写入|写至|写到|生成|创建|输出|产出|保存|导出|emit|write\\s+(?:out|to)|"
        "create|generate|produce|output|save|export)"
    ),
    re.IGNORECASE,
)


_SKILL_FILENAME_RE = re.compile(r"[\w/\-]+\.[\w]+")


_MERGE_REGEX_META = frozenset(".^$*+?{}[]\\|()")


_MERGE_KV_RE = re.compile((r'"([\w\-]+)"\s*:\s*("[^"]*"|true|false|null|-?\d+(?:\.\d+)?)'))


def _merge_is_literal_pattern(pattern: str) -> bool:
    """无正则元字符 → 纯字面量(可安全做包含/常量比较)。"""
    return bool(pattern) and not (_MERGE_REGEX_META & set(pattern))


def _merge_fixtures_conflict(group: list[EvalCase]) -> bool:
    """Gate 1:同 fixture path 出现 ≥2 不同 content → 不同 scenario,合并不安全。

    (原 union dedup key 本就是 ``(path, content)``,这里拿来做门:同 path 不同 content
    = transcript 成功段与失败段两套互斥 ground truth,合并必出自相矛盾题。)
    """
    by_path: dict[str, set[str]] = {}
    for e in group:
        for fx in e.fixtures or []:
            by_path.setdefault(fx.path or "", set()).add(fx.content or "")
    return any(len(values) > 1 for values in by_path.values())


def _merge_assertions_conflict(group: list[EvalCase]) -> bool:
    """Gate 2:断言互相矛盾 → 合并必出自相矛盾题。保守:只判两种确定性矛盾。

    (a) CONTAINS_PATTERN 字面量 L 命中 NOT_CONTAINS 正则 R(同 file 作用域):一要含、一不许出现。
    (b) 同 (file,key) 的 CONTAINS_PATTERN 字面量钉成不同常量(按 ``"key": value`` 形状抽取)。

    跳过(证不了,保守放过):LLM_GRADE/LLM_FACT/CUSTOM_SCRIPT(语义)、正则对可满足性
    (不可判定)、含元字符的 CONTAINS(非字面量,证不了字面重叠)、主观 expectation 文本对立。
    """
    contains: list[Assertion] = []
    not_contains: list[Assertion] = []
    _group_pattern_assertions(group, contains, not_contains)

    def _scope(a: Assertion) -> str:
        return (a.params.get("path") or "").strip()

    def _same_scope(p: str, q: str) -> bool:
        # 空 path = 未限定 = 覆盖任意显式 file;仅两者都显式且不同才视为不同作用域
        return (not p or not q) or p == q

    # (a) CONTAINS 字面量 ⊆ NOT_CONTAINS 正则
    for c in contains:
        cp = c.params.get("pattern") or ""
        if not _merge_is_literal_pattern(cp):
            continue
        for n in not_contains:
            nr = n.params.get("pattern") or ""
            if not (nr and _same_scope(_scope(c), _scope(n))):
                continue
            if _merge_regex_matches(nr, cp):
                return True

    # (b) 同 (file,key) 不同常量
    kv: dict[tuple[str, str], set[str]] = {}
    for c in contains:
        cp = c.params.get("pattern") or ""
        if not _merge_is_literal_pattern(cp):
            continue
        scope = _scope(c)
        for m in _MERGE_KV_RE.finditer(cp):
            kv.setdefault((scope, m.group(1)), set()).add(m.group(2))
    if any(len(values) > 1 for values in kv.values()):
        return True

    return False


def _group_pattern_assertions(group, contains, not_contains):
    from itertools import chain

    for a in chain.from_iterable(e.assertions or [] for e in group):
        if a.type == AssertionType.CONTAINS_PATTERN:
            contains.append(a)
        elif a.type == AssertionType.NOT_CONTAINS:
            not_contains.append(a)


_PROCEDURAL_PROMPT_TEMPLATE = (
    (
        "You are an expert at creating evaluation test cases for AI coding assistant skills.\n"
        "\n"
        "This skill contains executable procedures (code examples, scripts, or reference files). "
        "Generate a mix of L1 and L2 test cases.\n"
        "\n"
        "## L1 Tests (40% — ~3-4 cases)\n"
        "Test whether Claude **understands the rules**: which tool to use, what parameters, what order "
        "to do things, what NOT to do. Prompt asks a question or describes a scenario and expects a "
        "knowledgeable response. NO fixtures needed.\n"
        "\n"
        "## L2 Tests (60% — ~4-6 cases)\n"
        "Test whether Claude **actually executes** the task correctly. Provide fixture files as input "
        "and check output files with assertions. These test the exact code patterns and workflows from "
        "the skill.\n"
        "\n"
        "For L2, include fixtures that simulate real input data. For example, if the skill processes PDF "
        "files, provide a minimal valid fixture. Use FILE_EXISTS, CONTAINS_PATTERN, NOT_CONTAINS "
        "assertions to check outputs.\n"
    )
    + EVAL_JSON_HEADER
    + (
        '      "prompt": "Question/scenario testing knowledge of ONE rule",\n'
        '      "expectations": ["Verifiable claim about what Claude should say/know"],\n'
        '      "assertions": [\n'
        '        {{"name": "mentions correct tool", "type": "CONTAINS_PATTERN", "params": {{"pattern": '
        '"keyword"}}}}\n'
        "      ],\n"
        '      "fixtures": [],\n'
        '      "rationale": "<SKILL.md span this eval tests — same language as SKILL.md>",\n'
        '      "confidence": 0.0-1.0\n'
        "    }},\n"
        "    {{\n"
        '      "id": 5,\n'
        '      "name": "L2-<operation>-<scenario>",\n'
        '      "level": "L2",\n'
        '      "prompt": "A realistic task: process/generate/convert file X, produce output Y. Include '
        'the exact file paths from fixtures.",\n'
        '      "expectations": ["Verifiable claim about the OUTPUT FILES produced — e.g. \'generated '
        "merged.pdf with 3 pages', 'extracted text saved to output.txt'\"],\n"
        '      "assertions": [\n'
        '        {{"name": "output file exists", "type": "FILE_EXISTS", "params": {{"filename": '
        '"output.pdf"}}}},\n'
        '        {{"name": "contains expected content", "type": "CONTAINS_PATTERN", "params": '
        '{{"pattern": "Expected", "path": "output.txt"}}}}\n'
        "      ],\n"
        '      "fixtures": [\n'
        '        {{"path": "input.pdf", "content": "(simulated file content — make it realistic but '
        'minimal)"}}\n'
        "      ],\n"
        '      "rationale": "<SKILL.md span this eval tests — same language as SKILL.md>",\n'
    )
    + EVAL_RATIONALE_RULE
    + (
        "- Total 8-12 evals\n"
        "- L1: test rules/decisions (no fixtures). L2: test actual execution (with fixtures).\n"
        "- L2 fixtures must be REALISTIC minimal test data that Claude can actually process.\n"
        "- Fixture `path` MUST be a RELATIVE path (e.g. `input.pdf`, `logs/app.log`) and the prompt must "
        "reference that same relative path. NEVER absolute paths like `/tmp/...` — the executor runs in "
        "an isolated working directory, absolute paths escape it and assertions check the working dir → "
        "guaranteed false FAIL.\n"
        "- For text-based fixtures (JSON/code), include the ACTUAL content. For binary fixtures "
        "(PDF/image), write a minimal placeholder.\n"
        "- L2 expectations must mention CONCRETE output: file names, counts, patterns.\n"
        "- Every expectation is a FULL SENTENCE with a verb — including format/structure requirements. "
        "Never a noun-only label. BAD: 'correct output format'; GOOD: 'the generated file lists each "
        "entry with its name and line number'.\n"
        "- L1 assertions: CONTAINS_PATTERN, NOT_CONTAINS only. NO FILE_EXISTS on L1.\n"
        "- L2 assertions: FILE_EXISTS, CONTAINS_PATTERN, NOT_CONTAINS, JSON_HAS_FIELD.\n"
        "- Language: same as SKILL.md\n"
        "- Include at least 1 error-handling or edge-case scenario\n"
    )
    + SKILL_SOURCE_SECTION
)


_KNOWLEDGE_PROMPT_TEMPLATE = (
    (
        "You are an expert at creating evaluation test cases for AI coding assistant skills.\n"
        "\n"
        "Given the following Skill definition, generate **L1 rule-comprehension** evaluation test "
        "cases.\n"
        "L1 tests check whether Claude **understands the rules and conventions** — NOT whether it can "
        "execute tasks.\n"
        "\n"
        "## Categories (pick the most relevant)\n"
        "\n"
        "1. **Decision Logic**: branching rules, conditions, scenarios\n"
        "2. **Input Validation**: what happens when preconditions met/not met?\n"
        "3. **Output Format**: does Claude know the required output structure?\n"
        "4. **Skill Binding**: which sub-skill/tool to use in which context?\n"
        "5. **Error Handling**: failure recovery and fallback rules\n"
        "6. **Constraint Following**: NEVER/ALWAYS/MUST rules\n"
    )
    + EVAL_JSON_HEADER
    + (
        '      "prompt": "A specific question that tests ONE rule. Make it realistic — what would a real '
        'user ask?",\n'
        '      "expectations": ["Single, concrete, verifiable claim. GOOD: \'response mentions checking '
        "for AndroidManifest.xml'. BAD: 'the output is correct'. BAD: 'follows the skill "
        "instructions'.\"],\n"
        '      "assertions": [\n'
        '        {{"name": "mentions X", "type": "CONTAINS_PATTERN", "params": {{"pattern": '
        '"keyword"}}}},\n'
        '        {{"name": "avoids Y", "type": "NOT_CONTAINS", "params": {{"pattern": "forbidden"}}}}\n'
        "      ],\n"
        '      "fixtures": [],\n'
        '      "rationale": "<which SKILL.md section/bullet/quote this eval tests — SAME language as '
        'SKILL.md>",\n'
    )
    + EVAL_RATIONALE_RULE
    + (
        "- 5-10 evals covering the most important rules\n"
        "- NO vague expectations — every one must be objectively checkable\n"
        "- Every expectation is a FULL SENTENCE with a verb — including format/structure requirements "
        "(output shape, ordering, fields). Never compress to a noun-only label. BAD: 'correct output "
        "format'; GOOD: 'the response lists each entry with its file name and line number'.\n"
        "- assertions use ONLY: CONTAINS_PATTERN, NOT_CONTAINS for text checks. NO FILE_EXISTS — L1 "
        "tests don't produce files.\n"
        "- Language: same as SKILL.md\n"
        "- At least 1 negative test (wrong input, missing precondition)\n"
    )
    + SKILL_SOURCE_SECTION
)


_TRANSCRIPT_PROMPT_TEXT_1 = (
    "\n"
    "## ASSERTION SCHEMA(scorer 读这些精确 key):\n"
    '- FILE_EXISTS: {"filename": "<manifest path>"}\n'
    '- CONTAINS_PATTERN / NOT_CONTAINS: {"pattern": "<regex>", "path": "<manifest '
    'path>"}(查产物文件内容) 或 {"pattern": "<regex>", "scope": "transcript"}(只查 agent/skill '
    "文本输出,不碰文件——只读/纯建议 agent 用这个,别 path 指输入文件)\n"
    '- JSON_HAS_FIELD: {"filename": "...", "field": "..."}\n'
    '- CUSTOM_SCRIPT: {"script": "<checker .py>", "args": []}——checker 必须同时是该 eval 的 fixture\n'
)


_TRANSCRIPT_PROMPT_TEXT_2 = (
    "level: L1=不产文件/纯分析建议(大多数 agent,如审 PR 给建议);产口头建议时挖 review "
    "文本里的行号/严重度/关键词 → CONTAINS_PATTERN/NOT_CONTAINS **带 scope:'transcript'**(查 agent "
    "回答文本,别用 path 指输入文件——否则 grading 读输入 fixture 而非 agent "
    "的回答,判卷失效);L2=产文件:产物存在用 FILE_EXISTS(filename)、产物内容用 "
    "CONTAINS_PATTERN/NOT_CONTAINS(path)(key 见上 ASSERTION SCHEMA,别混);另:若该题考 agent "
    "的过程(真看 diff / 逐条核 / Grep 确认,非靠猜),给过程标记加 scope:'transcript' 断言查 agent "
    "推理文本(产物对≠过程对,蒙对的要抓)。\n"
)


_TRANSCRIPT_PROMPT_TEXT_3 = (
    "\n"
    '## Output JSON ONLY(EvalSet schema): {"skill_name":"...","evals":[{"id":1,"name":"...","level":"'
    'L1|L2","prompt":"...","expected_output":"...","expectations":["..."],"assertions":[{"name":"..."'
    ',"type":"...","params":{...}}],"fixtures":[{"path":"...","content":"..."}],"rationale":"<'
)


class _MergedRequirements(NamedTuple):
    """同题合并后需要写回考题的四组要求。"""

    assertions: list[Assertion]
    expectations: list[str]
    fixtures: list
    expected_outputs: list[str]


def _resolve_manifest_assertion(a: Assertion, fixture_paths: set[str], resolve) -> str | None:
    """归一断言中的文件路径；不能对应到产物时返回需要记录的断言名。"""
    p = a.params
    if a.type in (AssertionType.FILE_EXISTS, AssertionType.JSON_HAS_FIELD):
        fn = resolve(p.get("filename", ""))
        if fn:
            p["filename"] = fn
            return None
        else:
            return a.name
    elif a.type in (AssertionType.CONTAINS_PATTERN, AssertionType.NOT_CONTAINS):
        raw_path = p.get("path", "")
        if not raw_path:
            return None  # path-less 合法(scorer rglob+transcript),L1 用
        else:
            path = resolve(raw_path)
            if path:
                p["path"] = path
                return None
            else:
                return a.name
    elif a.type == AssertionType.CUSTOM_SCRIPT:
        script = p.get("script", "")
        resolved = resolve(script)
        if (resolved or script) in fixture_paths:
            p["script"] = resolved or script
            return None
        else:
            return f"{a.name}(unpaired checker)"
    else:
        return None


def _merge_compatible_evals(group: list[EvalCase], next_id: int) -> EvalCase:
    """合并已通过冲突检查的同题考点，保留首题元数据及原有去重顺序。"""
    first = group[0]
    assertions, expectations, fixtures, eouts = _collect_merged_requirements(group)
    flags = list(dict.fromkeys([*(first.flags or []), f"merged:{len(group)}-evals"]))
    return EvalCase(
        id=next_id,
        name=first.name,
        prompt=first.prompt,
        expected_output="\n".join(f"- {x}" for x in eouts) if eouts else "",
        files=first.files,
        expectations=expectations,
        assertions=assertions,
        fixtures=fixtures,
        level="L2" if fixtures else first.level,
        mode=first.mode,
        source_dir=first.source_dir,
        source_dir_as=first.source_dir_as,
        timeout=first.timeout,
        flags=flags,
        rationale=first.rationale,
        confidence=first.confidence,
    )


def _collect_merged_requirements(group: list[EvalCase]) -> _MergedRequirements:
    """按输入顺序去重断言、预期、fixture 和输出说明。"""
    seen_a, assertions = set(), []
    seen_x, expectations = set(), []
    seen_f, fixtures = {}, []
    seen_o, eouts = set(), []
    for e in group:
        for a in e.assertions or []:
            sig = (a.type, a.name, json.dumps(a.params or {}, sort_keys=True, ensure_ascii=False))
            if not (sig not in seen_a):
                continue
            seen_a.add(sig)
            assertions.append(a)
        for x in e.expectations or []:
            if not (x not in seen_x):
                continue
            seen_x.add(x)
            expectations.append(x)
        for fx in e.fixtures or []:
            fsig = (fx.path or "", fx.content or "")
            if not (fsig not in seen_f):
                continue
            seen_f[fsig] = fx
            fixtures.append(fx)
        eo = (e.expected_output or "").strip()
        if not (eo and eo not in seen_o):
            continue
        seen_o.add(eo)
        eouts.append(eo)
    return _MergedRequirements(assertions, expectations, fixtures, eouts)


def _transcript_evidence_lines(digest, skill_outputs: set[str]) -> list[str]:
    """按原顺序组织用户请求、输入、产物和命令证据。"""
    manifest = sorted(digest.manifest | skill_outputs)
    produced = sorted(f.path for f in digest.produced_files)
    inputs = sorted(f.path for f in digest.input_files)
    lines: list[str] = []
    lines.append("## REAL USER REQUESTS\n")
    for i, t in enumerate(digest.user_turns, 1):
        lines.append(f"[user {i}] {t[:1500]}\n")
    if digest.skill_used:
        lines.append(f"\nSkill invoked: {digest.skill_used}\n")
    lines.append("\n## INPUT FILES (作 fixture stage,真实内容)\n")
    for f in digest.input_files:
        lines.append(f"--- {f.path} ---\n{f.content[:2000]}\n")
    lines.append(("\n## PRODUCED FILES (在其上断言 FILE_EXISTS / 内容)\n"))
    for f in digest.produced_files:
        lines.append(f"--- {f.path} ---\n{f.content[:2000]}\n")
    if digest.commands:
        lines.append("\n## COMMANDS RUN + OUTPUTS (验证候选)\n")
        for c in digest.commands[:10]:
            lines.append(f"$ {c.cmd[:300]}\n{c.stdout[:800]}\n")
    if digest.unreconstructed:
        lines.append((f"\n## UNRECONSTRUCTED (Bash-only,别断言): {digest.unreconstructed}\n"))
    lines.append((f"\n## PATH MANIFEST (断言/fixture 路径只能引用这些): {manifest}\n"))
    lines.append((f"produced(FILE_EXISTS/内容目标): {produced}\ninput(fixtures): {inputs}\n"))
    if skill_outputs:
        lines.append(
            (
                "\n## SKILL-DECLARED OUTPUTS (skill 要求的产物;此 transcript 未必产出,但遵循 "
                "skill 就该产 —— 可放心对它们出 FILE_EXISTS / 内容断言): "
                f"{sorted(skill_outputs)}\n"
            )
        )
    return lines


def _transcript_prompt_intro(is_agent: bool, frontmatter: dict | None) -> list[str]:
    """说明出题对象，并在 agent 模式下带入工具白名单。"""
    lines: list[str] = []
    if is_agent:
        lines.append(("You are an expert at creating evaluation test cases for AI agents.\n"))
        lines.append(
            (
                "下面是 (a) Agent system prompt(角色指令正文) 与 (b) 一份该 agent 真实使用 "
                "session 的 transcript digest。从 transcript 挖真实 evals(会交给这个 agent "
                "的任务),别凭空造。\n"
            )
        )
        lines.append("## Agent 声明的工具白名单\n" + str((frontmatter or {}).get("tools", [])) + "\n")
    else:
        lines.append(("You are an expert at creating evaluation test cases for AI coding assistants.\n"))
        lines.append(
            (
                "下面是 (a) Skill 定义 与 (b) 一份该 skill 真实使用 session 的 transcript "
                "digest。从 transcript 挖真实 evals,别凭空造。\n"
            )
        )
    return lines


def _written_skill_outputs(text: str) -> set[str]:
    """仅从写入或生成动作所在行提取产物名。"""
    outputs: set[str] = set()
    for line in text.split("\n"):
        if not (_SKILL_WRITE_VERB_RE.search(line)):
            continue
        outputs |= {m.group(0) for m in _SKILL_FILENAME_RE.finditer(line)}
    return outputs


_AGENT_EVAL_PROMPT = (
    "你在为一个 AI agent 设计评测用例。被评的是 agent 定义本身(system prompt 角色),不是 skill。\n"
    + AGENT_SOURCE_HEADER
    + (
        "{body}\n"
        "\n"
        "## Agent 声明的工具白名单\n"
        "{tools}\n"
        "\n"
        "## 任务\n"
        "生成 5-8 道真实、会交给这个 agent 的考题。每道题要能区分好/坏 response(太 trivial "
        "区分不了就没评测价值),且必须匹配 description 声明的角色(它说管 X 就出 X 的题,不出它不该管的)。\n"
        "\n"
        "## Output JSON (ONLY this, no other text)\n"
        "```json\n"
        "{{\n"
        '  "skill_name": "{name}",\n'
        '  "evals": [\n'
        "    {{\n"
        '      "id": 1,\n'
        '      "name": "agent-<scenario>",\n'
        '      "level": "L1",\n'
        '      "prompt": "主 agent / 用户会把这件事怎么交给它(具体场景 + 必要上下文,别泛泛)",\n'
        '      "expected_output": "好的完成长什么样(可观测的质量标准)",\n'
        '      "expectations": [\n'
        "        \"单条、具体、可观测的预期。GOOD: 'response 指出具体行号'。BAD: '输出正确' / "
        "'遵循指令'(空泛不可查)\"\n"
        "      ],\n"
        '      "assertions": [\n'
        '        {{ "name": "mentions X", "type": "CONTAINS_PATTERN", "params": {{ "pattern": "keyword", '
        '"scope": "transcript" }} }},\n'
        '        {{ "name": "avoids Y", "type": "NOT_CONTAINS", "params": {{ "pattern": "forbidden", '
        '"scope": "transcript" }} }}\n'
        "      ],\n"
        '      "fixtures": [\n'
        '        {{ "path": "src/auth.py", "content": "真实文件内容(带要审的问题 / 要跑的代码),agent '
        '会真去 Read/Grep 它" }}\n'
        "      ],\n"
        '      "rationale": "这题考 agent 哪条职责(对应 description / 正文哪点;同 agent 声明语言)"\n'
        "    }}\n"
        "  ]\n"
        "}}\n"
        "```\n"
        "\n"
        "## Rules\n"
        "- 语言:同 agent 声明正文\n"
        "- expectations 一律写**完整句子**(有动词),包括格式/总评类(行号、严重度、总评结论)——别压成名词堆"
        '标签。BAD:"总评说明阻塞项与能否合并" / "带 文件:行号 + '
        '严重度";GOOD:"总评里说清有哪些阻塞项、这个 PR 能不能合" / "每条建议都标了 文件:行号 和严重度"\n'
        "- level: L1 = 不产文件 / 纯分析建议(大多数 agent);L2 = 产文件(产出的文件名写进 assertions 的 "
        "FILE_EXISTS)\n"
        "- 每条 expectation 都写对应 assertion(s)(可一条拆多个、可加隐含的);L1 用 CONTAINS_PATTERN / "
        'NOT_CONTAINS 且 **必须带 "scope":"transcript"**——只读 agent 不产文件,断言查 agent '
        "的回答文本(transcript),不查输入文件(带 path 或省 scope 会去读输入 fixture 而非 agent "
        "的回答,判卷失效);L2 产文件:产物**存不存在**用 FILE_EXISTS + "
        '{{"filename":"<产物文件>"}};产物**内容**用 CONTAINS_PATTERN/NOT_CONTAINS + '
        '{{"path":"<产物文件>","pattern":"..."}}(FILE_EXISTS 是 filename、内容检查是 '
        "path,别混);另:若该题考 agent 的**过程**(有没有真看 diff / 逐条核 / Grep "
        '确认,而非靠猜)——给过程标记加 {{"scope":"transcript","pattern":"..."}} 断言查 agent '
        "推理文本(产物对 ≠ 过程对,靠猜蒙对的要抓)\n"
        "- fixtures:若任务涉及文件(审代码 / 跑测试 / 读配置——agent 会用 Read/Grep/Bash "
        "碰文件),把相关文件作为 fixtures 落地(prompt 用**路径**引用,如「审查 src/auth.py "
        "的改动」,**别把代码塞进 prompt 文本**——agent 要能真 Read/Grep 文件才算测到它的工作流,塞 prompt "
        "里它就没文件可碰);纯建议、无文件操作的 agent 不带 fixtures。fixture 是**输入**(给 agent 读 / "
        '跑的),断言仍用 scope:"transcript" 查 agent 的回答,**不要**用 path 指 fixture\n'
        "- rationale 每题必填:说清考哪条职责\n"
        "- 总 5-8 道,至少 1 道边界 / 错误场景"
    )
)


def build_agent_prompt(source: PromptSource, focus: str | None = None, existing_evals: list | None = None) -> str:
    name = source.name
    description = source.description
    body = source.content
    frontmatter = source.frontmatter
    tools = frontmatter.get("tools", [])
    prompt = _AGENT_EVAL_PROMPT.format_map({"name": name, "description": description, "body": body, "tools": tools})
    directive = supplement_directive(focus, existing_evals)
    if directive:
        # 注在 ## Output JSON 前(同 _build_llm_prompt),把这次生成目标改成定向补 gap、
        # 对照现有题 prompt+rationale 语义去重——agent 与 skill 出题补题内核统一。
        prompt = prompt.replace("## Output JSON", directive + "## Output JSON", 1)
    return prompt


def split_into_sections(body: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_title = ""
    current_lines: list[str] = []

    for line in body.split("\n"):
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level > 2:
                current_lines.append(line)
                continue
            if current_lines:
                sections.append((current_title, "\n".join(current_lines)))
            current_title = line.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            sections.append((current_title or "Overview", text))

    return sections


def enforce_manifest(eval_set: EvalSet, manifest: set[str]) -> EvalSet:
    """确定性后处理:把裸名补全成 manifest 路径、scoped path、CUSTOM_SCRIPT↔fixture 配对;
    解析不了的断言 drop 并在 eval.flags 标记。
    """
    # basename -> full(仅当唯一)
    by_base: dict[str, str] = {}
    ambig: set[str] = set()
    for p in manifest:
        b = os.path.basename(p)
        if b in by_base:
            ambig.add(b)
        else:
            by_base[b] = p
    for b in ambig:
        by_base.pop(b, None)

    def resolve(raw: str) -> str | None:
        if not raw:
            return None
        if raw in manifest:
            return raw
        base = os.path.basename(raw)
        return by_base.get(base)

    for ev in eval_set.evals:
        fixture_paths = {f.path for f in ev.fixtures}
        kept: list[Assertion] = []
        dropped: list[str] = []
        for a in ev.assertions:
            drop_reason = _resolve_manifest_assertion(a, fixture_paths, resolve)
            if drop_reason is None:
                kept.append(a)
            else:
                dropped.append(drop_reason)
        ev.assertions = kept
        for f in ev.fixtures:
            if not (f.path not in manifest and resolve(f.path) is None):
                continue
            ev.flags.append(f"review:fixture-not-in-manifest:{f.path}")
        if not (dropped):
            continue
        ev.flags.append("review:dropped-assertions:" + ",".join(dropped))
    return eval_set


_FOCUS_PRESETS = {
    "negative-cases": (
        "## Focus: Negative scenarios\n"
        "\n"
        "ONLY generate evals for scenarios where this skill should **NOT** be triggered, or should "
        "**refuse / hand off / warn** — wrong input type, out-of-scope request, ambiguous trigger, "
        "precondition not met. Default generation over-indexes on happy-path / positive triggers; this "
        "run deliberately fills the negative-coverage gap.\n"
        "\n"
    ),
    "edge-cases": (
        "## Focus: Edge cases & error handling\n"
        "\n"
        "ONLY generate evals for boundary / error conditions: empty input, oversized input, malformed / "
        "corrupted input, missing fields, abnormal values, failure recovery. Default generation "
        "over-indexes on happy-path; this run deliberately fills the edge / error-coverage gap.\n"
        "\n"
    ),
}


_FOCUS_ALIASES = {"anti-triggers": "negative-cases"}


def focus_directive(focus: str | None) -> str:
    """--focus → 注入 prompt 的定向指令段(None/空 → "",默认生成整套不变)。

    预设类(negative-cases / edge-cases)补 LLM 生成整套时的**通用盲区**(prompt 精调,
    强制反面 / 边界思维);其余当**自由文本**(补 skill 特定薄弱面,如"加密 PDF")。
    通常配 ``--append`` 增量补到已有 evals.json(否则覆盖式只换套题,价值低)。
    """
    if not focus or not focus.strip():
        return ""
    key = focus.strip().lower()
    key = _FOCUS_ALIASES.get(key, key)  # 旧名 back-compat
    if key in _FOCUS_PRESETS:
        return _FOCUS_PRESETS[key]
    return (
        "## Focus: Targeted supplement (user-specified)\n\n"
        "ONLY generate evals for this specific direction:\n"
        f"> {focus.strip()}\n\n"
        "Default generation may have under-covered this; this run targets it deliberately.\n\n"
    )


def supplement_directive(focus: str | None, existing_evals) -> str:
    """定向补题指令:有现有题(init --append / coverage --augment)→ 硬化;无 → 退回软 _focus_directive。

    治 init --append --focus 半聚焦 + 不去重:旧版只把 focus 作软 directive 注到「生成整套」
    prompt 前,LLM 仍端到端生成一遍、且与现有题重复。有现有题时改用纯补题指令——明确
    「只补 focus 这一面、且现有未覆盖的」,并把现有题清单(name + prompt + rationale)喂进去
    让 LLM 语义避重(对照 prompt **和** rationale,不只字面)。init / coverage --augment 共用
    此路径(_build_llm_prompt 一处改,两路达标)。
    """
    if not existing_evals:
        # 无现有题(覆盖式 init)→ 行为不变,走软 focus directive
        return focus_directive(focus)
    lines = []
    for e in existing_evals[:30]:
        rat = (e.rationale or "").strip().replace("\n", " ")
        prm = (e.prompt or "").strip().replace("\n", " ")
        lines.append((f'- [{e.id}] "{e.name}" — prompt: {prm[:90]} | rationale: {rat[:110]}'))
    existing_block = "\n".join(lines)
    focus_line = (f"**Targeted gap to fill THIS run:** {focus.strip()}\n\n") if (focus and focus.strip()) else ""
    return (
        "## THIS IS A TARGETED SUPPLEMENT, NOT a fresh full suite\n\n"
        "evals.json already has the existing evals listed below. DO NOT regenerate or "
        "duplicate them. Generate ONLY new evals that:\n"
        "1. directly test the targeted gap named below, AND\n"
        "2. are NOT already covered by any existing eval — compare BOTH the prompt AND the "
        "rationale of each existing eval, because different wording can still test the same "
        "rule (e.g. an existing 'empty log → total_errors=0' eval already covers that edge).\n"
        "Every assertion MUST verify the targeted gap. If a candidate eval is really about a "
        "different rule (categorization, output format, fabrication guard, …) that the existing "
        "evals already cover, DROP it — do not pad the suite with off-topic cases.\n"
        "Keep it tight: 2-5 high-quality gap-filling evals, not a fresh full set.\n\n"
        f"{focus_line}"
        f"**Existing evals already cover (DO NOT duplicate):**\n{existing_block}\n\n"
    )


def build_llm_prompt(
    source: PromptSource,
    is_procedural: bool = False,
    focus: str | None = None,
    existing_evals: list[EvalCase] | None = None,
) -> str:
    name = source.name
    description = source.description
    content = source.content
    if is_procedural:
        prompt = build_procedural_prompt(name, description, content)
    else:
        prompt = build_knowledge_prompt(name, description, content)
    directive = supplement_directive(focus, existing_evals)
    if directive:
        # 注在 ## Output JSON 前(两个 builder 都有该锚点),改变这次生成目标
        prompt = prompt.replace("## Output JSON", directive + "## Output JSON", 1)
    return prompt


def build_knowledge_prompt(name: str, description: str, content: str) -> str:
    return _KNOWLEDGE_PROMPT_TEMPLATE.format(name=name, description=description, content=content[:10000])


def build_procedural_prompt(name: str, description: str, content: str) -> str:
    return _PROCEDURAL_PROMPT_TEMPLATE.format(name=name, description=description, content=content[:8000])


def collect_unknown_field_warnings(data: dict) -> list[str]:
    """raw LLM 输出含 EvalCase 不认的字段 → 收集警告字符串列表。

    EvalCase 走 pydantic 默认 extra='ignore',未知字段(如把 ``expected_output``
    拼成 ``expected``、``assertions`` 拼成 ``assertion``)会被**静默丢弃** ——
    init 看着生成成功、考卷实则缺字段,eval 时才暴雷。本检查在 ``model_validate``
    之前扫 raw dict,把可疑字段亮出来供 init 审阅(warn 不拒;拒会掉确定性
    fallback 更差,同 ``_flag_missing_rationale`` 哲理)。已知字段集取自
    ``EvalCase.model_fields``,自动随 model 演化,无需手维护。
    """
    known = set(EvalCase.model_fields.keys())
    warns: list[str] = []
    raw_evals = data.get("evals") if isinstance(data, dict) else None
    if not isinstance(raw_evals, list):
        return warns
    for i, raw in enumerate(raw_evals):
        if not isinstance(raw, dict):
            continue
        unknown = sorted(set(raw.keys()) - known)
        if not (unknown):
            continue
        ident = raw.get("id", f"第{i + 1}条")
        warns.append(f"eval {ident}: 未知字段 {unknown}(已忽略;可能拼错)")
    return warns


def merge_same_task_evals(eval_set: EvalSet) -> EvalSet:
    """合并 prompt 相同的 eval → 一题多断言(去冗余、省 spawn)。

    单 transcript 挖掘常产出多个同 prompt 的 eval(同一任务的多条评分细则:认某错误码、
    schema 完整、反编造…),各自 spawn 一次 cc 冗余、A/B 时是高度相关 trial。按 prompt
    分组合并:assertions / expectations / fixtures 取并集去重、expected_output 拼成 rubric、
    level 取有 fixture→L2。无同 prompt 则原样返回(仅重排 id)。flag 标 ``merged:N-evals``
    (不触发改降级警告,只认 ``degraded:`` 前缀)。

    合并前过两道确定性一致性门,任一冲突 → 不合并(各 eval 原样独立保留、仅重排 id,
    flag ``split:N-evals-conflict``):(1) fixture 冲突:同 path 出现 ≥2 不同 content
    (不同 scenario,如成功/失败两段 transcript);(2) 断言冲突:CONTAINS 字面量命中某
    NOT_CONTAINS 正则,或同 (file,key) 钉成不同常量。模糊/语义断言不参与(证不了,
    保守放过)——只合并能确定一致的组(见 ``_merge_fixtures_conflict`` / ``_merge_assertions_conflict``)。
    """
    evals = eval_set.evals
    if len(evals) <= 1:
        return eval_set
    groups: dict[str, list[EvalCase]] = {}
    order: list[str] = []
    for e in evals:
        key = (e.prompt or "").strip()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    if len(groups) == len(evals):
        # 无同 prompt:不合并,仅按原序重排 id
        return EvalSet(
            skill_name=eval_set.skill_name, evals=[e.model_copy(update={"id": i}) for i, e in enumerate(evals, 1)]
        )

    merged: list[EvalCase] = []
    next_id = 1
    for key in order:
        group = groups[key]
        first = group[0]
        if len(group) == 1:
            merged.append(first.model_copy(update={"id": next_id}))
            next_id += 1
            continue
        if _merge_fixtures_conflict(group) or _merge_assertions_conflict(group):
            # 确定性冲突 → 不合并:各 eval 原样独立保留(仅重排 id),flag 留痕
            split_flag = f"split:{len(group)}-evals-conflict"
            for e in group:
                flags = list(dict.fromkeys([*(e.flags or []), split_flag]))
                merged.append(e.model_copy(update={"id": next_id, "flags": flags}))
                next_id += 1
            continue
        merged.append(_merge_compatible_evals(group, next_id))
        next_id += 1
    return EvalSet(skill_name=eval_set.skill_name, evals=merged)


def extract_skill_outputs(body: str) -> set[str]:
    """确定性抽取 SKILL.md 声明的【产物】文件名 —— 不含输入/参考资料。

    mining 的客观断言受 ``_enforce_manifest`` closed-set 约束,manifest 默认只含
    transcript 真产出的文件;违规/不完整 transcript 没产出 skill 声明的产物时,连
    skill 明明要求的 ``FILE_EXISTS`` 也会被 drop(原则性断言本该 transcript 无关、
    源自 skill)。把 skill 声明的产物并入 manifest 即正本清源(方向 B)。

    两条高精信号,故不抓输入/参考资料里的文件名:
    (a) 输出语义小节(``## 输出`` / Output / 产物 / 交付物 …)内的所有文件名;
    (b) 步骤小节内、由写/产出动词(写出/生成/创建/保存 …)锚定的行上的文件名。
    """
    outputs: set[str] = set()
    for title, text in split_into_sections(body):
        names = {m.group(0) for m in _SKILL_FILENAME_RE.finditer(text)}
        if not names:
            continue
        low = title.lower()
        if _SKILL_OUTPUT_SECTION_RE.search(low):
            outputs |= names
        elif _SKILL_STEP_SECTION_RE.search(low):
            outputs |= _written_skill_outputs(text)
    return outputs


def build_transcript_prompt(
    source: PromptSource,
    digest,
    skill_outputs: set[str] | None = None,
    kind: str | None = None,
    *,
    focus: str | None = None,
    existing_evals: list[EvalCase] | None = None,
) -> str:
    name = source.name
    description = source.description
    body = source.content
    frontmatter = source.frontmatter
    skill_outputs = skill_outputs or set()
    is_agent = kind == "agent"
    lines: list[str] = []
    lines.extend(_transcript_prompt_intro(is_agent, frontmatter))
    lines.extend(_transcript_evidence_lines(digest, skill_outputs))
    lines.append((_TRANSCRIPT_PROMPT_TEXT_1))
    lines.append(
        (
            "\n"
            "## trap-A WARNING: transcript 是一次执行,not ground "
            "truth。别把它的错/绕路冻成期望;推导一般可验证的期望;不确定就略。\n"
        )
    )
    rationale_hint = (
        "对应 agent 正文哪条职责 或 transcript 哪个真实行为"
        if is_agent
        else "对应 SKILL.md 哪段/哪条 或 transcript 哪个行为"
    )
    lines.append((_TRANSCRIPT_PROMPT_TEXT_3) + rationale_hint + '>","confidence":0.0-1.0}]}\n')
    if is_agent:
        lines.append(
            (
                "生成 5-8 条 eval。语言同 transcript。匹配 description 声明的角色(它说管 X "
                "就出 X 的题,不出它不该管的)。\n"
            )
        )
        lines.append((_TRANSCRIPT_PROMPT_TEXT_2))
    else:
        lines.append("生成 5-8 条 eval(L1+L2 混)。语言同 transcript。\n")
    lines.append((f"每条 eval 必须带 rationale({rationale_hint})与 confidence(0-1,审阅分诊用)。\n"))
    lines.append(
        (
            "expectations 一律写完整句子(有动词),包括格式/总评类(行号、严重度、总评结论)"
            '——别压成名词堆标签。BAD:"总评说明阻塞项与能否合并";GOOD:"总评里说清有哪些阻'
            '塞项、这个 PR 能不能合"。\n'
        )
    )
    directive = supplement_directive(focus, existing_evals)
    if directive:
        lines.append("\n" + directive)
    seg_head = "## Agent system prompt" if is_agent else "## SKILL"
    lines.append((f"\n{seg_head}\nname: {name}\ndescription: {description}\n\n{body[:6000]}\n"))
    return "".join(lines)


class ParsedEvalResponse(NamedTuple):
    """考卷解析值及供调用方展示的诊断信息。"""

    eval_set: EvalSet | None
    warnings: list[str]


def parse_eval_response(response: str) -> ParsedEvalResponse:
    """解析模型给出的考卷，同时返回未知字段警告。"""
    warnings: list[str] = []
    try:
        data = json.loads(response)
        if "evals" in data:
            warnings.extend(collect_unknown_field_warnings(data))
            return ParsedEvalResponse(EvalSet.model_validate(data), warnings)
    except Exception:
        logging.getLogger(__name__).debug(("Optional operation failed; continuing with fallback"), exc_info=True)

    match = re.search(r"\{.*\}", response, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if "evals" in data:
                warnings.extend(collect_unknown_field_warnings(data))
                return ParsedEvalResponse(EvalSet.model_validate(data), warnings)
        except Exception:
            logging.getLogger(__name__).debug(("Optional operation failed; continuing with fallback"), exc_info=True)

    return ParsedEvalResponse(None, warnings)


def _merge_regex_matches(pattern, text):
    try:
        return bool(re.search(pattern, text))
    except re.error:
        logging.getLogger(__name__).debug("Optional operation failed; continuing with fallback", exc_info=True)
        return False
