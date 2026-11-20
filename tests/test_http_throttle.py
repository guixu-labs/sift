# tests/test_http_throttle.py
"""裸 HTTP 通道限流异常分派:429 → HttpRateLimited(透传做 AIMD)、529 → HttpThrottled(回退 cc)。"""

from __future__ import annotations

import email.message
import io
import urllib.error

import pytest
from sift import llm_transport as utils
from sift.llm_transport import LLMCallConfig


def _http_err(code: int, body: bytes = b'{"error":{"message":"x"}}') -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    return urllib.error.HTTPError("http://x/v1/messages", code, "err", hdrs, io.BytesIO(body))


def _raise(err):
    def _f(*a, **k):
        raise err

    return _f


@pytest.fixture(autouse=True)
def _stub_http_creds(monkeypatch):
    """call_http 在调 urlopen **前**先解析凭据(ANTHROPIC_AUTH_TOKEN / BASE_URL),
    CI 无凭据(os.environ 与 ~/.claude/settings.json 都没)会先抛 RuntimeError,urlopen 的
    mock 永远到不了——被测的「HTTP 错码 → 异常分派」根本没执行。stub resolve_anthropic_setting 返假
    凭据,隔离掉凭据解析(非本测试关注),让错码映射真正走到 urlopen。本地有凭据时这条
    无影响(假凭据覆盖真凭据,行为一致)。
    """
    monkeypatch.setattr(
        utils,
        "resolve_anthropic_setting",
        lambda key: "http://fake-host" if key == "ANTHROPIC_BASE_URL" else "fake-token",
    )


def test_rate_limited_not_runtime_error_subclass():
    """HttpRateLimited 非 RuntimeError 子类(防被通用 except RuntimeError 误吞,要触发 AIMD)。"""
    assert not issubclass(utils.HttpRateLimited, RuntimeError)


def test_llm_http_call_429_raises_rate_limited(monkeypatch):
    monkeypatch.setattr(utils.urllib.request, "urlopen", _raise(_http_err(429)))
    with pytest.raises(utils.HttpRateLimited):
        utils.call_http(
            prompt="prompt", on_usage=None, config=LLMCallConfig(model="m", timeout=10, schema={"type": "object"})
        )


def test_llm_http_call_529_still_throttled(monkeypatch):
    """529 行为不变(回归):仍 HttpThrottled 交 dispatcher 回退 cc。"""
    monkeypatch.setattr(utils.urllib.request, "urlopen", _raise(_http_err(529)))
    with pytest.raises(utils.HttpThrottled):
        utils.call_http(
            prompt="prompt", on_usage=None, config=LLMCallConfig(model="m", timeout=10, schema={"type": "object"})
        )


def test_llm_http_call_other_code_still_runtime_error(monkeypatch):
    """其他 HTTP 错(如 400)仍 RuntimeError(暴露、走既有重试/兜底)。

    match='400' 防假绿:若凭据解析先抛的 RuntimeError 混进来(消息是「缺凭据」不含 400),
    会响亮失败而非误绿——逼着走到真正的 urlopen 400 路径。
    """
    monkeypatch.setattr(utils.urllib.request, "urlopen", _raise(_http_err(400)))
    with pytest.raises(RuntimeError, match="400"):
        utils.call_http(
            prompt="prompt", on_usage=None, config=LLMCallConfig(model="m", timeout=10, schema={"type": "object"})
        )


def _pin_which(monkeypatch, present):
    """写死 which 探测(桥接层测试环境无关):present 里的名字视为已装,其余缺位。

    http→cc 回退支线现在查兜底表(缺 claude 时转 opencode),不写死的话测试结果随
    本机装了什么 CLI 而抖。
    """
    monkeypatch.setattr(utils.shutil, "which", lambda name: f"/fake/{name}" if name in present else None)


def test_call_claude_http_rate_limited_not_falling_back(monkeypatch):
    """429 透传,不回退 cc(同账号同限,cc 也撞)。"""
    monkeypatch.setattr(utils, "call_http", _raise(utils.HttpRateLimited("429")))
    cc = []
    monkeypatch.setattr(utils, "call_cli", lambda *a, **k: cc.append(1) or "cc")
    with pytest.raises(utils.HttpRateLimited):
        utils.call_claude(prompt="prompt", config=LLMCallConfig(model="m", transport="http"))
    assert cc == []  # 没回退


def test_call_claude_http_throttled_falls_back_to_cc(monkeypatch):
    """529 回退 cc(dispatcher 既有行为,回归)。"""
    _pin_which(monkeypatch, {"claude"})  # claude 在 → 回退目标维持 cc(兜底表不介入)
    monkeypatch.setattr(utils, "call_http", _raise(utils.HttpThrottled("529")))
    monkeypatch.setattr(utils, "call_cli", lambda *a, **k: "cc-result")
    assert utils.call_claude(prompt="prompt", config=LLMCallConfig(model="m", transport="http")) == "cc-result"


def test_no_tool_use_not_runtime_error_subclass():
    """HttpNoToolUse 非 RuntimeError 子类(防被通用 except RuntimeError 误吞,要穿透到 dispatcher)。"""
    assert not issubclass(utils.HttpNoToolUse, RuntimeError)


def test_call_claude_http_no_tool_use_falls_back_to_cc(monkeypatch):
    """forced tool_use 拿不到(GLM 只回 thinking / 撑满 max_tokens)→ HttpNoToolUse → 回退 cc。"""
    _pin_which(monkeypatch, {"claude"})  # claude 在 → 回退目标维持 cc(兜底表不介入)
    monkeypatch.setattr(
        utils,
        "call_http",
        _raise(utils.HttpNoToolUse("only thinking", stop_reason="end_turn")),
    )
    monkeypatch.setattr(utils, "call_cli", lambda *a, **k: "cc-result")
    assert utils.call_claude(prompt="prompt", config=LLMCallConfig(model="m", transport="http")) == "cc-result"


def test_call_claude_http_fallback_reports_progress(monkeypatch):
    """http → cc 回退前经 on_progress 报一行(升档三连耗尽后 cc 还要磨几分钟,
    不报行的话前后连起来整段静默像挂死)。429 不回退(既有行为)也不报行。
    """
    lines = []
    _pin_which(monkeypatch, {"claude"})  # claude 在 → 回退目标维持 cc(兜底表不介入)
    monkeypatch.setattr(
        utils,
        "call_http",
        _raise(utils.HttpNoToolUse("only thinking", stop_reason="max_tokens")),
    )
    monkeypatch.setattr(utils, "call_cli", lambda *a, **k: "cc-result")
    out = utils.call_claude(
        prompt="prompt", on_progress=lines.append, config=LLMCallConfig(model="m", transport="http")
    )
    assert out == "cc-result"
    assert lines == ["  ↳ http 通道拿不到结果,回退 cc…"]
