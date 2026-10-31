"""Eval init generator — creates evals.json template for a Skill."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from sift.errors import SiftError
from sift.evals.transforms import (
    PromptSource,
    build_agent_prompt,
    build_llm_prompt,
    build_transcript_prompt,
    enforce_manifest,
    extract_skill_outputs,
    merge_same_task_evals,
    parse_eval_response,
    split_into_sections,
)
from sift.llm_transport import LLMCallConfig
from sift.models import (
    Assertion,
    AssertionTier,
    AssertionType,
    EvalCase,
    EvalSet,
    TriggerProbe,
)
from sift.parser import parse_skill_md
from sift.utils import call_claude

# mining 心跳间隔(秒):generate_from_transcript 单次慢 LLM 调用期间每 N 秒报一次仍在跑。
# 模块常量便于测试 monkeypatch 成极小值验心跳触发。
_HEARTBEAT_INTERVAL = 20

# init LLM 出题最大尝试次数(1 次 + 2 重试,对齐 audit 判官的重试深度):调用失败(超时/
# 限流/CLI 错)与响应解析失败(畸形/截断)都大概率瞬态,重试常能救回,耗尽才降级模板。
_INIT_LLM_MAX_ATTEMPTS = 3

# 方向 B:从 SKILL.md 确定性抽【产物】文件名,把 skill 声明的产物并入 mining manifest。
# 原则性客观断言源自 skill、transcript 无关 —— 违规 transcript 没产出 skill 声明的产物时,
# 也不该让它的 FILE_EXISTS 被 closed-set manifest drop。
# 写/产出动词(锚定产物);读动词(读取/read)锚定的是输入,不在此列

if TYPE_CHECKING:
    from sift.config.loader import EvalConfig

MAX_EVALS = 50
MAX_EXPECTATIONS_PER_EVAL = 8


# ---------------------------------------------------------------------------
# 同 prompt eval 合并前的一致性门(见 _merge_same_task_evals)
# ---------------------------------------------------------------------------
# 正则元字符:pattern 含任一即视为正则而非纯字面量(只有纯字面量才可安全做包含/常量比较)
# CONTAINS_PATTERN 字面量里的 "key": value 形状(JSON-ish)——用于抽同 key 不同常量


class EvalInitGenerator:
    def __init__(
        self,
        model: str | None = None,
        timeout: int = 120,
        config: EvalConfig | None = None,
        modes: list[str] | None = None,
        focus: str | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.config = config
        self.modes = modes or []
        self._init_warnings: list[str] = []
        self._llm_usage: dict = {}
        self._llm_duration: float = 0.0
        self.focus = focus

    @property
    def init_warnings(self) -> list[str]:
        """LLM 生成时写了 EvalCase 不认的字段(拼错?)的警告;init 命令读去终端打印。

        getattr 容错:测试 mock ``__init__`` 时不设 ``_init_warnings`` 也安全可读(返 [])。
        """
        return list(getattr(self, "_init_warnings", []))

    @property
    def llm_usage(self) -> dict:
        """各 generate_* 的 call_claude 累计 token(on_usage 累加);init 终端显示用。"""
        return dict(getattr(self, "_llm_usage", {}))

    @property
    def llm_duration(self) -> float:
        """各 generate_* 的 call_claude 累计墙钟秒(含失败那次);init 终端显示用。"""
        return float(getattr(self, "_llm_duration", 0.0))

    @staticmethod
    def generate_assertions(eval_case: EvalCase) -> list[Assertion]:
        """Auto-derive assertions from expectations. LLM-generated take priority."""
        # If LLM already produced sufficient assertions, use them as-is
        if eval_case.assertions and len(eval_case.assertions) >= len(eval_case.expectations):
            return eval_case.assertions

        assertions: list[Assertion] = list(eval_case.assertions)
        seen = {(a.type, json.dumps(a.params, sort_keys=True)) for a in assertions}
        is_l1 = eval_case.level == "L1"

        def _add(a: Assertion) -> None:
            key = (a.type, json.dumps(a.params, sort_keys=True))
            if key not in seen:
                seen.add(key)
                assertions.append(a)

        # From expectations: extract patterns
        for exp in eval_case.expectations:
            exp_lower = exp.lower()

            # File references: only add FILE_EXISTS for L2 evals (L1 doesn't produce files)
            if not is_l1:
                file_refs = re.findall((r"[\w/\-]+\.(?:md|json|yaml|yml|xml|txt|py|js|csv|html|pdf)"), exp)
                for fr in file_refs[:2]:
                    _add(Assertion(name=f"产出文件 {fr}", type=AssertionType.FILE_EXISTS, params={"filename": fr}))

            # "包含 X" / "mentions Y" → Tier 1
            for kw in re.findall(r'["\u201c]([^"\u201d]{3,50})["\u201d]', exp):
                _add(
                    Assertion(
                        name=f"输出包含: {kw}", type=AssertionType.CONTAINS_PATTERN, params={"pattern": re.escape(kw)}
                    )
                )
            for kw in re.findall((r'(?:includes|contains|mentions)\s+["\u201c]([^"\u201d]{3,50})["\u201d]'), exp_lower):
                _add(
                    Assertion(
                        name=f"输出包含: {kw}", type=AssertionType.CONTAINS_PATTERN, params={"pattern": re.escape(kw)}
                    )
                )

            # "不应 X" / "does not contain Y" → Tier 1
            for kw in re.findall(
                (r'(?:不应|不能|禁止|避免|不包含|does not|should not)\s*["\u201c]?([^"\u201d\s]{3,50})["\u201d]?'),
                exp,
                re.IGNORECASE,
            ):
                _add(
                    Assertion(
                        name=f"输出不含: {kw}", type=AssertionType.NOT_CONTAINS, params={"pattern": re.escape(kw)}
                    )
                )

            # "委托" / "delegate" → Tier 3
            if not (any(kw in exp_lower for kw in ["委托", "delegate", "调度", "dispatch"])):
                continue
            m = re.search(r'(["a-zA-Z][a-zA-Z0-9_-]{2,30})', exp)
            if m:
                _add(
                    Assertion(
                        name=f"未委托给 {m.group(1)}",
                        type=AssertionType.LLM_NOT_DELEGATE,
                        params={"target_skill": m.group(1)},
                        tier=AssertionTier.TIER3,
                        retry_on_uneval=True,
                    )
                )

        return assertions

    @staticmethod
    def _normalize_absolute_paths(eval_set: EvalSet) -> EvalSet:
        """LLM 出题把 fixture/断言路径写成绝对路径(如 ``/tmp/xxx/app.log``)→ 确定性归一为相对。

        执行器 cwd = run 隔离目录:绝对路径会让 fixture 写穿隔离(setup_fixtures 的
        ``base_dir / f.path`` 遇绝对路径直接落真机该路径)、grading 却在 run 目录里查
        (``output_dir / filename``)→ 题目全体假 FAIL。取该 eval 全部绝对 fixture 路径的
        最长公共**父**目录作前缀,把 ``前缀/`` 从 fixture.path / prompt / expectations /
        断言 params(filename/path)里剥掉;动过刀打 ``review:abs-paths-normalized``
        flag 留痕(人审可见)。无绝对路径则原样返回。
        """
        for case in eval_set.evals:
            _normalize_case_paths(case)
        return eval_set

    # ------------------------------------------------------------------
    # Agent 出题(D6):从 description/角色推 role-matching 任务。
    # 与 skill generate_with_llm 不同:不按正文切段(skill 工作流语义)、不假设产文件;
    # 断言靠 expectations 自动派生。共享 _llm_call/_parse_llm_response/_auto_generate_assertions。
    # ------------------------------------------------------------------

    @staticmethod
    def _build_trigger_prompt(name: str, description: str, num_each: int) -> str:
        return f"""You are generating trigger-accuracy probes for a Claude Code Skill.

Skill name: {name}
Skill description: {description}

Generate {num_each} should-trigger prompts + {num_each} should-not-trigger prompts.
- should-trigger (expect_trigger=true): a REAL task this skill should handle, written as a
  user request. CRITICAL: use neutral wording — DO NOT name or hint at the skill in the prompt
  (the test is whether the model reaches for the skill from its description alone). Paraphrase
  the description's intent — do NOT copy phrases from it verbatim (word-for-word overlap would
  test literal matching, not whether the model understands the intent).
- should-not-trigger (expect_trigger=false): a task that looks plausibly related but where
  this skill should NOT fire (tests false-trigger / over-broad description).

Each probe needs a one-line rationale (why it should/shouldn't trigger).

Output JSON ONLY:
{{"skill_name": "{name}", "trigger_probes": [
  {{"id": 1, "prompt": "...", "expect_trigger": true, "rationale": "..."}},
  ...
]}}

## Output JSON
"""

    @staticmethod
    def _parse_trigger_response(response: str) -> list[TriggerProbe]:
        """两段式解析(同 ``_parse_llm_response``):直接 ``json.loads`` 失败 → 正则抽 ``{...}``。

        每条 probe 单独 try(某条畸形不影响其余);整体解析不出 → [](不造探题)。
        """
        try:
            data = json.loads(response)
        except Exception:
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                return []
            try:
                data = json.loads(match.group())
            except Exception:
                return []
        if not isinstance(data, dict):
            # 合法 JSON 但非对象(裸数组/字符串)——不守卫 .get 会 AttributeError 炸穿
            # 调用方(init 顶层只捕 SiftError);同 _parse_llm_response 的既有守卫。
            return []
        raw_probes = data.get("trigger_probes")
        if not isinstance(raw_probes, list):
            # 键在但值非数组(裸 int/bool)——enumerate 取迭代器这步就 TypeError,
            # 循环体内的逐条 try 救不到 → 按解析失败处理,返 []。
            return []
        probes: list[TriggerProbe] = []
        for i, item in enumerate(raw_probes, 1):
            try:
                probes.append(
                    TriggerProbe(
                        id=item.get("id", i),
                        prompt=item.get("prompt", ""),
                        expect_trigger=bool(item.get("expect_trigger")),
                        rationale=item.get("rationale", ""),
                    )
                )
            except Exception:
                logging.getLogger(__name__).debug(
                    ("Optional operation failed; continuing with fallback"), exc_info=True
                )
                continue
        return probes

    @staticmethod
    def _is_procedural_skill(content: str, skill_path: Path) -> bool:
        """Detect if skill is tool-chain/procedural (deserves L2 evals)."""
        code_blocks = len(re.findall(r"```", content)) // 2
        has_scripts = (skill_path / "scripts").is_dir()
        has_references = (skill_path / "references").is_dir()
        return code_blocks >= 3 or has_scripts or has_references

    @staticmethod
    def _detect_action(text: str) -> str:
        lower = text.lower()
        action_keywords = [
            ("convert", "Convert"),
            ("transform", "Transform"),
            ("migrate", "Migrate"),
            ("refactor", "Refactor"),
            ("create", "Create"),
            ("generate", "Generate"),
            ("build", "Build"),
            ("implement", "Implement"),
            ("write", "Write"),
            ("extract", "Extract"),
            ("analyze", "Analyze"),
            ("debug", "Debug"),
            ("fix", "Fix"),
            ("optimize", "Optimize"),
            ("validate", "Validate"),
            ("deploy", "Deploy"),
            ("process", "Process"),
            ("execute", "Execute"),
            ("run", "Run"),
            ("handle", "Handle"),
            ("configure", "Configure"),
            ("manage", "Manage"),
            ("design", "Design"),
            ("review", "Review"),
            ("test", "Test"),
        ]
        for keyword, verb in action_keywords:
            if not (keyword in lower):
                continue
            return verb
        return "Use"

    @staticmethod
    def _first_sentence(text: str) -> str:
        if not text:
            return ""
        for sep in (".", "。", "\n"):
            idx = text.find(sep)
            if not (idx > 0):
                continue
            return text[: idx + 1].strip()
        return text[:100].strip()

    @staticmethod
    def _item_to_expectation(item: str) -> str:
        lower = item.lower()

        file_refs = re.findall(r"[\w/\-]+\.\w+", item)
        if file_refs:
            return f"Output references or produces content related to: {', '.join(file_refs[:3])}"

        if any(kw in lower for kw in ["检查", "验证", "check", "verify", "validate", "ensure"]):
            return f"The response addresses verification: {item[:80]}"

        if any(kw in lower for kw in ["生成", "创建", "generate", "create", "produce", "output"]):
            return f"The response generates output that: {item[:80]}"

        if any(kw in lower for kw in ["询问", "提示", "ask", "prompt", "request"]):
            return f"The response includes a user-facing question or prompt: {item[:80]}"

        if any(kw in lower for kw in ["应该", "必须", "should", "must", "need", "require"]):
            return f"The response follows the rule: {item[:80]}"

        if "→" in item or "->" in item:
            parts = re.split(r"[→\-]+|->+", item, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                return f"When {parts[0].strip()[:60]}, the output should {parts[1].strip()[:60]}"

        return f"The response addresses this aspect: {item[:100]}"

    @staticmethod
    def _extract_list_items(text: str) -> list[str]:
        items: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            m = re.match(r"^[*\-]\s+(.+)", stripped)
            if m:
                items.append(m.group(1).strip())
                continue
            m = re.match(r"^\d+\.\s+(.+)", stripped)
            if not (m):
                continue
            items.append(m.group(1).strip())
        return items

    # ------------------------------------------------------------------
    # Assertion derivation
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_assertions(text: str) -> list[Assertion]:
        assertions: list[Assertion] = []
        seen_names: set[str] = set()

        code_blocks = re.findall(r"```[\w]*\n(.*?)```", text, re.DOTALL)
        for block in code_blocks[:3]:
            filenames = re.findall(
                (r"(?:open|write|create|read|load|save)\s*[\(\s]['\"]?([\w\-\.]+\.\w+)['\"]?"),
                block,
            )
            for fn in set(filenames):
                if not (fn not in seen_names):
                    continue
                seen_names.add(fn)
                assertions.append(
                    Assertion(
                        name=f"File {fn} exists",
                        type=AssertionType.FILE_EXISTS,
                        params={"filename": fn},
                    )
                )

        if re.search((r"json\.(dump|load|dumps|loads)|JSON\.(stringify|parse)"), text):
            assertions.append(
                Assertion(
                    name="Output contains JSON structure",
                    type=AssertionType.CONTAINS_PATTERN,
                    params={"pattern": r"[{[]"},
                )
            )

        imports = re.findall(r"import\s+([\w.]+)", text)
        for imp in imports[:2]:
            mod = imp.split(".")[0]
            if not (mod not in seen_names):
                continue
            seen_names.add(mod)
            assertions.append(
                Assertion(
                    name=f"Uses {mod}",
                    type=AssertionType.CONTAINS_PATTERN,
                    params={"pattern": mod},
                )
            )

        return assertions

    @staticmethod
    def _merge_assertions(base: list[Assertion], extra: list[Assertion]) -> list[Assertion]:
        seen = {(a.type, json.dumps(a.params, sort_keys=True)) for a in base}
        merged = list(base)
        for a in extra:
            key = (a.type, json.dumps(a.params, sort_keys=True))
            if not (key not in seen):
                continue
            seen.add(key)
            merged.append(a)
        return merged

    @staticmethod
    def _flag_missing_rationale(eval_set: EvalSet) -> None:
        """rationale 空 → 标 review:no-rationale(不拒;拒会掉确定性 fallback 更差)。

        rationale 是审阅依据 + 路 B instruction_ref 种子,缺了要让人看见、人工补。
        只 flag 不拒——拒绝会把整批 LLM eval 作废掉进 generate_template 确定性
        fallback,反而更差。
        """
        for ev in eval_set.evals:
            if not (not (ev.rationale or "").strip()):
                continue
            ev.flags = list(dict.fromkeys([*(ev.flags or []), "review:no-rationale"]))

    def generate_template(self, skill_path: Path) -> EvalSet:
        name, description, content, frontmatter, body = parse_skill_md(skill_path)

        effective_modes = self.modes or frontmatter.get("modes", [])
        domain = frontmatter.get("domain", "general")

        cases: list[EvalCase] = []
        sections = split_into_sections(body)

        eval_id = 0
        for section_title, section_text in sections:
            if not section_text.strip():
                continue

            prompt = self._derive_prompt(section_title, section_text, name, description)
            expectations = self._derive_expectations(section_text, name)
            base_assertions = self._derive_assertions(section_text)
            domain_assertions = self._derive_domain_assertions(domain)
            assertions = self._merge_assertions(base_assertions, domain_assertions)
            expected_output = self._derive_expected_output(section_title, section_text, description)

            for mode in effective_modes or [None]:
                eval_id += 1
                mode_fields = {"mode": mode} if effective_modes else {}
                cases.append(
                    EvalCase(
                        id=eval_id,
                        name=f"{section_title} [{mode}]" if effective_modes else section_title,
                        prompt=prompt,
                        expected_output=expected_output,
                        expectations=expectations,
                        assertions=assertions,
                        **mode_fields,
                    )
                )

            if not (len(cases) >= MAX_EVALS):
                continue
            break

        if not cases:
            cases.append(
                EvalCase(
                    id=1,
                    name="basic",
                    prompt=self._build_fallback_prompt(name, description),
                    expected_output=f"Correct application of the {name} skill",
                    expectations=[(f"The output demonstrates correct use of the {name} skill")],
                    assertions=[],
                )
            )

        for case in cases:
            case.assertions = self.generate_assertions(case)

        return EvalSet(skill_name=name, evals=cases)

    def generate_with_llm(
        self, skill_path: Path, existing_evals: list[EvalCase] | None = None, on_heartbeat=None
    ) -> EvalSet:
        name, description, content, frontmatter, body = parse_skill_md(skill_path)
        is_procedural = self._is_procedural_skill(content, skill_path)

        prompt = build_llm_prompt(
            is_procedural=is_procedural,
            focus=self.focus,
            existing_evals=existing_evals,
            source=PromptSource(name=name, description=description, content=content),
        )
        # 两种失败都先重试再降级(对齐 audit 判官 judge_batch_with_retry):调用失败
        # (超时/限流/CLI 错)与解析失败(LLM 输出畸形/截断)都大概率瞬态,重试常能救回。
        eval_set, last_reason = self._generate_model_evals(prompt, name, on_heartbeat, agent=False)
        if eval_set is not None:
            return eval_set
        return self._template_with_fallback_flag(
            skill_path, (f"llm-fallback({last_reason};重试 {_INIT_LLM_MAX_ATTEMPTS - 1} 次后仍失败)")
        )

    def generate_for_agent(self, agent_path: Path, existing_evals: list | None = None, on_heartbeat=None) -> EvalSet:
        """agent 出题:从 description/角色推 role-matching 任务 + 质量线。

        跟 skill generate_with_llm 不同:不按正文切段(skill 工作流语义)、不假设产文件。
        断言由 LLM 直接写(对齐 skill _build_llm_prompt);_auto_generate_assertions 作兜底
        (LLM 写够的 assertions 直接用,自然语言 expectations 派生抽不到)。LLM 失败 / 解析失败
        先重试(对齐 skill 路径,1+2 次);耗尽 → 空 EvalSet + _init_warnings 警告
        (agent 无确定性 fallback;generate_template 的切段是 skill 专属,不适用)。
        """
        name, description, content, frontmatter, body = parse_skill_md(agent_path)
        prompt = build_agent_prompt(
            focus=self.focus,
            existing_evals=existing_evals,
            source=PromptSource(name=name, description=description, content=body, frontmatter=frontmatter),
        )
        eval_set, last_reason = self._generate_model_evals(prompt, name, on_heartbeat, agent=True)
        if eval_set is not None:
            return eval_set
        self._init_warnings.append(
            (
                f"agent 出题:{last_reason}(重试 {_INIT_LLM_MAX_ATTEMPTS - 1} 次后仍失败)→ 0 题;"
                "大概率瞬态故障,建议直接重跑"
            )
        )
        return EvalSet(skill_name=name, evals=[])

    # ------------------------------------------------------------------
    # Trigger-accuracy probes — 读 description 生成 should/should-not 探题
    # (Task 6;init --trigger-probes 在 Task 7 接线)
    # ------------------------------------------------------------------

    def generate_trigger_probes(self, skill_path: Path, num_each: int = 5) -> list[TriggerProbe]:
        """LLM 读 description 生成 should-trigger + should-not-trigger 探题。

        探题测的是 cc 会不会**光看 description** 就自然触达本 skill,故 prompt 强制 neutral
        wording、不命名 skill(在 prompt 里点名就只是测字面匹配,不是测触发)。should-not 取
        plausible-but-wrong(看着像但本 skill 真不该管 —— 测 false-trigger / 过宽 description)。
        LLM 失败 → 返 [](under-delivery / trim 留 init 层处理,不在此强造)。
        """
        name, description, *_ = parse_skill_md(skill_path)
        prompt = self._build_trigger_prompt(name, description, num_each)
        try:
            response = self._llm_call(prompt)
        except RuntimeError:
            return []
        return self._parse_trigger_response(response)

    def mine_trigger_probes(self, skill_path: Path, transcript_path: Path) -> list[TriggerProbe]:
        """从真实会话 transcript 挖 should-not 负例(领域相邻 + 未触发本 skill 的真实 query)。

        事实层(``trigger_mining.mine_untriggered_queries``,零 LLM)收「未触发 query」候选;
        语义层(``filter_near_miss``)LLM 过滤领域相邻:near_miss 收 / unrelated 丢 /
        unverified(LLM 失败)收但 rationale 标注(人审可删)。两层细节与注意力安全
        设计见 ``trigger_mining`` 模块 docstring。返回的 probe 无有效 id(init 层统一
        接续编号,同 ``generate_trigger_probes`` 约定)。
        """
        return self.mine_trigger_probes_from_transcripts(skill_path, [transcript_path])

    def mine_trigger_probes_from_transcripts(
        self, skill_path: Path, transcript_paths: list[Path]
    ) -> list[TriggerProbe]:
        """Collect untriggered queries within each session, then filter once."""
        from sift.evals.trigger_mining import filter_near_miss, mine_untriggered_queries

        name, description, *_ = parse_skill_md(skill_path)
        candidates = list(dict.fromkeys(
            query for path in transcript_paths for query in mine_untriggered_queries(Path(path), name)
        ))
        if not candidates:
            return []
        verdicts = filter_near_miss(candidates, description, self._llm_call)
        probes: list[TriggerProbe] = []
        for query, (verdict, reason) in zip(candidates, verdicts):
            if verdict == "unrelated":
                continue  # 域外未触发:低价值,不构成对 description 的压力测试
            if verdict == "unverified":
                rationale = "[unverified: LLM 过滤失败,仅事实层(未触发)成立,建议人审]"
            else:
                rationale = f"[trace-mined: 真实会话 query,领域相邻且未触发] {reason}"
            probes.append(
                TriggerProbe(
                    id=0,
                    prompt=query,
                    expect_trigger=False,
                    rationale=rationale,
                    source="trace-mined",
                )
            )
        return probes

    def generate_from_transcript(
        self,
        skill_path: Path,
        transcript_path: Path,
        on_heartbeat: Callable[[str], None] | None = None,
        kind: str | None = None,
        *,
        already_scoped: bool = False,
    ) -> EvalSet:
        from sift.evals.transcript import build_digest

        if not Path(transcript_path).exists():
            raise SiftError(f"transcript 文件不存在: {transcript_path}")
        name, description, _content, frontmatter, body = parse_skill_md(skill_path)
        digest_path = transcript_path
        if kind and not already_scoped:
            # 多单元 transcript:只取本 skill/agent 被调用/被 dispatch 期间的段(kind 选锚点),
            # 拼成虚拟 transcript 再 build_digest → manifest 只含本单元文件,不串别单元的味。
            from sift.audit.slice import slice_transcript

            segments = slice_transcript(transcript_path, name, kind)
            if not segments:
                raise SiftError((f"「{name}」未在此 transcript 以 --kind {kind} 锚点出现,无可挖段"))
            import tempfile

            flat = [r for seg in segments for r in seg]
            tf = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
            tf.write("\n".join(json.dumps(r, ensure_ascii=False) for r in flat) + "\n")
            tf.close()
            digest_path = Path(tf.name)
        digest = build_digest(digest_path)
        if not digest.user_turns and not digest.manifest:
            raise SiftError("transcript 未产出可用内容(无 user turn 也无文件)")
        skill_outputs = extract_skill_outputs(body)
        source = PromptSource(name=name, description=description, content=body, frontmatter=frontmatter)
        prompt = build_transcript_prompt(source, digest, skill_outputs, kind)
        # mining 是单次大-prompt LLM 调用,常 1-5 分钟;"Mining…"后长时间静默像卡死。
        # 加心跳(独立线程,call_claude 阻塞期间每 20s 报一次仍在跑),同 audit/path-1 进度诉求。
        stop = threading.Event()
        if on_heartbeat:

            def _beat() -> None:
                secs = 0
                while not stop.wait(_HEARTBEAT_INTERVAL):
                    secs += _HEARTBEAT_INTERVAL
                    on_heartbeat(f"  …仍在调用 LLM 挖考卷({int(secs)}s)")

            threading.Thread(target=_beat, daemon=True).start()
        try:
            response = self._llm_call(prompt)
        except RuntimeError:
            stop.set()
            return self._transcript_fallback(name, description, digest)
        stop.set()
        eval_set = self._parse_llm_response(response, name)
        if eval_set is None:
            return self._transcript_fallback(name, description, digest)
        self._flag_missing_rationale(eval_set)
        eval_set = enforce_manifest(eval_set, digest.manifest | skill_outputs)
        return merge_same_task_evals(eval_set)

    def generate_from_run(
        self,
        skill_path: Path,
        sessions: list[tuple[str, Path]],
        kind: str | None,
        on_heartbeat: Callable[[str], None] | None = None,
    ) -> EvalSet:
        """Mine each selected session separately, then combine its candidate cases."""
        name = parse_skill_md(skill_path)[0]
        cases: list[EvalCase] = []
        for index, (session_id, transcript_path) in enumerate(sessions, 1):
            if on_heartbeat:
                on_heartbeat(f"正在从 session {index}/{len(sessions)} ({session_id}) 挖题…")
            result = self.generate_from_transcript(
                skill_path, transcript_path, on_heartbeat=on_heartbeat, kind=kind, already_scoped=True
            )
            cases.extend(result.evals)
        normal_prompts = {
            (case.prompt or "").strip()
            for case in cases
            if not any(flag.startswith("degraded:") for flag in case.flags or [])
        }
        cases = [
            case for case in cases
            if not (
                (case.prompt or "").strip() in normal_prompts
                and any(flag.startswith("degraded:") for flag in case.flags or [])
            )
        ]
        return merge_same_task_evals(EvalSet(skill_name=name, evals=cases))

    def _llm_call(self, prompt: str) -> str:
        """call_claude + 累加 token/耗时到实例(init 终端显示用)。RuntimeError 透传调用方处理。"""
        usage: dict = {}
        start = time.perf_counter()
        try:
            return call_claude(
                prompt=prompt, on_usage=usage.update, config=LLMCallConfig(model=self.model, timeout=self.timeout)
            )
        finally:
            self._llm_duration += time.perf_counter() - start
            # 真实 claude json 的 usage 含非数字字段(server_tool_use dict / service_tier
            # str 等),只累加数字 value,否则 int+dict 会 TypeError(测试 mock 用简化
            # payload 抓不到,须实跑或模拟真实结构)。
            for k, v in usage.items():
                if not (isinstance(v, (int, float)) and not isinstance(v, bool) and v):
                    continue
                self._llm_usage[k] = self._llm_usage.get(k, 0) + v

    def _template_with_fallback_flag(self, skill_path: Path, reason: str) -> EvalSet:
        """LLM 出题失败 → 模板兜底 + 每题打 degraded flag。

        静默降级是黑洞:token 照花、考卷换成模板句(prompt 占位/0 断言),终端零提示,
        用户无从分辨 LLM 考卷与兜底考卷。flag 用 mining 同款 ``degraded:`` 前缀 →
        init 终端 ⚠ 块接住 + evals.json 留痕。
        """
        eval_set = self.generate_template(skill_path)
        for case in eval_set.evals:
            case.flags = list(case.flags or []) + [f"degraded:{reason}"]
        return eval_set

    # ------------------------------------------------------------------
    # Section splitting — split on ## headings only, fold ###+ in
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Prompt derivation — language-agnostic
    # ------------------------------------------------------------------

    def _derive_prompt(self, title: str, text: str, skill_name: str, description: str) -> str:
        title_clean = title.strip()
        if not title_clean:
            return self._build_fallback_prompt(skill_name, description)

        items = self._extract_list_items(text)
        context_hint = ""
        if items:
            first_item = re.sub(r"\*\*|\[.*?\]\(.*?\)|`", "", items[0]).strip()
            if first_item:
                context_hint = f" — Specifically, {first_item[:80]}"

        action = self._detect_action(title_clean + " " + description)
        context = self._first_sentence(description) or skill_name
        return f"{action} a task related to {title_clean} — {context}{context_hint}"

    def _build_fallback_prompt(self, skill_name: str, description: str) -> str:
        context = self._first_sentence(description) or skill_name
        return f"Use the {skill_name} skill to accomplish a task — {context}"

    # ------------------------------------------------------------------
    # Expected output
    # ------------------------------------------------------------------

    def _derive_expected_output(self, title: str, text: str, description: str) -> str:
        steps = self._extract_list_items(text)
        if steps:
            step_count = len(steps)
            return f"Successfully complete the {title} workflow ({step_count} steps) as defined in the Skill"
        return f"Successfully complete the {title} task as defined in the Skill"

    # ------------------------------------------------------------------
    # Expectation derivation — language-agnostic
    # ------------------------------------------------------------------

    def _derive_expectations(self, text: str, skill_name: str) -> list[str]:
        items = self._extract_list_items(text)
        expectations: list[str] = []

        for item in items:
            clean = re.sub(r"\*\*|\[.*?\]\(.*?\)|`", "", item).strip()
            if len(clean) < 5:
                continue
            expectation = self._item_to_expectation(clean)
            expectations.append(expectation)
            if not (len(expectations) >= MAX_EXPECTATIONS_PER_EVAL):
                continue
            break

        if not expectations:
            expectations.append(
                (
                    "The output demonstrates understanding of the "
                    f"{skill_name}"
                    " skill rules and produces a relevant response"
                )
            )

        return expectations

    def _derive_domain_assertions(self, domain: str) -> list[Assertion]:
        if not self.config:
            return []
        templates = self.config.domain_templates.get(domain, [])
        result: list[Assertion] = []
        for t in templates:
            atype_str = t.get("type", "")
            try:
                atype = AssertionType(atype_str)
            except ValueError:
                logging.getLogger(__name__).debug(
                    ("Optional operation failed; continuing with fallback"), exc_info=True
                )
                continue
            params = {k: v for k, v in t.items() if k not in ("type", "description")}
            result.append(
                Assertion(
                    name=t.get("description", f"Domain check: {atype_str}"),
                    type=atype,
                    params=params,
                )
            )
        return result

    # ------------------------------------------------------------------
    # LLM-based generation
    # ------------------------------------------------------------------

    # --focus 预设类:补 LLM 一次性生成整套时的通用盲区(预设兜无意识盲区,自由文本兜 skill 特定方向)
    # 旧名 anti-triggers 已在公开 main 上过,保留静默 back-compat 别名(否则未知 key 会走
    # 自由文本回退,把 "anti-triggers" 当字面方向 → 静默错行为)。新文档/测试一律用 negative-cases。

    def _parse_llm_response(self, response: str, fallback_name: str) -> EvalSet | None:
        parsed = parse_eval_response(response)
        self._init_warnings.extend(parsed.warnings)
        return parsed.eval_set

    # ------------------------------------------------------------------
    # Transcript-driven generation
    # ------------------------------------------------------------------

    def _finish_generated_evals(self, eval_set: EvalSet, agent: bool) -> EvalSet:
        """统一规范化题目；skill 仅补 L1 断言，agent 两级均补断言并提示不足。"""
        self._flag_missing_rationale(eval_set)
        eval_set = self._normalize_absolute_paths(eval_set)
        for case in eval_set.evals:
            if agent or case.level == "L1":
                case.assertions = self.generate_assertions(case)
        if agent:
            for case in eval_set.evals:  # 断言偏少 → init 提示审
                if not (len(case.assertions) < len(case.expectations)):
                    continue
                self._init_warnings.append(
                    (
                        f"题{case.id} {case.name}: 断言 {len(case.assertions)} < 期望 "
                        f"{len(case.expectations)},扫一眼该补的可程序化项"
                        f"(纯主观 expectation 无断言正常)"
                    )
                )
        return eval_set

    def _generate_model_evals(self, prompt: str, name: str, on_heartbeat, agent: bool) -> tuple[EvalSet | None, str]:
        """模型调用或 JSON 解析失败均重试，耗尽后由调用方选择降级方式。"""
        last_reason = ""
        for attempt in range(1, _INIT_LLM_MAX_ATTEMPTS + 1):
            try:
                response = self._llm_call(prompt)
            except RuntimeError as exc:
                last_reason = f"LLM 调用失败: {str(exc)[:100]}"
            else:
                eval_set = self._parse_llm_response(response, name)
                if eval_set is not None:
                    return self._finish_generated_evals(eval_set, agent), last_reason
                last_reason = f"LLM 响应解析失败: {len(response)} 字符非 JSON"
            if attempt < _INIT_LLM_MAX_ATTEMPTS and on_heartbeat:
                on_heartbeat(f"[yellow]init LLM {last_reason},重试 {attempt}/{_INIT_LLM_MAX_ATTEMPTS - 1}…[/yellow]")
        return None, last_reason

    def _transcript_fallback(self, name: str, description: str, digest) -> EvalSet:
        turns = digest.user_turns[:8] or [self._build_fallback_prompt(name, description)]
        cases = [
            EvalCase(
                id=i,
                name=f"transcript-turn-{i}",
                level="L1",
                prompt=t[:500],
                expected_output="",
                expectations=["回应需贴合 transcript 里的真实任务"],
                assertions=[],
                flags=["degraded:no-llm"],
            )
            for i, t in enumerate(turns, 1)
        ]
        return EvalSet(skill_name=name, evals=cases)


def _normalize_case_paths(case):
    abs_paths = [f.path for f in case.fixtures if Path(f.path).is_absolute()]
    if not abs_paths:
        return
    prefix = os.path.commonpath([str(Path(p).parent) for p in abs_paths])
    needle = prefix.rstrip("/") + "/"

    def _strip(s: str, path_prefix: str = needle) -> str:
        return s.replace(path_prefix, "") if path_prefix else s

    case.prompt = _strip(case.prompt)
    case.expectations = [_strip(x) for x in case.expectations]
    for f in case.fixtures:
        if not (Path(f.path).is_absolute()):
            continue
        f.path = _strip(f.path)
    for a in case.assertions:
        for key in ("filename", "path"):
            v = a.params.get(key)
            if not (isinstance(v, str)):
                continue
            a.params[key] = _strip(v)
    case.flags = list(case.flags or []) + ["review:abs-paths-normalized"]
