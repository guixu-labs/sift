"""audit 命令的输出模型。

audit(transcript 对账)把若干 transcript 的实测行为与 skill 声明的指令逐条对照,
产出 AuditReport(若干 AuditFinding)。供后续 auditor 使用。

判定模型(2026-07-20 重构):每条 finding 有两个**正交**维度——

- **verdict(结果)**:五态。
  - ``PASS`` / ``FAIL``:已判明通过 / 违规。
  - ``NA``:不适用——规则存在但本次 transcript 场景不触发(已解决)。
  - ``UNRESOLVED``:没判成——LLM 调用 / 解析失败 / 未提及 / 畸形值,操作性失败,
    **可重跑**救回。
  - ``INDETERMINATE``:判不准——LLM 看了证据但真模糊(证据相互矛盾 / 关键缺失),
    给不出 pass/fail/na,**重跑无益**(附 indeterminate_reason)。
  没判成 vs 判不准:前者是"没拿到判定"(operational,建议重跑),后者是"拿到了判定、
  判定本身是'模糊'"(semantic,只能人工复核)。

- **method(来源/方法)**:两态—— ``PROGRAMMATIC``(程序对账:tool_trace 命中 / 子串匹配,
  不调 LLM)/ ``LLM``(LLM 给出结论,含 UNRESOLVED / INDETERMINATE:LLM 路径尝试过)。
  ``method`` 只回答"怎么查的",与"结果"正交:PASS/FAIL 既可程序判也可 LLM 判,
  没判成 / 判不准的 method 都是 LLM(那条路径产出的)。

旧版用单一 ``confidence`` 浮点(1.0/0.8/0.3)把"方法 + 是否判不准"揉成一个假浮点,
既不可叠加又难解释,已废弃。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from sift.models.provenance_audit_models import ProvenanceAuditResult


class AuditVerdict(str, Enum):
    """单条指令的对账结论(五态)。

    PASS / FAIL / NA 是已解决结论;UNRESOLVED / INDETERMINATE 是未解决(本次结果
    不完整),audit() 据二者汇总不同警告。
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NA = "N/A"  # 不适用:规则存在但本次 transcript 场景不触发(applicable=false / STEP-"na"),已解决
    UNRESOLVED = "UNRESOLVED"  # 没判成:LLM 调用/解析失败/未提及/畸形值,操作性失败,可重跑
    INDETERMINATE = "INDETERMINATE"  # 判不准:LLM 看了证据但真模糊,给不出结论,重跑无益(附 reason)


class AuditMethod(str, Enum):
    """判定方法(怎么查的)——与 verdict(结果)正交。

    PROGRAMMATIC:程序对账(tool_trace 命中 / 输出子串匹配),确定性、不调 LLM。
    LLM:LLM 给出结论(含没判成 / 判不准:LLM 路径尝试过但未出明确 pass/fail/na)。
    """

    PROGRAMMATIC = "PROGRAMMATIC"
    LLM = "LLM"


class AuditCategory(str, Enum):
    """指令类别(对应 InstructionSource 的语义分组)。

    conditional: 条件性(有触发条件:TOOL_REQUIREMENT / REFERENCE_USAGE)
    prohibition: 禁令类(CONSTRAINT 禁止性 / ANTI_FABRICATION)
    step:        HEADING_STEP
    output:      OUTPUT_FORMAT
    """

    CONDITIONAL = "conditional"
    PROHIBITION = "prohibition"
    STEP = "step"
    OUTPUT = "output"


class AuditFinding(BaseModel):
    """单条指令的对账结果。

    ``source`` 用 str 解耦,不直接依赖 batch_models 的 InstructionSource 枚举——
    这样 audit 模型即便 InstructionSource 扩展也无需改动。注意 ``source`` 是**指令
    的规则类型**(CONSTRAINT / STEP …),与 ``method``(判定方法)是两个不同维度。

    向后兼容:旧 JSON 用 ``verdict="UNCLEAR"`` + ``confidence`` 浮点(假浮点已废)。
    ``_legacy_normalize`` 在 validate 前把旧 ``UNCLEAR`` 折成 ``UNRESOLVED``,
    并据旧 ``confidence`` 推断 ``method``(>=1.0 → PROGRAMMATIC,否则 LLM),
    使旧 run 产物仍可加载。
    """

    instruction_text: str
    # 该指令在声明原文里所属 bullet 的完整文本(含主语上下文)。显示层用它补全被切的主语;
    # **判定不读它**(judge 吃 instruction_text)。透传自 InstructionCheck.source_excerpt,
    # 缺省空(合成文案 / derived / 旧 JSON)。加在 finding 上供下游(insight)渲染。
    source_excerpt: str = ""
    # source_excerpt 的「人话标题」:LLM 去噪+补主语+压一句的显示标题。判定不读,缺省空回退。
    headline: str = ""
    # 声明分组标题:透传自 InstructionCheck(source_section or section——body 级指令回填的
    # 真实标题,缺省回退伪节名 Constraint/Anti-Fabrication/References,或标题级 extractor 的
    # 真标题 / "transcript-derived")。覆盖视图按它分组;判定不读。缺省空(旧 JSON / 合成文案)。
    section: str = ""
    source: str  # InstructionSource 的值(如 "CONSTRAINT")——指令的规则类型
    verdict: AuditVerdict
    method: AuditMethod  # 判定方法(PROGRAMMATIC / LLM)——怎么查的
    category: AuditCategory
    evidence: str  # 中文,为何这个 verdict(INDETERMINATE 时此即 indeterminate_reason)
    transcript: str  # 该 finding 对应的 transcript 文件名
    derived_from_transcript: bool = False  # 反推声明来的指令标 True
    # 审计器**合成**的关系声明标 True(ORDER:“(顺序)「A」须先于「B」”)——既非
    # 声明原文指令、也非 transcript 反推,复用 derived 语义会污染 header 的反推计数。
    # 下游口径:coverage / by_instruction 不进分母(合成指令不是声明指令)、link_related
    # 跳过(不进 token 索引,related 由 audit() 按 pair 两端回填)、噪声判别豁免(不标
    # noise_label——「LLM+related」启发式在 ORDER 上语义是 pair 两端不是稳定性存疑)。
    # summary 照常计入(真实遵循面)。旧 JSON → False(向后兼容)。
    synthetic: bool = False
    # 控制流判官标记(纯显示):"quantity"=数量指令(带数量词,移交控制流判官判——程序计数
    # 或 LLM 数量判定)。数量 finding 按设计沿用原指令原文/category(对聚合透明),没有任何
    # 自明记号,报告里认不出来(实测连作者都要翻 evidence 找);此字段让 CLI/HTML 给它挂
    # 「数量」记号。ORDER 不设——synthetic=True + 「(顺序)」文案已自明。判定/聚合/缓存
    # 键都不读;旧 JSON → ""(向后兼容,渲染端不显)。
    control_flow: str = ""
    # findings 后处理(audit.dedup.post_process_findings)填:
    seq: int = 0  # #1..#N:去重后编排的序号(供 related 引用 + 报告里 #N 可读定位)
    sources: list[str] = Field(default_factory=list)  # 同文去重后合并的所有来源;单条=[source]
    related: list[str] = Field(default_factory=list)  # 同行为跨类(Case B)关联的其它 finding 序号,如 ["#9"]
    conflict_with: list[str] = Field(default_factory=list)  # 同源矛盾(Case C)对端序号:同 source_excerpt 声明行被判出 PASS 与 FAIL,互指对端;无矛盾留空
    # FAIL 噪声判别(audit.noise.annotate_noise)填,只对 FAIL 标;None/[] 向后兼容旧 JSON
    noise_label: str | None = None  # "tool_noise" / "skill_issue" / "uncertain";非 FAIL 为 None
    noise_reasons: list[str] = Field(default_factory=list)  # 命中理由(中文);非 FAIL 为空
    # finding 指向的 turn 序号(对应 transcript 里 [Tn] 标记)。
    # 仅 prohibition / step 类(走 transcript_text + turn 引用的 LLM judge)在 FAIL / INDETERMINATE
    # 时由 LLM 填;conditional / output 类的 FAIL 是段级缺席(trace 全程未调用 / 未读取 X、期望格式
    # 全程未出现),本就无具体 turn、恒空。PASS / N/A 或其它段级缺席亦为空。
    # 序号语义:cc / structured 切片保留会话全局序号;opencode 切片经 message 子集往返重建、按段内
    # 位置 re-stamp,seq 是段内 local(非会话全局)。旧 JSON 无此字段 → 空(向后兼容)。
    turn_refs: list[int] = Field(default_factory=list)
    # 失败根因分类(纯诊断,第二期):UNRESOLVED finding 才设,标"为什么没判成"。
    # timeout/rate_limited/parse_failed=基础设施(可重跑);unmentioned=语义(transcript 未覆盖,
    # 重跑可能无益);judge_error=判定器自身崩溃。其余 verdict 恒 None。旧 JSON → None(向后兼容)。
    unresolved_cause: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_normalize(cls, data: object) -> object:
        """旧 JSON 兼容:UNCLEAR→UNRESOLVED,confidence→method。

        旧版 verdict ``UNCLEAR`` 现拆成 ``UNRESOLVED``(没判成)/ ``INDETERMINATE``(判不准)。
        旧 ``UNCLEAR`` 实际只装"没判成"(LLM 失败/解析失败/未提及),故折成 ``UNRESOLVED``。
        旧 ``confidence`` 浮点(假浮点)废:>=1.0 是程序判 → PROGRAMMATIC,其余 LLM。
        新数据有 ``method`` 则不动。纯加载侧兼容,不影响新产出。**拷贝后改**,不动调用方 dict。
        """
        if isinstance(data, dict):
            data = dict(data)
            if data.get("verdict") == "UNCLEAR":
                data["verdict"] = "UNRESOLVED"
            if "method" not in data and "confidence" in data:
                try:
                    data["method"] = (
                        AuditMethod.PROGRAMMATIC if float(data["confidence"]) >= 1.0 else AuditMethod.LLM
                    )
                except (TypeError, ValueError):
                    data["method"] = AuditMethod.LLM
        return data


class InstructionAggregate(BaseModel):
    """单条指令跨 transcript 的对账聚合(见 audit.aggregate.aggregate_by_instruction)。"""

    instruction_text: str
    # 该指令所属 bullet 的完整原文(含主语);透传自 AuditFinding,显示用、判定不读。缺省空。
    source_excerpt: str = ""
    headline: str = ""  # 人话标题,透传自 AuditFinding,显示用、判定不读。缺省空。
    # 声明分组标题,透传自 AuditFinding(finding.section,同批同文 first-wins);缺省空兼容旧 report。
    section: str = ""
    # 控制流纯显示标记透传("quantity":带数量词指令移交控制流判官判;first-wins 同上)。
    # 概要表指令前挂「数量」徽章用——数量 finding 沿用原指令原文,聚合行不带头认不出;判定不读。
    control_flow: str = ""
    source: str
    category: AuditCategory
    pass_count: int
    fail_count: int
    na_count: int  # 不适用(applicable=false / STEP-"na")
    unresolved_count: int = 0  # 没判成(LLM 调用/解析失败/未提及),操作性失败、可重跑
    indeterminate_count: int = 0  # 判不准(LLM 看了证据但真模糊),semantic、重跑无益
    total: int
    fail_rate: float  # fail_count / total(total=0 时 0.0)
    fail_in_transcripts: list[str]  # 哪些 transcript 里判 FAIL(定位用)
    na_in_transcripts: list[str] = Field(default_factory=list)  # 哪些 transcript 判 N/A(场景不适用,定位用);默认空兼容旧 report
    fail_seqs: list[int] = Field(default_factory=list)  # FAIL findings 的明细 #N(汇总表交叉引用到明细用)
    na_seqs: list[int] = Field(default_factory=list)  # N/A findings 的明细 #N
    # FAIL 噪声判别(audit.noise.annotate_aggregate_noise)填,只对 fail_count>0 标;None/[] 向后兼容
    noise_label: str | None = None  # "tool_noise" / "uncertain";无 FAIL 为 None
    noise_reasons: list[str] = Field(default_factory=list)


class InstructionCoverage(BaseModel):
    """单条声明指令的覆盖状态(audit.aggregate.aggregate_coverage 产;覆盖视图行)。

    ``status`` 三态(优先级:covered > uncovered > unknown):
    - ``covered``:任一 transcript PASS 或 FAIL——会话碰过这条规则(守没守看 by_instruction);
    - ``uncovered``:全部 transcript 都 N/A——规则存在、场景从未触发;
    - ``unknown``:其余(N/A+UNRESOLVED 混合 / 纯 UNRESOLVED / 纯 INDETERMINATE)——说不清。
    """

    instruction_text: str
    source_excerpt: str = ""
    headline: str = ""
    source: str
    category: AuditCategory
    section: str = ""  # 分组键(= finding.section,first-wins)
    status: str  # "covered" | "uncovered" | "unknown"
    pass_count: int = 0
    fail_count: int = 0
    na_count: int = 0
    unresolved_count: int = 0
    indeterminate_count: int = 0
    total: int = 0
    seqs: list[int] = Field(default_factory=list)  # 该指令全部 finding 的 #N(交叉引用 ④ 明细)


class SectionCoverage(BaseModel):
    """单个声明分组的覆盖聚合(覆盖视图的一个 <details> 组)。"""

    section: str  # 原始键:真标题 / 伪节名(Constraint 等)/ "Root" / ""
    total: int = 0
    covered: int = 0
    uncovered: int = 0
    unknown: int = 0
    coverage_rate: float = 0.0  # covered/total;total==0 → 0.0
    instructions: list[InstructionCoverage] = Field(default_factory=list)


class CoverageSummary(BaseModel):
    """声明覆盖视图总聚合。coverage_rate = covered/total,unknown 计入分母、单列——诚实低估。"""

    total: int = 0
    covered: int = 0
    uncovered: int = 0
    unknown: int = 0
    coverage_rate: float = 0.0
    sections: list[SectionCoverage] = Field(default_factory=list)


class ToolUsageEntry(BaseModel):
    """越界视图单个领域工具的对账行(audit.boundary.aggregate_boundary 产)。

    ``classification`` 三态:``builtin`` 不产行(不构成越界信号);``domain`` = 内置
    表外 + 命中声明工具要求;``undeclared-domain`` = 内置表外 + 未命中(视图主体,
    信息性标注、**不判 FAIL**)。
    """

    name: str                     # 规范名(首次观测到的原样形态;cc 大写开头 / opencode 归一形)
    classification: str           # "domain" | "undeclared-domain"
    declared_by: str = ""         # 命中的声明指令显示文本(headline/instruction_text;未命中空)
    sessions: int = 0             # 出现于几个 transcript
    calls: int = 0                # 总调用次数
    transcripts: list[str] = Field(default_factory=list)  # 文件名(交叉定位)


class BoundarySummary(BaseModel):
    """越界视图(一期:领域工具对账)总聚合。纯信息性——不产 finding、不进 verdict。

    ``total_tools`` = builtin 外全部(分母);排序 undeclared-domain 先(sessions 降序)
    再 domain(calls 降序)。空输入全 0;旧 report 无此字段 → 全默认(渲染端不显)。
    """

    total_tools: int = 0
    declared: int = 0
    undeclared: int = 0
    tools: list[ToolUsageEntry] = Field(default_factory=list)


class AuditSuggestion(BaseModel):
    """一条改进建议(audit --suggest 产,可选视图;诊断产物、不进任何 gate)。

    粒度 = 逐指令簇:一条建议对应一条 by_instruction 指令(或一条合成 finding),
    ``target_seqs`` 列其全部 FAIL 的 finding #N(渲染复用 seq-link → #finding-N)。
    """

    category: str            # skill_fix(改 skill 正文) / declaration_fix(改声明)
    target_seqs: list[int]   # 关联 FAIL finding 的 #N(可点跳)
    suggestion: str          # 可直接照做的修改建议(含改哪段、怎么改)
    rationale: str           # 归因理由(为何属这类)
    keeps_pass: bool         # 自检:建议改的段落不与任何 PASS 指令撞车(可查面有限,见 note)
    keeps_pass_note: str = ""


class AuditSuggestResult(BaseModel):
    """--suggest 一次运行的产物(CLI 层后处理,引擎不感知)。

    ``skipped_reason`` 只有两个来源(UNRESOLVED 过高 / 失败)——**无 FAIL 是正常终态**,
    整个字段不落(None),不产空 result(同 trigger 无失败不留产物);渲染端
    skipped_reason 非空 → 只显原因行、不显计数(失败 ≠ 全怪模型)。
    """

    suggestions: list[AuditSuggestion] = Field(default_factory=list)
    model_env_count: int = 0   # 归因 model_env 的 FAIL 条数 = Σ target_seqs 去重,与文案单位一致
    dropped_count: int = 0     # wire 不合规整条丢弃计数(fix 类缺 suggestion / target_seqs 全无效 / 整批解析失败)
    skipped_reason: str = ""   # 未跑/失败原因(UNRESOLVED 过高 / LLM 失败);空 = 跑成
    tokens: int = 0
    # 消耗分项(CLI 把 suggest 行并入报告阶段表用;旧 JSON 默认 0):in/out 拆分 +
    # 实际响应次数(含截断/升档重试,与判官阶段 calls 同口径)。tokens = in + out 之和不变。
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    duration_seconds: float = 0.0


class AuditReport(BaseModel):
    """一个 skill 一次 audit 的完整报告。"""

    skill_name: str
    transcripts: list[str]
    transcript_sources: list[str] = Field(default_factory=list)  # 与 transcripts 同序并行;"cc"/"opencode"/...(未来扩)。标每个 transcript 来自哪侧;旧 report 无此字段 → 空(向后兼容)
    summary: dict  # {"total": N, "pass": X, "fail": Y, "na": Z(不适用), "unresolved": U(没判成), "indeterminate": I(判不准)}
    findings: list[AuditFinding]
    # 多 transcript 对账的指令级聚合(按 fail_rate 降序);单 transcript 也有(每指令 1 verdict)。
    by_instruction: list[InstructionAggregate] = Field(default_factory=list)
    # 声明覆盖视图(aggregate.aggregate_coverage 产,纯 post-judge 元数据、不进任何 gate):
    # 声明指令被本次 transcript(s) 触达多少,按声明分组。covered=任一 transcript PASS/FAIL;
    # uncovered=全 N/A(规则在、场景没触发);unknown=只有没判成/判不准。反推(derived)指令
    # 不计(不是声明规则、构造上偏 PASS)。旧 report 无此字段 → 全默认(total 0 → 渲染端不显)。
    coverage: CoverageSummary = Field(default_factory=CoverageSummary)
    # 越界视图(audit.boundary.aggregate_boundary 产,一期纯信息性、不进任何 gate):
    # 轨迹里的领域工具动作 × 声明工具要求 对账,未声明的列出来供人看。builtin 不进
    # 分母;undeclared ≠ 违规(skill 声明通常不穷举工具),是二期行为基线的数据采集面。
    # 旧 report 无此字段 → 全默认(total_tools 0 → 渲染端不显)。
    boundary: BoundarySummary = Field(default_factory=BoundarySummary)
    # 文件归因分析结果，按 transcript 文件名保存。该区块独立于 findings，
    # 不参与声明 coverage、PASS/FAIL 汇总或 exit code；没有 v2 证据时为空。
    provenance: dict[str, ProvenanceAuditResult] = Field(default_factory=dict)
    # 改进建议(audit.suggest.build_suggestions 产,--suggest 可选、默认关):对 FAIL
    # findings 做三类归因并起草改 skill/改声明建议;model_env 归因只透明计数不出建议。
    # **CLI 层后处理,引擎零感知**(run 内 compliance 不产)。None = 没跑(默认关 /
    # 无 FAIL 正常终态不落盘)——与 coverage/boundary 的全默认不同,可选产物不跑就没有;
    # 旧 report 无此字段 → None(向后兼容)。
    suggestions: AuditSuggestResult | None = None
    warnings: list[str] = Field(default_factory=list)
    # refine(LLM 精筛伪指令)砍掉的条数(len(before) - len(after))。
    # 0 = 没开 refine_instructions 或开了但没砍任何条;向后兼容旧 report(默认 0)。
    refine_dropped_count: int = 0
    # 本次 audit 所有 LLM 判定调用(refine + 条件/禁令/步骤判官 × 批)的 token 用量合计。
    # call_claude json 模式抓 usage,经 TokenUsageAccumulator 累加:{input_tokens,
    # output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
    # total_cost_usd, calls}。空 dict = 无 LLM 调用或旧 report(默认空,向后兼容)。
    token_usage: dict = Field(default_factory=dict)
    duration_seconds: float = 0.0  # Phase 2.5: audit 墙钟(audit() 头尾 perf_counter;判官 token 在 token_usage)
    # 各阶段(extract/refine/headline/rebuild/judge/postprocess)的耗时 + token,诊断慢在哪。
    # 每元素 {stage, seconds, input_tokens, output_tokens, calls, note};token 仅 LLM 阶段非 0。
    # note 仅 refine/headline 在 calls=0 时有值:"缓存命中"(开关开、跑了秒回)/"未启用"(开关关);
    # 其余阶段 calls=0 是本地程序常态,note 为空。
    # 各阶段 seconds 之和 ≈ duration_seconds(rebuild/judge 在 transcript 循环里累加)。空 = 旧 report。
    stage_breakdown: list[dict] = Field(default_factory=list)
    # UNRESOLVED findings 按 unresolved_cause 的聚合计数(cause → count),诊断"没判成"的根因分布。
    # 空 = 无 UNRESOLVED 或旧 report(默认空,向后兼容)。纯诊断,不进任何 gate/判决。
    unresolved_breakdown: dict[str, int] = Field(default_factory=dict)
