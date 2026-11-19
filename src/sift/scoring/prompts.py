
"""Pure prompt builders and agent-template loading for scoring."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


def load_template(agent: str) -> str:
    """读取随包提供的评分 agent 模板；未安装模板时保留空模板降级。"""
    path = Path(__file__).parent / "templates" / f"{agent}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class ComparisonInputs(NamedTuple):
    """同一次盲比较的两侧输出、题目和标签；重试期间保持不变。"""

    output_a: str
    output_b: str
    eval_prompt: str
    expectations: list | None = None
    label_a: str = "with"
    label_b: str = "without"


class ComparisonAnalysisInput(NamedTuple):
    """比较结果及两侧原始材料，避免赢家和输家的证据传反。"""

    winner: str
    winner_skill: str
    loser_skill: str
    comparison_reasoning: str
    winner_transcript: str
    loser_transcript: str
    focus_skill: str


def build_comparison_prompt(
    template: str,
    output_a: str,
    output_b: str,
    eval_prompt: str,
    expectations: list[str] | None,
) -> str:
    parts = [
        template,
        "",
        "--- COMPARISON TASK ---",
        "",
        f"Eval Prompt: {eval_prompt}",
        "",
        "--- OUTPUT A ---",
        output_a[:8000],
        "",
        "--- OUTPUT B ---",
        output_b[:8000],
    ]
    if expectations:
        parts.append("")
        parts.append("--- EXPECTATIONS ---")
        for e in expectations:
            parts.append(f"- {e}")
    parts.append("")
    parts.append(
        (
            "Write your comparison as JSON matching the comparison schema. All string "
            "values (reasoning, strengths, weaknesses, etc.) must be in Chinese (中文)."
        )
    )
    return "\n".join(parts)


def build_benchmark_prompt(template: str, benchmark_json: str, skill_description: str) -> str:
    parts = [
        template,
        "",
        "--- BENCHMARK ANALYSIS TASK ---",
        "",
    ]
    if skill_description:
        parts.append((f"Skill Description: {skill_description[:2000]}"))
        parts.append("")
    parts.append("--- BENCHMARK DATA ---")
    parts.append(benchmark_json[:12000])
    parts.append("")
    parts.append(("Generate analysis notes as a JSON array of strings. All string values must be in Chinese (中文)."))
    return "\n".join(parts)


def build_analysis_prompt(template: str, comparison: ComparisonAnalysisInput) -> str:
    winner = comparison.winner
    winner_skill = comparison.winner_skill
    loser_skill = comparison.loser_skill
    comparison_reasoning = comparison.comparison_reasoning
    winner_transcript = comparison.winner_transcript
    loser_transcript = comparison.loser_transcript
    focus_skill = comparison.focus_skill
    parts = [
        template,
        "",
        "--- COMPARISON ANALYSIS TASK ---",
        "",
        f"Winner: {winner}",
        (f"Comparator Reasoning: {comparison_reasoning[:2000]}"),
        "",
        "--- WINNER SKILL ---",
        winner_skill[:4000],
        "",
        "--- LOSER SKILL ---",
        loser_skill[:4000],
        "",
        ("--- FOCUS SKILL (改进对象;improvement_suggestions 必须针对它)---"),
        focus_skill[:4000],
        "",
        "--- WINNER TRANSCRIPT ---",
        winner_transcript[:4000],
        "",
        "--- LOSER TRANSCRIPT ---",
        loser_transcript[:4000],
        "",
        (
            "Write your analysis as JSON matching the analysis schema. All string "
            "values (strengths, weaknesses, suggestions, etc.) must be in Chinese "
            "(中文)."
        ),
    ]
    return "\n".join(parts)
