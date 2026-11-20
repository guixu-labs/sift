"""opencode_llm 判官执行层:spawn 形态、隔离布局、事件流解析。全部 mock,不真跑 opencode。

锁四条主线:① ``--standalone`` + 无工具 agent + prompt 走 stdin 的 spawn 形态
(opencode 2.x spike 写死的契约);② 单临时目录三合一(cwd = XDG_DATA_HOME =
tmp,内含 opencode.json agent 定义 + seed 的 auth.json);③ 事件流解析(text 按
part id 取末态/多 part 直拼、step_finish tokens/cost、error 事件、零文本);
④ model 语义(含 / 才传 -m)与 CLI 缺失的 loud 失败。
"""

from __future__ import annotations

import json

import pytest

from sift.execution import opencode_llm as oll


# ── _parse_stream ────────────────────────────────────────────────────────────


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def test_parse_stream_single_text_part():
    stdout = "\n".join(
        [
            _line({"type": "step_start", "part": {"type": "step-start"}}),
            _line({"type": "text", "part": {"id": "prt_a", "text": '{"verdict": "PASS"}'}}),
        ]
    )
    text, tokens, cost, reason, error = oll._parse_stream(stdout)
    assert text == '{"verdict": "PASS"}'
    assert tokens == {} and cost == 0.0 and reason == "" and error == ""


def test_parse_stream_same_part_updates_take_last():
    """同一 part 的多次事件是增量快照 → 按 part id 取末态(不是拼 delta)。"""
    stdout = "\n".join(
        [
            _line({"type": "text", "part": {"id": "prt_a", "text": '{"verdict"'}}),
            _line({"type": "text", "part": {"id": "prt_a", "text": '{"verdict": "PASS"}'}}),
        ]
    )
    text, *_ = oll._parse_stream(stdout)
    assert text == '{"verdict": "PASS"}'


def test_parse_stream_multi_part_joins_in_first_seen_order():
    stdout = "\n".join(
        [
            _line({"type": "text", "part": {"id": "prt_b", "text": "second"}}),
            _line({"type": "text", "part": {"id": "prt_a", "text": "first "}}),
            _line({"type": "text", "part": {"id": "prt_b", "text": "second-UPDATED"}}),
        ]
    )
    text, *_ = oll._parse_stream(stdout)
    # 首现顺序:prt_b 先出现 → 它排前;prt_b 取末态
    assert text == "second-UPDATEDfirst "


def test_parse_stream_step_finish_tokens_and_cost():
    stdout = "\n".join(
        [
            _line({"type": "text", "part": {"id": "p", "text": "ok"}}),
            _line(
                {
                    "type": "step_finish",
                    "part": {
                        "type": "step-finish",
                        "reason": "stop",
                        "cost": 0.01,
                        "tokens": {"input": 8723, "output": 26, "reasoning": 203, "cache": {"read": 1280, "write": 0}},
                    },
                }
            ),
        ]
    )
    _, tokens, cost, reason, _ = oll._parse_stream(stdout)
    assert tokens["input"] == 8723 and tokens["output"] == 26
    assert cost == 0.01
    assert reason == "stop"


def test_parse_stream_multi_step_tokens_last_cost_accumulated():
    """多步:tokens 取末个 step_finish(防步间缓存的 input 重复计入),cost 逐 step 累加(真实计费)。"""
    stdout = "\n".join(
        [
            _line(
                {
                    "type": "step_finish",
                    "part": {
                        "reason": "tool-calls",
                        "cost": 0.01,
                        "tokens": {"input": 100, "output": 10, "reasoning": 80, "cache": {"read": 50, "write": 0}},
                    },
                }
            ),
            _line(
                {
                    "type": "step_finish",
                    "part": {
                        "reason": "stop",
                        "cost": 0.03,
                        "tokens": {"input": 210, "output": 26, "reasoning": 30, "cache": {"read": 120, "write": 0}},
                    },
                }
            ),
        ]
    )
    _, tokens, cost, reason, _ = oll._parse_stream(stdout)
    # tokens 取末步(不随步数累加),cost 累加全程
    assert tokens["input"] == 210 and tokens["output"] == 26
    assert cost == 0.04
    assert reason == "stop"


def test_parse_stream_error_event_and_noise_lines():
    stdout = "\n".join(
        [
            "opencode v2.0.24 (non-JSON noise)",  # 非 JSON 噪声行跳过
            _line({"type": "error", "error": {"type": "provider.invalid-request", "message": "Model is unavailable."}}),
        ]
    )
    text, *_rest, error = oll._parse_stream(stdout)
    assert text == "" and error == "Model is unavailable."


# ── complete():spawn 形态与隔离布局 ──────────────────────────────────────────


@pytest.fixture
def patched_spawn(monkeypatch):
    cap: dict[str, object] = {"spawn_calls": [], "seed_calls": [], "tmpdirs": []}

    def fake_which(name):
        return cap.get("which", "/fake/opencode")

    def fake_spawn_cli(cmd, prompt, timeout, env=None, cwd=None):
        cap["spawn_calls"].append({"cmd": list(cmd), "prompt": prompt, "timeout": timeout, "env": env, "cwd": cwd})
        # 隔离目录在 complete() 的 finally 里即清,文件内容须在调用现场抓(fence:判官
        # agent 定义写进 cwd 的 opencode.json)。
        import os as _os

        if cwd and _os.path.isfile(f"{cwd}/opencode.json"):
            with open(f"{cwd}/opencode.json", encoding="utf-8") as f:
                cap["agent_config"] = json.load(f)
        if isinstance(cap.get("_raise"), Exception):
            raise cap["_raise"]
        return cap.get("_stdout", "")

    def fake_seed_auth(dest):
        cap["seed_calls"].append(str(dest))
        return True

    import tempfile as _tempfile

    real_mkdtemp = _tempfile.mkdtemp

    def tracking_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        cap["tmpdirs"].append(d)
        return d

    import sift.execution.opencode_llm as mod

    monkeypatch.setattr(mod.shutil, "which", fake_which)
    monkeypatch.setattr(mod, "spawn_cli", fake_spawn_cli)
    monkeypatch.setattr(mod, "seed_auth", fake_seed_auth)
    monkeypatch.setattr(mod.tempfile, "mkdtemp", tracking_mkdtemp)
    # 版本门写死(默认 2.0.24;单测可置 cap["version"] 覆写)——真探测在 CI 无
    # opencode 的环境会炸,且测试要的就是不依赖本机版本。
    monkeypatch.setattr(mod, "opencode_version", lambda: cap.get("version", (2, 0, 24)))
    return cap


def _ok_stream(text: str = "OK", **kw) -> str:
    stream = [
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "text", "part": {"id": "p1", "text": text}},
    ]
    if "tokens" in kw:
        stream.append(
            {"type": "step_finish", "part": {"type": "step-finish", "reason": kw.get("reason", "stop"), "cost": kw.get("cost", 0), "tokens": kw["tokens"]}}
        )
    return "\n".join(_line(e) for e in stream)


def test_complete_spawn_shape_and_isolation(patched_spawn):
    """--standalone + 无工具 agent + stdin prompt + 单临时目录三合一布局。"""
    patched_spawn["_stdout"] = _ok_stream('{"ok": 1}', tokens={"input": 10, "output": 2}, cost=0.03)
    result = oll.complete("judge prompt", model="gateway/glm-5.3", timeout=42)

    (call,) = patched_spawn["spawn_calls"]
    a = call["cmd"]
    assert a[0:6] == ["opencode", "run", "--standalone", "--format", "json", "--agent"]
    assert a[6] == oll.JUDGE_AGENT_NAME
    assert ["-m", "gateway/glm-5.3"] == a[7:9]  # 含 / → 传
    assert call["prompt"] == "judge prompt"  # prompt 走 stdin(spawn_cli 的 input)
    assert call["timeout"] == 42

    # 单临时目录三合一:cwd == XDG_DATA_HOME == tmp;PWD 必须重写为 cwd
    # (opencode server 用 env 的 PWD 做项目发现,残留 PWD → "Agent not found";spike 实证)
    cwd = call["cwd"]
    assert call["env"]["XDG_DATA_HOME"] == cwd
    assert call["env"]["PWD"] == cwd
    assert "OLDPWD" not in call["env"]
    # opencode.json(agent 定义)写进 cwd;auth seed 进 tmp/opencode/auth.json
    agent = patched_spawn["agent_config"]["agents"][oll.JUDGE_AGENT_NAME]
    # V2 形态:permissions deny-all 规则结构性封工具 + steps=1 末步移除工具双闸
    # (官方文档明令 V1 的 prompt/tools 在 V2 禁用;2026-11-20 本机 2.0.24 实证)。
    assert agent["permissions"] == [{"action": "*", "resource": "*", "effect": "deny"}], "判官 agent 必须 deny-all 封全部工具"
    assert agent["steps"] == 1
    assert "Never use tools" in agent["system"]
    assert "tools" not in agent and "prompt" not in agent, "V1 字段(prompt/tools)在 V2 配置中禁用"
    assert patched_spawn["seed_calls"] == [f"{cwd}/opencode/auth.json"]

    assert result.text == '{"ok": 1}'
    assert result.input_tokens == 10 and result.output_tokens == 2 and result.cost_usd == 0.03


def test_complete_bare_model_skips_m(patched_spawn):
    """裸名 model(无 /,cc 别名)→ 不传 -m,走 opencode config 默认模型。"""
    patched_spawn["_stdout"] = _ok_stream()
    oll.complete("p", model="glm-5.2[1m]")
    (call,) = patched_spawn["spawn_calls"]
    assert "-m" not in call["cmd"]


def test_complete_missing_cli_raises_loud(monkeypatch):
    import sift.execution.opencode_llm as mod

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="opencode CLI 未找到"):
        oll.complete("p")


def test_complete_version_gate_rejects_v1(patched_spawn):
    """1.x 无 --standalone/私有 server 与 agent V2 定义 → TransportUnavailable(静态
    环境事实,不重试),报当前版本并指路换引擎;2.x 正常放行。"""
    patched_spawn["version"] = (1, 19, 5)
    with pytest.raises(RuntimeError, match=r"opencode 判官 transport 需 opencode ≥ 2\.0") as caught:
        oll.complete("p")
    assert "1.19.5" in str(caught.value)
    assert "cc/http/dsh" in str(caught.value)
    assert patched_spawn["spawn_calls"] == [], "版本门须在 spawn 前拦下"


def test_complete_model_none_failure_appends_default_model_hint(patched_spawn):
    """model=None 且失败含模型/凭据类关键词 → 报错附「配默认模型或显式传模型」提示
    (opencode 配置没定默认模型时,内置默认会被继承的 ANTHROPIC_* 劫持到不可用模型)。"""
    patched_spawn["_raise"] = RuntimeError(
        'opencode CLI failed (exit 1): {"error":{"message":"Model is unavailable."}}'
    )
    with pytest.raises(RuntimeError, match='顶层设 \\"model\\" 字段') as caught:
        oll.complete("p")  # model=None
    assert "provider/model" in str(caught.value)


def test_complete_model_given_failure_keeps_raw_error(patched_spawn):
    """显式传了 model → 失败原样上抛,不加默认模型提示(超时/瞬态噪声不掺和)。"""
    patched_spawn["_raise"] = RuntimeError(
        'opencode CLI failed (exit 1): {"error":{"message":"Model is unavailable."}}'
    )
    with pytest.raises(RuntimeError, match="Model is unavailable") as caught:
        oll.complete("p", model="gateway/glm-5.3")
    assert "顶层设" not in str(caught.value)


def test_complete_error_event_raises(patched_spawn):
    patched_spawn["_stdout"] = _line({"type": "error", "error": {"message": "Model is unavailable."}})
    with pytest.raises(RuntimeError, match="Model is unavailable"):
        oll.complete("p")


def test_complete_empty_text_raises(patched_spawn):
    patched_spawn["_stdout"] = _line({"type": "step_start", "part": {"type": "step-start"}})
    with pytest.raises(RuntimeError, match="empty final text"):
        oll.complete("p")


def test_complete_spawn_failure_cleans_tmp(patched_spawn):
    """spawn 失败 → RuntimeError 透传 + 临时目录清理不留残。"""
    import os as _os

    patched_spawn["_raise"] = RuntimeError("opencode CLI failed (exit 1): boom")
    with pytest.raises(RuntimeError, match="boom"):
        oll.complete("p")
    assert all(not _os.path.isdir(d) for d in patched_spawn["tmpdirs"]), "finally 必须清理临时目录"
