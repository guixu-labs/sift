"""trigger_suggest.build_llm_suggestion 切 http(forced tool_use + 单字段 schema)专项测试。

schema 形状(单字段、inline 无 $ref)+ build_llm_suggestion http 行为(transport/schema 穿透、
成功取 suggestion 字段、重试耗尽/空响应降级、parse 失败 fallback、非数字 usage 守卫)。
重试次数本身由 call_llm_with_retry(concurrency)自测,本文件 mock 整体。
"""

import json
from unittest.mock import patch

from sift.concurrency import LLMRetryExhausted
from sift.execution.trigger_suggest import _SUGGEST_SCHEMA, build_llm_suggestion

_FAILED = [{"id": 1, "prompt": "分析日志"}, {"id": 3, "prompt": "查异常"}]
_FAILED_NOT = [{"id": 2, "prompt": "翻译小说"}]


class TestSuggestSchema:
    @staticmethod
    def test_schema_single_suggestion_field():
        assert "suggestion" in _SUGGEST_SCHEMA["properties"]
        assert _SUGGEST_SCHEMA["required"] == ["suggestion"]

    @staticmethod
    def test_schema_inlined_no_ref_no_defs():
        # $ref/$defs 经 inline_schema 就地展开(记忆 sift-derived-schema-must-inline)。
        assert "$defs" not in _SUGGEST_SCHEMA
        assert '"$ref"' not in json.dumps(_SUGGEST_SCHEMA)


class TestBuildLlmSuggestionHttp:
    @staticmethod
    def test_skipped_when_all_pass():
        """全 PASS → 不调 call_llm_with_retry(零成本)。"""
        calls = {"n": 0}

        def fake(prompt, *a, **k):
            calls["n"] += 1
            return "x"

        with patch("sift.execution.trigger_suggest.call_llm_with_retry", side_effect=fake):
            sugg, usage, reason = build_llm_suggestion("desc", [], [])
        assert sugg is None and usage == {} and reason is None
        assert calls["n"] == 0

    @staticmethod
    def test_degrades_on_retry_exhausted():
        """call_llm_with_retry 耗尽抛 LLMRetryExhausted → (None, usage, reason 含「限流」)。"""
        with patch("sift.execution.trigger_suggest.call_llm_with_retry", side_effect=LLMRetryExhausted("exhausted")):
            sugg, usage, reason = build_llm_suggestion("desc", _FAILED, [])
        assert sugg is None
        assert reason and "限流" in reason  # reason「LLM 调用重试耗尽(疑似限流)」

    @staticmethod
    def _fake(response, usage_payload=None):
        captured = []

        def _f(prompt, *a, **k):
            captured.append({"prompt": prompt, **k})
            if usage_payload and k.get("on_usage"):
                k["on_usage"](usage_payload)
            return response

        return _f, captured

    def test_success_returns_suggestion(self):
        "成功:JSON {suggestion:...} → Suggestion(message/probe_ids/tokens/duration)+ usage + reason=None。"
        resp = json.dumps({"suggestion": "做日志分析、不翻译\n理由:收窄 scope"})
        payload = {"input_tokens": 100, "output_tokens": 50, "total_cost_usd": 0.01}
        fake, cap = self._fake(resp, usage_payload=payload)
        with patch("sift.execution.trigger_suggest.call_llm_with_retry", side_effect=fake):
            sugg, usage, reason = build_llm_suggestion("当前 desc", _FAILED, _FAILED_NOT, model="m")
        assert sugg is not None and sugg.category == "llm_rewrite" and sugg.source == "llm"
        assert sugg.probe_ids == [1, 3, 2]  # 失败 probe 全列(should + should_not)
        assert "做日志分析" in sugg.message
        assert reason is None
        assert sugg.tokens == 150  # input(100)+output(50);cost 不进
        assert sugg.duration_seconds >= 0.0
        assert usage["total_cost_usd"] == 0.01  # usage 原样透传
        assert "当前 desc" in cap[0]["prompt"]  # 当前 description 喂进 prompt

    def test_transport_and_schema_passed(self):
        """call_llm_with_retry 收到 transport=http + schema=_SUGGEST_SCHEMA。"""
        fake, cap = self._fake(json.dumps({"suggestion": "x"}))
        with patch("sift.execution.trigger_suggest.call_llm_with_retry", side_effect=fake):
            build_llm_suggestion("desc", _FAILED, [])
        # site default http 已下沉到 call_llm_with_retry 内部(resolve_llm_transport,
        # SIFT_LLM_TRANSPORT 可覆盖)——调用方不再显式传参,锁「无显式 transport」。
        assert cap[0].get("transport") is None
        assert cap[0]["schema"] is _SUGGEST_SCHEMA

    def test_degrades_on_empty_response(self):
        """http 返空(call_llm_with_retry 不重空响应)→ (None, usage, reason 含「空」)。"""
        fake, _ = self._fake("   ")
        with patch("sift.execution.trigger_suggest.call_llm_with_retry", side_effect=fake):
            sugg, usage, reason = build_llm_suggestion("desc", _FAILED, [])
        assert sugg is None
        assert reason and "空" in reason

    def test_parse_failure_fallback_to_raw(self):
        """tool_use 偶发非 JSON → 整段当文本仍成 Suggestion(诊断文案不丢)。"""
        fake, _ = self._fake("这不是 JSON,就是一段纯文本 description 草案")
        with patch("sift.execution.trigger_suggest.call_llm_with_retry", side_effect=fake):
            sugg, usage, reason = build_llm_suggestion("desc", _FAILED, [])
        assert sugg is not None and sugg.category == "llm_rewrite"
        assert "纯文本 description" in sugg.message

    def test_non_numeric_usage_tokens_guarded(self):
        """usage 含非数字字段(service_tier str / server_tool_use dict)→ tokens 只累加数字,不炸。"""
        payload = {"input_tokens": 80, "output_tokens": 20, "service_tier": "standard", "server_tool_use": {"count": 2}}
        fake, _ = self._fake(json.dumps({"suggestion": "x"}), usage_payload=payload)
        with patch("sift.execution.trigger_suggest.call_llm_with_retry", side_effect=fake):
            sugg, _, _ = build_llm_suggestion("desc", _FAILED, [])
        assert sugg.tokens == 100  # 80+20,非数字字段不影响
