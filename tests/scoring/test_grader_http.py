"""P3:grader 切 http(forced tool_use + schema)的专项测试。

schema 形状(GRADING_SCHEMA 派生:剔 internal、留 $defs)+ _call_claude http 行为
(transport/schema/retry-exhaustion 降级→""→grade 走 _build_uneval_result→UNEVAL)。
"""

from unittest.mock import patch

from sift.concurrency import LLMRetryExhausted
from sift.scoring.llm_grader import GRADING_SCHEMA, LLMGrader


class TestGradingSchema:
    @staticmethod
    def test_schema_excludes_internal_fields():
        props = GRADING_SCHEMA["properties"]
        for internal in (
            "cache_key",
            "error",
            "execution_metrics",
            "timing",
            "claims",
            "eval_feedback",
            "user_notes_summary",
        ):
            assert internal not in props
        for kept in ("expectations", "summary"):
            assert kept in props

    @staticmethod
    def test_schema_inlined_no_ref_no_defs():
        # $ref/$defs 经 inline_schema 就地展开:部分端点(如 GLM)不解析 tool input_schema
        # 里的 $ref,必填字段是 $ref 时模型返回 {} → grade 全 UNEVAL(实测 100%)。
        import json

        assert "$defs" not in GRADING_SCHEMA
        assert '"$ref"' not in json.dumps(GRADING_SCHEMA)

    @staticmethod
    def test_schema_validates_minimal_grading():
        import json

        from sift.models import GradingResult

        sample = json.dumps({"expectations": [], "summary": {"passed": 0, "failed": 0, "total": 0, "pass_rate": 0.0}})
        r = GradingResult.model_validate(json.loads(sample))
        assert r.expectations == []


class TestGraderCallHttp:
    _EMPTY = '{"expectations":[],"summary":{"passed":0,"failed":0,"total":0,"pass_rate":0.0}}'

    @staticmethod
    def test_call_claude_returns_empty_on_retry_exhausted():
        with patch("sift.scoring.client.call_llm_with_retry", side_effect=LLMRetryExhausted("exhausted")):
            assert LLMGrader().client.complete("prompt") == ""

    @staticmethod
    def _fake(response):
        captured = []

        def _f(prompt, *a, **k):
            captured.append(k)
            return response

        return _f, captured

    def test_call_claude_uses_http_transport_and_grading_schema(self):
        fake, cap = self._fake(self._EMPTY)
        with patch("sift.scoring.client.call_llm_with_retry", side_effect=fake):
            LLMGrader().client.complete("prompt", schema=GRADING_SCHEMA)
        # site default http 已下沉到 call_llm_with_retry 内部(resolve_llm_transport,
        # SIFT_LLM_TRANSPORT 可覆盖)——调用方不再显式传参,锁「无显式 transport」。
        assert cap[0].get("transport") is None
        assert cap[0]["schema"] is GRADING_SCHEMA

    def test_grade_passes_grading_schema(self, tmp_path):
        """grade() 把 GRADING_SCHEMA 经 _call_claude 穿到 call_llm_with_retry。"""
        from sift.models import EvalCase

        fake, cap = self._fake(self._EMPTY)
        ec = EvalCase(id=1, prompt="do", expectations=["e1"])
        with patch("sift.scoring.client.call_llm_with_retry", side_effect=fake):
            LLMGrader().grade(ec, "t", tmp_path)
        assert cap[0]["schema"] is GRADING_SCHEMA
