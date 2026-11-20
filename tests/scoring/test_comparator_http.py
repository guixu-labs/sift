"""P2:comparator 切 http(forced tool_use + schema)的专项测试。

schema 形状(确定性,主风险点)+ _call http 行为(transport/schema/on_usage/
retry-exhaustion 降级)+ L1(call_llm_with_retry)↔ L2(_compare_with_retry)分层。
"""

from unittest.mock import patch

from sift.concurrency import LLMRetryExhausted
from sift.execution.comparison_suite import ComparisonInputs, ScoringCallbacks
from sift.scoring.comparator import COMPARATOR_SCHEMA, BlindComparator


class TestComparatorSchema:
    @staticmethod
    def test_schema_excludes_internal_fields():
        props = COMPARATOR_SCHEMA["properties"]
        for internal in ("failed", "pos_a_label", "pos_b_label"):
            assert internal not in props
        for llm_field in ("winner", "reasoning", "rubric", "output_quality", "expectation_results"):
            assert llm_field in props

    @staticmethod
    def test_schema_keeps_defs_to_avoid_dangling_ref():
        assert "$defs" in COMPARATOR_SCHEMA
        assert len(COMPARATOR_SCHEMA["$defs"]) == 6

    @staticmethod
    def test_schema_required_empty_all_fields_have_defaults():
        assert COMPARATOR_SCHEMA.get("required", "MISSING") == []

    @staticmethod
    def test_schema_conformant_json_validates():
        import json

        from sift.models import ComparisonResult

        sample = json.dumps({"winner": "A", "reasoning": "x", "rubric": {}, "output_quality": {}})
        r = ComparisonResult.model_validate(json.loads(sample))
        assert r.winner == "A"


class TestComparatorCallHttp:
    @staticmethod
    def test_call_returns_empty_on_retry_exhausted():
        with patch("sift.scoring.client.call_llm_with_retry", side_effect=LLMRetryExhausted("exhausted")):
            assert BlindComparator().client.complete("prompt") == ""

    @staticmethod
    def test_l1_exhaustion_flows_to_l2_pair_retry():
        import random

        from sift.execution.comparison_suite import _compare_with_retry

        valid = '{"winner":"A","reasoning":"ok","rubric":{},"output_quality":{}}'
        comparator = BlindComparator(rng=random.Random(0))
        with (
            patch("sift.scoring.client.call_llm_with_retry", side_effect=[LLMRetryExhausted("boom"), valid]),
            patch("sift.execution.comparison_suite.time"),
        ):
            result = _compare_with_retry(
                comparator=comparator,
                max_retries=3,
                comparison=ComparisonInputs(output_a="a", output_b="b", eval_prompt="p", expectations=None),
                callbacks=ScoringCallbacks(),
            )
        assert result is not None
        assert result.winner in ("with", "without")
        assert result.failed is False

    @staticmethod
    def _fake(response):
        captured = []

        def _f(prompt, *a, **k):
            captured.append(k)
            return response

        return _f, captured

    def test_call_uses_http_transport_and_comparator_schema(self):
        fake, cap = self._fake('{"winner":"A","reasoning":"t"}')
        with patch("sift.scoring.client.call_llm_with_retry", side_effect=fake):
            BlindComparator().client.complete("prompt")
        # site default http 已下沉到 call_llm_with_retry 内部(resolve_llm_transport,
        # SIFT_LLM_TRANSPORT 可覆盖)——调用方不再显式传参,锁「无显式 transport」。
        assert cap[0].get("transport") is None
        assert cap[0]["schema"] is COMPARATOR_SCHEMA

    def test_call_forwards_on_usage(self):
        fake, cap = self._fake('{"winner":"A","reasoning":"t"}')
        with patch("sift.scoring.client.call_llm_with_retry", side_effect=fake):
            BlindComparator().client.complete("prompt", on_usage=lambda u: None)
        assert cap[0]["on_usage"] is not None
