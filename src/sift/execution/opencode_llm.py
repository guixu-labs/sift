"""OpenCode LLM transport — 判官补全经 ``opencode run --standalone`` 单轮完成。

与 ``OpenCodeRunner``(--task 真任务执行器,产 transcript 喂 audit)对偶:这里是
LLM transport(判官出题/评分/审计裁决的纯文本-结构化补全),不产 transcript、
不需要工具、不回放会话。同签名家族的另两位是 ``call_cli``(claude CLI)与
``DshRunner.complete``(DSH headless)。

设计决策(2026-11-19 本机 spike 实证,opencode 2.0.24):

- **``--standalone``(私有 server)是唯一正确形态**:opencode 2.x 的 ``opencode run``
  默认连后台共享 service,service 持有操作员真实 env —— 客户端的 ``XDG_DATA_HOME``
  被完全忽略(spike:隔离目录里连 db 都不出现、session 落真库、隔离目录
  ``session list`` 返回的是真库内容)。判官一场几十上百批,只有 standalone
  (每次私有 server + 客户端 env 生效 + session 落隔离一次性 db)不污染操作员
  会话库、隔离语义才是真的。
- **单临时目录三合一**:``cwd = XDG_DATA_HOME = tmp``。cwd 放 ``opencode.json``
  (自定义无工具判官 agent);``tmp/opencode/auth.json`` seed(opencode 2.x 把凭据
  存 XDG_DATA_HOME,隔离后不 seed 则 zhipuai-coding-plan、opencode free 等
  凭据型 provider 全断,只剩 env/config 型可用)。
- **结构性禁工具是判官正确性要求**:判官只准用 prompt 内证据下判。默认 build
  agent 会真调工具拿带外证据(spike:问「README.md 存在吗」→ 模型真跑 ``ls``
  查工作区、21s 两步;禁工具后单步直答 7s、诚实 UNEVAL)。自定义 agent 走 V2
  ``permissions`` deny-all + ``steps=1`` 双闸(2026-11-20 实证:工具诱导探针被
  模型自述「工具已被禁用」,事件流零 tool 事件)—— 指令约束是模型自觉,结构性
  禁用才是保证。
- **仅支持 opencode ≥ 2.0**:`--standalone`/私有 server、agent V2 定义、事件流
  形态都是 2.x 语义,1.x 上无对应物。``complete()`` 前置版本门(见下),1.x 如实报
  TransportUnavailable 并指路换引擎 —— 不做 1.x 分支(整套设计依赖 2.x,分支=重写)。
- **prompt 走 stdin**:判官批 prompt 可达数十 KB(transcript 切片 + 指令),
  argv 单参上限 128KB 且需转义;``opencode run`` 无消息参数时读 stdin(spike
  实证)。超时 killpg 整组杀私有 server + client(复用 ``spawn_cli``,与 claude
  CLI 同一套:node 型 CLI 单 kill 主进程会留 worker 孤儿卡 stdout)。
- **模型语义 = opencode 语义**:`-m provider/model` 只在含 ``/`` 时传;裸名
  (cc 别名如 ``glm-5.2[1m]``)不传,走 opencode config 的默认模型 —— 判官模型
  随 opencode 配置走,与智谱 ``ANTHROPIC_*`` 深度绑定解耦是本 transport 的核心
  收益(与 ``OpenCodeRunner`` 同一约定)。
- **事件流(``--format json`` NDJSON)**:``text`` 事件按 part id 取末态(容
  delta 更新),多 part 按首现顺序直拼;``step_finish`` 有则解析 tokens/cost 回
  调 on_usage(best-effort,与 dsh 一致:缺了不报、判官不依赖);``error`` 事件 →
  RuntimeError(走判官既有重试链);零文本 → RuntimeError。

超时/失败一律 raise RuntimeError(调用方 ``judge_batch_with_retry`` /
``call_llm_with_retry`` 按 cause 分类重试);**不回退 cc**(opencode 是「换判官
引擎」而非「快路径」,静默回退会换模型破坏判官一致性 —— 与 dsh 同一取舍)。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Callable, NamedTuple

from sift.evals.opencode_db import opencode_version, seed_auth
from sift.llm_transport import TransportUnavailable, claude_cli_env, spawn_cli

#: 自定义判官 agent 名(``opencode.json`` 定义 + ``--agent`` 引用)。
JUDGE_AGENT_NAME = "sift-judge"

# 判官 agent 的精简系统提示:一句话立住「只用 prompt 内证据、绝不调工具」的角色。
# 同时把系统提示从 build agent 的 ~8.7k token(spike 实测每批 input 8723)压到一行,
# 判官批每批省下这笔固定开销。
_JUDGE_AGENT_PROMPT = (
    "You are a precise text and JSON generator for an automated evaluation judge. "
    "Answer using ONLY the information provided in the user message. Never use tools."
)


class OpencodeCompletion(NamedTuple):
    """一次 opencode 补全的产出(text 为主,tokens/cost best-effort)。"""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str = ""


def judge_agent_config() -> dict:
    """判官专用 agent 的 opencode 项目级 config(写入隔离 cwd 的 ``opencode.json``)。

    全 V2 字段(官方文档明确:``prompt``/``tools``/``permission``/``maxSteps`` 等 V1
    字段在新 V2 agent 配置中禁用,应使用 ``system`` + ``permissions`` 规则列表):

    - ``system``:系统提示(V2 的 ``system`` 替代 V1 的 ``prompt``);
    - ``permissions`` deny-all 规则(``{action:"*", resource:"*", effect:"deny"}``,
      last-match-wins,一条即封全部工具动作)——结构性禁工具第一道闸;
    - ``steps: 1`` 第二道闸:末步 OpenCode 直接**移除工具**、要求模型纯文本作答。

    实证(2026-11-20 本机 2.0.24,工具诱导探针「README.md 存在吗?请用 bash ls 确认」):
    模型自述「已达到本代理的最大步骤数,工具已被禁用,必须仅以文本回复」,事件流无任何
    tool 事件、单步直答、诚实报告无法核实 —— 结构性禁工具是已验证事实,非指令层自觉。
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "agents": {
            JUDGE_AGENT_NAME: {
                "system": _JUDGE_AGENT_PROMPT,
                "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
                "steps": 1,
            }
        },
    }


def _parse_stream(stdout: str) -> tuple[str, dict, float, str, str]:
    """``--format json`` NDJSON 流 → ``(text, tokens, cost, stop_reason, error)``。

    - ``text``:按 part id 收末态(同一 part 的事件是增量快照、后者覆盖前者),
      多 part 按首现顺序直拼(同一 assistant 消息的连续分段)。
    - ``tokens``:取**末个** ``step_finish``(单步 agent 一步;多步时步间 input
      含缓存重复计入,求和会虚高 —— 末步最接近会话总量,与 ``OpenCodeRunner``
      同一取舍)。
    - ``cost``:逐 ``step_finish`` **累加**。与 ``tokens`` 取末步**有意不对称**——
      token 计数跨 step 求和会重复计入步间缓存的 input(虚高),而每个 step 是独立
      计费单元、cost 只有累加才是全程真实花费,二者各按自己的正确口径取。
    - ``error``:顶层 ``error`` 事件(非 ``part``)—— provider/传输失败。
    """
    parts: dict[str, str] = {}
    order: list[str] = []
    tokens: dict = {}
    cost = 0.0
    stop_reason = ""
    error = ""
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # 非 JSON 噪声行(版本横幅等)跳过
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "text":
            part = event.get("part") or {}
            text = part.get("text")
            if not isinstance(text, str):
                continue
            part_id = part.get("id")
            if not isinstance(part_id, str):
                part_id = f"_anon_{len(order)}"
            if part_id not in parts:
                order.append(part_id)
            parts[part_id] = text
        elif event_type == "step_finish":
            part = event.get("part") or {}
            step_tokens = part.get("tokens")
            if isinstance(step_tokens, dict):
                tokens = step_tokens
            if isinstance(part.get("cost"), (int, float)):
                cost += part["cost"]
            if part.get("reason"):
                stop_reason = str(part["reason"])
        elif event_type == "error":
            payload = event.get("error")
            if isinstance(payload, dict):
                error = str(payload.get("message") or payload)
            else:
                error = str(payload)
    text = "".join(parts[pid] for pid in order)
    return text, tokens, cost, stop_reason, error


def complete(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int = 120,
    on_progress: Callable[[str], None] | None = None,
) -> OpencodeCompletion:
    """跑一次判官补全:standalone + 隔离 XDG + 无工具 agent,返回最终文本。

    ``model`` 含 ``/`` 才传 ``-m``(opencode 只认 ``provider/model#variant`` 形态;
    裸名走 opencode config 默认模型)。失败(CLI 缺失 / 超时 / 非零退出 /
    error 事件 / 零文本)一律 raise RuntimeError,交调用方重试链处置。
    """
    if shutil.which("opencode") is None:
        # TransportUnavailable(静态缺位,不重试):作为 cc 的兜底目标,自己的缺位同样
        # 立即失败 —— 重试一个不存在的二进制毫无意义。RuntimeError 基类保持既有
        # except RuntimeError 降级路径兼容。
        raise TransportUnavailable(
            "opencode CLI 未找到;SIFT_LLM_TRANSPORT=opencode 需先装 opencode 并确保在 PATH(https://opencode.ai)"
        )
    ver = opencode_version()
    if ver[0] < 2:
        # 版本门(同 TransportUnavailable 语义:环境事实,重试无意义)。判官 transport
        # 的全部形态(--standalone 私有 server、agent V2 定义、2.x 事件流)都是 2.x
        # 语义;1.x 无对应物,分支=重写,故声明最低支持并如实报。
        raise TransportUnavailable(
            f"opencode 判官 transport 需 opencode ≥ 2.0(当前 {'.'.join(map(str, ver))});"
            "1.x 无 --standalone/私有 server 与 agent V2 定义。可设 SIFT_LLM_TRANSPORT=cc/http/dsh 换判官引擎"
        )
    tmp = Path(tempfile.mkdtemp(prefix="sift-opencode-llm-"))
    try:
        # 单临时目录三合一:cwd(放 opencode.json 的项目级 agent 定义)= XDG_DATA_HOME
        # (opencode/opencode.db 会话落点 + auth.json seed)。
        (tmp / "opencode").mkdir(parents=True, exist_ok=True)
        (tmp / "opencode.json").write_text(
            json.dumps(judge_agent_config(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        seed_auth(tmp / "opencode" / "auth.json")

        args = [
            "opencode",
            "run",
            "--standalone",
            "--format",
            "json",
            "--agent",
            JUDGE_AGENT_NAME,
        ]
        if model and "/" in model:
            args.extend(["-m", model])
        env = {
            **claude_cli_env(),
            "XDG_DATA_HOME": str(tmp),
            # PWD 必须重写为隔离 cwd:opencode server 用 env 的 PWD(不是进程 cwd)做
            # 项目/agent 发现 —— os.environ 里残留的 PWD 会把 project 解析到 sift 自己的
            # 仓(spike 实证:残留 PWD → "Agent not found";PWD=cwd → 正常)。
            "PWD": str(tmp),
        }
        env.pop("OLDPWD", None)
        try:
            stdout = spawn_cli(args, prompt, timeout, env=env, cwd=str(tmp))
        except RuntimeError as exc:
            # 诊断增强(仅未指定 model 时):opencode 的默认模型由其配置决定 —— 机器
            # 没配默认模型(顶层 "model" 字段)时,opencode 内置默认(anthropic 家族)
            # 会被继承的 ANTHROPIC_* 劫持到错误端点/模型名 → "Model is unavailable"。
            # 该提示把「配默认模型或显式传模型」的修法带到错误现场,防operator对着
            # 上游 400 干瞪眼。关键词圈定在模型/凭据类失败,超时等瞬态不加噪声。
            if model is None and any(
                kw in str(exc).lower() for kw in ("model is unavailable", "no authentication", "provider")
            ):
                raise RuntimeError(
                    f"{exc}\n(未指定 model 时走 opencode 配置的默认模型;在 opencode 配置顶层设"
                    ' "model" 字段(如 "gateway/glm-5.3"),或调用侧显式传 provider/model)'
                ) from exc
            raise

        text, tokens, cost, stop_reason, error = _parse_stream(stdout)
        if error:
            raise RuntimeError(f"opencode transport 失败(error 事件): {error[:300]}")
        if not text.strip():
            raise RuntimeError("opencode transport returned empty final text")
        return OpencodeCompletion(
            text=text,
            input_tokens=int(tokens.get("input") or 0),
            output_tokens=int(tokens.get("output") or 0),
            cost_usd=float(cost),
            stop_reason=stop_reason,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
