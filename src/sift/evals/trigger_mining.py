"""从真实会话 transcript 挖 trigger 负例(trace-mined should-not 探题)。

生成负例的唯一信号源是 description 本身——出的题系统性偏「出题人想象」,不是真实
query 分布;领域内真实会话里「没触发该 skill 的 query」是最高价值的 near-miss 负例
(= --from-transcript「1 真挖」的同款逻辑,搬到触发维)。两层设计:

**事实层**(零 LLM,``mine_untriggered_queries``):收「本 skill 未触发的真实用户
query」候选。per-query 窗口判定:query 之后到下一条用户 query 之间无本 skill 的
Skill tool_use → 未触发。窗口而非全 transcript 判定:挖题给的 transcript 常是本
skill 跑过的会话,全局口径会把所有 query 全排除;窗口才能从中收「没触发本 skill
的那些 query」(skill 名匹配口径同 ``trigger_runner`` 的 fired:剥命名空间前缀后
小写比对)。query 卫生:跳过空/过短、slash 命令包装、点名本 skill 的 query
(点名 = 该触发语义,收进 should-not 必假 FAIL)。

**语义层**(``filter_near_miss``,LLM 过滤「领域相邻」)——注意力安全三条(对齐
critique 正文/描述拆两调用的先例):a) 只喂 description 不喂 skill 正文(触发本来
就只看 description,喂正文等于泄题);b) 分批 ≤``NEAR_MISS_BATCH``、每条独立
结构化 verdict(禁止整批一段话);c) 单条输入天然小。域外未触发 → 丢弃(域外
本来就不该触发,不构成对 description 的压力测试);LLM 失败 → 该批全
``unverified``(宁少收不乱收的人审兜底版:事实层已保证「未触发」事实,语义层只做
价值过滤,失败降级为「人审可删」而非丢弃)。

运行时 ``fired`` 判定零 LLM(``trigger_runner``:被测 skill 名逐字匹配
invoked_skills),考题多少不影响判定质量——本模块只影响出题。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable

from sift.evals.transcript import _records_from_cc

# 事实层 query 卫生阈值:< 此字符数的 query 视为碎片("ok" / "继续"),不值得收。
MINED_QUERY_MIN_CHARS = 4

# 语义层分批大小:每批 ≤ 此条喂 LLM 过滤(注意力安全:单条独立 verdict,输入小)。
NEAR_MISS_BATCH = 10


# ---------------------------------------------------------------------------
# 事实层(零 LLM)
# ---------------------------------------------------------------------------


def _user_text(rec: dict) -> str:
    """record 的真实用户文本(非 tool_result / 系统注入);无 → 空。

    同 ``transcript._digest_from_records`` 的 user_turns 口径:role=user + 纯字符串
    或 text 块(tool_result 块无 text → 自然跳过),跳 ``<system-reminder>`` 与
    caveat 包装。抽成本模块私有 helper 是因为窗口判定需要「逐 record 交错读用户
    文本与 Skill 调用」,digest 的 user_turns 列表丢失了这个交错关系。
    """
    msg = rec.get("message") or {}
    if rec.get("parent_tool_use_id") or msg.get("role") != "user":
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        t = content.strip()
    elif isinstance(content, list):
        t = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
    else:
        return ""
    if not t or t.startswith("<system-reminder>") or "Caveat: The messages below" in t:
        return ""
    return t


def _skill_fired(rec: dict, name_norm: str) -> bool:
    """assistant record 是否含本 skill 的 Skill tool_use(剥命名空间前缀、小写比对)。"""
    msg = rec.get("message") or {}
    if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
        return False
    for b in msg["content"]:
        if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill"):
            continue
        full = (b.get("input") or {}).get("skill") or ""
        if full.rsplit(":", 1)[-1].strip().lower() == name_norm:
            return True
    return False


def mine_untriggered_queries(transcript_path: Path, skill_name: str) -> list[str]:
    """事实层(零 LLM):收「本 skill 未触发的真实用户 query」候选(保序去重)。

    见模块 docstring 的窗口判定 + query 卫生说明。返回去重后的候选原文(语义层
    过滤的输入);transcript 不存在 / 无候选 → []。
    """
    path = Path(transcript_path)
    if not path.exists():
        return []
    records = _records_from_cc(path)
    name_norm = (skill_name or "").strip().lower()
    if not name_norm:
        return []
    candidates: list[str] = []
    seen: set[str] = set()

    def _flush(pending: str, fired: bool) -> None:
        # 同文重复提问只收一份(与既有 trigger_probes 的跨源去重同键不同层:
        # 这里管 transcript 内,probe_dedup_key 管 evals.json 合并期)。
        if pending and not fired and pending not in seen:
            seen.add(pending)
            candidates.append(pending)

    pending, fired = "", False
    for rec in records:
        t = _user_text(rec)
        if t:
            _flush(pending, fired)
            q = t.strip()
            is_command = q.startswith(("/", "<command-name>", "<local-command"))
            if len(q) >= MINED_QUERY_MIN_CHARS and not is_command and name_norm not in q.lower():
                pending, fired = q, False
            else:
                pending, fired = "", False  # 卫生不过关:占位防误归属,不收
            continue
        if not (_skill_fired(rec, name_norm)):
            continue
        fired = True
    _flush(pending, fired)
    return candidates


def probe_dedup_key(prompt: str) -> str:
    """trigger 探题归一去重键(prompt lower+strip 精确匹配;语义去重不做——量小人审兜底)。"""
    return (prompt or "").strip().lower()


# ---------------------------------------------------------------------------
# 语义层(LLM 过滤「领域相邻」)
# ---------------------------------------------------------------------------


def _build_near_miss_prompt(description: str, batch: list[str]) -> str:
    lines = "\n".join(f"{i}. {q}" for i, q in enumerate(batch, 1))
    return f"""You are filtering user queries mined from a real coding-session transcript.

Skill description (the ONLY thing the model sees when deciding to trigger):
{description}

Below are real user queries from sessions where this skill was available but did NOT
fire for that query. For each query, judge independently:
- "near_miss": plausibly related to this skill's domain (same tools / topics / file
  types / workflows) — a human might expect this skill to handle it. These are valuable
  false-trigger test probes.
- "unrelated": clearly outside the skill's domain — the skill should obviously not fire,
  so the query is not a meaningful test of the description.

Base your judgment ONLY on the description above (not on what a full skill body might
allow). Judge each query on its own; do not let one query's verdict influence another.

Queries:
{lines}

Output JSON ONLY:
{{"verdicts": [
  {{"index": 1, "verdict": "near_miss", "reason": "..."}},
  {{"index": 2, "verdict": "unrelated", "reason": "..."}}
]}}

## Output JSON
"""


def _parse_near_miss_response(response: str, batch_size: int) -> dict[int, tuple[str, str]]:
    """两段式解析(同 ``generator._parse_trigger_response``):直接 ``json.loads`` 失败 →
    正则抽 ``{...}``。

    返回 ``{0-based index: (verdict, reason)}``;只收 ``near_miss``/``unrelated`` 两种
    verdict、index 在批内合法的条目(畸形条目跳过,不影响其余;缺失 index 由调用方
    按 unverified 处理)。整体解析不出 → {}。
    """
    try:
        data = json.loads(response)
    except Exception:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group())
        except Exception:
            return {}
    if not isinstance(data, dict):
        # 合法 JSON 但非对象(裸数组/字符串)——不守卫会在 .get 上 AttributeError 炸穿
        # filter_near_miss 的 except RuntimeError(init 顶层只捕 SiftError)。
        return {}
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, list):
        # 键在但值非数组(裸 int/bool/字符串)——for 取迭代器这步就 TypeError,
        # 循环体内的条目守卫救不到;同属解析失败 → {}(该批降 unverified)。
        return {}
    out: dict[int, tuple[str, str]] = {}
    for item in verdicts:
        if not isinstance(item, dict):
            # 条目非对象(裸字符串/数字——截断或输出漂移的形态):跳过该条不影响
            # 其余;不守卫则 item.get 抛 AttributeError 穿透调用链(同 data 层
            # 非 dict 守卫的对位,item 层)。
            continue
        try:
            idx = int(item.get("index", 0)) - 1
            verdict = str(item.get("verdict", ""))
            if 0 <= idx < batch_size and verdict in ("near_miss", "unrelated"):
                out[idx] = (verdict, str(item.get("reason", "")))
        except (TypeError, ValueError):
            logging.getLogger(__name__).debug("Optional operation failed; continuing with fallback", exc_info=True)
            continue
    return out


def filter_near_miss(
    candidates: list[str],
    description: str,
    llm_call: Callable[[str], str],
) -> list[tuple[str, str]]:
    """语义层:逐条判「领域相邻」→ ``[(verdict, reason), ...]`` 与 candidates 对齐。

    verdict ∈ near_miss(收)/ unrelated(丢)/ unverified(LLM 失败或该条缺失——收,
    人审兜底)。分批 ≤``NEAR_MISS_BATCH``;单次调用失败该批全 unverified(不重试:
    unverified 可见可重跑,不静默丢真题)。``llm_call`` 失败形态约定同
    ``EvalInitGenerator._llm_call``(RuntimeError)。
    """
    verdicts: list[tuple[str, str]] = []
    for i in range(0, len(candidates), NEAR_MISS_BATCH):
        batch = candidates[i: i + NEAR_MISS_BATCH]
        try:
            parsed = _parse_near_miss_response(llm_call(_build_near_miss_prompt(description, batch)), len(batch))
        except RuntimeError:
            parsed = {}
        for j in range(len(batch)):
            if j in parsed:
                verdicts.append(parsed[j])
            else:
                verdicts.append(("unverified", "LLM 过滤失败或该条缺失,仅事实层(未触发)成立"))
    return verdicts
