
"""Shared model-call boundary for scoring components."""

from __future__ import annotations

from dataclasses import dataclass

from sift.concurrency import LLMRetryExhausted, call_llm_with_retry


@dataclass
class ScoringClient:
    """执行带重试的评分请求，耗尽后返回空结果供各评分器按领域规则降级。"""

    model: str | None = None
    timeout: int = 120
    default_schema: dict | None = None

    def complete(self, prompt: str, *, on_usage=None, schema=None) -> str:
        try:
            return call_llm_with_retry(
                prompt,
                model=self.model,
                timeout=self.timeout,
                schema=self.default_schema if schema is None else schema,
                on_usage=on_usage,
            )
        except LLMRetryExhausted:
            return ""
