"""LLM Grader — semantic evaluation via claude -p."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from sift.models import (
    AssertionVerdict,
    EvalCase,
    ExpectationResult,
    GradingResult,
    GradingSummary,
)
from sift.scoring._jsonschema import inline_schema
from sift.scoring.client import ScoringClient

# 结构化输出 schema:tier3 判官走 http forced tool_use(call_llm_with_retry transport="http",
# schema 作 tool input_schema、inline_schema 展开 $ref)。forced tool_use 的 input 必须是
# object,故顶层 array 包进 items;_extract_json_array 照常命中,正则 fallback 兜底。
_TIER3_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["PASS", "FAIL", "UNEVAL"]},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["index", "verdict", "evidence"],
            },
        }
    },
    "required": ["items"],
}

# grade() 给 LLM 的结构化 schema(forced tool_use 用)。从 GradingResult 派生,剔除
# internal/可选字段——grade() 今天只产 expectations+summary,其余(claims/feedback/
# user_notes_summary/execution_metrics/timing/error/cache_key)恒空,留 schema 里会逼
# 模型填=超出换传输的行为变更。**最后经 inline_schema 把 $ref/$defs 就地展开**:部分
# Anthropic 兼容端点(如 GLM)不解析 tool input_schema 里的 $ref,必填 summary 是 $ref
# 时模型看不到目标形状 → 返回 {} → grade 全 UNEVAL(实测 100%)。inline 后无此患。
_GRADER_FULL = GradingResult.model_json_schema()
_GRADING_INTERNAL = {
    "cache_key",
    "error",
    "execution_metrics",
    "timing",
    "claims",
    "eval_feedback",
    "user_notes_summary",
}
_GRADING_PROPS = {k: v for k, v in _GRADER_FULL["properties"].items() if k not in _GRADING_INTERNAL}
GRADING_SCHEMA: dict = inline_schema(
    {
        "type": "object",
        "properties": _GRADING_PROPS,
        "required": [r for r in _GRADER_FULL.get("required", []) if r in _GRADING_PROPS],
        **({"$defs": _GRADER_FULL["$defs"]} if "$defs" in _GRADER_FULL else {}),
    }
)


class LLMGrader:
    def __init__(self, model: str | None = None, timeout: int = 120):
        self.model = model
        self.timeout = timeout
        self.client = ScoringClient(model, timeout)
        self._prompt_template = self._load_prompt_template()

    @staticmethod
    def _load_prompt_template() -> str:
        prompt_path = Path(__file__).parent / "templates" / "grader.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return ""

    @staticmethod
    def _normalize_verdicts(result: GradingResult, eval_case: EvalCase) -> None:
        for exp in result.expectations:
            if not (exp.evidence and exp.evidence.startswith("Not evaluated")):
                continue
            exp.verdict = AssertionVerdict.UNEVAL
            exp.passed = False

    @staticmethod
    def _build_uneval_result(eval_case: EvalCase, reason: str) -> GradingResult:

        return GradingResult(
            expectations=[_unevaluated_expectation(e, reason) for e in eval_case.expectations],
            summary=GradingSummary(
                passed=0,
                failed=len(eval_case.expectations),
                total=len(eval_case.expectations),
                pass_rate=0.0,
            ),
        )

    @staticmethod
    def _collect_outputs(output_dir: Path) -> str:
        parts: list[str] = []
        for f in sorted(output_dir.rglob("*")):
            if not (f.is_file() and not f.name.startswith(".")):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                if len(content) > 4000:
                    content = content[:4000] + "\n... (truncated)"
                parts.append(f"=== {f.name} ===\n{content}")
            except Exception:
                parts.append(f"=== {f.name} === (binary)")
        return "\n\n".join(parts) if parts else "(no output files)"

    @staticmethod
    def _extract_json_array(response: str) -> list:
        match = re.search(r"\[[\s\S]*\]", response)
        if not match:
            return []
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return []

    def grade(
        self,
        eval_case: EvalCase,
        transcript: str,
        output_dir: Path,
        *,
        on_usage=None,
    ) -> GradingResult:
        prompt = self._build_prompt(eval_case, transcript, output_dir)
        response = self.client.complete(prompt, on_usage=on_usage, schema=GRADING_SCHEMA)

        if not response or not response.strip():
            return self._build_uneval_result(eval_case, "LLM grading returned empty response")

        try:
            data = json.loads(response)
            result = GradingResult.model_validate(data)
            self._normalize_verdicts(result, eval_case)
            return result
        except Exception:
            return self._parse_response(response, eval_case)

    def grade_tier3(
        self,
        tier3_assertions: list,
        transcript: str,
        output_dir: Path,
        *,
        on_usage=None,
    ) -> list[ExpectationResult]:
        """Grade Tier 3 assertions specifically with evidence/citation requirements."""
        if not tier3_assertions:
            return []

        outputs = self._collect_outputs(output_dir)
        prompt = self._build_tier3_prompt(tier3_assertions, transcript, outputs)
        response = self.client.complete(prompt, on_usage=on_usage, schema=_TIER3_SCHEMA)

        if not response or not response.strip():
            return [
                ExpectationResult(
                    text=a.name,
                    passed=False,
                    evidence="Grading returned empty response",
                    source="llm",
                    verdict=AssertionVerdict.UNEVAL,
                )
                for a in tier3_assertions
            ]

        return self._parse_tier3_response(response, tier3_assertions)

    def _build_tier3_prompt(self, assertions: list, transcript: str, outputs: str) -> str:
        lines = []
        for i, a in enumerate(assertions):
            lines.append(f"### Assertion {i}: {a.name}")
            lines.append(f"Type: {a.type.value}")
            lines.append((f"Params: {json.dumps(a.params, ensure_ascii=False)}"))
            lines.append("")
        return (
            (
                f"{self._prompt_template}\n\n"
                f"--- TIER 3 GRADING TASK ---\n\n"
                f"You are grading ONLY Tier 3 (semantic quality) assertions. "
                f"For each assertion, determine whether the agent's output satisfies it.\n\n"
                f"## Assertions to Grade\n\n"
            )
            + "\n".join(lines)
            + (
                "\n"
                "## Transcript\n"
                f"{transcript[:12000]}"
                "\n"
                "\n"
                "## Outputs\n"
                f"{outputs[:8000]}"
                "\n"
                "\n"
                "## Response Format\n"
                "\n"
                "Respond with ONLY a JSON array:\n"
                '[{"index": 0, "verdict": "PASS", "evidence": "specific citation from '
                'transcript or output", "confidence": 0.9}]\n'
                "\n"
                "## Verdict Values\n"
                "- PASS: Clear evidence the assertion is satisfied. REQUIRED: cite the "
                "exact text.\n"
                "- FAIL: No evidence or contradictory evidence.\n"
                "- UNEVAL: Cannot determine from available information (e.g. truncated "
                "transcript). Use this sparingly — only when genuinely unable to judge.\n"
                "\n"
                "All evidence strings must be in Chinese (中文)."
            )
        )

    def _parse_tier3_response(self, response: str, assertions: list) -> list[ExpectationResult]:
        results: list[ExpectationResult] = []
        items = self._extract_json_array(response)
        if not items:
            for a in assertions:
                results.append(_unevaluated_expectation(a.name, "Failed to parse LLM grading response"))
            return results

        results_by_index: dict[int, dict] = {}
        for item in items:
            if not (isinstance(item, dict)):
                continue
            idx = item.get("index")
            if isinstance(idx, int):
                results_by_index[idx] = item

        for i, a in enumerate(assertions):
            entry = results_by_index.get(i, {})
            verdict_str = entry.get("verdict", "FAIL").upper()
            try:
                verdict = AssertionVerdict(verdict_str)
            except ValueError:
                verdict = AssertionVerdict.UNEVAL

            evidence = str(entry.get("evidence", ""))
            if verdict == AssertionVerdict.UNEVAL:
                evidence = evidence or "Grading incomplete: could not determine"
            elif verdict == AssertionVerdict.PASS and not evidence:
                verdict = AssertionVerdict.UNEVAL
                evidence = "PASS verdict but no evidence provided"

            results.append(
                ExpectationResult(
                    text=a.name,
                    passed=(verdict == AssertionVerdict.PASS),
                    evidence=evidence,
                    source="llm",
                    verdict=verdict,
                )
            )

        return results

    def _build_prompt(self, eval_case: EvalCase, transcript: str, output_dir: Path) -> str:
        outputs = self._collect_outputs(output_dir)
        return (
            (
                f"{self._prompt_template}\n\n"
                f"--- EVALUATION TASK ---\n\n"
                f"Eval ID: {eval_case.id}\n"
                f"Eval Name: {eval_case.get_name()}\n"
                f"Prompt: {eval_case.prompt}\n"
                f"Expected Output: {eval_case.expected_output}\n\n"
                f"--- TRANSCRIPT ---\n{transcript[:8000]}\n\n"
                f"--- OUTPUTS ---\n{outputs[:8000]}\n\n"
                f"--- EXPECTATIONS ---\n"
            )
            + "\n".join(f"- {e}" for e in eval_case.expectations)
            + "\n\n--- TIER 3 ASSERTIONS ---\n"
            + "\n".join(f"- {a.name} ({a.type.value}): {a.params}" for a in eval_case.assertions if a.tier == "TIER3")
            + (
                "\n"
                "\n"
                "Write your grading as JSON matching the grading.json schema. All string "
                "values (evidence, suggestions, reasons, etc.) must be in Chinese (中文).\n"
                "IMPORTANT: If you cannot determine the verdict for any expectation, use "
                '"evidence": "Not evaluated: <reason>". Do NOT silently mark FAIL.'
            )
        )

    def _parse_response(self, response: str, eval_case: EvalCase) -> GradingResult:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                result = GradingResult.model_validate(json.loads(match.group()))
                self._normalize_verdicts(result, eval_case)
                return result
            except Exception:
                logging.getLogger(__name__).debug(
                    ("Optional operation failed; continuing with fallback"), exc_info=True
                )

        return self._build_uneval_result(eval_case, "Unable to parse LLM grading response")


def _unevaluated_expectation(text: str, evidence: str) -> ExpectationResult:
    """生成未评估条目，保留各调用方给出的失败原因。"""
    return ExpectationResult(
        text=text,
        passed=False,
        evidence=evidence,
        source="llm",
        verdict=AssertionVerdict.UNEVAL,
    )
