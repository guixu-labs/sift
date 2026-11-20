"""OpenCode-backed LLM transport: schema prompts, strict validation, no CC fallback.

与 ``test_dsh_llm_transport.py`` 镜像:mock ``opencode_llm.complete``,锁三条主线——
① resolve 支持 opencode(+default 参数的决策链语义);② schema 模式的 prompt 注入/
fence 剥离/jsonschema 校验/items 解包;③ 文本模式透传与 usage 回调形状。全部
不真跑 opencode。另含**静态缺位兜底(桥接层)**:cc 缺位自动转 opencode、必可见、
单向不回退、http 回退支线表驱动、TransportUnavailable 立即失败。
"""

from __future__ import annotations

import json
import logging

import pytest

from sift import llm_transport as transport
from sift.execution import opencode_llm
from sift.llm_transport import LLMCallConfig


def _completion(text: str, *, inp: int = 0, out: int = 0, cost: float = 0.0, reason: str = "stop"):
    return opencode_llm.OpencodeCompletion(
        text=text, input_tokens=inp, output_tokens=out, cost_usd=cost, stop_reason=reason
    )


def test_resolve_llm_transport_supports_opencode(monkeypatch):
    monkeypatch.setenv("SIFT_LLM_TRANSPORT", "opencode")
    assert transport.resolve_llm_transport(None) == "opencode"
    assert transport.resolve_llm_transport("opencode") == "opencode"


def test_resolve_llm_transport_default_param_and_env_precedence(monkeypatch):
    """site default 经 default 参数表达;env 覆盖 site default;显式值覆盖 env。"""
    monkeypatch.delenv("SIFT_LLM_TRANSPORT", raising=False)
    # 写死 which(cc 是兜底源,resolve 会探测 claude 在不在;不写死则结果随本机装的 CLI 抖)
    monkeypatch.setattr(transport.shutil, "which", lambda name: f"/fake/{name}" if name == "claude" else None)
    assert transport.resolve_llm_transport(None, default="http") == "http"
    monkeypatch.setenv("SIFT_LLM_TRANSPORT", "opencode")
    assert transport.resolve_llm_transport(None, default="http") == "opencode"  # env 胜 site default
    assert transport.resolve_llm_transport("cc", default="http") == "cc"  # 显式胜 env
    with pytest.raises(RuntimeError, match="cc / http / dsh / opencode"):
        transport.resolve_llm_transport("carrier-pigeon")


def test_call_claude_opencode_transport_schema_flow(monkeypatch):
    seen: dict[str, object] = {}

    def fake_complete(prompt, *, model=None, timeout=120, on_progress=None):
        seen["prompt"] = prompt
        seen["model"] = model
        seen["timeout"] = timeout
        return _completion('```json\n{"ok": true}\n```', inp=17, out=5, cost=0.02)

    monkeypatch.setattr(opencode_llm, "complete", fake_complete)
    monkeypatch.setattr(transport, "call_cli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call cc")))
    usage: list[dict] = []

    result = transport.call_claude(
        prompt="answer this",
        config=LLMCallConfig(model="gateway/glm-5.3", timeout=33, schema={"type": "object"}, transport="opencode"),
        on_usage=usage.append,
    )

    assert json.loads(result) == {"ok": True}
    assert "JSON Schema" in str(seen["prompt"])  # schema 内嵌进 prompt(dsh 同型)
    assert "Do not call tools" in str(seen["prompt"])
    assert seen["model"] == "gateway/glm-5.3"
    assert seen["timeout"] == 33
    # usage payload 形状对齐 call_claude 契约(input/output/cost)
    assert usage == [{"input_tokens": 17, "output_tokens": 5, "total_cost_usd": 0.02}]


def test_call_claude_opencode_transport_text_mode_passthrough(monkeypatch):
    monkeypatch.setattr(opencode_llm, "complete", lambda prompt, **k: _completion("plain answer"))
    out = transport.call_claude(
        prompt="say hi", config=LLMCallConfig(transport="opencode"), on_usage=lambda u: None
    )
    assert out == "plain answer"  # schema=None → 纯文本进出,不做校验


def test_call_claude_opencode_transport_rejects_invalid_json(monkeypatch):
    monkeypatch.setattr(opencode_llm, "complete", lambda prompt, **k: _completion("not json"))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        transport.call_claude(
            prompt="x",
            config=LLMCallConfig(schema={"type": "object"}, transport="opencode"),
        )


def test_call_claude_opencode_transport_rejects_schema_invalid(monkeypatch):
    monkeypatch.setattr(opencode_llm, "complete", lambda prompt, **k: _completion('{"wrong": 1}'))
    with pytest.raises(RuntimeError, match="schema-invalid"):
        transport.call_claude(
            prompt="x",
            config=LLMCallConfig(schema={"type": "object", "required": ["verdict"]}, transport="opencode"),
        )


def test_call_claude_opencode_transport_unwraps_items_array(monkeypatch):
    """array 判官约定:{"items":[...]} → 裸 array(与 call_cli/call_http 同约)。"""
    monkeypatch.setattr(
        opencode_llm, "complete", lambda prompt, **k: _completion('{"items": [{"verdict": "PASS"}, {"verdict": "FAIL"}]}')
    )
    result = transport.call_claude(
        prompt="x",
        config=LLMCallConfig(
            schema={"type": "object", "properties": {"items": {"type": "array"}}}, transport="opencode"
        ),
    )
    assert json.loads(result) == [{"verdict": "PASS"}, {"verdict": "FAIL"}]


def test_call_claude_opencode_transport_empty_text_raises(monkeypatch):
    monkeypatch.setattr(opencode_llm, "complete", lambda prompt, **k: _completion("   "))
    with pytest.raises(RuntimeError, match="empty final text"):
        transport.call_claude(prompt="x", config=LLMCallConfig(transport="opencode"))


def test_call_dsh_items_array_unwrap_parity(monkeypatch):
    """dsh 与 opencode 的 items 解包对齐(call_cli/call_http 原有约定,回归锁)。"""
    from sift.execution.dsh_runner import DshRunner
    from sift.models import RunOutput

    monkeypatch.setattr(
        DshRunner,
        "complete",
        staticmethod(
            lambda prompt, options: RunOutput(
                exit_code=0, stdout="", total_tokens=0, result_text='{"items": [1, 2]}', stop_reason="ok"
            )
        ),
    )
    result = transport.call_claude(
        prompt="x",
        config=LLMCallConfig(
            schema={"type": "object", "properties": {"items": {"type": "array"}}}, transport="dsh"
        ),
    )
    assert json.loads(result) == [1, 2]


# ---------------------------------------------------------------------------
# 静态缺位兜底(桥接层):「没装」自动转 + 必可见;「运行时坏」不转、报清楚
# ---------------------------------------------------------------------------


def _pin_which(monkeypatch, present: set[str]):
    """写死 which 探测:present 里的 CLI 视为已装,其余缺位(测试不随本机装的 CLI 抖)。"""
    monkeypatch.setattr(transport.shutil, "which", lambda name: f"/fake/{name}" if name in present else None)


def test_resolve_falls_back_to_opencode_when_claude_missing(monkeypatch, caplog):
    """cc 静态缺位 + opencode 在 → 转 opencode;转必可见(提示含固定方式)。"""
    monkeypatch.delenv("SIFT_LLM_TRANSPORT", raising=False)
    _pin_which(monkeypatch, {"opencode"})
    with caplog.at_level(logging.WARNING):
        assert transport.resolve_llm_transport(None) == "opencode"
        # 显式 cc 同样受兜底:他要的是「能跑」,而 cc 不存在是环境事实,不是引擎意图
        assert transport.resolve_llm_transport("cc") == "opencode"
    assert "claude CLI 未找到" in caplog.text
    assert "SIFT_LLM_TRANSPORT=opencode" in caplog.text  # 提示里带「怎么固定」


def test_resolve_fallback_announced_once_across_repeated_calls(monkeypatch, caplog):
    """同一转接(cc→opencode)进程内只播报一次:判官批多批 × 重试反复 resolve 不刷屏。"""
    monkeypatch.delenv("SIFT_LLM_TRANSPORT", raising=False)
    _pin_which(monkeypatch, {"opencode"})
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            assert transport.resolve_llm_transport(None) == "opencode"
            assert transport.resolve_llm_transport("cc") == "opencode"
    assert caplog.text.count("claude CLI 未找到") == 1  # 十次 resolve 只一条告警


def test_resolve_keeps_cc_when_fallback_target_also_missing(monkeypatch):
    """cc 缺 + 兜底目标也缺 → 维持 cc(如实,不 mask);由 call_cli 前置检查报清楚。"""
    monkeypatch.delenv("SIFT_LLM_TRANSPORT", raising=False)
    _pin_which(monkeypatch, set())
    assert transport.resolve_llm_transport(None) == "cc"


def test_resolve_no_fallback_when_cc_present(monkeypatch):
    """claude 在 → cc 原样(回归底线:兜底机制不改变已装机器的行为)。"""
    monkeypatch.delenv("SIFT_LLM_TRANSPORT", raising=False)
    _pin_which(monkeypatch, {"claude"})
    assert transport.resolve_llm_transport(None) == "cc"


def test_resolve_explicit_engine_missing_never_swapped(monkeypatch):
    """单向表:显式 opencode 而它缺位 → 维持 opencode 如实报,不静默换回 cc
    (显式引擎承载模型语义,静默换源破坏判官一致性 + 掩盖配置错误)。"""
    monkeypatch.delenv("SIFT_LLM_TRANSPORT", raising=False)
    _pin_which(monkeypatch, {"claude"})
    assert transport.resolve_llm_transport("opencode") == "opencode"


def test_resolve_non_source_transports_never_probed(monkeypatch):
    """非兜底源(http/dsh)不碰文件系统,行为不随环境抖动。"""
    monkeypatch.delenv("SIFT_LLM_TRANSPORT", raising=False)
    _pin_which(monkeypatch, set())
    assert transport.resolve_llm_transport(None, default="http") == "http"
    assert transport.resolve_llm_transport("dsh") == "dsh"


def test_call_cli_missing_binary_raises_transport_unavailable(monkeypatch):
    """cc 前置检查:缺位报一次清楚 + 指路(不裸抛 FileNotFoundError 进重试链空转);
    TransportUnavailable 是 RuntimeError 子类(外层降级路径零改动接住)。"""
    _pin_which(monkeypatch, set())
    with pytest.raises(transport.TransportUnavailable, match="SIFT_LLM_TRANSPORT=opencode"):
        transport.call_cli("p", LLMCallConfig(), max_turns=1, on_usage=None)
    assert issubclass(transport.TransportUnavailable, RuntimeError)


def test_http_fallback_targets_opencode_when_cc_missing(monkeypatch):
    """529 回退支线查兜底表:cc 缺位时回退 opencode,判官批的救命支线在无 claude
    的机器上不再是死路;call_cli 不被调。"""
    calls = {"opencode": 0, "cli": 0}
    monkeypatch.setattr(
        transport, "call_http", lambda *a, **k: (_ for _ in ()).throw(transport.HttpThrottled("529"))
    )
    monkeypatch.setattr(
        transport, "call_cli", lambda *a, **k: calls.__setitem__("cli", calls["cli"] + 1) or "cc-result"
    )
    monkeypatch.setattr(
        transport, "call_opencode", lambda *a, **k: calls.__setitem__("opencode", calls["opencode"] + 1) or "oc-result"
    )
    _pin_which(monkeypatch, {"opencode"})
    lines: list[str] = []
    out = transport.call_claude(
        prompt="p", on_progress=lines.append, config=LLMCallConfig(model="m", transport="http")
    )
    assert out == "oc-result"
    assert calls == {"opencode": 1, "cli": 0}
    assert any("回退 opencode" in line for line in lines)


def test_http_fallback_both_missing_raises_transport_unavailable(monkeypatch):
    """cc 缺 + opencode 也缺 → 回退目标维持 cc → call_cli 前置检查如实报
    TransportUnavailable(穿透 call_claude,进重试壳立即失败)。"""
    monkeypatch.setattr(
        transport, "call_http", lambda *a, **k: (_ for _ in ()).throw(transport.HttpThrottled("529"))
    )
    _pin_which(monkeypatch, set())
    with pytest.raises(transport.TransportUnavailable, match="claude CLI 未找到"):
        transport.call_claude(prompt="p", config=LLMCallConfig(model="m", transport="http"))
