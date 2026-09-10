# Sift 参考手册

本文档包含 Sift 的完整技术细节（命令参数、JSON schema、字段定义）。Sift 干两件事——**评测**（skill / agent 的能力）和 **审计**（skill / agent / 编排者的执行）：一面回答「能力行不行」，一面回答「过程照声明走没走」。评测主入口是 `eval`、真任务是 `run`、审计主入口是 `audit`（见使用手册）。为什么这么设计、怎么用、怎么看结果讲透见[使用手册](sift-handbook.md)；本档负责查具体参数 / 字段时回查。

---


## 目录

- [CLI 命令详细参数](#cli-命令详细参数)
  - [vet — 安全审查](#vet--安全审查)
  - [init — 初始化评测定义](#init--初始化评测定义)
  - [add — 追加评测用例](#add--追加评测用例)
  - [analyze — 静态分析](#analyze--静态分析)
  - [inspect — 题集体检](#inspect--题集体检)
  - [freeze — 冻结基线](#freeze--冻结基线)
  - [eval — A/B 评测](#eval--ab-评测)
  - [run — 真任务](#run--真任务)
  - [gate — 通过/失败判决](#gate--通过失败判决)
  - [audit — 真实 transcript 对账](#audit--真实-transcript-对账)
  - [trigger — 触发准确度（第 4 维）](#trigger--触发准确度第-4-维)
  - [coverage — 出题漏测诊断](#coverage--出题漏测诊断)
  - [critique — 正文内容质量诊断](#critique--正文内容质量诊断)
  - [report — HTML 报告](#report--html-报告)
- [sift-records v2：文件归因证据](#sift-records-v2文件归因证据)
- [评测体系](#评测体系)
- [JSON 输出格式](#json-输出格式)
- [项目结构](#项目结构)
- [Exit Code](#exit-code)
- [常见问题](#常见问题)

---

## CLI 命令详细参数

全局选项：`--version` 显示版本。

### `vet` — 安全审查

```bash
sift vet path/to/skill/                # 表格输出(默认不落盘,-o 写文件)
sift vet path/to/skill/ --format json  # JSON 输出
sift vet path/to/skill/ --output report.json
sift vet path/to/skill/ --skip-external # 跳过外部扫描器
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--format` | table | 输出格式：table / json |
| `--output` / `-o` | 无 | 输出文件路径（不给则仅终端输出，不落盘） |
| `--skip-external` | 关闭 | 跳过外部扫描器（aguara、skill-scanner） |

**四个扫描器**：

| # | 扫描器 | 依赖 | 检查内容 |
|---|--------|------|----------|
| 1 | aguara | 可选（Go） | Prompt 注入、混淆 |
| 2 | skill-scanner | 可选（pip） | 已知恶意模式、CVE |
| 3 | secrets-scan | 内置 | 硬编码 API Key / Token / 凭证、Base64 |
| 4 | structure-check | 内置 | 缺失 SKILL.md、缺少 frontmatter、危险 shell 命令 |

**判定规则**：

| Verdict | 含义 | Exit Code |
|---------|------|-----------|
| **SAFE** | 所有扫描器通过 | 0 |
| **REVIEW_NEEDED** | 存在中等风险（MEDIUM） | 0 |
| **BLOCKED** | 存在严重风险（CRITICAL/HIGH） | 1 |

### `init` — 初始化评测定义

根据 SKILL.md 内容自动生成 `evals.json`。三种生成路径，优先级 `--from-transcript` > `--generate-prompts`（LLM）> 模板。`--kind agent` 改为给 **agent 定义.md** 出题（读 description + system prompt 凭角色造 role-matching 考题，不切段、不假设产文件，断言由 LLM 直接写）——agent 的「没有 transcript 时的出题路」；有真实 dispatch session 时 `--from-transcript --kind agent` 保真度更高（挖真实派发场景）。

> 生成的 `evals.json` 格式（字段 / AssertionType 全表 / expectations vs assertions / tier 坑）详见 [evals-format.md](evals-format.md)。

```bash
sift init path/to/skill/                                  # LLM 生成(默认)
sift init path/to/skill/ --num-evals 8                    # 限制数量
sift init path/to/skill/ --from-transcript session.jsonl  # 从真实会话挖 eval
sift init path/to/agent.md --kind agent                   # 给 agent 出题(读 agent 定义凭角色造 role-matching 题)
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--output` / `-o` | 无 | evals.json 输出目录 |
| `--generate-prompts` / `--no-generate-prompts` | 开 | 是否用 LLM 生成 L1 单元 eval |
| `--model` | 默认 | LLM 模型 |
| `--num-evals` | 无 | 最多生成的 eval case 数量 |
| `--from-transcript` | 无 | 从真实 session transcript 挖 evals（加料 SKILL.md，非替代） |
| `--kind` | (无) | 声明类型：生成时 `agent` = 按 agent 角色出题（读 agent 定义.md 凭 description/system prompt 造 role-matching 题，断言由 LLM 直接写）；默认/`skill` 走 skill 出题。配 `--from-transcript` 则选挖掘作用域（`skill`=按 Skill 调用切 / `agent`=按 agent 归属切，同 audit --kind；不传 + 多 skill/agent → 警告） |
| `--timeout` | 600 | LLM 调用超时秒数（mining 是单次大 prompt；撞超时 → degraded fallback） |
| `--focus` | (无) | 定向生成某一类 eval 补盲区：`negative-cases`（反面场景）/ `edge-cases`（边界/错误）/ 任意自由文本（声明特定方向）；常配 `--append`。`anti-triggers` 是旧名，保留为静默别名 |
| `--append` | 关 | 追加到已有 evals.json（不覆盖）：读现有 evals 喂 LLM 定向补题（只补 `--focus` 面、与现有题语义去重），新 eval id 接续已有 `max+1` |
| `--trigger-probes` | 关 | 生成 trigger-accuracy 探题（should-trigger / should-not-trigger），追加到 evals.json 的 `trigger_probes` 段（见 [evals-format.md](evals-format.md)）。LLM 读 description 出题；默认每方向 5 道，`--num-evals` = 每方向**上限**（trim 多余、不补下限；只 trim 生成的，挖出的真题不受限）。**配 `--from-transcript` 时附带挖 trace 负例**：真实会话里「领域相邻且未触发本 skill」的用户 query 收为 should-not 真题（事实层程序判定 + LLM 只按 description 过滤领域相邻；LLM 失败仍收、标 `unverified` 人审）。不重生成已有 scoring evals（载入已有 evals.json 保留原题）；新探题 id 接续已有 trigger_probes `max+1`，与既有探题按 prompt（lower+strip）去重。配合 [`trigger`](#trigger--触发准确度第-4-维) 跑 |

`--from-transcript` 解析 session transcript（模型、user turns、用到的 skill），重建 input_files / commands / produced_files，生成带结构化 assertions 的 eval。若 LLM 失败降级为 fallback eval，会带 `degraded:*` flag 并在终端告警（不再静默）。多声明单元 transcript（多个 skill、或主 agent dispatch 了子 agent）不传 `--kind` 时会把全部当本声明挖（可能串味）→ 终端告警，建议加对应 `--kind skill` / `--kind agent` 只挖本声明段。默认 LLM 生成路径**先重试再降级**（对齐 audit 判官）：调用失败（超时/限流/CLI 错）或响应解析失败（畸形/截断）都大概率瞬态，重试最多 2 次（终端打 `重试 n/2…`），耗尽才退确定性模板出题——每题带 `degraded:llm-fallback(原因;重试 N 次后仍失败)` flag + 终端 ⚠ 块提示重跑，token 已花但考卷非 LLM 出的，一眼可辨。`--kind agent` 同样先重试（1+2 次）；耗尽返回 0 题 + 终端告警，且**不覆盖已有 evals.json**（exit 1，既有考卷字节不动——agent 无确定性 fallback，空集覆盖会把旧题毁在瞬时故障手里）。

**字段名校验**：LLM 生成时若把字段名拼错（如 `expected_output`→`expected`、`assertions`→`assertion`），pydantic 默认会**静默丢弃**该字段——evals.json 看着生成成功、实则缺字段，`eval` 时才暴雷。`init` 会在终端告警 `⚠️ N 条 eval 含未知字段(已忽略;可能拼错)`，逐条列出哪个 eval 的哪些字段可疑，免得考卷悄悄残缺。warn 不拒（拒绝会掉确定性模板 fallback，反而更差）。

**定向补薄弱面（`--focus` + `--append`）**：LLM 一次性生成整套时倾向正面题 / happy path，系统性漏反面场景、边界 / 错误处理。`--focus` 定向补这些盲区——预设 `negative-cases`（反面：skill 不该被触发的场景）/ `edge-cases`（边界/错误处理），或任意自由文本（skill 特定方向，如"加密 PDF"）。配 `--append` 增量补到已有 evals.json：生成阶段**读现有 evals 喂给 LLM**——走定向补题指令（只补 `--focus` 这一面、每个 assertion 直接验证它；并与现有题**语义去重**：对照现有题的 prompt 和 rationale，不同措辞测同一点的也不重复出），不再盲生成整套蹭 focus 前缀；新 eval id 接续已有 `max+1`、不覆盖旧题；**保留原有题原始字节**——只追加新题、不重序列化原有，故不丢未知字段、git diff 干净；终端**只列新增** eval、原有不全量重列）。典型用法：`sift init path/to/skill/ --focus negative-cases --append`。（`anti-triggers` 是旧名，仍作静默别名兼容，避免老脚本静默退化为自由文本回退。）

**终端输出**：生成后逐条列 eval（`[id] [level] name: N assertions, M expectations[, K fixtures] · conf X · ↳ rationale 溯源 SKILL.md 原文）——`[id]` 对照 evals.json 用（id 本就按显示顺序递增，不用数位置猜）；汇总行 `耗时 X.Xs · N tok`（LLM 生成消耗，同 coverage/trigger；确定性 template fallback 无 LLM 则不显）。

### `add` — 追加评测用例

LLM 辅助或手动添加单个 eval case 到已有 evals.json。

```bash
# LLM 辅助：输入想法 → LLM 生成完整 eval
sift add pdf/ --idea "encrypted PDF decryption with wrong password should fail" --level L2

# 手动模式
sift add pdf/ --prompt "Decrypt file.pdf..." --expectation "output exists" --level L2

# 预览
sift add pdf/ --idea "..." --dry-run

# 给 agent 追加单题(走 agent 单题 prompt:scope:transcript 断言 + fixtures)
sift add path/to/agent.md --kind agent --idea "审一个有 SQL 注入的 PR"
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--idea` | 无 | 自然语言描述，LLM 生成完整 eval case |
| `--prompt` | 无 | 手动指定 eval prompt |
| `--expectation` | 无 | 手动指定 expectation（可重复） |
| `--level` | L1 | 测试层级：L1 / L2 |
| `--dry-run` | false | 仅预览，不写入 |
| `--model` | 默认 | LLM 模型（用于 --idea） |

`--idea` 模式下，自动分配 id、自动推导 assertions 和 fixtures，生成后显示 `耗时 X.Xs · N tok`（LLM 消耗）。`--idea` 与 `--prompt` 必须给其一（手动 `--prompt` 模式无 LLM、不显示消耗）。

### `analyze` — 静态分析

11 项自动化体检，**零 API 成本，<1s 完成**。输出 scorecard（`score / max_score`）。

```bash
sift analyze path/to/skill/                  # 表格（默认不落盘，-o 写文件）
sift analyze path/to/skill/ --format json -o scorecard.json
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--format` | table | 输出格式：table / json |
| `--output` / `-o` | 无 | 输出文件路径（不给则仅终端输出，不落盘） |

**11 项检查**：

| # | 检查项 | 说明 |
|---|--------|------|
| 1 | `description_length` | 描述长度 ≥ 20 字符 |
| 2 | `description_has_trigger` | 描述包含动作触发词 |
| 3 | `description_third_person` | 描述使用第三人称 |
| 4 | `name_kebab_case` | name 符合 kebab-case 格式 |
| 5 | `name_max_64` | name 长度 ≤ 64 字符 |
| 6 | `name_no_reserved` | name 非系统保留字 |
| 7 | `skill_md_max_500_lines` | SKILL.md 不超过 500 行 |
| 8 | `has_examples` | 包含示例代码或用法 |
| 9 | `reference_depth` | references/ 目录保持扁平 |
| 10 | `anti_fabrication` | 包含反捏造指引 |
| 11 | `source_documented` | 引用了参考来源 |

输出还包含 Skill 分类：`CAPABILITY_UPLIFT` 或 `ENCODED_PREFERENCE`。`score < max_score` → exit 1。

### `inspect` — 题集体检

对已有 evals.json 做静态体检：完整性、稳定性（programmatic/LLM 占比）、问题 flag。**不执行、不调 LLM、不下判决**——只报指标 + 关注 flag，由人决定是否 freeze。

```bash
sift inspect path/to/evals.json            # 表格
sift inspect path/to/evals.json --json     # 机器可读
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--json` | 关闭 | 输出 JSON |

注意位置参数是 **evals.json 路径**（不是 skill 目录），与多数命令不同。

### `freeze` — 冻结基线

把 evals.json 冻结成命名基线快照 + 溯源 sidecar，供 CI / 跨 run 复用同一份题。loader 零改动（产出的仍是标准 evals 格式）。同名覆盖（幂等）。

```bash
sift freeze path/to/evals.json                         # → baseline.json + baseline.meta.json
sift freeze path/to/evals.json --name v1 --skill-version 1.2
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--name` | baseline | 基线名（→ `<name>.json` + `<name>.meta.json`） |
| `--out` | 无 | 输出目录（默认 evals.json 同目录） |
| `--skill-version` | 无 | 目标 skill 版本（记入 meta） |

> 设计原则：**机制不策略**。同卷 run 内架构保证一个 `--evals` 同进程；跨 run 的完整性靠 git（消费方决定是否 commit baseline.json）；门禁阈值 / CI 落点归消费方。

### `eval` — A/B 评测

A/B 评测（同一 skill 开/关对照，或 v1/v2 双 skill）。`--kind agent` 切换到 agent 评测：绝对质量（无 with/without 对照），agent 正文经 `--append-system-prompt` 注入 system 层，产出单 config 绝对 pass_rate（无 delta/盲比较）；需配 `--evals`（agent.md 不能自动发现 evals.json）。与 `--skill-b` 互斥。真任务（不读 evals.json、产 trace 喂 audit）走 [`run`](#run--真任务) 命令。

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--evals` | 自动查找 | evals.json 路径 |
| `--runs` | 1 | 每种配置的运行次数 |
| `--model` | 用户默认 | claude -p 模型 |
| `--workers` | 4 | 最大并行 worker 数 |
| `--timeout` | 300 | 每次运行超时（秒） |
| `--max-turns` | 10 | claude -p 最大轮数 |
| `--max-retries` | 2 | 每 eval 在 UNEVAL/出错时的最大重试（每次重跑 claude+scoring+compliance，重 skill 可设 0-1 省 token） |
| `--scoring` | hybrid | 评分模式：hybrid / programmatic / llm |
| `--no-compliance` / `--compliance` | 开 | 跳过/强制遵循性检查（默认开） |
| `--refine-instructions` | 开启 | 遵循性判定前 LLM 精筛（默认开，`--no-refine-instructions` 关）：砍掉 extractor 误抽的示例/占位伪指令（提精度，per-SKILL 缓存，见 `audit`） |
| `--headline` | 开启 | 人话标题（默认开，`--no-headline` 关）：独立 LLM 步骤把每条指令的原文 bullet 去噪+补主语压成一句显示标题（判定不读；refine 之后/judge 之前；per-声明缓存 `~/.cache/sift/headline/`） |
| `--no-calibration` | 关闭 | 跳过校准（结果×过程 coherence 判决） |
| `--security-gate` | 关闭 | 安全审查返回 BLOCKED / REVIEW_NEEDED 时判失败 |
| `--no-static` | 关闭 | 跳过末尾自动产静态评分卡（默认产，写进 output 目录根，供 report 评分卡面板） |
| `--no-vet` | 关闭 | 跳过末尾自动产安全审查（默认产，写进 output 目录根，供 report 安全面板） |
| `--no-report` | 关闭 | 跳过末尾自动产 HTML 报告（默认产 report.html；聚合 benchmark/评分卡/安全） |
| `--output` / `-o` | output/ | 输出目录 |
| `--eval-samples` | 无 | 运行 eval 的数量或比例（如 `10` 或 `0.25`）；**随机抽样**（不可控选哪几道） |
| `--eval-ids` | 无 | 只跑指定 id 的题（逗号分隔，如 `3,5,7`）；**和 `--eval-samples`/`--changed-from` 三选一互斥**；id 不存在报错（列可用）；保序去重 |
| `--changed-from` | 无 | 改 skill 后只跑受影响的题：给**基线 skill 目录**，自动 diff 两边 SKILL.md 指令 + LLM 判受影响 eval（回归探针）；**和 `--eval-ids`/`--eval-samples` 互斥**。子集 delta 非整体测量，周期仍需全量兜底。失败处理见下 |
| `--format` | table | 输出格式：table / json |
| `--skill-b` | 无 | **v1/v2 模式**：第二个 skill 路径（两边各注入各自 skill）；不给则默认 with/without（同 skill 开/关） |
| `--label-a` | skill 目录名 | v1/v2 模式 side A 显示标签 |
| `--label-b` | skill 目录名 | v1/v2 模式 side B 显示标签 |
| `--kind` | skill | 声明类型：`skill`（默认，A/B 对照）/ `agent`（绝对质量：`--append-system-prompt` 注入 agent 正文，无 with/without 对照、无 delta/盲比较；与 `--skill-b` 互斥；需 `--evals`） |

**`--changed-from` 判定的失败处理**：

- **判定失败**（没给出可用 id 列表）→ 报错退出（不静默跑全量、不静默跑空）。三类：① LLM 调用失败（API/超时/限流，重试后仍不行）；② 解析不出（非 JSON / 无 `affected_ids` 列表 / 含非整数）；③ 返回 id 不存在（列可用）。提示重试 / 改用 `--eval-ids` / 跑全量。
- **判定空集 ≠ 失败**：LLM 成功判定、结论“零受影响”（改的是与所有题无关的措辞）是**合法结果** → 正常退出（跑空），提示“不放心去掉 `--changed-from` 跑全量”。

**对照模式**：位置参数 skill = 候选（side A）。不给 `--skill-b` → with/without 对照（同 skill 开/关）；给 `--skill-b <dir>` → v1/v2 双 skill 对照（两边各注入各自的 skill，标签默认取目录名）。

> **退出码**：`eval` 是纯数据生产者——exit 0 = 产数据成功，2 = 输入错误，3 = 执行错误。**通过/失败判决不再由 `eval` 的退出码决定**（旧版读混合 `delta.pass_rate<0` 退 1 的逻辑已退役），改由独立 [`gate`](#gate--通过失败判决) 命令出。`--security-gate` 触发时仍退 1。

**输出目录结构**（默认 with/without 模式；v1/v2 模式把 `with_skill`/`without_skill` 换成各自标签名）：

```
output/
  with_skill/
    eval-<id>/
      run-1/                 ← 多次 run(--runs N);UNEVAL/出错会有 run-1-retry-*
        outputs/             ← Claude 产生的文件(skill references 已自动拷入)
        grading.json
        compliance.json
        calibration.json
        run_debug.json
        timing.json
  without_skill/
    eval-<id>/run-1/ ...
  benchmark.json             ← 聚合结果(含 comparison_summary 盲评聚合;--changed-from 子集跑时另记 selection)
                              `llm_duration_seconds` 两维度:墙钟分解(phase1_wall+phase2_wall=total,加总成立;
                              phase1_wall=逐题×两侧×runs 并发跑整段:准备+执行 executor+grader 评分+遵循性 audit+calibration,
                              并发交叠不再拆(各项 LLM 时长见 LLM 累计);phase2 再拆 read_runs_wall/comparator_wall/analyzer_wall;不含写后的报告生成)
                              + LLM 累计(scoring/grader/comparator/analyzer/audit/executor,并发叠加、可 ≥ 墙钟)
  scorecard.json             ← eval 末尾自动产(静态评分卡;也可 analyze -o 单独产)
  vet-report.json            ← eval 末尾自动产(原生安全扫描;也可 vet -o 单独产)
  report.html                ← eval 末尾自动产(也可 sift report 重生成/带 --previous)
  gate.json                  ← 运行 gate 命令后产生(非 eval 产出)
```

> **失败根因**:eval/audit 失败时,从原始 error 自动抽根因类别(timeout / 限流 / CLI 缺失 / 连不上 / 认证 / max_turns / session 不存在)+ 修复建议,免翻 log。三处可见:① `benchmark.json` 的 `runs[].error_root_cause`/`error_suggestion`(**纯诊断字段,不进 pass_rate/compliance 等判决**);② A/B 跑完终端「失败根因汇总」块;③ report.html「失败根因」section。`audit` 命令级失败(transcript 缺失/opencode 超时等)也终端额外打印根因。分不出的标 `unknown` + 原文,不假装分类。


### `run` — 真任务

跑真实任务、产 trace 喂 audit（不读 evals.json；审计的上游小功能，不与评测/审计并列）。`--task` 必填。

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--task` | **必填** | 真任务文本：直接 spawn 执行器跑此真实任务，产物 transcript 可喂 `audit` |
| `--cwd` | 无 | 真任务工作目录（任务材料真实文件）；拷贝隔离，cc 不碰原目录 |
| `--executor` | cc | 执行器：`cc`（默认）/ `opencode` / `cc,opencode`（逗号分隔，多执行器各跑 `--runs` 次）。未知名 exit 2。`opencode` 需 opencode CLI 在 PATH，且用 `provider/model` 格式配置模型（`--model` 给 cc 用；opencode 省略 `--model` 走 `opencode.json` 默认）。cc 产 `run-*/transcript.jsonl`、opencode 产 `run-*/transcript.json`（audit 都吃）。**公平性注意**：opencode 无 `--max-turns` 对等，多执行器 sweep 时 cc 轮数有界、opencode 无界（已知不对称） |
| `--runs` | 1 | 每任务运行次数 |
| `--model` | 用户默认 | claude -p 模型 |
| `--timeout` | 300 | 每次运行超时（秒） |
| `--max-turns` | 10 | claude -p 最大轮数 |
| `--output` / `-o` | output/ | 输出目录 |
| `--security-gate` | 关闭 | 安全审查返回 BLOCKED / REVIEW_NEEDED 时判失败 |

忽略 evals.json，按 `--executor`（默认 `cc`，可 `opencode` 或 `cc,opencode` 多执行器各跑）在 `--cwd`（拷贝隔离）跑真任务，产 `run-*/transcript.jsonl`（cc）或 `run-*/transcript.json`（opencode），结尾打印 `audit` 命令提示。每 run 另在终端打印 operational 摘要（耗时 / turns / tokens / stop_reason / tool_calls + 结果头部，读自 `run-*/run_debug.json`）。

> **退出码**：与 `eval` 同源——exit 0 = 产数据成功，2 = 输入错误，3 = 执行错误，`--security-gate` 触发时退 1。

### `gate` — 通过/失败判决

对已有 benchmark.json 出通过/失败判决。**post-hoc 纯函数**——不重跑 claude、毫秒级、可反复改阈值重判（与 report/audit 一样，只读 eval 的产物做后处理，不重新跑题）。

```bash
sift gate output/benchmark.json                        # 仅客观回归守卫
sift gate output/benchmark.json --min-win-rate 0.5     # 加主观 gate
sift gate output/benchmark.json --no-programmatic-guard --min-win-rate 0.6
sift gate output/benchmark.json --must-pass 3,5,7      # 必过题:candidate 侧这些题任一挂 → FAIL
sift gate output/benchmark.json --baseline freeze/baseline.json --strict-drift  # 题集漂移守卫
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--min-win-rate` | 无 | 主观 gate 阈值（候选 **tie-排除**胜率）；不给则只走客观回归守卫 |
| `--min-valid` | 3 | 主观 gate 的最少决定性比较数 |
| `--no-programmatic-guard` | 关闭 | 关掉客观回归守卫（只看主观质量时用） |
| `--candidate` | 自动 | 候选侧名（默认 `comparison_summary.label_a` / 首个 side key） |
| `--must-pass` | 无 | 必过题 eval id（逗号分隔，如 `3,5,7`）；**candidate 侧**这些题所有 run 都得全过，任一挂 → FAIL |
| `--baseline` | 无 | 冻结基线 evals.json（`freeze` 产）；启用题集漂移守卫（对比 eval id 集合增删） |
| `--strict-drift` | 关闭 | 题集漂移升 FAIL（默认只 warning） |
| `--out` / `-o` | benchmark 同目录 | gate.json 输出路径 |

**四轴**（客观 + 主观 + 必过题 + 漂移）：
- **客观侧 programmatic 回归守卫**（默认常开）：`delta.pass_rate_programmatic < 0` → FAIL；`>= 0` → PASS；纯主观题（无 programmatic expectation，值为 null）→ 此轴不适用。
- **主观侧 comparison**（`--min-win-rate` opt-in）：tie-排除胜率 `a_wins/(a_wins+b_wins)` ≥ 阈值 → PASS；< 阈值且决定性比较数 ≥ `--min-valid` → FAIL；样本不足 → UNCLEAR（判不准）。
- **必过题 must_pass**（`--must-pass` opt-in）：candidate 侧这些 eval 的所有 run 都得全过（`passed==total`），任一挂 → FAIL。未指定 → 此轴不适用。看 candidate 侧（= with skill）：关键题用了 skill 必须成；without 侧挂正常。
- **题集漂移 drift**（`--baseline` opt-in）：对比 `--baseline` 冻结基线的 eval id 集合 vs `benchmark.runs` 的 eval id 集合，增/删 → 报告。默认只 warning（不进 decision）；`--strict-drift` → 有增删升 FAIL。防「删掉挂的题拉高 pass rate」。

**判决**：AND——任一适用轴 FAIL → FAIL；无 FAIL 但有适用轴信号不足（判不准/样本不足）→ **UNCLEAR**；无适用轴（守卫关 / 未请求 / 无信号）→ **N/A（不适用）**；否则 PASS。写 `gate.json`，按 `PASS/FAIL/N/A/UNCLEAR` 退 `0/1/2/2`（N/A 与 UNCLEAR 都 exit 2，消费方定）。

> exit 2 多语义：**使用错误**（文件/解析错误，不写 gate.json）、**N/A 判决**（不适用，无适用轴）、**UNCLEAR 判决**（判不准，有轴但信号不足）都退 2——靠 gate.json 是否存在 + 其 `decision` 字段（N/A vs UNCLEAR）区分。
>
> **不适用（N/A）为何也 exit 2、不放行**：不适用=无适用轴，常意味着门禁根本没评上（如配置漏：纯主观题 + 没给 `--min-win-rate` + 关了 programmatic 守卫）。放行（exit 0）会让"没评"伪装成"通过"，有风险；故与判不准（UNCLEAR）一样退 exit 2 交消费方定（备选"不适用→exit 0 放行"暂缓，因放行风险）。两者都 exit 2，靠 gate.json 的 `decision` 字段区分 N/A vs UNCLEAR。

### `audit` — 真实 transcript 对账

把真实会话 transcript 与 SKILL.md 对账，判指令遵循性。**不 re-run**，直接对账历史执行（与 fixture compliance 互补）。支持多 transcript 指令级聚合。

> 想拿现成材料试手 + 对照预期结果：自带 3 个 audit 示例（合成违规 transcript，预期该抓的违规已逐条列明）+ 1 个 multi-skill-demo（演示 `--kind skill` 在一条多 skill transcript 里只对账一个 skill），见 [example-skills/README.md](../fixtures/example-skills/README.md)。

> 判定要逐批调 LLM（每批约 40–257s），跑期间**实时打印进度**：进每个 transcript 打头（`对账 transcript i/N: <name>`），每个 LLM 批前后各一行——`[条件性/禁令/步骤] 判定 N 条…` → `[条件性/禁令/步骤] 完成 ⏱ Xs`（类别名打头括号化；指令 >10 条分批时才显 `批 k/总`，单批不显）。**三种失败都会先自动重试（最多 4 次）再兜底**：① 调用失败（超时 / 退出码 / 限流）；② 调用成功但整批 0 条解析成功（LLM 输出畸形/截断，原是看着"完成"却全 N/A 的静默盲点）；③ **漏答过半**（成功但部分解析、有判定者不足半数——输出契约要求逐条判，过半缺席多为瞬态漏答/截断救回的短数组；漏 1 条不重试：模型系统性漏近重复指令是常态，别为它每批白烧重试）——都大概率瞬态、重试常能救回；③ 耗尽时**回覆盖最高的一份而非弃批**（已判对的保留、漏的走 UNRESOLVED，不再把已判对的也变没判成），从头到尾 0 覆盖才整批兜底 N/A。**判官走 http forced tool_use + 结构化 schema**（tool input_schema，inline_schema 展开 $ref；条件性/禁令/步骤/refine 四个判官输出 schema 验证过的对象，畸形 JSON 从源头大幅减少；顶层须 object，故 array 输出包 `{"items":[...]}`、代码侧解开；schema 偶尔不合规时降级走文本 fallback，上述重试兜底不变）。**forced tool_use 拿不到结构化输出**（GLM 服务端默认注入 thinking，偶尔只回 thinking 不产 tool_use 块 / thinking 撑满 max_tokens 没空间产——http 单次 forced 的结构死结，重试 http 无用）→ dispatcher **回退 cc** 重跑（cc `--json-schema` 是多轮 tool_use 循环，催出概率远高于 http 单次 forced；拿不到时还降级返 result 文本由 fallback parser 接住），cc 也失败才走上述重试。max_tokens 起步 4096（浅判定批足够）；截断（stop_reason=max_tokens）且无 tool_use 时升档重试一发——升档值 = max(首档×2, 16384) 封顶 32768（调用方可按工作量给首档覆盖——`--suggest` 按批内簇数给 6000+2500×n，其 ×2 已超下限则下限不生效），仍失败才回退 cc；非截断的主动不调工具不重试。**只有两档、不带 thinking budget 帽**——直连探针实证 glm-5.3 的兼容端点完全不理会 budget（同值重试必重复同样截断，白烧一趟）；深度任务的余量靠**升档下限**兜（16384 = 深度推理一发过的探针实测值），首档保持 4096 不抬——浅批快路径与稳定期行为不变（GLM 把 max_tokens 当思考锚，抬首档会把不需要深的批也拖慢；主流模型语义 max_tokens 是上限不是目标，抬升档对它们零代价），不靠多开档。升档/回退前终端各报一行进度（`↳ 截断升档…`/`↳ 回退 cc…`），不再静默数分钟。截断但有 tool_use 时仍返回（不回退，下游 parser 判完整性），只有真没 tool_use 才回退 cc。重试打 `重试 n/4…`，漏答过半先打 `⚠ 仅 x/y 条有判定(漏答过半),重试…`，耗尽才 `重试耗尽 → 全兜底 N/A`（0 覆盖；有部分覆盖则回覆盖最高的一份、其余未判；audit 汇总"未判定"警告提示重跑）。**第三道兜底**：单批或单个判定器抛**未捕获异常**（非上述瞬态失败，如解析逻辑 bug）也降级该批/该类为 UNRESOLVED——不击穿整个 audit、不翻转真假，与其余结果一起汇总提示重跑（并发下任一批/类失败都不连累其他）。看到 `[条件性/禁令/步骤] …` 滚动就是在跑，别误以为卡死。

> 三个 LLM 类别判定器（条件性/禁令/步骤）**并发跑**（全局在飞由 AIMD 自适应限制器动态调、适配不同后端容量），墙钟约 **2× 提速**（三类并发取最长、而非串行加总）。副作用：三类的进度行会**交错**出现（每行 `[类别]` 打头，一眼分清是哪类在跑）；输出格式判定器是确定性子串匹配（无 LLM），不走并发。

> **`--suggest` 改进建议（可选，默认关）**：报告落盘前对 FAIL findings 跑一次三类归因的 LLM 后处理——改 skill 建议 / 改声明建议 / 归因模型·环境的只透明计数不出建议（读法见下「怎么看结果」⑩）。CLI 层挂点：引擎与 run 内 compliance 零影响；全 PASS 不跑不落盘，UNRESOLVED 占比 >30% 拦下（证据残缺，先重跑），LLM 失败留 `skipped_reason` 不毁报告。归因粒度 = 逐指令簇（一条建议对应一条指令，`target_seqs` 列其全部 FAIL 的 #N），分批 ≤10 簇/批、批间并行（≤4 路，AIMD 门控；几十簇的墙钟 ≈ 最慢一批），输出上限按批内簇数弹性（6000+2500×簇数从宽给——深度归因的 thinking 就要这个量级且 run-to-run 有方差，给窄撞方差就是浪费一整趟截断）；不合规条目 `dropped_count` 透明计数。**判官完成与汇总行之间的静默期有阶段头**：先报一行 `生成改进建议:归因 N 个失败簇 · M 批并行（深度归因，可能数分钟）…`，多批时各批完成/失败再打 `  [批 k/n] 完成 ⏱ Xs`（看得出最慢一批），单批不叠批级行。消耗并入报告阶段表：真跑过 LLM（`calls>0`）时 CLI 在 `stage_breakdown` 多追加一行 `suggest`（token/calls 与判官阶段同口径，含截断/升档重试；被门控拦下/无 FAIL 的零消耗不占行），HTML 阶段表的耗时百分比分母随之变为引擎墙钟 + 建议耗时（% 占整条流水线）；终端底部汇总两行也并计建议段——`LLM tokens` 行追加「+ 建议 input=N output=M · K call(s)」，`耗时` 行变「Xs + 建议生成 Ys = Zs」（底部是这次执行的总消耗，只报判定段会与上面建议行自相矛盾）。

```bash
sift audit path/to/skill/ --transcript run-1/transcript.jsonl -o audit-out/
sift audit path/to/skill/ --transcript a.jsonl --transcript b.jsonl --format json
# transcript 里用了多个 skill,只对账本 skill 被调用的段(按 Skill 工具调用切分):
sift audit path/to/skill/ --transcript big-multi-skill.jsonl --kind skill
# opencode 跑出的 session(`opencode export <sessionID>` 导出,顶层 info/messages、parts 嵌在 message 里):
sift audit path/to/skill/ --transcript opencode-session.json
# 直接给 opencode .db 文件(内部自动 list/export/audit;单 session 自动选,多个列出让你挑):
sift audit path/to/skill/ --db opencode.db
```

> **`--transcript` 吃两种格式(按后缀自动分派)**：cc 会话 `.jsonl`(逐行 JSON 记录,含 `Skill`/`Read`/`Write`/`Bash` tool_use);opencode session `.json`(`opencode export <sessionID>` 的产出,顶层 `info`/`messages`,parts 嵌在 message 里、role/agent 在 `message.info`)。两者重建成同一 `SessionDigest` 再对账(经同一条 canonical 重建路径:归一到 cc-record 形 → 共享 `_digest_from_records`,三源重建逻辑单处定义);`--kind` 选锚点(`skill` 按 `Skill` tool_use、`agent` 按 `agent` 归属);opencode 单锚点,按 per-message `agent` 切分(扁平,无 cc 父子作用域,`skill`/`agent` 的区分对它无意义、都按 `agent` 切)。`run --task` 两种执行器都支持(默认 `cc` 产 `.jsonl`,`--executor opencode` 产 `.json`),run 产的 transcript 直接喂 audit 即可。生产导出 opencode 会话喂 audit:`opencode session list` 找 id → `opencode export <id> > session.json`(**导原文**,audit 靠 part.text 判遵循性;`--sanitize` 仅用于对外分享脱敏版,脱敏后别喂 audit)→ `sift audit --transcript session.json`。

> **拿到一个 opencode `.db` 文件,一行进 audit**(别人/别处拷来的库,不是你本机默认库):直接用 `--db`,sift 内部把 list/export/audit 全包了(隔离 `XDG_DATA_HOME` 指向那个 .db、**不碰本机真库**;需 opencode CLI 在 PATH):
> ```bash
> sift audit path/to/skill/ --db handed.db                        # 单 session 自动选;多个则列出让你挑
> sift audit path/to/skill/ --db handed.db --session ses_xxxx      # 已知 id,直接审
> ```
> 不带 `--session` 时:db 里**只有 1 个 session → 自动选**;**多个 → 打印 `id/标题/时间` 表 + exit 2**,记下 id 带 `--session` 重跑(非交互,可脚本化)。底层就是下面三步(隔离 `XDG_DATA_HOME` → `session list` → `export`);想手工控制(导出留底 / 脱敏分享)就照这套:
> ```bash
> # 1. 把 .db 摆成 opencode 认的布局(<envdir>/opencode/opencode.db;库大可 ln -s 软链代替 cp)
> mkdir -p /tmp/ocenv/opencode && cp /path/to/handed.db /tmp/ocenv/opencode/opencode.db
> # 2. 用 XDG_DATA_HOME 指向它,列 session 找 id
> XDG_DATA_HOME=/tmp/ocenv opencode session list
> # 3. 导出目标 session → audit(**导原文**,audit 靠 part.text 判遵循性;脱敏版会变 [redacted:text:…] → 审计降级。要脱敏分享另跑 opencode export --sanitize,别喂给 audit)
> XDG_DATA_HOME=/tmp/ocenv opencode export ses_xxxx > session.json
> sift audit path/to/skill/ --transcript session.json
> ```
> 若 .db **就是你本机 opencode 跑的**(默认位置),跳过 XDG,直接 `opencode session list` → `opencode export <id>` → audit。

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--transcript` | — | 真实会话 transcript（cc `.jsonl` 或 opencode session `.json`，按格式自动分派；**可重复**，多 transcript 聚合)。与 `--db` 二选一,都不给 → exit 2 |
| `--db` | — | opencode `.db` 文件:内部自动 list/export/audit(隔离 `XDG_DATA_HOME`、不碰本机真库;需 opencode CLI)。与 `--transcript` 互斥 |
| `--session` | — | `--db` 下指定 session id;不给则单 session 自动选、多个列出让你挑了重跑(仅搭 `--db`) |
| `--kind` | (无) | 不传=**宽松全 session 模式**(整条 transcript 对 body 对账、不切分,**skill/agent/root 三者共同默认**);传 `skill`\|`agent`\|`root`=精确收窄到对应执行段(`skill` 按 `Skill` 调用切;`agent` 按 `agent` 归属切;`root` 切顶层主 agent 作用域——cc `parent_tool_use_id` 为空 / opencode `session.info.agent`,target 名忽略)。**不传但 transcript 含可收窄锚点 → 黄字对称提示可加 `--kind`**(skill/agent/root 一视同仁、非阻断、宽松照跑)。opencode 单锚点按 per-message `agent` 切,见下 |
| `--refine-instructions` | 开启 | 判定前 LLM 精筛（默认开，`--no-refine-instructions` 关）：砍掉 extractor 误抽的示例/占位伪指令（精度>召回，per-SKILL 缓存，见下） |
| `--headline` | 开启 | 人话标题（默认开，`--no-headline` 关）：refine 之后、判定前的独立 LLM 步骤，把指令原文 bullet 去噪+补主语压成一句显示标题（判定不读；per-声明缓存 `~/.cache/sift/headline/`；显示回退 headline>source_excerpt>instruction_text，详见下） |
| `--turn-refs` / `--no-turn-refs` | 关(`--no-turn-refs`) | turn 定位（默认关）：`--turn-refs` 时 judge 输出 turn_refs + digest 加 `[T{idx}]` 前缀，finding 带违规发生的 turn 序号（供能 link 到 turn 内容的下游消费方）；默认 `--no-turn-refs` 回到无前缀的干净判定。加前缀会动 LLM prompt、引入条件分支过严，故默认关、按需开 |
| `--output` / `-o` | ./output | 输出目录 |
| `--model` | 默认 | 语义判定 LLM |
| `--format` | table | table / json |
| `--open` | 关闭 | 生成后浏览器打开 `audit-report.html` |
| `--max-concurrent` | 自适应(基线 6) | LLM 判定并发模式。**默认 AIMD 自适应**:动态探当前后端容量边界(撞限流乘性减、连续成功加性增),适配不同模型/服务商的并发或速率限制。显式给 N = **固定 N**(关自适应,向后兼容);env `SIFT_MAX_CONCURRENT` 同效;env `SIFT_ADAPTIVE_CONCURRENCY=0` 显式关自适应(回退固定基线)。大声明 / 低内存 / 多 audit 共存时固定调小 |

> **默认（不带 `--kind`）= 宽松全 session 模式，三者共同默认**：不带 `--kind`，skill / agent / root 声明一律拿整条 transcript 对 body 对账、不切分（不是 skill 专属捷径）。skill 指令多"做事方式"类、不活跃段多落 N/A（误伤轻），故 skill 宽松模式常可接受；但 agent / root 的角色专属 / 编排指令会扣到不相干 record 上（agent 尤甚 → 误 FAIL）。带 `--kind` 精确收窄到声明单元真实执行作用域。**对称提示**：不传但 transcript 含可收窄锚点（本 skill 被调用 / `agent==name` 段 / 子 agent 作用域）→ audit 打黄字提示可加哪个 `--kind`（三者一视同仁、非阻断、宽松照跑）；opencode 单锚点不提示。

> **`--kind {skill,agent}`：多单元 transcript 只对账一个声明**。transcript 里调了多个 skill / dispatch 了多个 agent 时，宽松模式（见上）会把整段当一个袋子判（别的单元的行为混进报告）。加 `--kind` 后切成本单元被调用/被 dispatch 期间的段，只对账这些段。**kind 选锚点、不并集**：`--kind skill` 按 transcript 里的 `Skill` 工具调用切（归段 **scope-aware**：每个 `Skill` 调用起一段、归该 skill 到下一次调用；子 agent（Agent 工具）是独立作用域，其记录带 `parent_tool_use_id`，子 agent 里用的 skill 切到 Agent 返回、**不污染主 agent 归属**；target 调多次 → 多段 → 多 transcript 聚合）；`--kind agent` 按 `agent` 归属字段切（被 dispatch 的 agent 的执行段，如 developer）。同名既是 skill 又是 agent（dual-nature，两份不同声明）时，kind 明确审哪一面、各切各的、不串味。**两条限制（skill 面）**：① skill 没有"返回"信号，所以"调 A → 调 B → 不重调 A 接着干 A"那段会归给 B（不可检测，解法是 agent 显式重调 A）；② 把委派子 skill 的段折进主 skill 审计（inclusive）需用户声明，暂不支持。该单元没在 transcript 里以此锚点出现 → 报错退出（exit 2），不静默跑空。

> **`--kind root`：审顶层主 agent / 编排者**。切主 agent 作用域——**cc** = `parent_tool_use_id` 为空的全部 record（**单 span**：主 agent 自己的 turn + 它发起的 Agent dispatch tool_use/tool_result 都在主作用域；子 agent 内部 record 的 `parent_tool_use_id` 指向某 Agent 调用，**排除**、归各自 `kind="agent"` 单元审）；**opencode** 无父子作用域，root 取 `session.info.agent`（declared 主 agent），复用连续同 agent message 切段（主 agent message 被子 agent 夹断可能**多 span**，findings pool 后按指令聚合、与单 span 等价）。**target 名对 root 无意义**——root 只认作用域、不按声明名切（skill/agent 路才靠 `name` 契约）。语义：审主 agent 的**编排**（dispatch 谁 / 何时 / 怎么接返回），而非子 agent 内部；主 agent 的声明常是 workflow 级 SKILL.md，故 root 是整 session / workflow 审计的地基。**无子 agent 的 session**：主作用域 = 整条 transcript，`--kind root` 切片与不带 `--kind`（宽松模式）等价；root 的价值正在有子 agent 时**排除其内部**、只审主 agent 编排。exit code 不变（无 gate）：cc 主作用域在**非空 transcript** 下恒非空（never-invoked 不触发 exit 2）；**完全空 transcript 仍触发 exit 2**（无 record → 主作用域也为空，never-invoked）。opencode 缺 `session.info.agent` → exit 2。

> **`--kind root` 的子代理证据旁路（两层，默认开）**：root 只审主作用域的语义不变，但编排范式下执行类动作（跑命令 / 写文件 / 加载 skill）全部由子代理执行、主作用域痕迹恒为 0 → 判官按「缺席」误判（实测一类编排会话样本的 FAIL 过半属此）。两条旁路补证据、不动切片语义：① **条件性两遍匹配（确定性，零 LLM）**——条件性指令主作用域 trace 未命中时，再到被 root 切片排除的**子代理全量工具痕迹**里做第二遍**严格**匹配（只认执行痕迹：Bash `command` 字段的命令词 / Skill 调用名 / Read 等的结构化路径字段；**文档内容提及不算执行**——子代理报告里写过「已使用 X」≠ 真调用了 X）。命中 → PASS（method=程序），evidence 为 `主 trace 未命中;子代理 <角色>(T<turn>) 实际调用了 X(经派发执行)`——与「主 agent 亲自执行」的 PASS 在证据上明确区分。禁令 / 步骤判官不走这条路（「子代理干了」恰恰不违反「主 agent 不许亲自动手」类禁令）。② **派发-回报段（喂步骤 / 禁令判官）**——task/Agent 派发的 tool_result 是子代理完工报告，本就是主作用域记录；audit 把「派发指令头 + 完工报告（剥 `<task>` 包装后取头 80 + 尾 60 字——验收结论惯例在报告尾部）」配成清单段（8KB 封顶，失败回报全保、DONE/空压计数）+ 子代理执行统计行（bash/write 次数、角色分布）喂给判官。步骤判官另有规则：**派发指令里写了要求 ≠ 要求达成**——验收 / 结果类步骤以回报结论 + 主 agent 处置为准（回报 FAILED 仍派发后续 → FAIL 门禁失守；声明要求验收但无验收派发 → FAIL 免验）。开关（env，默认全开，消融 / 逃生口）：`SIFT_TASK_SUMMARY_LIMIT`（dispatch 类 `input_summary` 上限，默认 2000——统一 200 会掐掉派发指令里的要求清单，判官「看不见」派发内容；=200 回旧行为）、`SIFT_AUX_TRACE=0`（关①）、`SIFT_DISPATCH_PAIRS=0`（关②）、`SIFT_PROHIBITION_PAIRS=0`（禁令判官不共享②）。已知残余：子代理实际做了但完工报告没提（交付物清单不全）的场景仍可能误判——报告没列成为新的缺席推断源，根治需按派发配对附子代理工具痕迹（待做）。

> **`--refine-instructions`：判定前精筛伪指令**。extractor 是纯正则（静态全抽 SKILL.md），会把教程型 skill 里的**示例文字 / 模板占位 / 描述性句子**误抽成"指令"——这些伪指令喂给判官只能出 N/A（无法对账）或偶发假 FAIL。开启（默认，`--no-refine-instructions` 关）后，audit 在 extract 后、判定前用 LLM 逐条分类"真实可检查指令 vs 示例/占位"，**只砍明确判为伪指令的**（精度 > 召回：判不准 / 没判成 / 调用失败一律保留，宁可留伪指令判 N/A，也不砍真指令）。伪指令类目含**命令行/代码片段里的标识符被抽成的伪工具要求**（"工具名"其实是编译目标/函数名/脚本参数/任务编号词，如「调用 TilingFunc」、"`./run.sh` 跑 Task 25 用例"抽出的「跑 Task」——判它要看原文，prompt 喂 `source_excerpt` 上下文）。per-skill 只精筛一次（多 transcript 共享），砍掉的条数和来源写进 report 顶部 warnings。OUTPUT_FORMAT（纯程序子串匹配，精筛无收益）不精筛。`eval` 也支持（经 RunConfig 传给内部 audit）。**per-SKILL hash 缓存**：同 instructions（含摘录）+ model + prompt 版本只 refine 一次，跨 audit() 调用复用（runner 每 eval×config×run 调一次 audit，缓存避免同 SKILL.md 反复 refine），存 `~/.cache/sift/refine/`；指令内容 / 摘录 / model / prompt 版本变则失效重算。误砍率实测 0（4 结构差异 skill × 139 条，砍表头/占位/资源描述，留步骤/禁令/约束）。

> **`--headline` + `source_excerpt`：指令显示可读性（判定不读）**。extractor 按 markdown 结构切指令时，谓语常被切走、主语留在列表项首行（如「必须存在」丢了 `design_doc_cases.json`）——但判定吃 instruction_text + 整段 scene、不依赖主语，故只伤显示不伤判定。两步纯显示增强（judge 始终吃 instruction_text 不变）：① `source_excerpt`（extract 时记指令所属 bullet 完整原文，`_bullet_excerpt` 回溯列表项首行含主语，无 bullet 回退所在行）；② `--headline`（默认开，`--no-headline` 关）在 refine 之后、判定之前的**独立 LLM 步骤**把 instruction_text + source_excerpt 去噪（反引号/星号/括号注释）+ 补主语 + 压一句人话——**故意没塞进 refine**（refine 判 is_real 决定哪些指令进 judge，塞进去会漂 is_real→滤指令集合变→判定间接变），独立步骤才真零判定影响。per-声明 hash 缓存（`~/.cache/sift/headline/`，键含 source_excerpt）。显示回退 headline > source_excerpt > instruction_text；audit-report.html / insight 均保留 instruction_text 作「判定锚点」折叠核对。指令的**声明分组标题** `finding.section` = `source_section`（body 级 extractor 在 match 偏移处回填的真实 markdown 标题）or `section`（伪节名兜底），③b 覆盖视图按它分组；判定与 refine/headline 缓存键均不读（`section` 值不动、新字段不进键，旧缓存全保）。

判定分四类——每条指令按 SKILL.md 声明来源路由到一类，各按不同判法出 verdict。每条 finding 有两个**正交**维度：**verdict（结果，五态）** + **method（怎么查的，程序 `PROGRAMMATIC` / LLM `LLM`）**。

- **PASS / FAIL**：已判明通过 / 违规。
- **N/A（不适用）**：规则存在但本次 transcript 场景不触发——已解决的有效结论。
- **UNRESOLVED（没判成）**：LLM 调用 / 解析失败 / 未提及 / 畸形值——操作性失败，**可重跑救回**。
- **INDETERMINATE（判不准）**：LLM 看了证据但真模糊（证据相互矛盾 / 关键缺失），给不出 pass/fail/na——**重跑无益**（只能人工复核），强制带 `indeterminate_reason`（evidence 字段）。

| 类别 | 来自哪些指令 | 怎么判 | 结论 |
|------|------------|--------|------|
| 条件性 CONDITIONAL | TOOL_REQUIREMENT / REFERENCE_USAGE（该用 X 工具、读 X 参考） | 命中 tool_trace（**含参数级断言**：SKILL 要求的 CLI 参数也得对、否则不命中走 LLM——参数在存在性路径同样搜完整结构化 input，且含 `{...}` 的**模板占位符 token 不参与字面断言**（声明写 `docs/{OP}/`、trace 里是实值）；路径规范化比对；命令词匹配搜 `input_summary` **和完整结构化 input**——摘要是 200 字截断的显示串，Task 派发 prompt 里交代的命令常被截掉，root 对账对的是「派发里有没有交代」；**频次判定不搜全文**，提到 ≠ 执行）→ PASS（**程序判定，不调 LLM**）；未命中 → LLM 判该场景该不该触发 | 遵循 / 违反 / N/A / 没判成 / 判不准 |
| 禁令 PROHIBITION | CONSTRAINT / ANTI_FABRICATION（不许做 X） | LLM **主动检测这次有没有违规**（不是判“遵守没”） | 未违反→PASS / 违反→FAIL / 没判成 / 判不准 |
| 步骤 STEP | HEADING_STEP（应走第 N 步） | LLM 判走过 / 该走没走 / 不涉及 | pass / fail / na / 没判成 / 判不准 |
| 输出格式 OUTPUT | OUTPUT_FORMAT（产物应含某格式） | **确定性子串匹配，程序判定，不调 LLM** | 命中→PASS / 未命中→FAIL（无 N/A / 没判成 / 判不准） |

> **禁令判的角度反着来**：其它三类问“该不该做 / 做没做”（正面遵循），禁令问“这次执行里有没有发生**违反**这条禁令的行为”——主动找违规。例：`NEVER delete production data`、`不得编造不存在的错误码`（模型 hallucinate 一个码 → `violated=true` → FAIL）。

> **判官看什么 + 两处结构性噪声的防呆**：条件性看「场景」（`digest.user_turns`，任务上下文）——**未命中交 LLM 时同时喂完整 `tool_trace`**（判「系于从未开始的任务/阶段的指令」不判应触发：记录是有限窗口、窗口外=证据不覆盖；该阶段已开始的照常判，不得借窗口洗掉），禁令 / 步骤看「执行记录」（`transcript_text`）。两处防呆：① **条件性场景回退**——`--kind skill/agent` 段内无 user turn（任务在父作用域）→ 空场景原致条件性一律弃判 INDETERMINATE（实测占 22%–55%）；现空场景回退用 `transcript_text` 当场景。② **头 + 尾窗口**——执行记录超 prompt 预算（实测单段 60–150KB）时，旧裸 `[:8000]` 只留头、尾部（最终结果/总结）全盲 → 禁令类假 PASS / 弃判；现取头 + 尾各半 + 截断标记，成本不变、双端可见。步骤类另有完整 `tool_trace`（不截断）兜底，「有没有调某工具」不受截断影响。

> **没判成不乱报**：三类 LLM 判定（条件性 / 禁令 / 步骤）在 LLM 失败 / 解析失败 / 未提及时一律报 **UNRESOLVED（没判成）**，**不保守 FAIL**——FAIL 只留给 LLM 明确判“违反 / 该做没做”。
>
> **N/A、没判成、判不准 是三码事，别混**：① **UNRESOLVED（没判成）** = LLM 没出判定（调用 / 解析失败 / 未提及 / 畸形值）→ 操作性失败、**可重跑**、汇总进“建议重跑”警告；② **INDETERMINATE（判不准）** = LLM 出了判定、判定本身是“证据真模糊判不了”→ semantic、**重跑无益**，须带具体 reason（写明矛盾/缺失的证据，空 reason 降级回 UNRESOLVED）；③ **N/A（不适用）** = 规则存在但本次场景不触发——条件性类 LLM 明确判“不涉及”（`applicable=false`）、步骤类 LLM 返回“na”→ 已解决的有效结论、不进警告、不用重跑。**条件性 N/A 的 evidence 带 LLM 给的“为何不涉及”理由**（非硬编码套话——便于核对“场景真不触发”还是判官没喂饱；LLM 未给 reason 时退兜底套话、**不降级**，与 indeterminate 不同：N/A 合法高频，不设惩罚闸，只捕获+用理由）。N/A 为什么会有：提取是**对 SKILL 静态全抽**（skill 写了“读 references/foo.md”就抽一条），但“该读 foo.md”有触发条件——audit 一个根本不涉及该条件的 transcript 时，agent 没读是对的，不能 FAIL 也不能 PASS（规则没被触发），故 N/A。**步骤/条件类 N/A 还有第二个来源：记录窗口外**——执行记录是有限窗口，记录止于某处、其后的计划任务在记录里从未开始时，那些指令属「证据不覆盖」而非「该走没走/应触发」（步骤判 na、条件性判 applicable=false；步骤 evidence 注明记录止于何处）、进覆盖视图的未触达面；两路 prompt 都带反向闸——所属任务/阶段**已开始**、只是中途没走某步的仍照常判，防窗口规则被滥用成洗 fail 的口子；步骤路还有一道**中段空洞防脑补**——工具记录从 Tn 直接跳到 Tm、其间无该步骤任何痕迹的就是没走，判官不得脑补「中间的调用未收录但已执行」把段级缺席洗成 pass（同证据下 FAIL↔PASS 轮次摆动的实锤封口；窗口判 na 只适用于记录末尾之后的计划步骤）（条件性靠把完整 tool_trace 喂进 applicable 判官实现——它原只看场景、看不到记录止于何处，场景提过整个计划就会一律判应触发→假 FAIL；根因：判官口径会随模型漂移，深思考档曾把这类批量判 fail——一次多出 64 条假违规；计划没做完这类**整体性**未完成由数量类判定兜住，不靠每步一 FAIL 重复报）。
>
> **判不准三道滥用闸**（INDETERMINATE 是 LLM 逃逸口，需防滥用）：① **强制 reason**——parser 见 indeterminate 必须带具体矛盾/缺失证据，空则降级 UNRESOLVED（不许空口“判不准”）；② **占比告警**——audit 统计 indeterminate 占比，超 20% 且总条数 ≥5 → 报告顶部告警提示人工复核是否 LLM 滥用；③ **prompt 纪律**——三个判官 prompt 都写明“优先明确结论，indeterminate 须写具体矛盾证据，不许偷懒”。
>
> **何时提示“建议重跑”**：`○ 没判成`（UNRESOLVED）条数 > 0 时，audit 在报告顶部出警告（`X 条指令没判成(UNRESOLVED:LLM 调用/解析失败/未提及)，本次结果可能不完整，建议重跑`）。**重跑 = 重跑同一条 `audit` 命令**（纯对账、无副作用，不 re-run）；这些失败多为瞬态（LLM 后端失败 / 限流 / 输出截断，retry 没救回才落到这），重跑常能救回。INDETERMINATE（判不准）**不**提示重跑（重跑无益）。不重跑则那些条没判成、可能漏掉真违规，故 **“0 FAIL ≠ 全过”**——看警告里 UNRESOLVED 是不是 0。
>
> **每条 UNRESOLVED 带根因**（`findings[].unresolved_cause`，纯诊断、不改判定）：`timeout` / `rate_limited` / `parse_failed` / `connection` / `cli_missing` / `auth` / `max_turns` / `session_not_found` = **基础设施失败**（重跑常能救回）；`unmentioned` = transcript 未覆盖 / LLM 未提及该条（**重跑可能无益**——查出题或 transcript）；`judge_error` = 判定器自身异常（查完整日志 / 上报）。终端在「建议重跑」警告下追加一行根因分布、HTML 显「没判成根因」块——从一条笼统警告变成能分辨「该重跑 vs 重跑无益」。顶层 `unresolved_breakdown`（cause → 计数）是同份聚合。

产出 `<out>/audit-report.json`（机器读）+ `<out>/audit-report.html`（人读，**always 写**，与 JSON 并列，对齐 scoring report 视觉）。HTML 自上而下：① 头部（skill / summary 计数徽章：✓通过 / ✗失败 / ⊘不适用 / ○没判成（>0 时）/ ?判不准（>0 时）/ 反推指令数 / ↺ refine 砍 N 条（`refine_dropped_count`>0 时，默认开 refine 砍掉的伪指令数）/ ⚙ tokens in N / out M · K calls（`token_usage.calls`>0 时，本次 audit 所有 LLM 判定调用的 token 用量合计；判官 token，非 executor；cost 暂隐藏——http 路径不记 total_cost_usd、cc 真值与 http $0 混报会误导，数据字段仍记录于 token_usage））①b **阶段耗时 / token 表**（诊断慢在哪：抽取/精筛/标题/切片/判定/聚合逐行——判定行含条件性/禁令/步骤/控制流四路判官的合并消耗；`--suggest` 真跑过时多一行「建议」，% 占整条流水线 = 引擎墙钟 + 建议耗时）② 警告框（没判成建议重跑、判不准占比偏高告警）②b **没判成根因分布块**（有 UNRESOLVED 才显：按 `unresolved_cause` 计数降序，`N× <标签>` 一行行——分辨「该重跑 vs 重跑无益」）③ 系统性聚合表（单/多 transcript 都显——拆违规 FAIL 表 + 场景不适用 N/A 表各一张；多 transcript 带跨会话 fail% + fail in transcripts 列，单 transcript 无跨会话可比、精简成「指令 + 类别（+ FAIL 噪声判别）」做概要扫描）③b **声明覆盖视图（coverage，`coverage.total`>0 才显）**：总覆盖 covered/total = pct% + 未覆盖/未知计数一行，按声明分组 `<details>` 展开（真实标题原样；伪节名 Constraint / Anti-Fabrication / References / Root / transcript-derived 显中文标签）——组内列**未覆盖/未知**指令（标题走 headline > source_excerpt > instruction_text 回退链 + #N 明细链接）；口径 covered=任一 transcript PASS/FAIL、uncovered=全 N/A、unknown=只有没判成/判不准，反推（derived）指令不计入分母；CLI 摘要同步打一行「声明覆盖: X/Y (Z%) 未覆盖 n · 未知 k」③c **工具对账视图（boundary，`boundary.total_tools`>0 才显）**：轨迹里的领域工具动作 × 声明工具要求 对账——顶部「领域工具 a/b 已在声明中 · 未声明 n 个」+ 表（工具名 / 分类 / 声明出处 / 会话数 / 调用数 / transcripts），undeclared-domain 排前；分类 builtin（read/bash/grep 等内置原语 + non_business 白名单）不进分母、domain（命中声明）不标警、undeclared-domain（未命中）标「未声明」徽章；声明侧四源并集：正文工具要求指令 / 指令文本里的 `mcp__server__tool` 全名 / 反引号代码字 / **声明 frontmatter 工具白名单**（agent `tools:`、skill `allowed-tools:`，`declared_by` 显 `frontmatter: tools`——工具只写在 frontmatter 的 agent 靠这源不误标；`Tool(spec)` 权限限定形剥后缀入键，trace 调用名是裸名）；**显式「未声明 ≠ 违规」免责声明**（声明通常不穷举工具，此视图不构成违规判定，是行为基线的数据采集面）；声明零工具时头部另注「声明未列任何工具」防误读；CLI 摘要同步打一行「工具对账: 未声明领域工具 n 个 · m 会话（不构成违规判定）」（非零才打）④ findings 全集（**含 PASS**，每条带 verdict 徽章 + method 标签（程序/LLM）+ evidence + category + source + FAIL 噪声判别徽章（`noise_label`，见下「怎么看结果」⑥）+ **UNRESOLVED 行的根因标签**（`unresolved_cause` 短标签，hover 看类别）+ **同源矛盾徽章**（`conflict_with` 非空时：⊘ 判定矛盾 + 对端 #N 可点跳，见下②）；**可折叠分组、问题在前**——单 transcript 按 verdict 分组（✗失败 / ○没判成 / ?判不准 / ⊘不适用 默认展开、✓通过折叠），多 transcript 按指令分组（FAIL/UNRESOLVED/INDETERMINATE/N/A 默认展开））。

> **transcript 来源标注（cc / opencode）**：JSON 的 `transcript_sources`(与 `transcripts` 同序并行)标每条 transcript 来自哪侧(`"cc"` / `"opencode"` / …,旧 report 无此字段 → 空,向后兼容);HTML 头部 transcript 列表 + 每条 finding 的 transcript 行都带来源徽章,CLI 摘要行显 `sources: …`。多 transcript 混合来源(同时审 cc + opencode)时一眼分清。

> **「X 条由 transcript 反推」是什么**：findings 里 `derived_from_transcript: true` 的不是 SKILL.md 抽出来的，而是从 transcript 实际行为**补**出来的检查（只增不减）。当前只补一类：transcript 里 agent 真实 Read 过的输入/参考文件（`digest.input_files`）→ 各补一条「读取 \<path\>」（REFERENCE_USAGE）。例：SKILL.md 只写"读用户指定的日志文件"不点名，transcript 实际读了 `app.log` → 反推一条「读取 app.log」并确认命中。这类由 Read 行为反推的 read 检查构造上即 PASS（不膨胀 fail），作用是把 agent 真碰过的输入文件记进报告。`required_tools`（Bash 命令分不清业务/通用）、`required_outputs`（历史 trace 产物未必还在盘）不反推。

> **怎么看结果（别只数 fail）**：每条 finding 带 **#N 序号**。① **同文重复已去重**——同句被抽成 `CONSTRAINT` + `ANTI_FABRICATION` 的合并成一条、来源徽章显 `CONSTRAINT + ANTI_FABRICATION`（不再虚增 fail）。② **同行为跨类已关联**——同一行为（如没读某 reference）同时违反 条件性 + 步骤 两条**不同**规则的，仍是两条（确是两条规则），但互标 `↔ 同源 #N`（可点跳）；故 fail 条数仍可能 > 真实问题数，**逐条看 evidence 对照"哪个行为"**，别数条数。**同源矛盾已标注**——同一声明行被多个镜头抽取（如禁令 + 步骤各收一行）后判出一 PASS 一 FAIL 的，两条互标 `⊘ 判定矛盾 #N`（可点跳对端），警告框报「同源矛盾 N 组…建议人工复核对端证据」；标注不仲裁（不翻判、不加第三方判官），谁对谁错看两端 evidence 人工定夺。③ **没判成（UNRESOLVED）多 ≠ 全过**——LLM 没判成的一律 UNRESOLVED（见上），汇总成警告提示重跑；N/A（不适用）是已解决结论、不进警告。④ **对照预期**：自带 3 个 audit 示例的预期违规已逐条列明（见 [example-skills/README.md](../fixtures/example-skills/README.md)），跑完比对该抓的违规是否都出现在 FAIL、evidence 是否点中。⑤ **verdict 徽章 + method 标签**（每条）：左侧 verdict 五态图标（✓通过 / ✗失败 / ⊘不适用（场景不触发）/ ○没判成（可重跑）/ ?判不准（重跑无益）），右侧 method 标签「程序」（tool_trace 命中 / 子串匹配，不调 LLM）或「LLM」。⑥ **FAIL 噪声判别**（每条 FAIL + by_instruction 失败行）：`noise_label` 徽章（`tool_noise` 疑似工具噪声 / `skill_issue` 疑似声明问题 / `uncertain` 待确认）+ `noise_reasons`（hover 看理由）。fixture 下 FAIL 混着工具噪声（extractor 误提资源描述当步骤、evidence 自相矛盾、LLM 判定不稳）和真声明问题（硬约束程序判定 FAIL），判别帮区分、避免据噪声误改声明。治本仍 `--refine-instructions` / 结构化声明 / 真实 transcript audit。**source / category 徽章**：单次 audit-report、viewer per-run、整体汇总三处都显 `source`（InstructionSource，如 `CONSTRAINT` / `HEADING_STEP`）+ `category` —— `source` 比 `category` 细（6 vs 4），是判别噪声的关键信号（如 `HEADING_STEP` + 资源链接 → 疑似资源描述被当步骤）。⑦ **声明覆盖视图怎么读**（③b 区块 / CLI 一行）：covered/total 是本批对账的**证据面**——低覆盖 ≠ 声明差（条件式指令场景没触发很正常），但意味着上面的 FAIL 结论只覆盖被触达的子集；unknown 先治「建议重跑」的 UNRESOLVED（重跑常能把它变 covered/uncovered）；uncovered 集中的分组 = 该批会话没锻炼到的声明面，想验证就补跑对口场景的 transcript 再对账。⑧ **工具对账视图怎么读**（③c 区块 / CLI 一行）：「未声明」≠ 违规——声明通常不穷举工具（bash/read 本就不在声明里；skill/agent/编排声明同理），未命中只说明声明没写；此视图**不构成违规判定**（不产 finding、不进 verdict），用途是给「轨迹里出现了哪些声明之外的领域工具」一个诚实的数据面：一眼看出会话调了哪些 MCP/领域工具、哪些在声明工具要求里、哪些没有。它也是二期 per-skill 行为基线（声明工具要求 ∪ 零 FAIL 会话观测双源交叉后升级判罚）的数据采集面——本期的 sessions/calls 列就是在攒基线数据。⑨ **控制流 finding 怎么读**：声明里写了**显式顺序连词**（先/然后/再/接着/随后/之后/其次/下一步/最后 等）的同节相邻步骤，会多出一条合成 finding——`(顺序)「A」须先于「B」`。它是审计器**合成的关系声明**（`synthetic`），**不进**声明覆盖视图与 by_instruction 聚合表（不是声明原文指令），只在 ④ 明细出现，`↔ 同源` 链两端可点跳到 A/B 两条真实 finding。判定口径**宁少报**：只有 B 的**全部**证据都先于 A 才算违序（交错/部分重排算过），A 或 B 压根没发生算不适用；**数字编号列表（1. 2. 3.）不算顺序声明**（编号是排版惯例，倒序执行常常无害）。带**数量词**（每个/所有/逐个/至少 N 次/不超过 N 次…）的指令会移交给**控制流判官**判（不再由原条件/禁令/步骤判官判，每条指令仍只有一个 verdict）：「应做未做、数量不足」一律 FAIL（不洗成不适用），evidence 带实测数字与期望——但**窗口与条件边界**例外：单条任务级条目所属的任务/阶段在记录里从未开始（记录止于某处、该条目排在其后），或条件触发型条目（「据 X 的失败报告修复」类）的触发条件在记录里从未发生，判 N/A（证据不覆盖/条件未触发）；所属阶段已开始、条目中途没走的仍 FAIL。**完成度类**（逐一完成/全部完成其中的 Tasks/项）**以本执行记录为全集**——对账对象就是这份记录，记录结束时未完成就是数量不足（FAIL），判官不得推测「记录之外可能继续」来洗成 pass/na；「按顺序」与「完成全部」同现时完成度是主判据，记录内的顺序合规不掩盖数量不足（窗口外步骤/单条任务级条目逐条判 N/A 与此不矛盾：条目级不逐条冤枉，整体缺口由完成度条目兜底报出，单条判 N/A 不算漏报）。count 型且带工具名的少数场景由程序数 tool_trace 调用次数（method=程序，evidence 如「实测调用 X 1 次,不满足『至少 2 次』」）。数量 finding 用原指令原文/原类别（对聚合透明），但带 `control_flow="quantity"` 纯显示标记：CLI 违规表在指令前显 `(数量)`、HTML 概要表（违规 FAIL 行）与 finding 头挂「数量」徽章（聚合行透传同名字段）——不挂记号它在报告里长得跟普通 conditional/step 判定一模一样，认不出（ORDER 靠 `(顺序)` 合成文案自明，不另标）。⑩ **改进建议怎么读**（`--suggest` 可选、默认关；建议区块 / CLI 一行）：三类归因——`skill_fix`（声明写了、执行系统性没做到 → 改 skill 正文的建议）/ `declaration_fix`（执行做了更合理的事、声明过时 → 改声明的建议）/ 模型·环境（单发失败、工具噪声 → **不出建议**，只透明计数「N 条 FAIL 归因为模型/环境」）。每条建议带 #N 链接（点跳对应 FAIL finding）+ keeps_pass 徽章（自检不与 PASS 指令撞车；⚠ 未自检通过 = LLM 自己声明有冲突，人工复核）；skipped_reason 非空时只显原因行（失败 ≠ 全怪模型）。建议**仅供参考**——审计的判决不受建议影响（独立性红线）。

## sift-records v2：文件归因证据

Opsbot 可以将完整 session 事件与文件归因 capture 导出为一个结构化 JSON，直接作为 `--transcript` 输入。当前实现内部证据模型：

### 内部证据模型
#### 输入 Json 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `format` | string | 固定为 `sift-records` |
| `version` | integer / string / null | 真正的整数 `2` 表示 v2；缺省、`null`、整数 `1` 或字符串 `"1"` 按 v1 处理 |
| `producer` | string | 证据包生产方，例如 `opsbot-file-provenance` |
| `exported_at` | string | 导出时间 |
| `session` | object | session ID、native session 和父子 lineage 等元数据 |
| `records` | array | 按时间排序的 user/assistant turn；工具调用放在 `tool_calls` |
| `captures` | object | `capture_id` 到工具生命周期和文件 effects 的映射 |
| `blobs` | object | content-addressed before/after 文件内容 |
| `coverage` | object | producer 已知的证据覆盖情况 |
| `warnings` | array | producer 或 reader 需要人工关注的缺口 |

#### BaseModel 校验边界

Sift 只在 v2 分支调用 `SiftRecordsV2.model_validate()`。`SiftRecordsV2` 及其嵌套模型使用严格类型，不会把字符串数字或布尔值隐式转换为整数。模型 validator 同时校验 capture key、tool call 的 capture 引用、effect 的 blob 引用、base64 内容、SHA-256 和 size。校验失败会转换为统一的 `InputError`，整个 bundle 被拒绝。

v1 不经过这些模型，也不改变原有 `_load_v1_records()` 的行为。v2 在 canonical record 之前继续以 BaseModel 作为主表示；仅在把证据挂到既有 canonical cc-record 的 `_sift_evidence` 或 standalone capture 时转换为字典。

#### capture、effect 和 blob 规则

- `records[].tool_calls[]` 可以携带原生 `id`、`timestamp`、`result`、`is_error` 和 `capture_id`。提供原生 `id` 时 Sift 原样保留；缺失时生成的 `tu_<n>` 只是 canonical 管线内部的临时 ID，不声称是 producer 的原生 ID。
- `captures[capture_id]` 保存 actor、agent、runtime/model、工具生命周期、父子 session lineage 和 `effects`。即使没有 mutation，capture 也必须保留，表示工具执行过但没有产生文件变化。
- `effects[]` 至少包含 `path`；可以提供 `before_blob`、`after_blob`、`additions`、`deletions` 和 `conflict`。文件证据引用采用 `capture:<capture_id>:<path>`。
- `blobs` 只包含 mutation 涉及文件的完整 before/after 字节，不是整个 worktree 快照。`encoding` 为 `utf-8` 时保存文本，为 `base64` 时保存二进制。
- `SiftRecordsV2` 会校验 blob 的 SHA-256 和 size，以及 effect 对 blob 的引用。缺失引用、损坏内容或 hash 不匹配会使整个输入失败，不生成部分可信报告。

`captures[].quality` 只接受以下四种值：

| quality | 语义 | Sift 行为 |
|---|---|---|
| `observed` | 完整、无冲突且关联可用 | 可用于重建可信文件状态 |
| `incomplete` | 生命周期或 before/after 证据不完整 | 保留证据，但不覆盖可信状态 |
| `conflicted` | 存在重叠、冲突或跨 commit 情况 | 保留证据，但不覆盖可信状态 |
| `ambiguous` | 无法唯一归属到工具调用 | 保留为 standalone capture，不伪造 tool call |

`quality` 描述 capture 本身的证据完整性，`association_quality` 描述它与主轨迹 tool call 的关联结果；两者是独立字段。只有 `quality=observed` 且关联可用时，后续确定性分析才可以把 effect 当作可信文件事实。

一个 capture 被多个 tool call 引用时，或 capture 自身为 `ambiguous`，reader 会将其关联质量标记为 `ambiguous`。完全没有 tool call 引用的 capture 标记为 `unmatched`，仍会写入 canonical record 的 `_sift_standalone_capture` 字段，供 loose/root/agent 审计查看；Skill 切片不会把它归给附近的 Skill 调用。

#### 进入 audit 的中间格式

CLI 检测到 `sift-records` JSON 后，调用 `load_structured_records()` 将它展开为临时 canonical JSONL，再交给既有 `slice_transcript()`、`_digest_from_records()` 和 `TranscriptAuditor`。因此：

- `slice_transcript()` 接收的是 canonical JSONL，不直接解析 v2 顶层 bundle JSON；
- `_sift_bundle_meta` 只附在第一个 canonical record 上，切片时复制到每个 segment 的第一条记录；
- 关联 capture 放在工具块的 `_sift_evidence`，standalone capture 放在 `_sift_standalone_capture`；
- `SessionDigest` 保存 `evidence_refs`、`evidence_warnings`、`evidence_metadata` 和 `provenance_timeline`，同时继续填充原有 `tool_trace`、`input_files`、`produced_files`。

### 确定性 Provenance Analyzer

```python
from sift.audit.provenance import analyze_provenance

result = analyze_provenance(digest)
```

Analyzer 只消费已校验的 `SessionDigest`，不调用 LLM，也不修改既有声明审计结果；字段和取值以以下模型定义为准。

#### `ProvenanceFinding`

`ProvenanceFinding` 描述一条确定性文件归因观察，不属于 Markdown 声明的 `AuditFinding`，不会计入声明 coverage 或改变原有 PASS/FAIL。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `dimension` | string | 必填 | 分析维度：`evidence_health`、`attribution_integrity`、`file_churn`、`actor_ownership`、`verification_closure` |
| `severity` | string | 必填 | `info` 表示行为事实或诊断信息；`warning` 表示已确认的风险提示；`indeterminate` 表示证据不足、冲突或归因不确定，不能给出确定结论 |
| `code` | string | 必填 | 稳定的机器可读 finding 代码，例如 `UNMATCHED_CAPTURE`、`FINAL_MODIFICATION_UNVERIFIED` |
| `summary` | string | 必填 | 面向人的简短结论 |
| `explanation` | string | `""` | 规则触发原因和限制说明 |
| `paths` | array[string] | `[]` | 涉及的文件路径 |
| `actors` | array[string] | `[]` | 涉及的 actor/agent |
| `capture_ids` | array[string] | `[]` | 相关 provenance capture ID |
| `tool_use_ids` | array[string] | `[]` | 相关原生工具调用 ID |
| `evidence_refs` | array[string] | `[]` | 文件证据引用，格式通常为 `capture:<capture_id>:<path>` |
| `turn_refs` | array[integer] | `[]` | 能定位到的 transcript turn；无 turn 定位时为空 |

#### `ProvenanceAuditResult`

`ProvenanceAuditResult` 是 `analyze_provenance(digest)` 的返回值。它只描述文件归因分析结果，不替代 `AuditReport`。

`AuditReport.provenance` 是一个按 transcript 文件名索引的对象，值为对应的 `ProvenanceAuditResult`。`TranscriptAuditor.audit()` 会自动填充其中可用的 v2 结果；普通 v1/session 不写入条目。该字段只作为独立报告区块，不参与既有 findings、coverage、PASS/FAIL 汇总或 exit code。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `available` | boolean | `false` | 是否存在可供分析的 v2 provenance 证据；普通 v1/session 为 `false` |
| `coverage` | dict | `{}` | producer 提供的覆盖信息和缺口 |
| `statistics` | dict | `{}` | capture/effect 数量、quality/association 分布、可信 effect、涉及文件和最终写入者等统计 |
| `findings` | array[`ProvenanceFinding`] | `[]` | 独立的行为 finding 列表 |
| `file_timelines` | object<string, array[`ProvenanceTimelineEntry`]> | `{}` | 按路径组织的 effect 时间线摘要；节点包含 sequence、timestamp、capture、actor、quality、hash 和 trusted 状态，不复制完整 blob |
| `warnings` | array[string] | `[]` | 输入证据覆盖或分析过程中的警告 |

`available=true` 不代表所有文件 effect 都可信。可信文件状态必须同时满足 capture 和 effect `quality=observed`、`association_quality=exact` 且不存在 capture/effect conflict；其他 effect 会保留在 `file_timelines` 中，但不能覆盖可信状态，并可能产生 `warning` 或 `indeterminate` finding。

#### Analyzer 判定规则

`analyze_provenance()` 按以下顺序执行确定性规则：

1. **可用性**：`evidence_metadata.version == 2`，或 `provenance_timeline` / `evidence_refs` 至少一个非空时，`available=true`。因此 v2 的空 capture 仍然可分析；没有 provenance 证据的普通 session 返回 `available=false`。
2. **可信分区**：每个 `(capture, effect)` 只分类一次。可信 effect 必须同时满足 capture 和 effect 的 `quality=observed`、`association_quality=exact`，且 capture/effect 没有冲突；其余 effect 进入 untrusted 分区。所有 effect 都会保留在时间线中，但只有 trusted effect 参与最终文件状态、churn 和 ownership 判断。
3. **Evidence Health**：分别统计 unmatched、ambiguous、conflicted、incomplete capture；检查工具轨迹引用了但时间线缺失的 capture；合并输入 warnings 和无法重建的路径。证据不足或冲突使用 `indeterminate`，覆盖缺口和不完整生命周期使用 `warning`。
4. **Attribution Integrity**：检查 capture 与工具轨迹的工具 ID/名称是否一致；错误或明确失败的工具产生文件变更时报告 `ERROR_WITH_MUTATION`；只读工具产生变更时报告 `NON_WRITE_MUTATION`；明确成功的写工具没有 effect 时报告 `WRITE_WITHOUT_EFFECT`。
5. **File Churn / Actor Ownership**：只对 trusted effect 按路径排序，识别重复修改、恢复旧内容哈希和完整覆盖。多执行者修改同一路径本身不产生 finding；只有 producer 明确给出与前一执行者不一致的 `parent_agent` 时才报告 lineage 问题，缺少 actor 信息时报告 `LINEAGE_GAP`。
6. **Verification Closure**：对通常需要测试的代码文件，检查最终可信修改之后是否存在测试工具调用。`.md`、`.markdown`、`.mdx` 默认豁免；没有后续测试时报告 `FINAL_MODIFICATION_UNVERIFIED`。`is_error` 表示结果错误标记，`succeeded` 表示明确成功；两者矛盾时错误标记优先。测试失败后继续修改本身是正常修复流程，不单独产生 finding。

事件按有效时间戳、异常时间戳、缺失时间戳的优先级排序；同一优先级下使用解析后的时间和 event/capture ID 作为稳定顺序，因此分析结果可重复。

#### `ProvenanceTimelineEntry`

`file_timelines` 的每个列表元素都是 `ProvenanceTimelineEntry`。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `sequence` | integer | `0` | provenance 时间线中的稳定顺序 |
| `timestamp` | string/null | `null` | capture 开始或关联工具事件时间 |
| `capture_id` | string | 必填 | 归因 capture 标识 |
| `tool_use_id` | string/null | `null` | 关联的原生工具调用 ID |
| `actor` | string | `""` | 执行 actor/agent |
| `tool_name` | string | `""` | 产生 effect 的工具 |
| `quality` | string | 必填 | `observed`、`incomplete`、`conflicted` 或 `ambiguous` |
| `association_quality` | string | 必填 | `exact`、`ambiguous` 或 `unmatched` |
| `trusted` | boolean | `false` | 是否满足可信文件状态条件 |
| `evidence_ref` | string | 必填 | 通常为 `capture:<capture_id>:<path>` |
| `before_blob` / `after_blob` | string/null | `null` | before/after 内容 hash 引用 |
| `additions` / `deletions` | integer | `0` | 文件增删统计 |
| `conflict` | boolean | `false` | 该 effect 是否存在冲突 |

### `trigger` — 触发准确度（第 4 维）

测 cc 会不会**仅凭 skill 的 `description` 自然调用本 skill**——不评输出对错、只看触发。前三层（scoring / compliance / calibration）都默认 skill 已"上场"：scoring 的 `_build_non_interactive_prompt` 把 skill body 强制注入 cc prompt + "Follow its instructions" → cc 对"要不要够这个 skill"没得选，**触发能力从不被测**。一个永不触发的 skill，body 再好也是死重量；trigger-accuracy 补的就是这个上游缺口。

> skill 放进 `<cwd>/.claude/skills/<name>/`、给 cc 一条**中性 prompt**（不点名 skill），看 cc 按描述自动发现并调用。检测复用 `audit/slice.invoked_skills`（扫 transcript 里的 `Skill` tool_use）。**用真实 HOME、不隔离**（对齐 run/audit）：cc 2.x 的全局 plugin 不随 HOME（实测换空 HOME 后全局 plugin skill 照样加载，没隔掉），而 cc 的文件/keyring 认证源在 HOME 里——覆 `HOME=<temp>` 会把认证一起断掉，终端 shell（无 IDE 注入的 token）撞 "Not logged in" → 全 probe UNRESOLVED。故 spawn 用真实 HOME（`claude_cli_env` 补身份头 + 继承终端 env）。可信度不靠 HOME 隔离撑：`fired` 只看本 skill 名在不在 `invoked_skills`，别的 skill 参与发现不改变这个布尔。

```bash
sift trigger path/to/skill/ --runs 2 -o /tmp/out
sift trigger path/to/skill/ --evals path/to/evals.json --model claude-sonnet-5
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--evals` | 自动查找（`<skill>/evals/evals.json`） | 含 `trigger_probes` 的 evals.json 路径；无 `trigger_probes` 段 → 友好警告 + exit 0 |
| `--runs` | 1 | 每条 probe 重复跑几次（触发带随机性，多跑聚合成 `trigger_rate` 更可信） |
| `--model` | 用户默认 | claude -p 模型 |
| `--timeout` | 120 | 单次 spawn 超时（秒） |
| `--max-turns` | 10 | claude -p 最大轮数 |
| `--output` / `-o` | `<skill>/evals/` | 输出目录（写 `trigger-report.json` + `trigger-report.html` + 终端摘要） |
| `--open` | 关 | 写完后用浏览器打开 `trigger-report.html` |

**输出目录**：`trigger-report.json`（机器读）+ `trigger-report.html`（人读，always 写）+ `probe-<id>/run-<r>/` 沙箱（每条 probe 每次 run 一个，含 cc 的 `transcript.jsonl` 供人审）。**重跑自动清**：再跑同一 `-o` 目录时，开跑前先删旧的 `probe-*` 沙箱（report 靠覆盖），不留过期 transcript；`evals.json` 等非 trigger 产物不动。

**两种探题**：

- **should-trigger**（`expect_trigger: true`）：该 skill 该处理的真任务，措辞**中性**——不点名 skill、也**不照抄 description 原文**（生成时 prompt 强制 paraphrase；生成后还做一道 anti-leakage 校验：逐条检查 prompt 是否含 description 的 ≥15 字符连续子串，命中则报「词面命中」让用户复核）。照抄措辞测的是字面匹配，不是语义触发。
- **should-not-trigger**（`expect_trigger: false`）：看着相关但 skill **不该**触发的任务——测误触发 / description 过宽。两个来源（探题 `source` 字段区分）：`generated`（LLM 读 description 出题——信号源只有 description 本身，系统性偏「出题人想象」）/ `trace-mined`（`init --trigger-probes --from-transcript` 从真实会话挖的真题：领域相邻且未触发本 skill 的用户 query，事实层程序判定「未触发」、LLM 只按 description 过滤领域相邻——不喂 skill 正文防泄题；LLM 失败仍收、rationale 标 `unverified` 人审可删）。

**判定**：每条 probe 跨 N runs 算 `trigger_rate = fired 次数 / runs`，阈值 0.5 二值化 → PASS / FAIL；spawn 失败 / 超时 / **cc 非零退出且没触发**（限流 / 后端不可用：cc 实测 exit 1 + 错误打 stdout、stderr 空，`run_trigger` 不 raise 而返正常输出）→ 该 probe = `UNRESOLVED`（**不进率分母**、`trigger_rate` 置 `null`，宁少报不乱报）。聚合**分开报**（各带样本量）：`recall` = should-trigger 方向均值；`false_trigger_rate` = should-not 方向均值——两者是**不同失效模式、不合成一个分**。某方向 0 probe → 对应率 `null`（N/A，不是假 0.0）。

**分层诊断（纯诊断，主指标之外）**：每条 probe 报告期再标两个派生维度（不进 evals.json）——`phrase_type`（`literal` 词面命中 / `paraphrase` 语义改写，由 anti-leakage 子串检查定）+ `length_bucket`（`short` / `medium` / `long`，按 prompt 字符数）。`trigger-report.json` 的 `breakdown` 字段把 `recall` / `false_trigger_rate` 再按这两个维度切：例如「词面命中 recall 90% · 语义改写 recall 40%」→ cc 主要靠字面匹配触发，不是真懂。终端打一行 `分层 recall`，HTML 报告给两维度 × 两方向完整分层（详见 [handbook](docs/sift-handbook.md)）。

> **诊断定位、无门禁**：**无 gate / 不出 exit-code 判决**（纯诊断）。`trigger-report.json` 是独立 artifact，**不并入 `benchmark.json`**。用 [`init --trigger-probes`](#init--初始化评测定义) 生成探题。`description_hash` = sha256(frontmatter `description`)[:16]——锚定触发判定唯一依赖的输入，跨版本对比时判断 recall 变化是否源于 description 改动。成本：每 probe = 1 次 cc spawn（比 `eval` 轻——无 with/without 双 spawn、无 grader、无 compliance judge）。

**终端输出 + 产物**：

```
recall 75%(5 should-trigger) · false-trigger 10%(5 should-not) · 2 runs · 1 unresolved · 耗时 12.3s · 28400 tok
分层 recall: 措辞 词面 90% · 语义 40% | 长度 短 80% · 中 70% · 长 60%
  [+] probe-1: 100% → PASS (842 tok · 4.5s)
  [-] probe-2: 0%   → FAIL (318 tok · 2.1s)
  [?] probe-3: ?    → UNRESOLVED (0 tok · 0.0s)
操作建议：
  [rule:recall] 漏召:2 条该触发的任务 cc 没触发…考虑补触发关键词：
    · probe-2:「…」
  [llm:llm_rewrite] 修订 description:做 X…\n理由:收窄了 scope
Written to /tmp/out/trigger-report.json · /tmp/out/trigger-report.html
```

写 `trigger-report.json`（机器读）+ `trigger-report.html`（人读，**always 写**，与 JSON 并列，对齐 audit）+ 终端进度（逐 probe 实时 `probe i/N` + cc 动作 `🔧/✓`，同 `eval`）+ 跑完的摘要。汇总行：`recall`（绿）/ `false-trigger`（红）各带样本量 + runs + unresolved + 耗时 + 总 token；有 should-trigger 时还打一行**分层 recall**（措辞 词面 / 语义 · 长度 短 / 中 / 长，纯诊断）；逐 probe `[+/-/?] probe-N: 触发率 → verdict (token · 耗时)`（UNRESOLVED 触发率显 `?`、消耗显 0）。

**操作建议（怎么改 description 才更容易 / 更精准地被触发）**：报告 `suggestions[]` 给可直接照做的建议，两路——

- **规则建议**（永远给、零成本）：`recall`（should-trigger FAIL=漏召）→ 列出没触发的 prompt +「补触发关键词 / 动作动词」；`false_trigger`（should-not FAIL=误触）→「收窄 scope / 加边界排除语」；`unresolved` →「加大 `--runs` 重跑确认」；零 FAIL 零 unresolved → `all_good`（无需改）。
- **LLM 建议**（**有失败才自动跑、无 flag**）：喂当前 description + 失败 probe 的 prompt，起草一个修订 description 草案（可直接抄改）+ 一行理由。相对 trigger 本体（每 probe × run = 1 次完整 cc spawn）这是一次单轮 LLM 调用、开销 <5%，全 PASS 跳过（零成本），故默认开、不设 flag。LLM 调用失败自动重试 2 次（限流是瞬态——trigger 本体跑完紧接着调，常撞后端限流窗口）；重试耗尽只丢这条、规则建议照在，终端打失败原因（疑似限流 / 空响应）让你判断是瞬态还是真问题。

### `coverage` — 出题漏测诊断

测 evals.json 对 SKILL.md **内容点**（可测的指令 / 步骤 / 能力 / 条件分支）的覆盖——`covered_by` 为空的内容点 = **漏测**（0 道题覆盖）。`run` 只能验证被考卷覆盖到的点；一个漏测点是 `run` 够不到的盲区，再跑多少轮也测不着。coverage 把这些盲区标出来。

> **诊断定位、无门禁**（同 [`trigger`](#trigger--触发准确度第-4-维)）：**无 gate / 不出 exit-code 判决**，`coverage-report.json` 是独立 artifact，**不并入 `benchmark.json`**。不 re-run Claude 跑题——做的是**一次性 LLM 映射**：喂 SKILL.md 正文 + 各 eval 的 `id` / `prompt` / `rationale`，让 LLM 列出 SKILL.md 的可测内容点、并判断每点被哪几道 eval 真的**验证**（不是仅在 prompt 里提到）。`covered_by` 空 = 漏测。

```bash
sift coverage path/to/skill/                       # 默认:诊断漏测,出 JSON + HTML
sift coverage path/to/skill/ --augment             # 漏测点自动补题(写回 evals.json,需人审)
sift coverage path/to/skill/ --evals path/to/evals.json
sift coverage path/to/agent.md --kind agent        # 诊断 agent 定义内容点覆盖
sift coverage path/to/agent.md --kind agent --augment  # 漏测点自动补题(走 generate_for_agent)
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--evals` | 自动查找（`<skill>/evals/evals.json`） | evals.json 路径；不存在或无 eval → 友好警告 + exit 0 |
| `--augment` | 关 | 自动补漏测题（见下） |
| `--model` | 默认 | LLM 模型 |
| `--timeout` | 120 | LLM 调用超时（秒） |
| `--output` / `-o` | `<skill>/evals/` | 输出目录（写 `coverage-report.json` + `coverage-report.html`） |

**内容点怎么来 + 为什么不建结构化表**：LLM 从 SKILL.md 列出**可测的指令点**（具体的规则 / 步骤 / 能力 / 条件分支——不含散文和示例），映射靠各 eval 的 `rationale`（`init` 自报的自由文本）+ `prompt` 的**语义**，**不建结构化 instruction_ref 表**：自动建的 eval↔指令对应表准度和当场 LLM 判定（同一个 LLM）一样，却白搭维护复杂度；只有**人工逐条校对**的表才更准，而工具不做人工表。故 coverage 每次跑时当场让 LLM 按语义判。

> **诚实：LLM 估、有噪声**。内容点清单的粒度（怎么切一个点）是主观的，映射是 LLM 判的——都抖。**「0 覆盖」的大漏测可靠**（一个内容点没任何 eval 碰，是明确的盲区信号）；边界态的「半覆盖」（点被部分 eval 碰到）噪声大。它是**漏测预警、不是精确度量**——拿它定位明显盲区，别对每条 covered_by 过度解读。

**终端输出**（默认模式，无 `--augment`）：

```
内容点 12 · 漏测 3 · 耗时 8.4s · 4200 tok
漏测点(0 道题覆盖):
  · 安全事件分支
      ↳ SKILL.md:「若日志含安全事件标记,先读 security-policy.md 再判断」
  · 错误码不捏造
      ↳ SKILL.md:「不得编造日志中不存在的错误码」
  · 输出含时段聚合
      ↳ SKILL.md:「报告必须按端点×小时聚合并标出异常时段」
补题:逐条人工 review 后,用以下命令定向补(或加 --augment 自动补):
  init path/to/skill/ --append --focus "安全事件分支"
  init path/to/skill/ --append --focus "错误码不捏造"
  init path/to/skill/ --append --focus "输出含时段聚合"
Written to path/to/skill/evals/coverage-report.json · path/to/skill/evals/coverage-report.html
```

汇总行：内容点数 · 漏测数（红）· 耗时 · LLM token；逐漏测点列 `label` + SKILL.md 原文片段（`source_quote`，截断 120 字）；末尾给每个漏测点一条 `init --append --focus "<label>"` 处方（人工定向补，复用 [`init`](#init--初始化评测定义) 的 `--focus` 机制）。无漏测时显 ✓ 绿（`✓ 无漏测:SKILL.md 内容点均有 eval 覆盖。`）。

**`--augment`（自动补题）**：每个漏测点复用 `init` 的生成器（`EvalInitGenerator(focus=<label>)`）自动补 eval——生成时**喂现有 evals**（含本批已补题），走与 `init --append --focus` **同一个定向补题内核**（只补该漏测点这一面、与现有题语义去重），不再盲生成整套；新 eval 带 `review:coverage-augment` flag（标记待人审）、再按 `prompt` 字面去重作兜底（重复的跳过计数）、id 接续已有 `max+1`，写回 evals.json。某漏测点补题失败（限流 / 异常）逐条跳过、不中断整体。补完打印 `--augment:补 N 题(带 review:coverage-augment,需人审)`（有跳过则附 `· 跳过 M 题(与现有 prompt 重复)`）。

> **`--augment` 补题必须人审**：自动生成的 eval 质量不保证——`review:coverage-augment` flag 标着等审，人确认"这题真测到了那个漏测点、且不是凑数题"后再接受。别直接拿 `--augment` 的结果跑 `run`。

### `critique` — 正文内容质量诊断

诊断 SKILL.md **正文内容质量**——补 [`analyze`](#analyze--静态分析) 的 11 项格式体检只查格式、不查正文写得好不好的空白。一次 LLM 调用喂 SKILL.md 正文（`parse_skill_md` 的 `body_text`，**不含 frontmatter description**——description 留给 [`trigger`](#trigger--触发准确度第-4-维) / `analyze`），按 4 维 rubric 给**读者反馈**（不是打分）：① 失败机制编码 ② 可执行具体性 ③ 高风险黑名单 ④ 示例驱动。每维 `met`（满足）/ `partial`（部分）/ `missing`（缺失）+ `evidence`（**字符串数组**，每处引用一条、分属不同段落拆多条）+ `feedback`（✓ 认可 / 已做到）+ `gap`（✗ 缺口 + 方向），**大白话短句、不整段重写 SKILL.md**。**不依赖 evals.json**（纯读 body）。`--kind agent` 可审 **agent 定义正文**（system prompt）——4 维 rubric 与 skill 共用（agent 正文也是指令），只换 prompt 措辞，description 同样不进 critique。

> **诊断定位、无门禁**（同 [`trigger`](#trigger--触发准确度第-4-维) / [`coverage`](#coverage--出题漏测诊断)）：**无 gate / 不出 exit-code 判决**，`content-quality-report.json` 是独立 artifact，**不并入 `benchmark.json`**。LLM 调用失败降级为空报告（exit 0，不炸）。


> **linter 降姿态**：给的是"读者反馈"不是"打分"——LLM 是外行评内行领域，rubric 判定会漂（同一份正文 partial / missing 边界会抖），故只标三态提示哪里薄弱、不进任何 gate。当 linter 提示读，别当权威判决。

```bash
sift critique path/to/skill/                       # 诊断正文质量,出 JSON + HTML(单次,默认)
sift critique path/to/skill/ -o /tmp/out
sift critique path/to/skill/ --runs 3              # 多数投票跑 3 次,稳边界维度(显 X/N)
sift critique path/to/agent.md --kind agent        # 审 agent 定义正文(system prompt),4 维共用
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--model` | 默认 | LLM 模型 |
| `--timeout` | 120 | LLM 调用超时（秒） |
| `--runs` | 1 | 多数投票跑 N 次（>1 启用：每维 status 取多数、原文取首个判出多数档的 run、显 `X/N` 可信度；「结果会抖」） |
| `--kind` | `skill` | 声明对象：`skill`（审 SKILL.md 正文）/ `agent`（审 agent 定义正文，即 system prompt）。4 维 rubric 两边共用，只换 prompt 措辞；description 不进 critique（归行为层 trigger/dispatch-accuracy） |
| `--output` / `-o` | `<skill>/evals/` | 输出目录（写 `content-quality-report.json` + `content-quality-report.html`） |

**终端输出**：

```
正文内容质量 my-skill · 满足 1 · 部分 2 · 缺失 1 · 耗时 6.3s · 2800 tok
  失败机制编码 部分
      ↳ 「处理错误要小心」
      ✓ 有提醒读者注意错误
      ✗ 太泛了,没说具体栽在哪。补一条:某操作因某原因会失败,对应怎么查。
  可执行具体性 满足
      ↳ 「改完重新查对象拿真 ID」
      ↳ 「再用这个 ID 调用」
      ✓ 步骤具体、引用真实对象名,照着能干。
  高风险黑名单 缺失
      ✗ 没拉黑看起来对但会害死人的操作。补一条带原因的禁令。
  示例驱动 缺失
      ✗ 只有规则没演示。补一段真实命令或输入→输出。
Written to path/to/skill/evals/content-quality-report.json · path/to/skill/evals/content-quality-report.html
```

汇总行：满足 / 部分 / 缺失计数 · 耗时 · LLM token；逐维列 `name` + 状态徽章（满足 绿 / 部分 黄 / 缺失 红）+ `evidence`（每处引用一行，单条截断 160 字）+ `feedback`（✓ 认可 / 已做到，仅 met / partial）+ `gap`（✗ 缺口 + 方向，仅 partial / missing）。`--runs>1` 时维度名后附 `vote_tally`（如 `部分 (2/3)`）。

### `report` — HTML 报告

```bash
sift report output/        # 生成静态 report.html
sift report output/ --open # 生成并打开
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--benchmark` | 自动 | benchmark.json 路径 |
| `--previous` | 无 | 上一轮 benchmark.json（对比） |
| `--scorecard` / `--static` | 自动 | scorecard.json 路径（双别名） |
| `--open` | 关闭 | 生成后浏览器打开 |

自动发现 `benchmark.json`、`scorecard.json`、`vet-report.json`、`gate.json`（存在则标题旁渲染门禁徽章）。两个标签页：
- **单次执行** — 浏览该 run 的 prompt、输出文件、评估详情（打分 + 遵循性 + 校准三层）。顶部下拉按 eval 分组，可直接跳转到指定 run（不必逐个翻「上一个/下一个」）；方向键仍可逐条翻
- **整体汇总** — 门禁徽章 + 安全面板 + 评分卡 + 异常检测 + 通过率对比（双率）+ 盲评对比 + 深度分析 + 检查点总览（per-eval 程序/LLM/混合 3-dim 一览，点行跳明细）+ 逐项评测明细（per-run 3-dim + 检查项 ✓/✗ 矩阵）+ 遵循性聚合（整个考试）

---

## 评测体系

### 三层质量保障

三层各自独立产出，**不合成单一总分**：

```
┌─────────────────────────────────────────────────────────────┐
│                    Skill 质量评估                            │
├─────────────┬─────────────────┬─────────────────────────────┤
│  评分引擎    │  遵循性引擎      │  校准                       │
│ (Scoring)   │  (Compliance)   │  (Calibration)              │
│             │                 │                             │
│ 输出正确？   │  过程遵循？      │  结果×过程协调？             │
│             │                 │                             │
│ grading.json│ compliance.json │ calibration.json            │
└─────────────┴─────────────────┴─────────────────────────────┘
```

| 场景 | 评分 | 遵循性 | 正确判断 |
|------|------|--------|----------|
| 输出正确且过程遵循 | PASS | PASS | Skill 真正有效 |
| 输出正确但过程违规 | PASS | FAIL | 输出正确是巧合，不能归功于 Skill |
| 输出错误但过程遵循 | FAIL | PASS | Skill 逻辑正确但执行失败，需改进 |

> 三层是**并列独立信号**，不存在复合 Score 公式（历史文档里 `Score = StaticCompliance + DeltaPassRate + ...` 的公式在代码中并未实现）。消费方按需组合判断。

### 触发准确度（trigger-accuracy，第 4 维）

前三层都默认 skill 已"上场"——scoring 的 `_build_non_interactive_prompt` 把 skill body **强制注入** cc prompt + "Follow its instructions"，cc 对"要不要够这个 skill"没得选，**触发能力从不被测**。但真实会话里 cc 调不调用一个 skill，靠的**只有 frontmatter 的 `description`**——这一项三维都不评。一个永不触发的 skill，body 再好也是死重量。

[`trigger`](#trigger--触发准确度第-4-维) 命令补这个上游缺口：把 skill 放进 `<cwd>/.claude/skills/`、给中性 prompt，测 cc 会不会仅凭 description 自动触发（用真实 HOME、不隔离）。产 `trigger-report.json` + `trigger-report.html` + 「怎么改 description」的操作建议（规则永远给 + 有失败自动起草 LLM 改写草案）——`recall`（漏召）+ `false_trigger_rate`（误触）分开报、各带样本量，独立 artifact（**不并入 `benchmark.json`**），无 gate / 不进 exit-code 判决（诊断定位）。

### 双率：客观侧 vs 主观侧

每条 expectation/assertion 带 `source`（`programmatic` / `llm`）。**评分**按 source 拆**双率**，让干净信号不被 LLM 阅卷噪声污染：

| 率 | 来源 | 稳定性 | 信任度 |
|---|---|---|---|
| `pass_rate_programmatic` | 确定性断言（程序判定） | 稳（A=0 噪声） | 强信 |
| `pass_rate_llm` | LLM 语义判定 | 抖（阅卷噪声） | 看方向 |
| `pass_rate`（总率） | 全部混算 | 历史兼容 | 粗略 |

> **遵循性不分 source 双率**：`compliance.json` 现是 **AuditReport**（遵循性引擎已统一到 audit），按语义 category 四类（condition/prohibition/step/output）判，产 `summary{total,pass,fail,na,unresolved,indeterminate}`（没判成/判不准）。遵循度 = `pass/(pass+fail)`，排除 **N/A**（不适用）+ **UNRESOLVED**（没判成）+ **INDETERMINATE**（判不准）——三者都不进分母，与 audit 命令的 `audit-report.json` 同 schema。

**双主位分工**（fixture-eval 下）：
- **客观侧主位** = `delta.pass_rate_programmatic`（候选 - 基线）——A/B 噪声抵消到 0，最稳。
- **主观侧主位** = 盲比较胜率（见下）——同一 LLM 同时看两边产出判谁更好，相对判断消阅卷噪声。

`gate` 命令正是按这两轴判决：客观侧读 `delta_programmatic`，主观侧读盲比较 tie-排除胜率。

### 盲比较（BlindComparator）

同一 LLM 同一次看 side A / side B 两份产出（不知哪个是候选），判谁更好 + 打 rubric 分。**相对判断、噪声抵消**，比 per-run 独立打分稳。位置随机化（coin-flip 决定 A/B 摆位），winner 自描述为语义标签（`label_a`/`label_b`/tie）。

聚合为 `comparison_summary`：`a_wins` / `b_wins` / `ties`（genuine tie，计入分母）/ `failed`（调用失败，**排除出分母**）/ `valid` / `win_rate`（= `a_wins/valid`，tie-**计入**）/ `win_rate_ci_lo` / `win_rate_ci_hi`（Wilson 95% 置信区间；`lo>0.5` → A 显著更优，`hi<0.5` → B 显著更优，含 0.5 → 不显著、样本可能不足）。

> 注意 `comparison_summary.win_rate` 是 tie-**计入**版；`gate --min-win-rate` 用的是 tie-**排除**版 `a_wins/(a_wins+b_wins)`，两者在阈值边界会判决翻转——故 gate 自行计算，不复用该字段。

盲比较只服务主观题（客观题 programmatic 已把噪声消到 0），且在 `programmatic`-only 评分下自动跳过。

### 评分模式

| 模式 | 说明 | LLM 调用 | 适用场景 |
|------|------|----------|----------|
| `hybrid`（默认） | 先程序化断言，再 LLM 语义评分 | 是 | 通用场景 |
| `programmatic` | 仅确定性断言 | 否 | 输出结构化、可机械验证 |
| `llm` | 仅 LLM 语义评分 | 是 | 输出需要语义判断 |

### 程序化断言类型

**Tier 1 — 确定性**（程序判定，零噪声）：

| 类型 | 说明 | 参数 |
|------|------|------|
| `FILE_EXISTS` | 文件存在 | `filename` |
| `FILE_CONTAINS` | 文件含子串 | `filename`, `pattern` |
| `CONTAINS_PATTERN` | 正则匹配 | `pattern`, `path`(可选), `scope`(可选,`"transcript"` 时只查 agent/skill 文本输出不碰文件) |
| `JSON_HAS_FIELD` | JSON 字段 | `filename`, `field`（支持 `a.b.c`） |
| `LINE_COUNT` | 行数范围 | `min`/`max`(至少一个), `path`(可选) |
| `NOT_CONTAINS` | 反向匹配 | `pattern`, `path`(可选), `scope`(可选,同上) |
| `CUSTOM_SCRIPT` | 自定义脚本 | `script`, `args`(可选) |
| `CROSS_FILE_CONSISTENCY` | 检查相对 import | `include`(可选, glob) |
| `CODE_COMPILES` | 基础语法检查 | `include`(可选, glob) |
| `CUSTOM_PLUGIN` | 领域特定插件断言 | `plugin_name` |
| `TOOL_CALLED` | 检查工具调用（看 tool_trace） | `tool_name`, `min_count`(默认 1) |
| `STEP_COUNT` | agentic 步数范围（看 num_turns） | `min`/`max`(至少一个) |

**Tier 3 — 语义**（LLM 判定，归 LLM grader 处理）：

| 类型 | 说明 |
|------|------|
| `LLM_GRADE` | LLM 语义评分 |
| `LLM_FACT` | LLM 事实核查 |
| `LLM_NOT_DELEGATE` | 检查未把任务不当委托 |

### 测试层级

| 层级 | 测试什么 | 需要什么 |
|------|----------|----------|
| **L1**（单元级） | 单个 Skill 的规则理解是否正确 | evals.json + expectations |
| **L2**（Mock 场景） | Skill 在有 fixture 的场景下能否正确产出文件 | evals.json + assertions + fixtures |

### 校准判决（结果×过程 coherence）

`calibration.json` 是单次 with_skill 执行的**结果×过程 coherence 判决**——打分（结果面）与遵循性（过程面）协不协调，产出一个 5 态 `verdict`，不是凑出来的置信度数。

- **结果轴**：优先押**客观** `pass_rate_programmatic ≥ 0.8` → 结果对；纯主观题无客观锚时回退主观率（`scoring_rate_basis=mixed`，显示标注「基于主观判分」）。
- **过程轴**：遵循性（audit）里有无 prohibition/output **硬类**违规。有 → 过程错；无 → 过程对；compliance 缺失/无可判定 → 过程未知。step/conditional 的 FAIL 只记 `soft_notes`、不翻判决（避免 LLM 方差污染 verdict）。

| verdict | 结果 | 过程 | 含义 |
|---|---|---|---|
| TRUSTED 可信 | 对 | 对 | 未发现过程问题 |
| HIGH_SCORE_SUSPECT 高分存疑 | 对 | 错 | 输出对了但过程违反硬性规则（列 hard_violations evidence） |
| FIXABLE 可修复 | 错 | 对 | 输出未达标，过程无明显违规 |
| SKILL_FAILED 技能失效 | 错 | 错 | 输出未达标且过程违反硬性规则 |
| PROCESS_UNKNOWN 过程未知 | — | 无可对账 | compliance 缺失或无可判定项（如纯推理题） |

`calibration.json` 字段：`verdict` / `result_pass` / `process_status`（ok/violated/unknown）/ `hard_violations`（每条 category+instruction_text+evidence）/ `soft_notes`（step/conditional FAIL 计数）/ `scoring_rate` / `scoring_rate_basis`（programmatic|mixed）/ `scoring_rate_programmatic` / `scoring_rate_llm` / `compliance_rate`。

> 设计取舍：verdict 只押在实测高精度的 prohibition/output 硬类违规上（"高分存疑"= 值得一看的分诊，非定罪）。仅 with_skill 产（without 无 compliance）。旧版校准的 confidence/grade/agreement/dual-judgment 机制已移除——eval 设计**不再需要**为校准刻意"同一项同时写 assertion + expectation"。
>
> **聚合 + gate 消费**：per-run `calibration.json` 在 benchmark 聚合时聚合成顶层 `calibration_summary`（per-side：`sides: {<side>: {total, verdict_counts, high_score_suspects:[{eval,run,category,instruction_text,evidence,compliance_rate}]} | null}`；with/without 模式 without 侧 null，v1/v2 两侧都有）。gate 的 `_eval_calibration` 读它——把 HIGH_SCORE_SUSPECT 作 **soft warning**：分诊两侧、填 `GateResult.calibration` + `warnings`，**不进 axes、不改 decision/exit_code**（HIGH_SCORE_SUSPECT 是 gate 两轴的盲区：结果对→客观轴过、输出好→盲评过，但 prohibition 被违反）。SKILL_FAILED/FIXABLE 结果错归客观轴，不重复标。viewer `renderCalibrationSummary` 同源显示。

---

## JSON 输出格式

### evals.json（输入）

```json
{
  "skill_name": "my-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "用户的任务描述",
      "expected_output": "预期输出描述",
      "level": "L1",
      "fixtures": [],
      "expectations": ["可验证的预期语句1", "预期语句2"],
      "assertions": [
        {"name": "脚本存在", "type": "FILE_EXISTS", "params": {"filename": "output.py"}},
        {"name": "包含 json", "type": "CONTAINS_PATTERN", "params": {"pattern": "json.dump", "path": "output.py"}}
      ],
      "timeout": 600,
      "flags": [],
      "rationale": "对应 SKILL.md ## 步骤 第2条",
      "confidence": 0.8
    }
  ],
  "trigger_probes": [
    {"id": 1, "prompt": "<中性用户请求，应让 cc 够到本 skill>", "expect_trigger": true,  "rationale": "为何该触发"},
    {"id": 2, "prompt": "<看着相关但 skill 不该触发的请求>",  "expect_trigger": false, "rationale": "为何不该触发"}
  ]
}
```

> 顶层 `trigger_probes` 段**可选**（旧 evals.json 无 → `[]`，向后兼容），只 [`trigger`](#trigger--触发准确度第-4-维) 命令读、`run` 忽略它。字段见下表。

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 唯一标识，整数 |
| `name` | 否 | eval 名（空则显示 `eval-{id}`） |
| `prompt` | 是 | 发给 claude 的任务描述 |
| `expected_output` | 否 | 预期输出的文字描述(grader 评分参考,**不喂给被测 cc**;拼进 prompt = 答案泄露致 A/B 失真) |
| `level` | 否 | 测试层级：`L1`/`L2`，默认 `L1` |
| `fixtures` | 否 | 前置文件列表 |
| `expectations` | 否 | 语义预期语句列表（LLM 判） |
| `assertions` | 否 | 结构化断言列表（Tier 1 程序判 / Tier 3 语义类 LLM 判） |
| `timeout` | 否 | per-eval 超时覆盖（秒）；不设则用 `--timeout` |
| `source_dir` | 否 | 从 skill 目录拷进工作区的源目录 |
| `source_dir_as` | 否 | source_dir 拷贝的目标名（空则用源目录名） |
| `flags` | 否 | 后处理标记（如 `degraded:no-llm`）；默认空 |
| `rationale` | 否 | 该 eval 对应 SKILL.md 哪段/哪条（`init` 自报,审阅溯源;空 → `review:no-rationale`） |
| `confidence` | 否 | `init` 生成器自报 0-1（该题切中规则的把握）;仅 init 审阅分诊,**不入任何 gate**。同名区分:≠ audit-finding 的 confidence,也 ≠ 旧版校准已删的 confidence（grade/agreement 那套）——这是出卷阶段生成器自评 |

### trigger_probes（`trigger` 命令的输入，可选）

`evals.json` 顶层可选段，给 [`trigger`](#trigger--触发准确度第-4-维) 命令用（`run` 忽略它）。旧 evals.json 无此段 → `[]`（向后兼容）。由 `init --trigger-probes` 生成、id 接续已有 `max+1`。

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 唯一标识，整数 |
| `prompt` | 是 | 喂给 cc 的中性用户请求（should-trigger：不点名 skill；should-not：看着相关） |
| `expect_trigger` | 是 | `true` = 该触发（贡献 `recall`）/ `false` = 不该触发（贡献 `false_trigger_rate`） |
| `rationale` | 否 | 为何该 / 不该触发（人审抓手）；trace-mined 探题带 `[trace-mined: …]` 前缀，LLM 过滤失败的带 `[unverified: …]` |
| `source` | 否 | 探题来源：`generated`（LLM 生成，默认）/ `trace-mined`（真实会话挖的负例）/ `manual`（手写）。旧 evals.json 无此字段 → `generated`；`trigger-report.json` 的 `probes[].source` 透传（HTML 探题明细「来源」列） |

### grading.json

```json
{
  "expectations": [
    {"text": "The output includes X", "passed": true, "evidence": "Found in transcript Step 3",
     "source": "llm", "verdict": "PASS"}
  ],
  "summary": {
    "passed": 3, "failed": 1, "total": 4, "pass_rate": 0.75,
    "passed_programmatic": 2, "total_programmatic": 2, "pass_rate_programmatic": 1.0,
    "passed_llm": 1, "total_llm": 2, "pass_rate_llm": 0.5
  },
  "execution_metrics": {"tool_calls": {"Read": 3, "Write": 2}, "total_tool_calls": 5},
  "timing": {"total_tokens": 84852, "duration_ms": 23332, "total_duration_seconds": 23.3},
  "error": "",
  "cache_key": "d5077d0f9e83586a"
}
```

每条 expectation 带 `verdict`（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`）与 `source`（`programmatic`/`llm`）。报告里 UNEVAL/TIMEOUT/ERROR 与 FAIL **区分显示**（?/⏱/!），不会被混成红 ✗——"没评出来/超时/出错"不是"失败"。

`summary` 的双率字段（`*_programmatic` / `*_llm`）按 expectation 的 `source` 分桶；某类无 expectation 时为 `null`（不报假 0）。

`cache_key` 是**内容感知续跑键**：`= sha256(SKILL 正文 + eval 全身 + 模型/配置 + sift 代码版本)[:16]`。重跑同一 skill 时，`run` 只对 `cache_key` 与本次输入**完全一致**且 `summary.total > 0` 的槽跳过重跑（省 token）；改了 SKILL.md / evals / model / scoring 配置 / **升级了 sift 自身** → key 变 → 自动失效重跑，不再盲复用旧结果。代码版本 = `src/sift/**/*.py` 内容聚合 hash（进程内只算一次），故 `pip install -e .` 升级或本地改了 runner/grader 后默认目录重跑也会用新代码。BASE 侧（`without_skill`）不含 skill 正文，故改 SKILL.md 只重跑 with 侧。**已知缺口**：skill 目录里 eval 引用的 `source_dir` / `references/` 文件内容改动不检测——改这些请用新 `-o` 强制重跑。老 `grading.json`（无此字段）首次重跑时全部失效一次（向后兼容）。

### benchmark.json

```json
{
  "metadata": {"skill_name": "my-skill", "timestamp": "2026-07-12T10:30:00Z", "runs_per_configuration": 3},
  "runs": [...],
  "run_summary": {
    "with_skill": {
      "pass_rate": {"mean": 0.85, "stddev": 0.05},
      "pass_rate_programmatic": {"mean": 0.90, "stddev": 0.04},
      "pass_rate_llm": {"mean": 0.80, "stddev": 0.08}
    },
    "without_skill": { "...": "..." },
    "delta": {
      "pass_rate": "+0.50",
      "pass_rate_normalized_gain": "+0.83",
      "pass_rate_programmatic": "+0.27",
      "pass_rate_llm": "+0.12",
      "time_seconds": "+13.0",
      "tokens": "+1700"
    }
  },
  "comparisons": [
    {"winner": "with", "reasoning": "...", "rubric": {...}, "output_quality": {...}, "failed": false}
  ],
  "comparison_summary": {
    "total": 10, "a_wins": 6, "b_wins": 2, "ties": 1, "failed": 1,
    "valid": 9, "win_rate": 0.67, "win_rate_ci_lo": 0.36, "win_rate_ci_hi": 0.89,
    "label_a": "with", "label_b": "without"
  },
  "analysis": {...},
  "llm_token_usage": {
    "scoring": {"input_tokens": 8200, "output_tokens": 1100, "total_cost_usd": 0.09, "calls": 14},
    "audit": {"input_tokens": 15000, "output_tokens": 2000, "total_cost_usd": 0.21, "calls": 9},
    "executor": {"input_tokens": 84852, "output_tokens": 0, "calls": 0},
    "total": {"input_tokens": 108052, "output_tokens": 3100, "total_cost_usd": 0.30}
  },
  "llm_duration_seconds": {
    "scoring": 12.5, "audit": 45.0, "executor": 3.2,
    "total": 52.3
  }
}
```

每条 `comparisons` 还携带 `rubric`（两侧 overall/content/structure 分）、`output_quality`（score + strengths/weaknesses 数组）、`expectation_results`（两侧 passed/total + 逐 expectation `{text, passed}` 明细）——报告逐条比较渲染这些；`analysis.transcript_insights`（dict）、`metadata` 的 `executor_model`/`analyzer_model`/`comparator_model` 也一并展示。

- 每个 config 的 `run_summary` 含 `pass_rate` / `pass_rate_programmatic` / `pass_rate_llm`（后两者可能为 `null`）+ `time_seconds` / `tokens`。
- `delta` 全为有符号字符串（`+0.27`），= `configs[0] - configs[1]`（候选 - 基线）；双率项可能为 `null`。
- **客观 Δ 趋零解读（D6）**：`delta.pass_rate_programmatic` 绝对值 ≤ 0.05（对齐染色阈值）= with/without 客观通过率无显著差异 = skill 相对 base 未带来增益。viewer「通过率变化」卡显 ⚠️ 警告。**⚠ 只是「无增益」信号，不等于 skill 无用——先排查依赖目录是否进入工作区（常见假无增益，见 handbook），排除后再考虑 skill 已被模型原生能力覆盖（dead weight）、该核查 scope 或收窄**。客观为主（稳）；主观 Δ 不单独触发（漂）。
- `comparison_summary.win_rate` = `a_wins/valid`（tie-计入）；`gate` 用 tie-排除版自行算。`win_rate_ci_lo/hi` = Wilson 95% 区间，viewer 盲评胜率卡显区间 + 显著性（样本不足标「不显著，建议加 run」）。
- `delta.pass_rate_normalized_gain` = `(post-pre)/(1-pre)` 天花板修正（raw Δ 在高分段如 0.9→0.95 系统性低估改进）；viewer Δ 卡并列报出。
- viewer 检查点总览：主侧（with）某 eval 跨 run 的程序通过率忽过忽挂（不全等）→ 题号旁 ⚠ 红旗，tooltip 给补跑命令 `run --eval-ids <id> --runs 更大`（补跑须调大 runs 才走 accumulate 累加，否则 fresh 会归档全部旧采样）。
- `selection`（可选,仅 `--changed-from` 子集跑时）:`{mode, ids, affected, total}`——标识本次为受改动影响的子集跑、delta 非整体测量。
- `llm_token_usage`:本次评测的 LLM token 用量,分四段——`scoring`(grader+comparator+analyzer 合计(另细分 grader/comparator/analyzer 分计段),带 cost/calls,calls=各子项真实调用次数之和)、`audit`(compliance/audit rollup 自 per-run compliance.json)、`executor`(被测 cc token,仅 token 数无 cost)、`total`(全加 token;cost = scoring+audit)。**executor 是被测 skill 的 token 效率指标(无 cost);scoring+audit 才是评测系统付出的 LLM 成本**。现有「令牌数」/「Δ 令牌数」(executor)语义不变,viewer 头部新增「LLM token 用量」分列(评分/遵循性/executor)+ 总计。
- `llm_duration_seconds`:本次评测的耗时(秒),同四段——`scoring`(grader+comparator+analyzer 调用时长加总(另细分 grader/comparator/analyzer 分计))、`audit`(compliance rollup)、`executor`(各 run 时长加总)、`total`(**墙钟**,run 命令头尾独立测)。**与 token 的关键差异**:token 是消耗品可累加(total=分列和);time 是墙钟、各段并行重叠(workers/线程池),**分列和 ≥ total 是正常的**,total 不由分列推导、viewer **不对账**分列和 vs total。分列看"哪段重",total 看"等多久"。viewer 头部「耗时」分列 + 墙钟总计。

### gate.json（`gate` 命令产出）

```json
{
  "decision": "PASS",
  "exit_code": 0,
  "reason": "客观侧无回归(+0.27); 主观胜率 0.75 >= 0.50 (8 决定性)",
  "artifact": "output/benchmark.json",
  "candidate": "with_skill",
  "baseline": "without_skill",
  "thresholds": {"min_win_rate": 0.5, "min_valid": 3, "programmatic_guard": true},
  "axes": {
    "programmatic": {"verdict": "PASS", "delta": 0.27, "reason": "..."},
    "comparison": {"verdict": "PASS", "win_rate": 0.75, "decisive": 8, "a_wins": 6, "b_wins": 2, "ties": 1, "reason": "..."}
  },
  "warnings": [],
  "calibration": {
    "sides": {
      "with_skill": {"triggered": true, "count": 3, "total": 21,
                     "evidence": [{"eval": 1, "run": 2, "category": "prohibition",
                                   "instruction_text": "引用日志原文行", "evidence": "未引用",
                                   "compliance_rate": 0.875}]}
    }
  }
}
```

`axes` 只列**启用的轴**：示例开了 `programmatic_guard` + `min_win_rate`，故只有 `programmatic` + `comparison`；`--must-pass` / `--baseline` 启用时 `axes` 会多 `must_pass`（candidate 侧指定题全过才 PASS）/ `drift`（题集 id 增删）两轴。`calibration` 的 `HIGH_SCORE_SUSPECT` 进 `warnings` 作 soft warning，不进 `decision` / `exit_code`。

### trigger-report.json（`trigger` 命令产出）

```json
{
  "skill_name": "my-skill",
  "description": "分析日志、找出异常突增的时段",
  "description_hash": "a1b2c3d4e5f60718",
  "runs": 2,
  "recall": 0.75,
  "false_trigger_rate": 0.10,
  "n_should_trigger": 5,
  "n_should_not": 5,
  "unresolved": 0,
  "token_usage": {"total_tokens": 28400, "suggest": {"input_tokens": 200, "output_tokens": 80, "total_cost_usd": 0.02}},
  "duration_seconds": 12.3,
  "suggestions": [
    {"category": "recall", "source": "rule", "message": "漏召:…考虑补触发关键词:\n  · probe-N:「…」", "probe_ids": [3]},
    {"category": "llm_rewrite", "source": "llm", "message": "修订 description:…\n理由:…", "probe_ids": [3]}
  ],
  "probes": [
    {"id": 1, "expect_trigger": true,  "prompt": "把访问日志按端点聚合并标出异常时段", "phrase_type": "paraphrase", "length_bucket": "medium", "trigger_rate": 1.0, "verdict": "PASS", "tokens": 842, "duration_seconds": 4.5, "transcript_path": "evals/probe-1/run-0/transcript.jsonl"},
    {"id": 2, "expect_trigger": false, "prompt": "把这本小说翻译成英文", "phrase_type": "paraphrase", "length_bucket": "short", "trigger_rate": 0.0, "verdict": "PASS", "tokens": 318, "duration_seconds": 2.1, "transcript_path": "evals/probe-2/run-0/transcript.jsonl"}
  ],
  "breakdown": {"by_phrase": {"recall": {"literal": 0.9, "paraphrase": 0.4}, "false_trigger": {"literal": null, "paraphrase": 0.1}},
                "by_length": {"recall": {"short": 0.8, "medium": 0.7, "long": 0.6}, "false_trigger": {"short": null, "medium": 0.1, "long": null}}}
}
```

| 字段 | 说明 |
|------|------|
| `skill_name` | 被测 skill 名（frontmatter `name`） |
| `description` | 被测 skill 的 frontmatter description 原文（HTML 展示 + LLM 建议的输入；旧报告无此字段 → `""`） |
| `description_hash` | sha256(frontmatter `description`)[:16]——触发判定唯一依赖输入的锚，跨版本对比 / 判断 recall 变化是否源于 description 改动 |
| `runs` | 每条 probe 跑的次数 |
| `recall` | should-trigger 方向 `trigger_rate` 均值；该方向 0 probe → `null`（N/A） |
| `false_trigger_rate` | should-not 方向 `trigger_rate` 均值；该方向 0 probe → `null`（N/A） |
| `n_should_trigger` / `n_should_not` | 两方向 probe 数（各自样本量） |
| `unresolved` | spawn 异常 / cc 非零退出且没触发（限流）的 probe 数（**不进** recall / false_trigger_rate 分母） |
| `token_usage` | `{total_tokens}`（所有 probe spawn 的 cc token 合计）+ 可选 `suggest`（LLM 建议那次调用的 input/output/cost，有失败才产生） |
| `duration_seconds` | cc spawn 总墙钟（各 probe 各 run `RunOutput.duration_ms` 之和 / 1000；不含 LLM 建议那次调用；旧报告无此字段 → `0.0`） |
| `suggestions[]` | 操作建议：`category`（`recall` / `false_trigger` / `unresolved` / `all_good` / `llm_rewrite`）+ `source`（`rule` / `llm`）+ `message`（中文文案，失败时带 probe 的 prompt 原文）+ `probe_ids` + `tokens` / `duration_seconds`（LLM 建议那次调用（走 `call_llm_with_retry`）的消耗，含重试；`rule` 建议为 0）。规则建议永远给；`llm_rewrite` 仅在有失败时自动产生（见上「操作建议」） |
| `probes[]` | 每条 probe：`id` / `expect_trigger` / `prompt`（中性请求原文）/ `phrase_type`（`literal` 词面命中 / `paraphrase` 语义改写，anti-leakage 子串检查定）/ `length_bucket`（`short` / `medium` / `long`）/ `trigger_rate`（fired / runs；UNRESOLVED 时 `null`）/ `verdict`（PASS / FAIL / UNRESOLVED）/ `tokens` / `duration_seconds` / `transcript_path`（首个成功 run 的 transcript 路径 `probe-<id>/run-0/transcript.jsonl`，人审 / 重跑抓手；`--runs`>1 其余在同目录 `run-1…`，spawn 全失败则空） |
| `breakdown` | 分层 rollup（纯诊断）：`by_phrase` / `by_length` 各含 `recall` / `false_trigger` 两方向，把主指标再按措辞（词面 / 语义）、长度（短 / 中 / 长）切；某组无 probe → `null`。旧报告无此字段 → `{}` |

> `recall` 与 `false_trigger_rate` 是**不同失效模式、分开报**（前者测漏召、后者测误触），不合成单一分。某方向 0 probe → 对应字段 `null`（N/A，不是假 0.0）。**无 gate、不进 `benchmark.json`**（诊断定位，独立 artifact）。

### coverage-report.json（`coverage` 命令产出）

```json
{
  "skill_name": "my-skill",
  "points": [
    {"label": "按端点聚合", "source_quote": "报告必须按端点聚合并标出异常时段", "covered_by": [1, 3]},
    {"label": "安全事件分支", "source_quote": "若日志含安全事件标记,先读 security-policy.md", "covered_by": []}
  ],
  "token_usage": {"input_tokens": 3000, "output_tokens": 1200, "total_cost_usd": 0.04},
  "duration_seconds": 8.4
}
```

| 字段 | 说明 |
|------|------|
| `skill_name` | 被测 skill 名 |
| `points[]` | 内容点清单（LLM 从 SKILL.md 列出），每条：`label`（短名，截断 200 字）/ `source_quote`（SKILL.md 原文片段，LLM 自报、mostly-verbatim 非保证逐字）/ `covered_by`（覆盖该点的 eval id 列表，**空 = 漏测**） |
| `token_usage` | 这次 LLM 映射调用的 `{input_tokens, output_tokens, total_cost_usd}`（LLM 失败降级空报告时为 `{}`） |
| `duration_seconds` | LLM 调用墙钟（秒） |

> **`gaps` 是派生属性、不存 JSON**：`coverage-report.json` 只存 `points[]`；「漏测」= `covered_by` 为空的 point，读取时派生（`report.gaps = [p for p in points if not p.covered_by]`），不冗余存进 JSON（避免与 points 失同步）。`coverage-report.html` 渲染同一份 points，漏测行（`covered_by` 空）高亮红色。

### content-quality-report.json（`critique` 命令产出）

```json
{
  "skill_name": "my-skill",
  "dimensions": [
    {"key": "failure_mechanism", "name": "失败机制编码", "status": "partial", "evidence": ["处理错误要小心"], "feedback": "有提醒读者注意错误。", "gap": "太泛了,没说具体栽在哪。补一条:某操作因某原因会失败,对应怎么查。"},
    {"key": "actionable_specificity", "name": "可执行具体性", "status": "met", "evidence": ["改完重新查对象拿真 ID", "再用这个 ID 调用"], "feedback": "步骤具体、引用真实对象名,照着能干。", "gap": ""},
    {"key": "high_risk_blacklist", "name": "高风险黑名单", "status": "missing", "evidence": [], "feedback": "", "gap": "没拉黑看起来对但会害死人的操作。补一条带原因的禁令。"},
    {"key": "examples", "name": "示例驱动", "status": "missing", "evidence": [], "feedback": "", "gap": "只有规则没演示。补一段真实命令或输入→输出。"}
  ],
  "token_usage": {"input_tokens": 1800, "output_tokens": 1000, "total_cost_usd": 0.03},
  "duration_seconds": 6.3
}
```

| 字段 | 说明 |
|------|------|
| `skill_name` | 被测 skill 名（frontmatter `name`） |
| `dimensions[]` | 4 维评审，每条：`key` / `name`（中文显示名）/ `status`（`met` 满足 / `partial` 部分 / `missing` 缺失）/ `evidence`（**字符串数组**，LLM 引用的 SKILL.md 正文片段，每处引用一条、分属不同段落拆多条；missing 为 `[]`）/ `feedback`（**认可 / 已做到**的大白话，1 句：met 好在哪、partial 已做到的、missing 空）/ `gap`（**缺口 + 方向**的大白话，1-2 句：met 空、partial / missing 缺什么 + 怎么补；不整段重写原文）/ `vote_tally`（多数投票可信度 `X/N`，**仅 `--runs>1`** 有；单次跑为空）。按 `key` 归位（4 条固定，不依赖 LLM 返回顺序）；漏条 / 非法 status → 该条默认 `missing` |
| `token_usage` | 这次 LLM 调用的 `{input_tokens, output_tokens, total_cost_usd}`（LLM 失败降级空报告时为 `{}`） |
| `duration_seconds` | LLM 调用墙钟（秒） |

> **`status_counts` 是派生属性、不存 JSON**：`met` / `partial` / `missing` 三态计数在终端 / HTML 汇总用时读取派生（`report.status_counts`），不冗余存进 JSON。三态**不用 `n/a`**——4 条 rubric 对任何 skill 都适用，极简 skill 没"高风险操作" → `missing` 正好提醒补。

### 输出文件说明

`run` 命令完成后，`output/` 目录：

| 文件 | 内容 |
|------|------|
| `benchmark.json` | 聚合结果（双率 delta + 盲比较 comparison_summary + calibration_summary per-side 高分低遵循 + `llm_token_usage` 评测 LLM 成本 scoring/audit/executor/total；`--changed-from` 子集跑时另记 `selection`） |
| `report.html` | 自包含 HTML 报告（读 gate.json 显门禁徽章 + previous/benchmark.json 显「vs 上次」纵向 delta 卡）；eval 末尾自动产（也可 `report` 命令重生成 / 带 `--previous` 基线对照） |
| `previous/` | 重跑归档：fresh 重跑时把上一次完整 run 树整体 MOVE 到此（单槽覆盖）。含旧 `benchmark.json`（聚合纵向 delta 数据源）+ 旧 outputs/（report「上次输出」文件级面板）+ 旧 `report.html`（eval 末尾自动产，随树归档）；别命令产物（`audit-report.*`/`slices/`/`gate.json`/skill 级报告）与未知文件不卷。仅 fresh 产生；accumulate/resume 不产。见下「重跑归档」 |
| `gate.json` | `gate` 命令产出（非 run 产出）；判决 + 两轴明细 + calibration soft warning（高分低遵循 HIGH_SCORE_SUSPECT，不进 decision） |
| `scorecard.json` | eval 末尾自动产（静态评分卡；也可 `analyze -o` 单独产） |
| `vet-report.json` | eval 末尾自动产（原生安全扫描；也可 `vet -o` 单独产） |
| `with_skill/eval-<id>/run-1/grading.json` | expectations + assertions 的 pass/fail（双率） |
| `with_skill/eval-<id>/run-1/compliance.json` | SKILL.md 指令遵循性（AuditReport：summary 计数 + findings 五态 PASS/FAIL/N-A/UNRESOLVED/INDETERMINATE；遵循度 = pass/(pass+fail)，排除 N/A + UNRESOLVED + INDETERMINATE） |
| `with_skill/eval-<id>/run-1/calibration.json` | 结果×过程 coherence 判决（5 态 verdict；仅 with_skill 产） |
| `with_skill/eval-<id>/run-1/run_debug.json` | 完整回复 + tool_calls + timing |
| `with_skill/eval-<id>/run-1/timing.json` | 执行耗时和 token 统计 |

#### 重跑归档（resume / accumulate / fresh）

`run` 开始时按 `output/benchmark.json` 是否存在 + 现有 slot 的 cache_key 是否仍匹配 + `--runs` 是否调大，判三种行为：

| 情况 | `--runs` vs 已有 | 行为 | previous/ |
|---|---|---|---|
| 上次没跑完（`benchmark.json` 不在） | 任意 | **resume**：cache 续跑已完成的 slot，补跑剩下的 | 不产 |
| 上次跑完 + inputs 没改 | 调大 | **accumulate**：旧 slot cache 复用 + 补新 run 号，聚合算全部 | 不产 |
| 上次跑完 + inputs 改了（skill/代码/eval/model） | 任意 | **fresh**：旧树 MOVE 到 `previous/` + 全部 fresh 重跑 | ✅ 产生 |
| 上次跑完 + inputs 没改 | 同/变小 | **fresh**：MOVE 旧树 + fresh 重跑（看复现性 / 主观侧方差） | ✅ 产生 |

fresh 用 **MOVE**（非 copy）：清空原位 → 全 slot cache miss → 必然 fresh 重跑；同时旧树进 `previous/` 供对比。重跑要看的就是方差（cc 非确定性，主观侧有真方差），copy 反而会让 cache skip 出"和上次一模一样"的无意义归档。accumulate 的"复用旧"靠 cache_key 匹配自然达成（不 MOVE → grading.json 在位 → skip）。

report 读 `previous/benchmark.json` 算「vs 上次」聚合 delta（双率 + 盲评 + 样本数）；样本数不同（如 6 vs 上次 3 runs）时显式标出提醒可比性。文件级「上次输出」面板只对 previous 里也存在的 run 显示，本次新增的 run 自然无对照（隐藏）。

---

## 项目结构

```
sift/
├── pyproject.toml
├── agents/                        # LLM prompt 模板
├── src/sift/
│   ├── cli/                       # typer CLI 入口(12 个命令)
│   ├── models/                    # Pydantic 数据模型
│   ├── parser.py                  # SKILL.md 解析器
│   ├── config/                    # 配置驱动架构
│   ├── security/                  # 安全审查(4 扫描器)
│   ├── static_analysis/           # 静态分析(11 项检查)
│   ├── evals/                     # evals.json 加载/生成 + transcript + baseline
│   ├── execution/                 # Claude 执行 + A/B 测试 + run_paths(retry 索引)
│   ├── scoring/                   # 评分引擎(程序化 + LLM + 盲比较 + 分析)
│   ├── compliance/                # 遵循性引擎
│   ├── calibration/               # 评分校准
│   ├── audit/                     # 真实 transcript 对账(TranscriptAuditor)
│   ├── gating/                    # gate 判决纯函数(客观+主观双轴)
│   └── reporting/                 # HTML 报告
├── tests/
└── docs/
```

---

## Exit Code

各命令语义不同，无全局统一含义：

### `eval` / `run`

两命令退出码同源（纯数据生产者）：

| Code | 含义 |
|------|------|
| 0 | 产数据成功（**不代表评测通过**——通过/失败由 `gate` 判） |
| 2 | 输入错误（SKILL.md 缺失、evals.json 格式错误；`run` 缺 `--task`） |
| 3 | 执行错误（claude CLI 未找到、超时） |

`--security-gate` 触发（BLOCKED/REVIEW_NEEDED）时退 1。

### `gate`

| Code | 含义 |
|------|------|
| 0 | PASS |
| 1 | FAIL（客观回归 或 主观不过阈值） |
| 2 | N/A（不适用，无适用轴）或 UNCLEAR（判不准，信号不足）或 使用错误（文件/解析错误）——靠 gate.json 是否存在 + decision 字段区分 |

### 其他

| 命令 | 退 1 条件 |
|------|----------|
| `vet` | Verdict = BLOCKED |
| `analyze` | score < max_score |
| `init`/`inspect`/`freeze`/`gate`/`report` | 2 = 输入错误 |

---

## 常见问题

### 安全审查需要安装额外的扫描器吗？

不需要。内置 2 个原生扫描器（secrets-scan、structure-check），开箱即用。aguara / skill-scanner 是可选增强。

### `eval` 退 1 了，是评测失败吗？

不一定。`eval` 是**纯数据生产者**——退 1 只在 `--security-gate` 触发时；负 delta 不再导致退 1（旧逻辑已退役）。评测通过/失败改由独立 `gate` 命令判：

```bash
sift eval v2/ --skill-b v1/ --evals baseline.json --runs 3 -o out/
sift gate out/benchmark.json --min-win-rate 0.5
echo $?   # 0=PASS / 1=FAIL / 2=N/A(不适用) 或 UNCLEAR(判不准)
```

### 静态分析 / inspect 需要 Claude CLI 吗？

不需要。`analyze`、`inspect` 都是零 API 成本、<1s 完成。

### evals.json 放在哪里？

推荐 `<skill_path>/evals/evals.json`。`run` 会自动查找，也可用 `--evals` 手动指定。跨 run 想固定同一份题，用 `freeze` 冻结成 baseline.json 并 commit。

### 如何在 CI 中使用？

```bash
# 1. 静态体检 + 安全审查
sift analyze path/to/skill/ --format json -o scorecard.json
sift vet    path/to/skill/ --format json

# 2. 冻结基线题集(commit 它,保证同卷)
sift freeze path/to/evals.json --name baseline

# 3. A/B 跑(候选 v2 vs 基线 v1)
sift eval v2/ --skill-b v1/ --evals baseline.json --runs 3 -o out/

# 4. 出判决 → exit code 驱动 CI check
sift gate out/benchmark.json --min-win-rate 0.5
# exit: 0=PASS(放行) / 1=FAIL(卡合并) / 2=N/A(不适用) 或 UNCLEAR(判不准),消费方定
```

门禁阈值与 CI 落点（分支保护、check 名、N/A / UNCLEAR 是否阻塞）归消费方配置——sift 只提供机制（PASS/FAIL/exit-2 兼盖 N/A 与 UNCLEAR + gate.json）。
