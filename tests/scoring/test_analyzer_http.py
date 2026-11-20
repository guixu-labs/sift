"""P3:analyzer 切 http(forced tool_use + schema)的专项测试。"""

from unittest.mock import patch

from sift.concurrency import LLMRetryExhausted
from sift.scoring.analyzer import ANALYSIS_SCHEMA, PosthocAnalyzer
from sift.scoring.prompts import ComparisonAnalysisInput


class TestAnalysisSchema:
    @staticmethod
    def test_schema_keeps_all_properties():
        # AnalysisResult 全字段都是真分析产出,无 internal 可剔。
        props = ANALYSIS_SCHEMA["properties"]
        for kept in (
            "winner_strengths",
            "loser_weaknesses",
            "instruction_following",
            "improvement_suggestions",
            "transcript_insights",
        ):
            assert kept in props

    @staticmethod
    def test_schema_inlined_no_ref_no_defs():
        # 同 grader:$ref 经 inline_schema 展开(部分端点不解析 $ref → 空响应 → None)。
        import json

        assert "$defs" not in ANALYSIS_SCHEMA
        assert '"$ref"' not in json.dumps(ANALYSIS_SCHEMA)

    @staticmethod
    def test_schema_required_empty_all_optional():
        assert ANALYSIS_SCHEMA.get("required", "MISSING") == []


class TestAnalyzerCallHttp:
    @staticmethod
    def test_call_returns_empty_on_retry_exhausted():
        with patch("sift.scoring.client.call_llm_with_retry", side_effect=LLMRetryExhausted("exhausted")):
            assert PosthocAnalyzer().client.complete("prompt") == ""

    @staticmethod
    def _fake(response):
        captured = []

        def _f(prompt, *a, **k):
            captured.append(k)
            return response

        return _f, captured

    def test_call_uses_http_transport(self):
        fake, cap = self._fake('["note"]')
        with patch("sift.scoring.client.call_llm_with_retry", side_effect=fake):
            PosthocAnalyzer().client.complete("prompt", schema=ANALYSIS_SCHEMA)
        # site default http 已下沉到 call_llm_with_retry 内部(resolve_llm_transport,
        # SIFT_LLM_TRANSPORT 可覆盖)——调用方不再显式传参,锁「无显式 transport」。
        assert cap[0].get("transport") is None

    def test_analyze_comparison_passes_analysis_schema(self):
        fake, cap = self._fake("{}")
        with patch("sift.scoring.client.call_llm_with_retry", side_effect=fake):
            PosthocAnalyzer().analyze_comparison(
                comparison=ComparisonAnalysisInput(
                    winner="A",
                    winner_skill="winner",
                    loser_skill="loser",
                    comparison_reasoning="reasoning",
                    winner_transcript="wt",
                    loser_transcript="lt",
                    focus_skill="f",
                )
            )
        assert cap[0]["schema"] is ANALYSIS_SCHEMA
