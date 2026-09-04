# Sift 使用手册

> 本手册面向第一次接触 sift 的读者，从「为什么需要它」讲起，逐层展开每个功能的设计思路与使用方法。读完能够：理解 sift 的整体架构、独立跑通**评测**和**审计**两件事、看懂每一份产物、在 CI 里用它做门禁。
>
> 命令的完整参数表、JSON schema、字段级定义请查 [参考手册](sift-reference.md)。本手册负责把「为什么这么设计 + 怎么用 + 怎么看结果」讲透，需要查具体参数时再回参考手册。

---

## 目录

### 总论

- [第一章 建立直觉：sift 在做什么](#第一章-建立直觉sift-在做什么)

### 第一部 评测 —— skill / agent 的能力

- [第二章 静态体检：不跑 cc 先看声明](#第二章-静态体检不跑-cc-先看声明)
- [第三章 触发准确度 trigger](#第三章-触发准确度-trigger)
- [第四章 考卷：出题、补题、体检、漏测诊断、冻结](#第四章-考卷出题补题体检漏测诊断冻结)
- [第五章 动态评测 eval](#第五章-动态评测-eval)
- [第六章 评测产物解析与重跑归档](#第六章-评测产物解析与重跑归档)
- [第七章 评分引擎](#第七章-评分引擎)
- [第八章 盲比较](#第八章-盲比较)
- [第九章 校准：结果×过程](#第九章-校准结果过程)
- [第十章 判决与展示：gate + report](#第十章-判决与展示gate--report)

### 第二部 审计 —— skill / agent / 编排者的执行

- [第十一章 审计的判定机制](#第十一章-审计的判定机制)
- [第十二章 审计的可信度与噪声](#第十二章-审计的可信度与噪声)
- [第十三章 审计的作用域与 transcript](#第十三章-审计的作用域与-transcript)

### 附录

- [附录](#附录)

---

## 第一章 建立直觉：sift 在做什么

### 1.1 sift 干两件事

sift 干两件事：

- **评测（eval）**——测一份声明（skill / agent）**有没有能力**：加了它，输出变好了吗？能不能把任务做对？评测是 A/B 对照（skill）或绝对质量打分（agent）。
- **审计（audit）**——测一份声明（skill / agent / 编排者）**执行时有没有照声明做**：过程规不规范、有没有跳过指令、有没有违反约束。

两件事面向的对象略有不同：评测目前测 skill 和 agent（要不要 A/B 对照是它们的关键能力问题）；审计更宽，skill、agent、以及把多个 agent 串起来的**编排者**都能审。工具支持 cc（Claude Code）和 opencode 两种执行器产生的会话。

### 1.2 为什么是一个工具、不是两个

评测和审计看起来是两件独立的事（一个看结果、一个看过程），为什么放在一个工具里？因为它们深度耦合，拆开会让两边都变弱：

- **共享同一个对象**——「声明」。评测要读懂声明才能出题、注入；审计要读懂声明才能提指令、对账。声明解析是同一套。
- **都围绕 transcript（会话记录）工作**。评测 spawn 执行产 transcript、拿它跑过程对账和评分；审计对账的也是 transcript。两者共用同一套解析、重建 transcript 的代码。
- **共享同一个遵循性引擎**。评测的 eval 内置「过程对账」（compliance + 校准），这个对账和独立的 audit 命令是**同一套引擎**——评测在每次执行后顺手查一遍过程，audit 则是把这套对账独立深化、做成能审任意会话的产品。
- **`run --task` 产真实 trace 喂 audit（产→审闭环）**。`run --task`（真任务模式）在真实代码库/数据目录里跑一个真任务、产真实 transcript，能直接喂给 audit 对账——「产数据」和「审数据」的前后手就接在这一环。
- **理念上都是「符合性」**。评测看的是**结果符合预期**（能力），审计看的是**过程符合声明**（遵循）。一个查「做对了没」，一个查「按规矩做了没」，都是符合性，只是符合的对象不同。

拆成两个工具会切断这个闭环、还要重复实现声明解析 / transcript 重建 / 遵循性引擎，所以 sift 把它们做成一个工具的两面。

### 1.3 什么是「声明」

sift 的评测和审计都围绕「声明」转。声明有三类：

- **skill**——一份 `SKILL.md`（写明什么时候该用、按什么步骤做、有什么约束），加上可选的 `references/`（参考文档、脚本、schema）和 `scripts/`（可执行工具）。模型用 skill 时，正文被拼进发给 Claude 的 prompt，references 拷进工作区供按需读取。
- **agent 定义**——一份 `.md`（含 frontmatter + 正文 system prompt），描述一个 subagent 该怎么干（角色、工具白名单、约束、黑名单）。agent 被 dispatch 时跑这份指令。
- **编排者声明**——一份 workflow / 主 agent 级的声明，描述顶层主 agent 怎么编排（dispatch 了谁、何时、怎么接返回），管的是「怎么串」而非某个子单元的行为。

sift 不在乎声明是 skill 还是 agent 还是编排者——对它来说都是「一份指令 + 一段执行」。评测出题/注入按声明的能力，审计提指令/对账按声明的约束。

### 1.4 能力 vs 过程：两个独立的问题

很多人会把「能力强」和「守规矩」混成一回事，但它们是正交的：

| 问题 | 含义 | 谁来回答 |
|------|------|----------|
| **能力**：能把任务做对吗？ | skill：开/关声明看增益（A/B delta）；agent：绝对通过率（无开/关对照，就像它没有 A/B 一样） | 评分引擎（scoring）；skill 另有盲比较 |
| **过程**：模型有没有真的按声明做？ | 输出即使对了，过程是否遵循指令 | 遵循性引擎 compliance（eval 内置一步 + 独立 audit） |

为什么非要拆开？因为存在两种危险情况：

- **输出对了，但过程没遵循**——模型自己蒙对了答案，并不能归功于你的声明。这种「高分」不可信。
- **输出错了，但过程是对的**——声明的逻辑没问题，只是执行失败，改指令即可，不用推翻声明。

能力主要由评测（第一部）测，过程主要由审计（第二部）测——不过评测的 run 也内置过程对账（compliance + 校准），一次执行里把能力、过程都测出来；独立的 audit 命令则把过程对账做成能审任意会话的产品。

### 1.5 就两件事：评测 + 审计

sift 就两件事：**评测（eval）**测能力、**审计（audit）**测执行过程；另有 `run` 命令跑真任务产 trace（审计上游），不构成第三件并列的事。

`run`（真任务命令，让 Claude 在真实代码库/数据目录里跑一个真任务、产真实 transcript）产的 transcript 主要喂给 audit 做过程对账（产→审闭环），本身不构成独立的一件事。

工具跨 cc 和 opencode：执行器默认 cc（Claude Code），也能 `--executor opencode`；两种执行器产的 transcript 都能喂 audit 对账。

### 1.6 全景图：评测与审计两面

sift 两件事各有核心结构，共享同一套底座——都读同一份声明、`eval` 内置的过程对账和独立 `audit` 用的是同一套遵循性引擎。

**评测（eval）** 产数据并出判决，核心是三层架构（评分 / 遵循性 / 校准）：

```
┌─────────────────────────────────────────────────────────────┐
│                    声明质量评估                              │
├─────────────────────┬────────────────┬──────────────────────┤
│  评分 Scoring        │ 遵循性          │ 校准 Calibration      │
│                     │ Compliance     │                      │
│  输出对不对？         │ 过程遵不遵循？  │ 结果×过程协不协调？    │
│                     │                │                      │
│  • 客观 programmatic │ compliance.json│ calibration.json     │
│    → grading.json   │                │  读「评分 × 遵循性」   │
│  • 主观 盲比较        │                │                      │
│    → comparison_    │                │                      │
│      summary        │                │                      │
└──────────┬──────────┴────────────────┴──────────────────────┘
           │ 客观 delta + 主观胜率（盲比较），都来自评分层
           ▼
      gate 门禁（事后判决，CI 用）
      读评分两轴定 decision；校准 HIGH_SCORE_SUSPECT 作 soft warning（不进 decision）
```

> 盲比较是评分层的主观侧主信号（对比两侧 outputs；**skill A/B 专属**——agent 无 A/B 故无盲比较），不是独立第四层；校准读「评分 × 遵循性」交叉；gate 读评分两轴（客观 delta + 主观胜率）定 decision + 校准 HIGH_SCORE_SUSPECT 作 soft warning（不进 decision）。
>
> 评测的三层图之外，还有正交的诊断维度画不进来：**触发准确度（trigger，第 4 维）**评的是「cc 会不会按 description 自然触发这个 skill」（见第三章）——前三层都假设 skill 已上场，trigger 补的是更上游的「会不会上场」，只诊断、不出 gate。（`coverage` 也是诊断工具，但它评的是考卷本身、属第四章出卷环节，不在这张主流程图里；`critique` 同属诊断、评内容质量（正文 + description）、属第二章静态体检。）

评测这一面有三条贯穿的设计原则：

1. **三层是并列独立信号，不合成单一总分**。不存在一个「综合 Score」公式。消费方按需组合判断——这避免了用一个数字掩盖问题。
2. **客观与主观分开报（双率）**。能程序化验证的（文件在不在、内容匹不匹配）归「客观」，零 LLM 成本、不可作弊、阅卷噪声为零；需要语义判断的归「主观」，会抖。把它们分开，干净信号才不会被阅卷噪声污染。
3. **判决事后出，不和产数据耦合**。`eval` 只负责产数据，通过/失败由独立的 `gate` 命令判——改阈值不用重跑 Claude。

**审计（audit）**——对账已有 transcript，核心是声明驱动：

```
声明（SKILL.md / agent.md / 编排者）
    │ 抽指令
    ▼
6 类指令 → 归 4 类语义，各配判法：
    条件性（该用某工具 / 读某参考） → 命中 tool_trace 程序判，否则 LLM
    禁令（不得做某事）              → LLM 主动查违规
    步骤（应走某步骤）              → LLM 判走过没 / 该走没走
    输出格式（产物格式要求）        → 子串匹配程序判
    │ 对照 transcript（tool_trace + transcript_text）
    ▼
五态 verdict：PASS / FAIL / N-A / UNRESOLVED / INDETERMINATE
（method：PROGRAMMATIC 程序 / LLM 语义，与 verdict 正交）
    │ 跨 transcript 聚合
    ▼
audit-report.json（遵循度 = pass / (pass + fail)）
```

审计这一面的核心原则正好相反——**精度 > 召回、宁少报**：FAIL 只在明确判出违规时报，判不准的标 UNRESOLVED（建议重跑）而非保守 FAIL，免得假 FAIL 一多、声明作者就不信这工具了（详见第十二章）。

> **全套命令按工作流阶段归类**（平铺速查见附录 A）：
>
> | 阶段 | 命令 | 干什么 | 详 |
> |---|---|---|---|
> | 静态体检（不跑 cc） | `analyze` / `vet` / `critique` | 声明本身 11 项体检（skill）+ 4 扫描器安全审查（skill）+ 内容质量诊断（正文 4 + description 2 维，skill + agent） | 第 2 章 |
> | 出题 | `init` / `add` / `inspect` / `freeze` / `coverage` | 生成 / 追加 / 体检（不下判决）/ 冻结基线 / 漏测诊断（eval 覆盖声明内容点） | 第 4 章 |
> | 评测 | `eval` | A/B 对照（skill）/ 绝对质量（agent） | 第 5 章 |
> | 真任务 | `run` | 跑真实任务产 trace 喂 audit（审计上游） | 第 5.5 节 |
> | 审计 | `audit` | transcript 遵循性对账（skill / agent / 编排者） | 第 12–14 章 |
> | 判决 | `gate` | 事后门禁（CI 用，毫秒级） | 第 11 章 |
> | 触发 | `trigger` | 触发准确度（第 4 维，skill 专属诊断、无门禁） | 第 3 章 |
> | 看结果 | `report` | HTML 报告（读 benchmark/gate/previous） | 第 11 章 |
>
> 典型顺序（skill）：`analyze`/`vet`（skill 健不健康）→ `init`/`inspect`/`freeze`（出一份好考卷）→ `eval`（跑测产 benchmark）→ `gate`（出判决）+ `audit`（查过程）→ `report`（看全貌）。出卷（init/inspect/freeze）治出卷噪声 C，`eval` 内的 `--runs` 治答题噪声 B，评分时的 programmatic / 盲比较治阅卷噪声 A。

---

# 第一部 评测 —— skill / agent 的能力

评测回答「能力」问题：一份声明（skill / agent）有没有把任务做对、加了它输出有没有变好。这部从静态体检讲起，经触发、出题、动态 run，到评分、盲比较、校准，最后 gate/report 判决展示。

在展开各命令前，先建立两个贯穿整个评测的度量框架——怎么读评测产出的数字（双率），以及噪声从哪来、各靠什么治（三层噪声）。后面每一章都在用它们。

### 双率与双主位

评测产出的数字怎么看，记两组概念。

**双率**——评分时每条被判项都按**判定方式**带一个 `source`：`programmatic`（程序判定）或 `llm`（LLM 语义判定）。大致对应：`expectations`（自然语言预期语句）一律 LLM 判；`assertions`（结构化断言）主要由程序判（Tier 1 类型，如 `FILE_EXISTS`），少数语义类型（Tier 3，如 `LLM_GRADE`）仍由 LLM 判（第 7 章细讲）。汇总时按 source 分别统计，得到三个率：

| 率 | 来源 | 稳定性 | 信任度 |
|---|---|---|---|
| `pass_rate_programmatic` | 确定性断言 | 稳（阅卷噪声为 0） | 强信 |
| `pass_rate_llm` | LLM 语义判定 | 抖（阅卷噪声） | 看方向 |
| `pass_rate`（总率） | 全部混算 | 历史兼容 | 粗略 |

skill 和 agent 的评分都产这三个率。**agent 评测没有 A/B 对照**（单 config 绝对质量），所以 agent 主要就看双率——绝对通过率够不够高；下面「双主位」是 skill A/B 才有的概念，agent 跳过即可。

**双主位**（skill A/B 对照下，客观侧和主观侧各信哪个数）：

- **客观侧主位** = `delta.pass_rate_programmatic`（候选 − 基线；**候选 = 开 skill 那侧 / v2，基线 = 关 skill 那侧 / v1**）。A/B 两边跑同样的题，程序化判定的噪声相互抵消到 0，所以这个 delta 最稳——它回答「skill 到底让客观正确率涨了多少」。
- **主观侧主位** = 盲比较胜率（盲比较是 **skill A/B 专属**，见第八章）。同一个 LLM 同时看两份产出判谁更好，相对判断消掉了独立打分时的阅卷偏差。胜率还自带一个可信度区间（Wilson 95%）：比的次数太少（比如就 3 次）时，它会老实标「不显著、建议多比几次」，不让你把碰巧当结论。另外，哪道题「忽过忽挂」（同题同条件、不同 run 结果翻）会在检查点总览标 ⚠ 红旗——这种题测了等于没测，该单独补跑（红旗 tooltip 会给补跑命令）。

`gate` 门禁正是按这两轴判决：客观侧读 `delta.pass_rate_programmatic`，主观侧读盲比较胜率。后面会展开。

> 遵循性**不分** source 双率——它按指令的语义类别（条件性/禁令/步骤/输出格式）判，产的是**遵循性五态**计数（`PASS`/`FAIL`/`N/A`/`UNRESOLVED`/`INDETERMINATE`，见第十一章），不区分 programmatic/llm。双率是评分侧的概念。注意：这里的「遵循性五态」和评分侧的「评分五态」（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`，见 7.4）是**两套不同的状态集**，别混。

### 三层噪声，各治各的

你可能要问：为什么 sift 要分 programmatic、盲比较、`--runs`、inspect 这么多东西？因为从出题到打分，噪声不是一个地方抖，而是**三层**，分布在三个环节，各自由不同机制治。先记住这个分工，后面每一章都在治其中一层。

| 噪声 | 在哪个环节 | 长什么样 | 谁治 |
|------|-----------|---------|------|
| **A 阅卷噪声** | 打分（LLM 判分时） | 同一个判官今天判 PASS、明天判 FAIL；或整批偏严/偏松 | 客观题交给代码（`programmatic`，噪声归零）+ 主观题盲比较（skill A/B；相对判断，系统偏差相减抵消） |
| **B 答题噪声** | Claude 答题时 | 同一个 skill + 同一道题，这次写了文件、下次只口头总结；这次写 192KB、下次没写 | `--runs N` 多跑几次取聚合 |
| **C 出卷噪声** | init 出题时 | evals.json 本身是 LLM 生成的，会抖：每套出得不一样；这套题断言全太松 → 两边假高分；漏抽 skill 该用的能力 → 考卷没覆盖关键点 | `inspect`（题集体检）+ `freeze`（锁基线跨 run 复用）+ `--from-transcript`（真实挖题，源头降噪）+ `coverage`（漏测诊断：查 eval 覆盖了声明哪些点）+ 补题（`coverage --augment` / `init --append --focus`，定向补盲区，见第 4 章） |

三个要分清的点：

- **A 和 B 是两回事，别混**。`--scoring programmatic` 消的是阅卷噪声 A（LLM 改卷波动），`--runs 3` 消的是答题噪声 B（Claude 每次产出不一样）。所以 `programmatic` + `runs 3` 才是稳的组合，缺一不可——光选 programmatic 不多跑，Claude 这次答对、下次答错，客观率照样跟着波动。
- **C 藏在 benchmark 之外**。A、B 都在 `eval` 内部，能测、能消；C 是出题环节的偏差，`eval` 里看不出来——跑同一份「坏卷子」，分数照样不可信，你却以为稳了。所以 C 只能在出题时防。它有三面，各靠不同手段：这套题**每次 init 出得不一样**（逐套抖动）→ `freeze` 锁定一份基线跨 run 复用；这套题**本身可能不真实、断言太松**（单套偏差）→ `inspect` 体检发现问题，或 `--from-transcript` 干脆从一份真实好会话挖题（场景真、断言从真实产物里抽，不让 LLM 凭空捏），从源头避开；这套题**漏测关键点**（出题器没覆盖声明的某些能力）→ `coverage` 把 eval 逐条对回声明、报漏测点，再用 `coverage --augment` / `init --append --focus` 定向补上（见第 4 章）。
- **每一章都在治其中一层**：第 7 章 programmatic（治 A）、第 8 章盲比较（治 A 里独立打分抹不掉的系统偏差）、`--runs`（治 B）、第 4 章 inspect/freeze/coverage（治 C）。

---

## 第二章 静态体检：不跑 cc 先看声明

静态体检是评测的第一道关——在花 token 跑 Claude 之前，先看声明文件本身写得怎么样、有没有安全隐患。分三种：`analyze` / `vet` 是 **skill 专属**的纯静态检查（`analyze` 查 SKILL.md 结构、`vet` 查安全），零 API 成本、不到 1 秒；`critique` **skill 和 agent 都能用**，但要调 LLM 查内容质量（正文 + description，两次调用、不是零成本、也非秒级）。

### 2.1 analyze：11 项静态体检（skill 专属）

> 11 项检查是 11 个确定性检查函数——零 API 成本、逐条可复现，判的是结构质量（描述 / 命名 / 行数 / 示例 / 反捏造等），不是内容质量（内容走 §2.3 的 `critique`）。

```bash
sift analyze path/to/skill/                    # 表格输出
sift analyze path/to/skill/ --format json -o scorecard.json
```

11 项检查分四组（前 6 项查 SKILL.md 顶部 **frontmatter**——即文件开头 `---` 包围的 YAML 段，里面的 `name` / `description` 字段；后 5 项查正文 / `references/` 目录）：`description`（3 项）、`name`（3 项）、SKILL.md 正文（4 项）、`references/`（1 项），每项 pass/fail，汇总成 `score / max_score`（满分 11）。逐条：

| # | 检查项 | 判定逻辑 | 为什么重要 / 不过怎么办 |
|---|--------|----------|--------------------------|
| 1 | `description_length` | frontmatter 的 description ≥ 20 字符 | 太短模型抓不到「何时该用这个 skill」；补具体动作描述 |
| 2 | `description_has_trigger` | description 含动作触发词（中英：create/生成/分析/重构/配置/部署…） | 没动作词=没说清做什么；动词开头 |
| 3 | `description_third_person` | description 不含第一人称（I / 我会 / 我的 / 我们…；用词组匹配，「自我」这种不会误伤） | 描述应客观陈述技能而非自述；去掉「我」 |
| 4 | `name_kebab_case` | name 是小写字母+数字+连字符、首尾为字母或数字 | 命名规范才便于引用；改成 kebab-case |
| 5 | `name_max_64` | name ≤ 64 字符 | 太长在 UI / 调用时被截断；缩短 |
| 6 | `name_no_reserved` | name 不在保留字集（help / init / run / eval / test / skill / create / list / config） | 保留字会和系统命令冲突；换名 |
| 7 | `skill_md_max_500_lines` | SKILL.md ≤ 500 行 | 太长模型读不完、抓不住重点；详情拆进 references/ |
| 8 | `has_examples` | 正文含 example / 示例 / 例如 / 代码块 等 | 没示例模型难对齐预期；加一段用法示例 |
| 9 | `reference_depth` | references/ 扁平（无子目录；没有该目录算过） | 子目录模型可能漏读；保持一层 |
| 10 | `anti_fabrication` | 正文含反捏造词（never fabricate / 不得编造 / 不臆造…，中英词表与遵循性提取器共用） | 显式约束防模型幻觉；写明反捏造指引 |
| 11 | `source_documented` | 正文含来源词（reference / 参考 / 依据 / 出处…） | 标注信息来源可追溯；写明依据 |

> 第 10 项的反捏造词表和审计的指令抽取（第十一章）**共用同一份词表**（`sift.i18n`）——所以一个 skill 过了静态反捏造检查，它的反捏造指令也大概率会被审计引擎抽出来查。两层的判定口径一致。

每项产出 pass/fail，汇总成 `score / max_score`，还会给一个分类标签：

- `CAPABILITY_UPLIFT`——技能注入新能力（教模型做原本不会的事）
- `ENCODED_PREFERENCE`——编码某种偏好/规范（让模型按特定方式做本来就会的事）

`score < max_score` 时 exit 1，方便接 CI。

### 2.2 vet：安全审查（skill 专属）

> 内置的 secrets-scan（密钥 / Token / Base64）与 structure-check（SKILL.md 结构 + 危险命令）两个扫描器是原生 Python 实现、零外部依赖；另有 aguara / skill-scanner 两个可选外部扫描器（`--skip-external` 跳过）。

```bash
sift vet path/to/skill/                # 默认表格，-o 写文件
sift vet path/to/skill/ --skip-external # 只跑内置 2 个扫描器
```

四个扫描器，两个内置开箱即用、两个可选增强：

| 扫描器 | 类型 | 查什么 |
|---|---|---|
| secrets-scan | 内置 | 硬编码 API Key / Token / 凭证、Base64 |
| structure-check | 内置 | 缺 SKILL.md / 缺 frontmatter / 危险 shell 命令 |
| aguara | 可选（Go） | Prompt 注入、混淆 |
| skill-scanner | 可选（pip） | 已知恶意模式、CVE |

判定三态：`SAFE`（全过，exit 0）/ `REVIEW_NEEDED`（有中等风险，exit 0）/ `BLOCKED`（有严重风险，exit 1）。

> `eval` 命令默认会在末尾自动产出 `scorecard.json` 和 `vet-report.json`（可用 `--no-static` / `--no-vet` 跳过），所以你不用单独跑这两步也能在 report 里看到静态体检结果。

### 2.3 critique：内容质量诊断（正文 + description,skill + agent）

`analyze` 的 11 项体检查的是**格式**(description 够不够长、name 是不是 kebab-case、有没有示例标记……),但「格式都对」不等于「内容写得好」——一段空洞抽象、没演示、没说清会栽在哪的正文,照样能过 11 项。critique 补的就是这个空白:让 LLM 当**读者**通读声明,按 rubric 给读者反馈。评两块内容——**正文**(SKILL.md / agent 定义的 body)和 **description**(frontmatter 那行触发描述),两者都是读者(或 cc 触发逻辑)判断「这声明干什么、该不该用」的依据,内容质量都不依赖跑 transcript。

```bash
sift critique path/to/skill/              # 诊断内容质量,出 JSON + HTML(单次跑,默认)
sift critique path/to/skill/ --runs 3     # 多数投票跑 3 次,稳边界维度(显 X/N 可信度)
sift critique path/to/agent.md --kind agent   # 审 agent 定义,6 维共用,只换 prompt 措辞
```

`--kind agent` 把 critique 跨到 **agent**:agent 定义(system prompt)也是指令,rubric 原样共用,只换 prompt 措辞(不把 agent.md 叫 SKILL.md)。正文 + description 两块对 skill 和 agent 都评。

六条 rubric 分两组(报告也按这两组分节渲染),每条判 `met`(满足)/ `partial`(部分)/ `missing`(缺失)+ `evidence`(**每处引用一条**的数组,分属不同段落拆多条)+ `feedback`(✓ 认可 / 已做到)+ `gap`(✗ 缺口 + 方向),**大白话短句、不整段重写原文**:

**正文 4 维**(评 body):

| # | 维度 | 问什么 | 好例 / 反例 |
|---|------|--------|-------------|
| ① | 失败机制编码 | 有没有写「agent 具体会栽的具体坑」+ 为什么栽(失败条件 + 因果链) | 「API 分页上限 100,不翻页会静默丢数据」/「处理错误要小心」 |
| ② | 可执行具体性 | 步骤够不够照着能干,引用真实的工具 / API / 对象名 | 「改完重新查对象拿真 ID,再用这个 ID 调用」/「把任务拆小」 |
| ③ | 高风险黑名单 | 有没有明确拉黑「看起来对但其实会害死人」的操作,带具体原因 | 「别直接拿用户串当正则——`.*` 会让匹配静默失败,先转义」/「注意危险操作」 |
| ④ | 示例驱动 | 有没有 worked example(完整演示一遍),而非纯抽象规则 | 一段真实命令序列 / 真实输入→真实输出 / 只有规则没有演示 |

**description 2 维**(评 frontmatter 触发描述):

| # | 维度 | 问什么 | 好例 / 反例 |
|---|------|--------|-------------|
| ⑤ | 意图清晰 | description 有没有清楚说明这个声明干什么、什么场景下该用 | 「用户要重构代码时,审查改动是否引入性能回归」/「帮助用户处理代码」 |
| ⑥ | 触发具体性 | description 够不够具体,使对的输入触发、错的不触发 | 「仅在提交 git diff 后要求审查时触发」/「和代码有关的时候用」 |


> **为什么 description 也评、却不和 trigger / audit 冲突**:三者看 description 的不同面——`trigger` 测「cc 凭这行 description 会不会真触发」(行为,需 spawn cc);`audit --kind agent` 测「agent 被派发后执行得对不对」(过程,需 transcript);critique 测「这行 description 本身写得清不清楚、具体不具体」(内容,零 transcript、纯读文本)。手头没 transcript 又想判 description 写得好不好时,critique 是唯一入口。

**linter 降姿态,不当打分**:critique 给的是「读者反馈」不是「打分」——LLM 是外行评内行领域,rubric 判定会漂(同一份声明换个模型 / 多跑几次,partial / missing 的边界会抖),故只标三态提示哪里薄弱、**不进任何 gate**(同 trigger / coverage,独立 artifact、不并入 benchmark)。把它当 linter 提示读:`missing` 提醒补、`met` 认可写得好的地方,别当成权威判决。**两次 LLM 调用**(正文一次、description 一次,走 http 裸通道——比 cc 快、省 token;description 缺失就不调那一次、直接标 missing + 提示),不依赖 evals.json;LLM 调用失败降级为空报告(exit 0,不炸)。

> **结果会抖,但有规律(默认单次跑,理解一下为什么)**:内容质量是**模糊判断**(不是客观对错),同一份声明跑几次,**界限分明的维度稳**(明显写得好→满足、明显缺→缺失),**处在边界上的维度只在相邻档抖**(部分↔满足、部分↔缺失),**不会跨档大跳**(没有满足↔缺失)。实测一个 skill 连跑 3 次:6 维里 4 维稳、2 个边界维在相邻档抖。所以:满足 / 缺失明确的可以直接信,`partial` 当「这条值得看一眼、但不一定非改」——**抖本身就是在告诉你「这条是边界」**。这是 linter 的固有性质(不是 bug),所以不进 gate。

> **想稳就 `--runs N`(多数投票)**:默认单次(`--runs 1`,便宜)接受抖;要稳就 `--runs 3`——同一 prompt 跑 N 次,每维 status 取多数(并列取更严档,偏向提醒)、原文取**首个判出多数档**的那次(保证贴的原文与判的档自洽)、并显**`X/N` 可信度**(X = N 次跑里投出多数档的次数:3/3 全一致=稳、2/3 有一票不同=边界),把抖变成显式信息。代价是 N× 调用(正文 + description 各 N 次)。linter 的价值在「指出哪里值得看」不在「精确判决」,多数情况下默认单次就够;只有某条反复在 partial / missing 之间晃、你想确认到底算不算问题时,才值得上 `--runs`。

产 `content-quality-report.json`(机器读,字段见 [参考手册](sift-reference.md))+ `content-quality-report.html`(人读,always 写,按正文 / description 分节)+ 终端摘要(满足 N · 部分 N · 缺失 N · 耗时 · token)+ 分节(正文内容质量 / description 质量)下逐维 `[维度名] 满足/部分/缺失`(`--runs>1` 时附 `X/N`)+ 每处 evidence 一行 + `feedback`(✓ 认可)/ `gap`(✗ 缺口)。

---

## 第三章 触发准确度 trigger

前三层（评分 / 遵循性 / 校准）回答的都是 skill 已经「上场」之后的问题——输出对不对、过程遵不遵循、结果×过程协不协调。但有一个更上游的问题它们都没碰：**cc 到底会不会调用这个 skill？** trigger-accuracy 补的就是这个缺口——**skill 专属**的诊断维度（agent 由编排者显式 dispatch，不存在「会不会被自然触发」的问题），只诊断、不进 gate。

### 3.1 上游缺口：三层都假设 skill 已「上场」

一个 skill 在真实会话里被不被调用，靠的**只有 frontmatter 的 `description`**——cc 扫到一份 skill、读它的 description、据此决定这次任务要不要够它。description 写得不到位，cc 永不触发，skill body 再好也是死重量。

但前三层都跳过了这一步：

- **评分（eval A/B）**：eval 的执行器把 skill body **强制注入** cc prompt（开头就是 "You have the following Skill loaded. Follow its instructions"）——cc 对「要不要够这个 skill」没得选，触发能力**从不被测**。
- **遵循性（audit）**：审的是 skill 被调用**之后**的行为（默认它已触发）。
- **校准**：结果×过程 coherence，与触发无关。

`description` 这一触发判定唯一依赖的输入，前三层都不碰。critique 虽然也看 description——但评的是它**写得清不清楚**（静态读文本、零 transcript）；**cc 真的会不会凭它触发**这个行为问题，谁都没测。trigger 补的就是这个缺口——它和前三层正交：前三层评「上场后表现如何」，trigger 评「会不会上场」。

### 3.2 怎么测：中性 prompt + 真实 HOME

```bash
sift trigger path/to/skill/ --runs 2 -o /tmp/out
```

`trigger` 命令把被测 skill 放进 `<cwd>/.claude/skills/<name>/`，给 cc 一条**中性 prompt**——不点名 skill、不暗示该用它，就是一个普通用户请求。然后看 cc 会不会仅凭 description 自动发现并调用本 skill。检测复用遵循性引擎的 `audit/slice.invoked_skills`（扫 transcript 里的 `Skill` tool_use，看本 skill 名在不在里面）。

**用真实 HOME、不隔离**（对齐 run/audit）：spawn 用 `claude_cli_env` 补身份头 + 继承终端 env（含真实 HOME），不把 `HOME` 指向临时空目录。曾尝试用空 HOME 隔离用户全局 plugin，但实测两点推翻：① cc 2.x 的全局 plugin 不随 HOME——换空 HOME 后全局 plugin skill 照样加载，没隔掉；② cc 的文件/keyring 认证源在 HOME 里，覆空 HOME 会把认证一起断掉，终端 shell（无 IDE 注入的 token）撞 "Not logged in" → 全 probe UNRESOLVED。trigger 可信不靠 HOME 隔离撑：`fired` 判定只看本 skill 名在不在 `invoked_skills`（扫 transcript 的 `Skill` tool_use），别的 skill 参与发现不改变这个布尔——触发是否成立由 transcript 里有没有本 skill 的调用决定，与测试机装了哪些别的 skill 无关。

### 3.3 两种探题：recall 与 false-trigger 是不同失效模式

trigger 探题分两个方向，各测一种失效：

- **should-trigger**（`expect_trigger: true`）：该 skill 该处理的真任务，措辞**中性**（在 prompt 里点名 skill 就只是测字面匹配，不是测触发）。
- **should-not-trigger**（`expect_trigger: false`）：看着相关但 skill **不该**触发的任务——测 description 会不会过宽、把不该接的活也揽下来。

两种是**不同的失效模式**，所以聚合时**分开报、不合成一个分**：

| 率 | 来自 | 测的失效 | 方向 |
|----|------|---------|------|
| `recall` | should-trigger 方向 trigger_rate 均值 | 漏召（该触发没触发） | 越高越好 |
| `false_trigger_rate` | should-not 方向 trigger_rate 均值 | 误触（不该触发却触发） | 越低越好 |

为什么不合？因为「漏召」和「误触」指向相反的修法：recall 低 → description 太窄 / 缺关键词，要加触发线索；false_trigger 高 → description 太宽 / 太泛，要收窄 scope。合成一个分会把这两个相反信号互相抵消，丢掉诊断价值。

### 3.4 判定与样本

每条 probe 跨 N runs 算 `trigger_rate = fired 次数 / runs`（触发带随机性，`--runs` 多跑聚合成率更可信），阈值 0.5 二值化 → PASS / FAIL。spawn 失败 / 超时 / **cc 非零退出且没触发**（限流 / 后端不可用：cc 实测 exit 1 + 错误打 stdout、stderr 空，`run_trigger` 不 raise 而返正常输出）→ 该 probe = `UNRESOLVED`，**不进 recall / false_trigger_rate 的分母**，且其 `trigger_rate` 置 `null`（没判成、不显假数值率，避免把「没跑出来」误读成「触发率 0」）——宁少报不乱报，避免单次异常污染率（同审计「没判成不乱报」的哲学）。某方向 0 probe → 对应率 `null`（N/A，不是假 0.0）。

每个率都带自己的样本量（`n_should_trigger` / `n_should_not`），别脱离样本量解读一个率：init 默认每方向 5 道，<3 道会告警「样本少」。同三层噪声的分工，trigger 侧也有「答题噪声 B」（cc 触发带随机性）——靠 `--runs` 多跑抹平。

**分层诊断：cc 靠字面还是语义触发？**

除 `recall` / `false_trigger_rate` 两个主指标，每条 probe 报告期再标两个**派生维度**（不进 evals.json，从 prompt + description 算出）：

- **`phrase_type`**（`literal` / `paraphrase`）：anti-leakage 子串检查——prompt 含 description 的 ≥15 字符连续子串（归一化：去标点 / 空白 / 大小写）→ `literal`（词面命中），否则 `paraphrase`（语义改写）。
- **`length_bucket`**（`short` / `medium` / `long`）：prompt 按字符数分桶（<30 / 30–80 / >80）。

`trigger-report.json` 的 `breakdown` 把 `recall` / `false_trigger_rate` 再按这两个维度切。最有诊断价值的一刀：**词面命中 recall** vs **语义改写 recall**——前者远高于后者，说明 cc 主要靠字面匹配触发（抄了 description 的词就触发），不是真理解了意图。终端打一行 `分层 recall`，HTML 报告给两维度 × 两方向的完整分层。

### 3.5 出题：`init --trigger-probes`

```bash
sift init path/to/skill/ --trigger-probes                 # 默认每方向 5 道，追加到 trigger_probes 段
sift init path/to/skill/ --trigger-probes --num-evals 8   # 每方向上限 8（trim 多余、不补下限）
```

LLM 读 skill 的 description 生成两方向探题，追加到 evals.json 的 `trigger_probes` 段（新 id 接续已有 `max+1`）。**不重生成已有 scoring evals**——载入已有 evals.json、只加 trigger 探题（`--trigger-probes` 单用、不加 `--append` 也保留原 scoring 题）。

**anti-leakage（防词面命中）**：生成 prompt 强制探题 paraphrase description、不照抄措辞；生成后还做一道校验——should-trigger 探题若含 description 的 ≥15 字符连续子串，init 打「⚠ N/M 道该触发探题为词面命中」（M 是该触发探题的数量；should-not 方向没这个问题——它本来就不该触发，抄不抄词都无所谓）让用户复核。词面命中的探题测的是 cc 字面匹配（抄了词就触发），不是语义触发 → recall 虚高、诊断失真。**只标记、不 retry**（词面命中探题仍有诊断价值：它告诉你 cc 对字面重叠敏感），报告里按 `phrase_type` 分层体现。

**`--num-evals` 是每方向上限、不是下限**（关键）：LLM 生成的探题多于这个数会被 trim，少于这个数**原样保留**、不强行凑满——硬凑会出重复 / 充数题，反而劣化信号质量。这是有意的「欠交付」：宁可少几道真信号，不要凑数的噪声。所以实际跑出来某方向可能少于你给的 `--num-evals`，别当成 bug。

**trace-mined 负例（从真实会话挖 should-not 真题）**：配 `--from-transcript` 时，除了 LLM 编的探题，还从你给的真实会话记录（transcript——agent 干活的完整过程，含工具调用，不是纯文字聊天）里**挖**现成的负例——

```bash
sift init path/to/skill/ --trigger-probes --from-transcript path/to/session.jsonl
```

**为什么要挖**：LLM 凭 description 编负例，措辞终究是「出题人想象出来的假用户」；而真实会话里那些「用户问了、但 agent 没动用本 skill」的问题，才是真用户嘴里会说出的原话——拿它们去测「这种问题该不该唤醒 skill」，最接近实战。

**怎么挖**（两层，先程序筛、再 LLM 拣）：

1. **程序筛**（零 LLM，不花钱）：把会话里的用户提问逐条过一遍——这句问完到下一句提问之间，agent 有没有调过本 skill？没调 = 确实没触发，收作候选。三类直接跳过：太短的碎话（「ok」）、斜杠命令（`/clear`）、点名本 skill 的提问（用户明确要它上场，拿去当「不该触发」的题必冤枉）。
2. **LLM 拣**：候选里把跟 skill 领域**八竿子打不着**的扔掉——「帮我写周报」没触发日志分析 skill，这不算 description 的失误（本来就不该触发，测了也白测）；只留**领域相近**的——比如用户问「这个报错是什么意思」而错误分诊 skill 没上场，人类都可能觉得「这不该触发吗？」，这才是有压力的测试题。候选多时分批（每批 ≤10 条）、每条单独判，防互相带偏。

两个设计细节：**LLM 拣的时候只给它 description、不给 skill 全文**——cc 决定触不触发时也只能看到 description，给全文等于泄题；**LLM 这步挂了不丢题**——照收，但 rationale 标 `unverified` 让人过目（第 1 步查明的「确实没触发」是程序事实，不受影响）。

挖出的探题带 `source: trace-mined`（报告明细「来源」列可见），跟已有探题按问题原文去重、id 接着排；也不受 `--num-evals` 上限影响——真题有多少收多少，不 trim。

### 3.6 边界：诊断定位，无门禁

trigger-accuracy 是**诊断工具，不是门禁**：

- **无 gate / 不出 exit-code 判决**——只产 `trigger-report.json` + 终端摘要。recall 低或 false_trigger 高是「该改 description」的信号，不是合并阻断（那是 skill 内容要改、不是 PR 该卡的红灯）。
- **独立 artifact，不并入 `benchmark.json`**——trigger 回答的是上游独立问题，和 scoring / compliance / calibration 不耦合，benchmark 的双率 delta / 盲评胜率都不含触发信号。
- **`description_hash`**（sha256(frontmatter `description`)[:16]）锚定触发判定唯一依赖的输入——跨版本对比时，hash 变了说明 description 改了，recall 的变化才有「是 description 改动引起」的归因依据；hash 没变而 recall 变了，更可能是 cc 触发随机性（看 `--runs`）。

> **成本**：每条 probe = 1 次 cc spawn（有 LLM 成本，但比 `eval` 轻——无 with/without 双 spawn、无 grader、无 compliance judge）。配合前三层用：先 `trigger` 看 description 能不能让 cc 够到这个 skill，再 `eval` 看上场后表现。

### 3.7 怎么读报告 + 怎么改 description

跑完 `trigger` 产三样：`trigger-report.json`（机器读）+ `trigger-report.html`（人读，**always 写**，与 JSON 并列、对齐 audit，`--open` 浏览器打开）+ 终端摘要。

**终端进度 + 摘要**——跑的过程实时显示进度（每个 probe 开头 `probe i/N`（该触发 / 不该触发）+ cc 的实时动作 `🔧 …` / `✓ 完成`，同 `eval` 命令）；跑完是一行汇总 + 逐 probe + 操作建议：

```
recall 75%(5 should-trigger) · false-trigger 10%(5 should-not) · 2 runs · 1 unresolved · 耗时 12.3s · 28400 tok
分层 recall: 措辞 词面 90% · 语义 40% | 长度 短 80% · 中 70% · 长 60%
  [+] probe-1: 100% → PASS (842 tok · 4.5s)
  [-] probe-2: 0%   → FAIL (318 tok · 2.1s)
  [?] probe-3: ?    → UNRESOLVED (0 tok · 0.0s)
操作建议：
  [rule:recall] 漏召:…考虑补触发关键词:
    · probe-2:「…」
  [llm:llm_rewrite] 修订 description:…\n理由:… (480 tok · 3.1s)
Written to …/trigger-report.json · …/trigger-report.html
```

- 汇总行：`recall`（绿，越高越好）/ `false-trigger`（红，越低越好）各带样本量 + runs + unresolved + 耗时（cc spawn 总墙钟，不含 LLM 建议那次调用）+ 总 token。有 should-trigger 时还打一行**分层 recall**（措辞 词面 / 语义 · 长度 短 / 中 / 长，纯诊断）。逐 probe `[+/-/?] probe-N: 触发率 → verdict (token · 耗时)`：`+` PASS、`-` FAIL、`?` UNRESOLVED（触发率显 `?`、消耗显 0，因为没判成）。

**怎么改 description**——报告 `suggestions[]` 直接给方向，两路：

| 信号 | 含义 | 怎么改 |
|---|---|---|
| `recall`（should-trigger FAIL） | 漏召：description 没够到该处理的任务 | 补与失败 prompt 匹配的触发关键词 / 动作动词 |
| `false_trigger`（should-not FAIL） | 误触：description 过宽 | 收窄 scope / 加边界排除语（明确不处理这类） |
| `unresolved` | 没判成（限流 / 异常） | 加大 `--runs` 重跑确认，别据未判成下结论 |
| `all_good` | 零 FAIL 零 unresolved | description 触发边界够精准，无需改 |
| `llm_rewrite`（source=llm） | LLM 起草的修订 description 草案 | 可直接抄改（有失败才自动产，带「理由:」） |

规则建议（`recall`/`false_trigger`/`unresolved`/`all_good`）永远给、零成本；LLM 建议（`llm_rewrite`）**有失败才自动跑**（喂当前 description + 失败 probe 的 prompt，起草可抄改的 description + 一行理由），全 PASS 跳过。相对 trigger 本体（N probe × run 次完整 cc spawn）这是单次轻 LLM 调用、<5%，故默认开、无 flag；调用失败自动重试 2 次（限流瞬态），重试耗尽只丢这条、规则建议照在，终端打失败原因（疑似限流 / 空响应）让你判断是瞬态还是真问题。

> `trigger-report.json` 字段全表见 reference 的 `trigger-report.json` 段。建议口径是**诊断、不是门禁**——给「改 description 的方向」，不下 PASS/FAIL 判决（同 3.6）。


---

## 第四章 考卷：出题、补题、体检、漏测诊断、冻结

动态评测的前提是有一份「考卷」——`evals.json`。这一章讲考卷的完整生命周期：怎么**出题**（`init`）、怎么**补题**（`add` / `--focus`）、怎么**体检**（`inspect`）、怎么查**漏测**（`coverage`）、最后怎么**冻结**成基线（`freeze`）。一份好考卷是后续所有评测的前提——考卷本身有盲区，`eval` 再多轮也测不到。**skill 和 agent 通用**这套考卷机制。

### 4.1 evals.json 长什么样

一份考卷就是一个 JSON，里面是若干道 eval（题）。每道题的核心字段：

```json
{
  "id": 1,
  "name": "L2-排序 CSV 产出",
  "prompt": "把 data.csv 按日期排序后输出 sorted.csv",
  "rationale": "对应 ## 步骤(按日期升序排序后产出 sorted.csv)",
  "confidence": 0.85,
  "expected_output": "生成 sorted.csv，内容按日期升序",
  "level": "L2",
  "fixtures": [{"path": "data.csv", "content": "date,val\n2024-03-01,b\n2024-01-01,a\n2024-02-01,c\n"}],
  "expectations": ["输出文件存在", "内容按日期升序"],
  "assertions": [
    {"name": "文件存在", "type": "FILE_EXISTS", "params": {"filename": "sorted.csv"}},
    {"name": "含排序逻辑", "type": "CONTAINS_PATTERN", "params": {"pattern": "sort", "path": "sorted.csv"}}
  ],
  "source_dir": "src",
  "flags": []
}
```

几个关键概念：

- **L1 vs L2 是题型，不是「有没有 expectations」**。L1（单元级）测模型对声明规则的理解对不对，通常纯推理、无 fixture；L2（Mock 场景）测在有 fixture 的场景里能不能正确产出文件。两者都能同时有 expectations 和 assertions。
- **expectations vs assertions 是两个判分层**。`expectations` 是自然语言预期语句，由 LLM 判；`assertions` 是结构化断言（`type` + `params`，`params` 里可带 `scope`——如 `"transcript"` 只查回答文本不碰文件，给只读 / 纯建议 agent 用，第 7 章细讲），主要由程序判（Tier 1），少数语义类（Tier 3）仍由 LLM 判。一道题可以只有其一，也可以都有。
- **expected_output 是给阅卷参考的，绝不喂给被测 Claude**。把它拼进 prompt 等于把答案泄露给考生，A/B 对照会失真。sift 只在评分侧用它。
- **fixtures** 是前置文件（输入数据等），每条是 `{path, content}`，会被拷进工作区当考场材料（agent 题用 `files` 同理）。
- **source_dir** 是 eval 自带的额外目录（如 skill 的 `scripts/`、被处理的样例代码），会和 fixtures 一起拷进工作区——skill 依赖、但不在 `references/` 里的东西靠它带进考场（见 5.4 的依赖拷贝坑）。
- **rationale / confidence**（`init` 生成时自带）：每道题带 `rationale`（对应声明哪段/哪条,审阅溯源抓手）和 `confidence`（0-1,生成器自报把握）。`init` 终端逐条打印这俩,方便审阅「这题到底在考什么、生成器多有把握」。两者都**不入评分 gate**（confidence ≠ audit-finding 的 method,别混淆）;`rationale` 缺失打 `review:no-rationale` flag 提示人工补。
- 完整字段（`timeout` / `flags` 等）见参考手册和 [evals-format.md](evals-format.md)。

### 4.2 init：自动出题

```bash
sift init path/to/skill/                                  # LLM 生成（默认）
sift init path/to/skill/ --num-evals 8                    # 限制数量
sift init path/to/skill/ --from-transcript session.jsonl  # 从真实会话挖题
sift init path/to/agent.md --kind agent                   # 给 agent 出题（凭角色造 role-matching 题）
```

`--kind agent` 把出题跨到 **agent 定义.md**：不按正文切段（那是 skill 工作流语义）、不假设产文件，而是让 LLM 据 agent 声明的角色（description + system prompt）出「会交给这个 agent 的真实考题」，断言由 LLM 直接写（对齐 skill，逐条对应 expectations、可拆细）。这是 agent「没有 transcript 时的出题路」——保真度不如 `--from-transcript --kind agent`（后者挖真实派发场景），但新 agent 没跑过时是唯一来源。两路互补，同 skill 的 generate_with_llm vs generate_from_transcript。

`init` 有三种生成模式，按优先级自动选择：

1. **`--from-transcript`（从真实会话挖）**：读一段真实 session transcript，重建当时的输入文件/命令/产物，挖出真实场景的 eval。其中有一条是**逐字节真挖**——直接还原会话里真实执行的任务，fixture 就是 agent 当时 Read 的真实文件内容；此外 LLM 还会基于这段会话再生成若干变体题。为防 LLM 臆造文件名，生成结果过一道**反幻觉闭集约束**：允许引用的文件被限定在一个闭集里（transcript 实际出现的输入/产物文件 + 声明产物），引用闭集外文件的断言和 fixture 会被确定性后处理丢弃并打 `review:fixture-not-in-manifest` / `review:dropped-assertions` flag——这样生成的题不会冒出会话里根本不存在的文件。如果 transcript 里涉及多个声明单元（调了多个 skill、或主 agent dispatch 了子 agent），不切分会把全部当本声明挖、串味——加对应的 `--kind skill` / `--kind agent` 只挖本声明被调用 / 被 dispatch 期间的段（终端会警告，两者一视同仁）。
2. **`--generate-prompts`（LLM 生成，默认开）**：LLM 读声明自动出 L1/L2 题，并尝试推导程序化断言。
3. **`--no-generate-prompts`（纯模板，不调 LLM）**：不调 LLM，纯规则从声明结构推导——把正文按 `##` 二级标题切成若干段，每段出一道 eval：prompt 由标题 + 描述里检测到的动作词拼成（如 `Convert a task related to <段> — …`），`expectations` 从段里的列表项（`-` / `*` / `1.`）逐条转成预期语句，`expected_output` 形如 `Successfully complete the <段> workflow (N steps)`。**「模板」不是磁盘文件**，而是 `evals/generator.py` 里的一套确定性推导规则（`_derive_prompt` / `_derive_expectations` / `_derive_assertions`）；唯一可配的「模板」是配置文件里按 domain 注入的领域断言（`domain_templates`）。**产出的不全是程序断言**——主体是 `expectations`（LLM 判的文本预期），程序断言（`FILE_EXISTS` / `CONTAINS_PATTERN` 等）只是从代码块 / import / 领域模板**抽得到才补**，纯规则类声明可能零断言。

**两条出题路：凭声明生成 vs 从 transcript 挖**。三种模式里真正可比的是两条主路——**凭声明生成**（默认 `init` / `--kind agent`，LLM 读 SKILL.md 或 agent.md，凭声明的能力 / 角色出题）和**从 transcript 挖**（`--from-transcript`，从一份真实使用 session 挖题）。它们是**保真 vs 覆盖**的权衡，互补不替代：

- **凭声明生成 → 覆盖广、保真低**。LLM 据声明出题，不受某场会话局限，能覆盖声明里的全部规则；但场景是 LLM **编的**，缺真实使用锚点。以 pr-reviewer（agent）实测：直出 7 道打满声明的 7 类规则（含大 diff、批量重命名），但每道都是凭角色造的、无真实场景锚定。
- **从 transcript 挖 → 保真高、覆盖窄**。有且只有 **1 道**是逐字节真挖（还原会话里真实执行的任务，fixture 就是当时 Read 的真实文件），其余是 LLM 以 transcript 摘要做种子生成的变体；但覆盖被那场会话圈死——transcript 里没有的场景挖不出来。同 pr-reviewer：mining 5 道缺大 diff / 批量重命名（那场 transcript 没有这两类）。过反幻觉闭集（引用文件限定在 transcript 实际出现的）防臆造。

> 怎么选：**还没真实跑过 → 只能凭声明生成**；**有一份真实好会话 → 挖一道真场景打底，再凭声明补覆盖盲区**。两路产出的题形态一致、可混用（挖的题 + 凭声明补的题拼进同一份 evals.json）。只读 / 纯建议 agent（只给意见、不产文件，如 pr-reviewer）两路都一样：断言带 `scope:"transcript"` 只查 agent 的**回答文本**——它不产文件，答案就在回答里，断言去查输入 fixture 会判错对象（见 §7.1）；待审文件落地成 fixture、prompt 只引用路径，让 agent 真 Read/Grep（别把代码塞进 prompt 文本，否则它没文件可碰、测不到工作流）。

挖题/生成失败时（LLM 超时 / 限流 / 退出码，或返回了但解析不出结构化 eval）不会静默产空。LLM 生成路径**先重试**（调用失败与解析失败都大概率瞬态，重试最多 2 次、终端打 `重试 n/2…`，对齐 audit 判官的重试哲学；skill 与 agent 出题都如此），耗尽才产**兜底 eval**：skill 退模板出题，agent 无确定性 fallback → 0 题 + 终端告警，且**不覆盖已有 evals.json**（exit 1、既有考卷字节不动——覆盖式 init 的语义是「换新考卷」，不是「失败时清空」）。兜底 eval 的 prompt 退到最朴素形态（transcript 挖题用真实 user turns、LLM 生成退到模板），`assertions` 往往为空，并打 `degraded:*` flag（transcript 挖题是 `degraded:no-llm`，LLM 生成是 `degraded:llm-fallback(原因;重试 N 次后仍失败)`）。`init` 检测到后黄色告警——`⚠️ N 个 eval 为 degraded fallback(LLM 失败,已降级模板/兜底挖取——题面为模板句、assertions 可能为空):`，逐条列出哪些 eval + flag，并提示`建议重跑,或检查 claude CLI 可用性`。这样既不因一次失败就产不出题，也不假装成功——token 已花但考卷不是 LLM 出的，终端和 evals.json 里都留痕可辨。

**字段名拼错也会告警**：LLM 生成时如果把字段名写错（如 `expected_output`→`expected`、`assertions`→`assertion`），pydantic 默认会**悄悄丢掉**该字段——考卷看着生成成功、实则缺那块，`eval` 时才暴雷。`init` 会校验 LLM 输出的字段名，发现不认识的就告警 `⚠️ N 条 eval 含未知字段(已忽略;可能拼错)`，逐条指出哪条 eval 的哪些字段可疑，免得拿着残缺考卷还以为没问题。同样 warn 不拒（拒绝会掉进纯模板兜底，反而更差）。

### 4.3 add：追加单道题

```bash
sift add pdf/ --idea "加密 PDF 用错密码应失败" --level L2   # 一句话 → LLM 生成完整 eval
sift add pdf/ --prompt "..." --expectation "..." --level L2  # 手动模式
sift add pdf/ --idea "..." --dry-run                          # 预览不写入
```

`--idea` 模式下自动分配 id、自动推导 assertions 和 fixtures。

**给 agent 追加单题**：加 `--kind agent`——`--idea` 走 agent 单题 prompt（喂 agent system prompt + idea，产一道带 `scope:"transcript"` 断言、文件操作时带 fixtures 的 eval；平行 skill 的 `--idea`）。手动模式（`--prompt --expectation`）kind 无关。

```bash
sift add path/to/agent.md --kind agent --idea "审一个用 pickle 反序列化用户输入的 PR"
```

### 4.4 定向补薄弱面：`--focus` + `--append`

`init` 一次性生成整套时，LLM 有个系统性盲区——**倾向正面题 / happy path**，容易漏反面场景（声明不该被触发的请求）、边界 / 错误处理（空输入、超大、损坏、异常）。`init --focus` + `--append` 帮你定向补这些盲区，且不丢之前生成 / 手改的题：

```bash
sift init path/to/skill/ --focus negative-cases --append  # 补反面场景,追加到已有考卷
sift init path/to/skill/ --focus edge-cases --append      # 补边界/错误处理
sift init path/to/skill/ --focus "加密/损坏 PDF" --append # 自由文本:skill 特定方向
```

- **`--focus <类>`** 决定这次生成什么：预设 `negative-cases`（不该被触发的反面场景）/ `edge-cases`（边界 / 错误处理）覆盖最常见的通用盲区；也接受**自由文本**（任意描述一个特定方向，如「加密 PDF」「超大日志」），补通用类够不到的薄弱处。它往 prompt 注入一段定向指令，强制 LLM 进入反面 / 边界思维。（`anti-triggers` 是旧名，仍作静默别名兼容。）
- **`--append`** 决定怎么写：追加到已有 evals.json——生成时**读现有 evals 喂给 LLM**（走定向补题：只补 `--focus` 这一面、与现有题**语义去重**——对照现有题的 prompt 和 rationale，不同措辞测同一点的也不重复），新 eval 的 id 接续已有 `max+1`、不覆盖旧题。不带 `--append` 则默认覆盖（首次出题或想重练一套时用，此时无现有题可对照、focus 仍定向但不去重）。

**为什么用这个而不是 `add`**：`add`（4.3）是手动一道道加，得你自己想反面场景；`--focus` 让 LLM 在定向模式下批量生成一类，省掉手想的力气——尤其补的是你**没意识到的**盲区（通用预设类主动提示）。两者互补：`--focus` 批量铺面，`add` 精修单道。

> `--focus` 只对 LLM 生成路径（默认 `init`）生效；`--from-transcript` 是从真实会话挖题，不适用 `--focus`。补完别忘了 `inspect`（4.5）体检、`freeze`（4.7）冻成基线再 `eval`。

### 4.5 inspect：题集体检（不下判决）

```bash
sift inspect path/to/evals.json        # 表格
sift inspect path/to/evals.json --json  # 机器可读
```

注意位置参数是 **evals.json 路径**（不是 skill 目录）。`inspect` 对已有考卷做静态体检：完整性、稳定性（programmatic/LLM 占比）、潜在问题 flag。它**不执行、不调 LLM、不下判决**——只报指标和关注点，帮你决定「这份卷子能不能冻结成基线」。

### 4.6 coverage：出题漏测诊断

出完题、补完薄弱面，还要回头看一眼：**这份考卷覆盖了声明的多少内容点？** `init` 出题是 LLM 生成的（出卷噪声 C），会系统性漏掉某些指令 / 条件分支——这些漏测点是 `eval` 够不到的盲区（跑多少轮也测不着）。coverage 把它们亮出来，是考卷上线前的最后一道体检。

**怎么测：一次 LLM 映射，不 re-run Claude**

```bash
sift coverage path/to/skill/                # 默认:诊断漏测
sift coverage path/to/skill/ --augment      # 漏测点自动补题(写回 evals.json,需人审)
```

coverage 不 spawn Claude 跑题——它做的是**一次性 LLM 映射**：喂声明正文 + 各 eval 的 `id` / `prompt` / `rationale`，让 LLM 列出声明的**可测内容点**（具体的规则 / 步骤 / 能力 / 条件分支，不含散文和示例），再判断每点被哪几道 eval 真的**验证**（不是仅在 prompt 里提到）。`covered_by` 为空的内容点 = **漏测**。

产 `coverage-report.json`（机器读）+ `coverage-report.html`（人读，always 写，漏测行高亮红）+ 终端摘要（内容点 N · 漏测 M · 耗时 · token），并给每个漏测点一条 `init <skill> --append --focus "<label>"` 处方（人工定向补，复用 §4.4 的 `--focus` 机制）。

**给 agent 诊断漏测**：加 `--kind agent`——内容点从 agent 定义正文切（措辞走 agent 导向），漏测处方带 `--kind agent`；`--augment` 自动补题走 `generate_for_agent`（非 skill 的 `generate_with_llm`）。

```bash
sift coverage path/to/agent.md --kind agent            # 诊断 agent 定义内容点覆盖
sift coverage path/to/agent.md --kind agent --augment  # 漏测点自动补题(走 agent 出题)
```

**怎么读报告**

- **内容点 N / 漏测 M**：内容点是 LLM 从声明切出的可测点（粒度主观，见下），漏测是其中 `covered_by` 空的点。
- **逐漏测点**：每个漏测点列 `label` + 声明原文片段（`source_quote`），帮你判断「这点声明真这么要求吗、漏得对不对」——LLM 偶尔会把示例 / 散文误当内容点，看 `source_quote` 能秒辨。
- **HTML** 漏测行（`covered_by` 空）整行高亮红色，一眼定位。

**补漏测：人工定向（推荐）/ `--augment` 自动**

两条路：

- **人工定向**（推荐）：逐条看漏测点的 `source_quote`，确认是真盲区后，照终端处方跑 `init path/to/skill/ --append --focus "<label>"`（§4.4 的 `--focus`：定向补某一类、`--append` 追加不覆盖旧题、id 接续）。每条都过一遍人脑，质量最稳。
- **`coverage --augment`**（自动）：每个漏测点复用 `init` 的生成器（`EvalInitGenerator(focus=<label>)`）自动补 eval——生成时**喂现有 evals**（含本批已补题），走与 §4.4 `--append --focus` **同一个定向补题内核**（只补该漏测点、与现有题语义去重），新题带 `review:coverage-augment` flag、再按 `prompt` 字面去重兜底、id 接续，写回 evals.json。**质量不保证、必须人审**：自动补的 eval 只是「贴着那个 label 生成的题」，不一定真测到那个点、也可能是凑数题——`review:coverage-augment` flag 标着等审，人确认「这题真测到了、且不是凑数」后再接受，别直接拿去跑 `eval`。某点补题失败（限流 / 异常）逐条跳过、不中断。

**诚实：LLM 估、有噪声、无 gate**

- **LLM 估、有噪声**：内容点清单的粒度（一个点怎么切）是主观的，映射是 LLM 判的——都抖。**「0 覆盖」的大漏测可靠**（一个内容点没任何 eval 碰，是明确的盲区信号）；边界态的「半覆盖」（点被部分 eval 碰到）噪声大。coverage 是**漏测预警、不是精确度量**——拿它定位明显盲区，别对每条 `covered_by` 过度解读。LLM 调用整体失败时降级为空报告（exit 0，不炸）。
- **无 gate、独立 artifact**（同 trigger 第 4 维）：**无 gate / 不出 exit-code 判决**，`coverage-report.json` 不并入 `benchmark.json`。coverage 只产诊断报告，不挡 CI、不改 `eval` 结果。

**为什么不建结构化指令表**：coverage 不建固化的 eval↔指令对应表，而是**每次跑时当场让 LLM 按语义判**——靠 eval 的 `rationale`（init 自报「这题对应声明哪段」）做桥梁。自动建的表准度和当场 LLM 判一样，却白搭维护复杂度（只有人工逐条校对才更准，工具不做人工表）。

> coverage 是诊断维度：eval 的三维（scoring / compliance / calibration）通过考卷评声明的能力，coverage 退一步评**考卷本身**——考卷覆盖不全，这三维的结论就只能覆盖考卷够到的部分（trigger 用 trigger_probes、critique 直接读声明，都不走考卷、不受此限）。先 `coverage` 看考卷有没有大盲区，补全后再 `eval`，这三维的信号才扎实。

### 4.7 freeze：冻结基线

```bash
sift freeze path/to/evals.json                         # → baseline.json + baseline.meta.json
sift freeze path/to/evals.json --name v1 --skill-version 1.2
```

`freeze` 把考卷冻成命名快照 + 溯源 sidecar，供 CI / 跨 run 复用同一份题。产出的仍是标准 evals 格式，loader 零改动；同名覆盖（幂等）。

这里有一个贯穿全工具的设计原则值得记住——**机制不策略**：sift 只提供「冻一份题、跨 run 复用」的机制；要不要把 baseline.json commit 进 git、门禁阈值定多少、CI 里 N/A 阻不阻塞，都归消费方决定。工具不替你做这些策略选择。

---

## 第五章 动态评测 eval

`eval` 是评测主命令、也最重。它把 Claude 真的跑起来、评分、查遵循性、校准、聚合，是纯数据生产者。它有两种能力评测模式：skill 走 **A/B 对照**，agent 走**绝对质量打分**（无 A/B）。另有 `run` 命令跑真任务（不读 evals.json），见 §5.5。

### 5.1 A/B 对照：回答「skill 到底有没有用」（skill 专属）

`eval` 的默认模式是 A/B 对照：同一份考卷，让 Claude 在**开 skill**（with_skill）和**关 skill**（without_skill）两种配置下各跑一遍，比较差异。**agent 评测没有 A/B**。

为什么要对照？因为模型本身就有能力。如果只跑开 skill 看通过率是 85%，你不知道这 85% 是 skill 带来的、还是模型本来就会。只有和「关 skill」的基线对比，delta 才是 skill 的真实贡献（处理效应）。这本质上是随机对照实验（RCT）。

两种对照方式：

- **默认 with/without**：同一个 skill，开/关。位置参数就是 skill 目录。
- **v1/v2 双 skill**：给 `--skill-b <另一个 skill 目录>`，两边各注入各自的 skill，标签默认取目录名。适合回归对照（候选 v2 vs 基线 v1）。

```bash
sift eval path/to/skill/ --runs 3                       # with/without，跑 3 轮
sift eval v2/ --skill-b v1/ --evals baseline.json        # v1/v2 回归对照（baseline.json 由 freeze 产生，见 4.7）
sift eval path/to/skill/ --scoring programmatic          # 仅程序化评分，零 LLM 成本
```

> 下文把两个配置（with_skill / without_skill，或 v1/v2）统称为 **side**。
>
> eval 默认会在末尾自动跑静态体检 + 安全审查（产出 `scorecard.json` / `vet-report.json`，可 `--no-static` / `--no-vet` 跳过）。还有一个安全相关开关 **`--security-gate`**：开启后，若 vet 判 `BLOCKED` / `REVIEW_NEEDED`，eval 直接退 1（否则 eval 的退出码只表示产数据成败，通过/失败由 `gate` 判，见附录 exit code 表）。

### 5.2 选题：全量 / 抽样 / 指定某几道

run 默认跑 evals.json 的**全部题**。题多时全量耗时耗 token（每道 × runs × sides × Claude 调用），开发迭代常不需每次全量。四种选题策略：

- **全量（默认）**：跑全部题——完整评估，正式评测用。耗时最长。
- **`--eval-samples N` 或 `pct`**：**随机抽样** N 道或 pct% 道（如 `--eval-samples 10` 或 `0.25`）——快速估算整体表现，但**不可控选哪几道**（随机）。
- **`--eval-ids 3,5,7`**：**精确指定**某几道（按 eval.id，逗号分隔）——调试某道 fail、回归某道、重现某道行为。和 `--eval-samples`/`--changed-from` **三选一互斥**。
- **`--changed-from <baseline 目录>`**：**自动判受影响**——改声明后给基线目录，自动 diff 两边指令 + LLM 判哪些 eval 受这次改动影响、只跑那些（**回归探针**）。和 `--eval-samples`/`--eval-ids` **互斥**。

```bash
sift eval path/to/skill/ --eval-samples 10              # 随机抽 10 道估整体
sift eval path/to/skill/ --eval-ids 3,5                 # 只跑 id=3、5（调试/回归某几道）
sift eval path/to/skill/ --changed-from v1/             # 改 skill 后只跑受影响的题（给基线目录）
```

**为什么 ids 和 samples 互斥（不是叠加）**：指定了 id 又随机抽样会矛盾——「我选了第 3、5 道却又被随机抽掉」。语义清晰起见，同给报错；要减量就二选一（随机估整体用 samples / 聚焦某几道用 ids）。

**id 不存在报错（不静默）**：`--eval-ids 99`（evals.json 没这题）直接报错并列出可用 id，不静默漏跑——id 是精确指定，笔误该立刻发现（不像 `--eval-samples` 格式错静默当全量：那个随机抽样，笔误当全量可接受）。保序去重（`--eval-ids 3,3,5` 跑 3、5 两道，按给定顺序）。

**何时用哪个**：正式评测 → 全量；快速看 skill 大致水平 → `--eval-samples`；调试/回归/重现某道 → `--eval-ids`；**改声明后回归受影响的几道 → `--changed-from`**（省全量，但子集 delta 非整体测量，周期仍需全量兜底）。

> `--changed-from` 是**回归探针，不是代表性测量**：它只跑 LLM 判定受改动影响的子集，聚合 delta 在偏置子集上算、不代表整体质量；判定靠 LLM（偏过召：拿不准就带），不对劲可用 `--eval-ids` 覆盖。判定失败（调用/解析/返回不存在 id）报错退出；判定空集（零受影响）合法、跑空退出。周期性仍跑一次全量拿真信号。

> **想要更可控的判定？** `--changed-from` 现在靠 LLM 每次自动判受影响 eval（够用，但每次重判、不保证确定）。若需要更精确、可复现的判定，一个思路是**手工给每道 eval 标注它考的是声明的哪条指令**（存一张 eval↔指令对应表），改声明后直接查表得出受影响 eval——确定性、可审计，但这张表只有人工逐条校对维护时才比自动判定强；若也让 LLM 自动建、没人管，准度和自动判定（同一个 LLM 判断）一样，却白搭维护复杂度——权衡后当前未采用。自动判定不敷用时再考虑。

> `run` 命令（真任务，见 §5.5）不读 evals.json、没有 id 概念，`--eval-ids`、`--eval-samples`、`--changed-from` 都不是它的参数。

### 5.3 一次 eval 的完整流程（A/B 模式）

一道 eval 在**注入了 skill 的 side** 上跑时，背后走 7 步——with/without 模式只有 with_skill 侧满足，v1/v2 模式两边都满足：

1. **准备工作区**：拷贝 fixtures、所注入 skill 的 references、`source_dir` 进去。
2. **拼 prompt**：把 skill 正文拼进发给 Claude 的 prompt。
3. **spawn Claude**：`claude -p` 跑这道题，产出 `outputs/`（Claude 生成的文件）+ `transcript.jsonl`（完整会话）。
4. **评分（grading）**：跑程序化断言 + LLM 语义判分，产 `grading.json`（双率）。
5. **遵循性（compliance）**：从声明提取指令，对照 transcript/tool_trace 查，产 `compliance.json`。**注入了声明的 side 才跑**（with/without 模式只有 with_skill 侧产，v1/v2 模式两边都注入各自的 skill、两边都产）；**默认开**（注入声明即跑；单 transcript 过程对账本就有效、不 re-spawn cc），想跳过用 `--no-compliance`。遵循性用的是和 audit 同一套引擎，机制见第十一章。
6. **校准（calibration）**：交叉评分与遵循性，产 `calibration.json`。门控同上（有 compliance 才产）。
7. **聚合**：所有 run 跑完后，聚合成 `benchmark.json` + 盲比较 + 深度分析 + report.html。

> with/without 模式下，without_skill 侧没注入 skill、谈不上遵循，只跑前 4 步 + 聚合（无 compliance / 无 calibration）；v1/v2 模式两边都有 skill，7 步全跑。

> **agent 评测模式（`--kind agent`）走绝对质量、不做 A/B 对照**：只一个 `"agent"` config（无 with/without、无 delta、无盲比较），报绝对 pass_rate。注入也不同——agent 正文（frontmatter 后的 body）经 cc `--append-system-prompt` 进 **system 层**（不是 §5.4 那样拼进 user prompt），prompt 原样。评分 / 遵循性 / 校准照跑（单 config，各自对账 agent 正文里的指令）；`scorecard` + `vet` 不跑（它们是 SKILL.md 结构检查，对 agent.md 跑出假信号如「Missing SKILL.md」；agent 的静态质量由 `critique --kind agent` 覆盖）。需配 `--evals`（agent.md 不能像 skill 目录那样自动发现 evals.json）。

评分里若出现 **UNEVAL（评不出来）**——某条 expectation/assertion LLM 判不了（transcript 截断 / 解析失败 / 信息不足），区别于 FAIL（确定错）——会触发自动重试（`--max-retries`，默认 2），重试目录形如 `run-1-retry-1`。**重试有上限、不是无限重试到评出来为止**：耗尽次数仍 UNEVAL 就原样落盘，所以最终 grading 不保证全 PASS/FAIL，可能仍含 UNEVAL（以及 TIMEOUT/ERROR）。

**这些阶段哪些调 LLM**

上面 7 步按「调不调模型、调的是哪种模型」分一下——这跟 §12.9「audit 的阶段流水线」是同一种看法：把流程拆成阶段、标清每个是程序还是 LLM。步骤 1–2（搭工作区、拼 prompt）是纯程序、不展开；从步骤 3 起：

| 阶段 | 类型 | 干什么 |
|---|---|---|
| 执行 executor（步骤 3 spawn） | 被测 LLM | `claude -p` 跑题干活的那个 Claude——**是被测对象，不是判分模型**；token 单列、不计入评分成本 |
| 评分 grading | 混合 | expectations[LLM] + Tier 1 断言[程序] + Tier 3 断言[LLM]，产 `grading.json` 双率（§7） |
| 遵循性 compliance | 混合 | 6 阶段流水线：extract[程序] / refine[LLM] / headline[LLM] / rebuild[程序] / judge[LLM] / postprocess[程序]（§12.9） |
| 校准 calibration | 程序 | 评分 × 遵循性交叉的**纯算术**，产 `calibration.json`，不调 LLM（§9） |
| 聚合 aggregate | 混合 | 主体读各 run 的 JSON 算统计[程序] + **尾部还挂两个 LLM 步骤**：盲比较 comparator[LLM] + 深度分析 analyzer[LLM]（§8） |

要点：

- **调 LLM 的有三处**：grading（expectations + Tier 3）、compliance（refine / headline / judge）、aggregate（comparator / analyzer）。grading 里的 Tier 1 断言、整个 calibration 都是纯程序——零 token、零阅卷噪声。
- **两种 LLM 别混**：步骤 3 spawn 的 Claude 是**被测对象**（执行任务，token 单列、不算评分成本）；后三处的 LLM 是**判分 / 分析模型**（评执行得好不好）。报告的 token 表把两者分开列。
- **aggregate 不是纯聚合**：它主体是程序的（读 JSON 算双率 / token / 耗时汇总），但尾部把 comparator + analyzer 也吸了进来（各自 `try/except`，失败跳过不炸）——所以「聚合」这一步既产程序统计、也产 LLM 结论。

### 5.4 skill 怎么「注入」：拼进 prompt，不是加载

skill 不是一个运行时加载的模块。sift 做的是：

- 把 `SKILL.md` 正文**拼进**发给 `claude -p` 的 prompt；
- 把 skill 的 `references/`、`scripts/` 等拷进工作区，让模型可以按 SKILL.md 里写的「读 references/foo.md」真的读到。

所以「模型用了 skill」≈「模型读到了这份手册并照着做」。这也是为什么 SKILL.md 的写法直接决定效果——它就是模型看到的全部指令。

> 一个常见坑：**references/ 在 skill 注入时会自动拷进工作区**（无需配置）；但 skill 依赖的 `scripts/` 等其它目录**不会**自动拷——若 SKILL.md 让模型运行 `scripts/foo.py` 或读取 references 以外的文件，得在 eval 里用 `source_dir` 把它拷进来，否则模型反复找不存在的文件直到 timeout，效果从「有增益」掉回「和不开一样」。看到 with/without delta 接近 0 时，先排查依赖目录进没进工作区。（客观 Δ 趋零时 viewer 会主动标 ⚠ 提醒——但 ⚠ 只是「无增益」信号，首选按这里排查依赖这一常见假阳性；排除依赖后，才考虑 skill 是否已被模型原生能力覆盖、该收窄 scope。）

> agent 评测（`--kind agent`）不走这套 user prompt 注入——agent 正文经 `--append-system-prompt` 进 cc 的 **system 层**（agent 评测恒走 cc），prompt 原样不包；也没有 references/scripts 拷贝（agent 是单 `.md` 声明，非 skill 目录）。

### 5.5 真任务模式（`run` 命令）：产真实 trace 喂 audit

> 本节属 `run` 命令（拆分后专做真任务、`--task` 必填），非本章主命令 `eval`——留在此处因它是评测的姊妹用法，产物同为 `run-N/`。

`run` 命令**忽略 evals.json**，spawn 执行器在真实代码库/数据目录里跑一个真任务，产真实 transcript。

```bash
sift run path/to/skill/ --task "重构 X 模块，拆成三个文件" --cwd ./repo --runs 2 -o /tmp/live
sift run path/to/skill/ --task "重构 X 模块，拆成三个文件" --cwd ./repo --executor opencode -o /tmp/live   # 改用 opencode 跑（产 transcript.json）
```

`--task` 模式 spawn Claude 在 `--cwd` 里跑这个真任务，产 `run-*/transcript.jsonl`（默认 cc）或 `run-*/transcript.json`（`--executor opencode`，需 opencode CLI 在 PATH，模型在 `opencode.json` 里配，省略 `--model`）；`--executor cc,opencode` 两个执行器各跑 `--runs` 次。每 run 在终端打印 operational 摘要（耗时/turns/tokens/stop_reason/tool_calls + 结果头部，读自 run_debug.json）。此模式 `--evals`/`--scoring`/`--workers` 等大多不生效，`--eval-ids`/`--eval-samples`/`--changed-from` 也无效（它不读 evals.json、没有 id 概念）。

**拷贝隔离**：`--cwd` 会被**拷贝隔离**——Claude 不碰你的原目录。这是为了安全（真任务可能改文件），也为了让多次 run 互不污染。cc 产物写在隔离拷贝 `output/workspace/`、不写回原目录。

**与 audit 闭环**（这是真任务模式的主要用途）：它产的 transcript 可以直接喂给 audit——

```bash
sift audit path/to/skill/ --transcript /tmp/live/run-1/transcript.jsonl -o /tmp/live-audit
```

所以真任务模式 + audit =「在真环境跑真任务 → 对账有没有按声明做」的完整闭环。`--task` 必须对口 skill（不对口 → cc 不按声明走 → audit 假 FAIL；这是使用约束，不是工具 bug）。

**它的边界：是什么、不是什么**——信号最真（真实目录 + 真实任务，cc 真跑），但有几条别误用：

- **它不判「skill 好不好 / 有没有用」——那是 A/B 对照（§5.1）的活。** 真任务模式产的是单次真任务 transcript，没有 with/without 对照；scoring 的 A/B 需要基线。想判 skill 值不值得用 → `eval`（A/B 对照），不是这里的真任务 `run`。真任务模式的产出喂 audit（过程对账诊断）。
- **它是「真任务模式」，不是「真环境模式」。** `--task` 是你给的受控真任务，不是生产系统自然产生的任务流——信号真，但仍是你指定的任务。
- **transcript 是声明演进快照。** 审的是 spawn 那一刻声明的执行；声明改了，旧 transcript 审旧版，要重新审得重跑。

> 真任务模式很轻——它不评测、不打分、不做 A/B，只产 trace。所以别把它当成和评测、审计并列的事；它是产真任务 trace 的小功能，产出给审计用。

### 5.6 产物目录树

```
output/
├── benchmark.json          # 聚合：双率 delta + 盲比较 + 深度分析；`llm_duration_seconds` 耗时两维度(墙钟分解加总=总计:phase1=准备+执行+grader+遵循性+calibration 并发整段、phase2 拆读产物/盲比较/深度分析 / LLM 累计并发叠加)
├── report.html             # 自包含 HTML 报告
├── gate.json               # gate 命令产出（非 eval 产出）
├── scorecard.json          # 静态评分卡（eval 末尾自动产）
├── vet-report.json         # 安全扫描（eval 末尾自动产）
├── previous/               # 重跑归档：fresh 重跑时上次整棵树移入（单槽）
├── with_skill/             # v1/v2 模式换成各自标签名（两边都注入 skill → 两边都有 compliance + calibration）
│   └── eval-<id>/run-<n>/
│       ├── outputs/        # Claude 产生的文件
│       ├── grading.json    # 评分（双率）
│       ├── compliance.json # 遵循性（五态 + method）
│       ├── calibration.json# 结果×过程校准
│       ├── run_debug.json  # 完整回复 + tool_calls + timing
│       └── timing.json     # 耗时和 token
└── without_skill/
    └── eval-<id>/run-<n>/  # 同上，但无 compliance / 无 calibration
```

各文件的字段级含义见[第六章](#第六章-评测产物解析与重跑归档)和参考手册。

> `eval` 是纯数据生产者——exit 0 只代表产数据成功，**通过/失败判决由 `gate` 出**，不再由 eval 的退出码决定（旧版读混合 delta 退 1 的逻辑已退役）。

---

## 第六章 评测产物解析与重跑归档

这一章下钻两件事：一次评测产出的每个 json 里什么字段什么意思（§6.1–6.3），以及重跑同一个声明时旧产物怎么归档、横向 A/B delta 和纵向 vs-上次 delta 各指什么（§6.4–6.6）。

### 6.1 一次评测的链路

```
spawn claude -p
   → transcript.jsonl（完整会话）+ outputs/（产物文件）
   → grading.json（评分：expectations + assertions，双率）
   → compliance.json（遵循性：AuditReport，五态 + method）         [注入 skill 的 side 才产]
   → calibration.json（校准：结果×过程 5 态）                       [有 compliance 才产]
   → benchmark.json（聚合：per-run → run_summary + delta + 盲比较 + 分析）
```

### 6.2 per-run 产物逐个解读（字段级）

#### grading.json（评分）

顶层 `GradingResult`：

| 字段 | 含义 |
|------|------|
| `expectations[]` | 每条 expectation / assertion 的判定（见下表） |
| `summary` | 汇总：`passed`/`failed`/`total`/`pass_rate` + 双率分项 |
| `execution_metrics` | 工具调用统计：`tool_calls`{工具名→次数}、`total_tool_calls`、`total_steps`、`files_created[]`、`errors_encountered`、`output_chars`、`transcript_chars` |
| `timing` | `total_tokens` / `duration_ms` / `total_duration_seconds` / `grader_token_usage`（Phase 2：per-run grader LLM token，rollup 进 `benchmark.json.llm_token_usage.scoring`）/ `grader_duration_seconds`（Phase 2.5：per-run grader 耗时，rollup 进 `llm_duration_seconds.scoring`）|
| `claims[]` | grader 抽取的事实声明（`claim`/`type`/`verified`/`evidence`） |
| `user_notes_summary` | grader 标的不确定项：`uncertainties[]` / `needs_review[]` / `workarounds[]` |
| `eval_feedback` | grader 对这道 eval 设计本身的反馈（`suggestions[]` / `overall`） |
| `error` | 非空表示评分过程出错 |
| `cache_key` | 内容感知续跑键（见 7.5） |

每条 `ExpectationResult`：

| 字段 | 含义 |
|------|------|
| `text` | expectation 文本 |
| `passed` | 是否通过（bool） |
| `evidence` | 证据引用（强制非空） |
| `source` | `programmatic` / `llm`（决定进哪个双率桶） |
| `verdict` | `PASS` / `FAIL` / `UNEVAL` / `TIMEOUT` / `ERROR` |
| `retry_count` | 这条经过了几次重试 |

`summary`（`GradingSummary`）：`passed`/`failed`/`total`/`pass_rate` + `passed_programmatic`/`total_programmatic`/`pass_rate_programmatic` + 对应的 `*_llm`。某类没有 expectation 时，对应分项为 `null`（不报假 0）。注意 `failed = total − passed`，所以 UNEVAL/TIMEOUT/ERROR（`passed=false`）在数值上算「未通过」，会拉低 `pass_rate`——但报告里它们与 FAIL 显示不同图标（见 7.4）。

#### compliance.json（遵循性，仅注入 skill 的 side 产）

= `AuditReport`（与 `audit` 命令的 `audit-report.json` 同 schema）：

| 字段 | 含义 |
|------|------|
| `skill_name` / `transcripts[]` | 被审声明 + transcript 文件名 |
| `summary` | 计数 `{total, pass, fail, na, unresolved, indeterminate}` |
| `findings[]` | 每条指令的对账结果（见下） |
| `by_instruction[]` | 按指令跨 transcript 聚合：`pass_count`/`fail_count`/`na_count`/`unresolved_count`/`indeterminate_count`/`total`/`fail_rate`/`fail_in_transcripts[]`/`section`（声明分组标题，first-wins）（单 transcript 也有，每指令 1 条） |
| `coverage` | 声明覆盖视图（§12.6；旧 report 无 → 默认空）：`{total, covered, uncovered, unknown, coverage_rate, sections[]}`——covered=任一 transcript PASS/FAIL、uncovered=全 N/A、unknown=只有没判成/判不准；反推（derived）指令不计入分母 |
| `boundary` | 工具对账视图（§12.7；旧 report 无 → 默认空）：`{total_tools, declared, undeclared, tools[]}`——会话里调过的领域工具 × 声明工具要求，只陈列、不进判决 |
| `warnings[]` | 顶部警告（没判成建议重跑、判不准占比偏高等） |
| `token_usage` | 本次所有 **LLM 判定调用**（refine + 条件/禁令/步骤判官 × 批）的 token 用量合计：`{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, total_cost_usd, calls}`。空 `{}` = 无 LLM 调用或旧 report。**注意是判官 token**——与 eval 侧 `RunResultMetrics.tokens`（executor 跑任务的 token，走 stream-json）不同；eval 的 grader/comparator/analyzer token 已计入 `benchmark.json.llm_token_usage`（Phase 2：`scoring` 段含 grader+comparator+analyzer；本 audit token rollup 进 `audit` 段） |

每条 `AuditFinding`（两个正交维度 `verdict` × `method`）字段全表见 §11.3。

遵循度 = `pass / (pass + fail)`，排除 N/A + UNRESOLVED + INDETERMINATE（三者都不进分母）。

#### calibration.json（校准，有 compliance 才产）

`CalibrationReport`——结果×过程 coherence 判决（机制见 [9.3](#93-五态矩阵)）：

| 字段 | 含义 |
|------|------|
| `verdict` | 5 态：`TRUSTED` / `HIGH_SCORE_SUSPECT` / `FIXABLE` / `SKILL_FAILED` / `PROCESS_UNKNOWN` |
| `result_pass` | 结果轴是否通过（客观率 ≥ 0.8，或无客观锚时回退主观率达标）|
| `process_status` | 过程轴：`ok` / `violated` / `unknown`（无可对账）|
| `hard_violations[]` | 硬类（prohibition / output）违规明细，每条 `{category, instruction_text, evidence}` |
| `soft_notes` | `{"step": N, "conditional": N}`——这两类 FAIL 的计数（软类，只记提示、不翻判决）|
| `scoring_rate` | 驱动判决的率：客观优先，无客观锚时回退主观 |
| `scoring_rate_basis` | `programmatic`（押客观）/ `mixed`（无客观锚、含主观）|
| `scoring_rate_programmatic` | 客观通过率（参考；`None` = 该 run 无客观断言）|
| `scoring_rate_llm` | 主观通过率（参考）|
| `compliance_rate` | = `pass/(pass+fail)`（显示用）；过程未知时为 `None` |

> gate 读 calibration_summary 的 HIGH_SCORE_SUSPECT 作 **soft warning**（不进 decision）。`hard_violations` 里列的就是值得人秒辨真伪的「高分存疑」证据。

#### run_debug.json

单次执行的运行时快照（调试、聚合时被读）：

| 字段 | 含义 |
|------|------|
| `config` | 配置名（with_skill / without_skill，或 v1/v2 标签）|
| `eval_name` | eval 真名（聚合时被读出来填进 benchmark，否则只显 "Eval N"）|
| `eval_id` / `run_num` | eval id + run 编号 |
| `prompt` | 发给 claude 的 prompt（截断 500 字符）|
| `duration_ms` | 单次执行耗时（毫秒）|
| `total_tokens` | 本次 token 数 |
| `num_turns` | claude 跑的轮数 |
| `stop_reason` | 结束原因（`end_turn` / `max_turns` / `timed_out` …；含 `timed_out` 会被异常检测抓）|
| `tool_calls` | 工具调用计数 |
| `tool_trace[]` | 工具调用序列（逐条明细）|
| `result_text` | claude 最终回复（截断 2000 字符）|
| `transcript_preview` | transcript 预览（截断 2000；完整会话在 `transcript.jsonl`）|
| `grading_summary` | 本次的 grading summary（双率）|

#### timing.json

`TimingData`，三个字段：

| 字段 | 含义 |
|------|------|
| `total_tokens` | 总 token |
| `duration_ms` | 耗时（毫秒）|
| `total_duration_seconds` | 耗时（秒）|

聚合时若 grading.json 的 `timing` 缺失，会回退读它——是 timing 的兜底源。

### 6.3 聚合成 benchmark.json

所有 per-run 跑完后，`BenchmarkAggregator.aggregate()` 把它们聚合成 `BenchmarkData`：

| 顶层字段 | 含义 |
|---------|------|
| `metadata` | `skill_name` / `skill_path` / `executor_model` / `comparator_model` / `analyzer_model`（executor 跑题 / comparator 盲比较 / analyzer 深度分析 三个 LLM 角色，当前锁定三者用同一个 `--model`；真实模型名都借 executor 从 transcript 抓，故同源）/ `timestamp` / `evals_run`（跑过的 eval id 列表）/ `runs_per_configuration` |
| `runs[]` | 每个 (eval, run) 一条 `RunResultEntry`（见下） |
| `run_summary` | 每 config 一组统计 + `delta`（见下） |
| `anomaly_report` | 异常检测（超时/离群） |
| `comparisons[]` | 盲比较逐条（读 `outputs/`，不是 benchmark） |
| `comparison_summary` | 盲比较聚合 `{total, a_wins, b_wins, ties, failed, valid, win_rate, label_a, label_b}` |
| `analysis` | 深度分析（见 11.5） |
| `selection` | （可选，仅 `--changed-from` 子集跑时）`{mode, ids, affected, total}`——标识本次为受影响子集跑、delta 非整体测量 |
| `llm_token_usage` | （Phase 2）本次评测的 LLM token 用量 `{scoring, audit, executor, total}`：`scoring`=grader+comparator+analyzer 合计（另细分 grader/comparator/analyzer 分计段;带 cost/calls,calls=各子项真实调用次数之和）；`audit`=compliance rollup 自 per-run `compliance.json`；`executor`=被测 cc token（仅 token 数无 cost，对应 `run_summary.tokens`）；`total`=全加 token、cost=scoring+audit。**executor 是被测 token 效率（无 cost），scoring+audit 才是评测系统付出的 LLM 成本**。viewer 头部「LLM token 用量」分列 + 总计；旧 report 无此字段 → `{}` |
| `llm_duration_seconds` | （Phase 2.5）本次评测耗时（秒）`{scoring, audit, executor, total}`：分列=各类调用时长加总（grader+comparator+analyzer（另细分 grader/comparator/analyzer 分计）/ compliance / 各 run）；**`total`=墙钟**（eval 命令头尾独立测）。与 token 根本不同：token 是消耗品可累加（total=分列和）；time 是墙钟、各段并行重叠（workers/线程池），**分列和 ≥ total 正常**，total 不由分列加、viewer 不对账分列和 vs total。分列看「哪段重」，total 看「等多久」；旧 report 无此字段 → `{}` |

每条 `RunResultEntry`：`eval_id` / `eval_name` / `configuration` / `run_number` / `result`（`RunResultMetrics`：`pass_rate`/`passed`/`failed`/`total`/`time_seconds`/`tokens`/`tool_calls`/`errors` + 双率 `pass_rate_programmatic`/`pass_rate_llm`）/ `expectations[]` / `compliance`（**通常为 null**——聚合器不嵌完整 compliance，完整 AuditReport 在 per-run `compliance.json` 里，report 用 `find_runs` 另读）/ `notes[]` / `is_anomaly` / `anomaly_reason` / `retry_count`。

`run_summary`：每个 config 一组——`pass_rate` / `pass_rate_programmatic` / `pass_rate_llm`（后两者可能为 null）/ `time_seconds` / `tokens`，各带 `{mean, stddev, min, max}`；再加 `delta`：全是**有符号字符串**（如 `"+0.27"`），= 候选 − 基线，双率项可能为 null（任一侧无该类 expectation 时不报假 0）。

### 6.4 retry 机制

UNEVAL / 出错时自动重试（`--max-retries`，默认 2），每次重跑完整的 Claude + 评分 + 遵循性。重试目录形如 `run-1-retry-1`。聚合时 `index_runs` 只取**最终 retry**（否则被取代的失败 base 会被当成独立低分 run 计入，拉低均值）。`retry_count` 记在 RunResultEntry 里。

### 6.5 重跑归档：resume / accumulate / fresh

重跑同一个声明时，sift 会先回答两个问题：① **上次跑完了吗**（`benchmark.json` 在不在）？② **inputs 变了吗**（现有 slot——即每个 (eval, run) 槽位，如 `eval-1/run-2`——的 `cache_key` 还匹不匹配）？再结合 `--runs` 是不是调大，落进 resume / accumulate / fresh 三种行为之一。

**为什么要关心重跑归档**——它支撑的是 report 里的两种 delta，别混：

- **横向 A/B delta**（report「通过率变化 Δ」卡）：**同一次 eval 内**，with vs without 之差——回答「这个 skill 到底有没有用」。
- **纵向 vs-上次 delta**（report「vs 上次」卡）：**跨两次 run**，同一个 side 之差——回答「这次比上次进步/退步了吗、复现性怎样」。

第二种 delta 赖以成立的前提，就是 fresh 重跑时把旧树挪进 `previous/` 保留下来。所以归档不只是「旧数据去哪了」，而是纵向对比的根基。

三态判定：

| 情况 | `--runs` vs 已有 | 行为 | previous/ |
|------|------------------|------|-----------|
| 上次没跑完（`benchmark.json` 不在） | 任意 | **resume**：cache 续跑已完成的槽，补跑剩下的 | 不产 |
| 上次跑完 + inputs 没改 | 调大 | **accumulate**：旧槽 cache 复用 + 补新 run 号，聚合算全部 | 不产 |
| 上次跑完 + inputs 改了 | 任意 | **fresh**：旧树 MOVE 到 `previous/` + 全部 fresh 重跑 | ✅ 产生 |
| 上次跑完 + inputs 没改 | 同 / 变小 | **fresh**：MOVE 旧树 + fresh 重跑（看复现性 / 主观侧方差） | ✅ 产生 |

**resume（续跑）**——上次没跑完就中断了（`benchmark.json` 不在 = 聚合没成功）。不清空、不归档，在原地接着跑：已完成的 slot（`cache_key` 仍匹配）跳过，缺的 slot 补跑。典型场景：跑到一半 Ctrl+C / 崩了 / 撞限流中断，重跑同一条命令把没跑完的补上，省 token。

**accumulate（累加）**——上次完整跑完了、inputs 一点没改、这次只是把 `--runs` 调大（想要更多样本）。不归档，旧 slot（已有 run-1/2）cache 复用、不重跑 Claude，只补跑新增的 run 号（run-3…），最后聚合把新旧全部算进去。典型场景：3 runs 看完觉得样本不够，改 `--runs 5` 接着跑，只多花 2 runs 的钱。

**fresh（全新重跑）**——两种触发：① inputs 改了；② inputs 没改但 `--runs` 没调大（想看复现性 / 主观侧方差）。两种都把旧树整体 MOVE 到 `previous/`（单槽覆盖），原位清空 → 所有 slot cache miss → 必然从头重跑。典型场景：改了声明想看新效果；或同样配置再跑一遍看主观侧抖不抖。

> **accumulate vs fresh 的分界**：两者都是「跑完了再跑」，但 accumulate 复用旧 slot（inputs 没变、只是要更多样本），fresh 丢弃旧 slot 重跑（inputs 变了，旧样本是旧输入的产物、不能混进新聚合）。判据就是现有 slot 的 `cache_key` 还匹不匹配。

**「inputs 改了」具体指什么**：SKILL.md / agent.md 正文 / evals.json 内容（含删 eval、改 prompt / expectations / assertions / fixtures / 换 sides）/ `--model` / scoring 配置（hybrid/programmatic/llm）/ sift 代码版本。任一改动 → `cache_key` 变 → fresh → 产 `previous/`。已知缺口：eval 引用的 `source_dir` / `references/` 文件**内容**改动不检测（见 7.5），改这些请用新 `-o` 强制重跑。

### 6.6 为什么 fresh 用 MOVE 不用 COPY + 几个诚实的设计取舍

重跑要看的就是**方差**——Claude 非确定性，主观侧有真方差（约 23%，客观侧 programmatic 稳）。如果 copy 旧树（`grading.json` 还在原位），cache 会全 skip 出「和上次逐字相同」的结果，`previous/` 和 current 一模一样——归档了个寂寞。MOVE 清空原位 → 所有槽 cache miss → 必然 fresh 重跑，`previous/` 里才有真东西可比。accumulate 的「复用旧」靠 cache_key 匹配自然达成（不 MOVE → grading.json 在位 → skip），它和 fresh 的分界就是 inputs 变没变。

**归档范围：只 move eval 自产的产物，别命令的一律不卷。** eval 自产 = config 目录（`with_skill/`…含 `eval-*/run-*`）+ `benchmark.json`/`scorecard.json`/`vet-report.json`/`analysis-*.json` + `report.html`（eval 末尾自动产）。留原位的：`audit` 的 `audit-report.json`/`.html` + `slices/`、`gate` 的 `gate.json`（拆分后都是独立命令自己的产物，eval 重跑不代表它们失效）、skill 级报告（critique/coverage/trigger，与具体 run 无关）和未知文件——`previous/` 单槽会被下次 fresh 清空，把不随 eval 失效的数据卷进去等于 eventual 删除。

几个诚实的设计取舍（价值）：

- **样本数不同就标出来**：fresh 重跑若 runs 变了（previous 3 vs current 6），delta 卡显式提醒。均值仍可比，但别对一个小 delta 过度解读。
- **新增 run 无对照 = 隐藏，不是 bug**：6 个 run 里 run-4/5/6 是本次新增、`previous/` 没有 → 文件级「上次输出」面板对它们隐藏；聚合卡照常算（比的是均值）。两层分开、不矛盾。
- **双率哲学在纵向的延续**：客观侧跨 run 稳、主观侧会漂。vs-上次卡上你会看到客观 △ 多半小、主观 △ 可能大——看到主观 △ 大别慌（可能是 cc 方差而非 skill 退步），看到客观 △ 大才真该去查。
- **move 的诚实**：重跑 = 「重新测一遍」，承认不复用旧数据。比 copy 假装「既有对比又省 token」诚实——这俩在「输入没改 + 同 runs」时不可兼得，copy 会产出无意义的 identical previous。
- **单槽 previous，不做历史**：永远只留上一次，不搞 `previous-1/`/`previous-2/` 叠层（对比关心「现在 vs 上次」，叠层反而引来「到底比哪两个」的歧义）。想留多次快照？多次重跑间及时把 `previous/` 拷出去自己存。

**怎么用（四个典型场景）：**

- **(a) 改了声明，想看进步** → 同 runs 重跑 → fresh → `previous/` 存住上次 → report「vs 上次」卡看客观 / 主观 △（客观 △ 才是真改观；主观 △ 看方向）。
- **(b) 啥也没改，就想再看一次** → 同参数重跑 → fresh → vs-上次卡看主观侧漂移（复现性 / 噪声底）。**重跑必然 fresh（`benchmark.json` 在）——这是设计，不是漏了 cache。**
- **(c) 想加样本更稳** → 调大 `--runs`（3→6）→ accumulate → 旧 3 个复用 + 补 3 个，聚合算 6 个（无 vs-上次卡；这是一个在长的数据集，不是一次对比）。
- **(d) 上次撞限流断了** → 直接重跑 → resume → 跳过已完成的、补跑剩下的（不产 `previous/`）。

**没有「强制 fresh」开关**：重跑默认就是 fresh（只要上次跑完了）。accumulate 是「调大 `--runs`」这一个明确动作的专属——想 fresh 就直接重跑，想复用旧样本就调大 runs，语义清晰无歧义。

**报告里「vs 上次」那一栏怎么看**：这一栏把这次和上次的指标并排放，让你看变化。分三类读：客观通过率——看绝对水平和变化幅度（变化小说明稳）；主观率 / 盲评胜率——看趋势方向（变化大别马上怪 skill，先想是不是 cc 这次发挥的方差）；样本数那一行（比如 ⚠ 这次 6 runs vs 上次 3 runs）告诉你这次对比有多可信——样本数差很多，delta 就别太当真。上次没跑某项的话，那格直接不显示，不会拿 0 凑数。


---

## 第七章 评分引擎

评分回答「输出对不对」。**skill 和 agent 共用**同一套评分引擎。**被判的东西分两类**（后面所有概念都建在这上面）：

- **expectations** —— 自然语言预期语句（如「输出文件存在」「内容按日期升序」）。
- **assertions** —— 结构化断言，有 `type` + `params`（如 `{type: FILE_EXISTS, params: {filename: x}}`）。

**关键是分清两个维度，别混**：

- **声明形式**（怎么写进题里）：expectations 是自然语言一句话；assertions 是结构化声明（`type` + `params`）。上面两类就按这个分。
- **判定方式**（怎么判过没过）：程序判（客观可查，如文件在不在、正则匹不匹配）vs LLM 判（语义，程序没客观判据）。

「结构化断言」指**声明形式**（有 type+params），**不等于「程序可判」**——`LLM_GRADE` 也用结构化声明，但它判的是语义质量、程序查不了，只能 LLM 判：

| 断言 | 声明形式 | 判的内容 | 判定方式 |
|---|---|---|---|
| `FILE_EXISTS` | 结构化 | 文件在不在 | 程序（Tier 1） |
| `CONTAINS_PATTERN` | 结构化 | 正则匹不匹配 | 程序（Tier 1） |
| `LLM_GRADE` | 结构化 | 语义质量 | LLM（Tier 3） |

判定方式上：assertions 按内容能不能客观查分两档 **tier**——Tier 1 程序判、Tier 3 LLM 判（**tier 只管 assertions**）；expectations 没有 tier、一律 LLM 判。于是 LLM 判两处（expectations + Tier 3）、程序判 Tier 1；判完按判定方式（`programmatic` / `llm`）汇成**双率**。

> **Tier 是断言的能力分级，只有两档**：Tier 1（确定性，程序判定）和 Tier 3（语义，LLM 判定），**没有 Tier 2**。两档对应的是 sift 自己的双率判定体系——断言的判定方式实际就两类：程序判（Tier 1）和 LLM 判（Tier 3），「Tier」只是分级的习惯叫法。「跳过 2」是编号残留：Tier 2 原本指「程序 / 客观」侧再细分出的一档，sift 的体系用不上这个细分（`TOOL_CALLED` / `STEP_COUNT` 这种查 tool_trace / 运行时行为的都归 Tier 1），编号上就空过去了——不代表缺了什么能力档。

### 7.1 程序化断言（Tier 1，确定性）

这是评分里最值得信赖的部分——程序判定、零 LLM 成本、阅卷噪声为零、不可作弊。完整类型表见参考手册，常用的有：

| 类型 | 判什么 | 关键参数 |
|------|--------|----------|
| `FILE_EXISTS` | 文件存在 | `filename` |
| `FILE_CONTAINS` | 文件含子串 | `filename`, `pattern` |
| `CONTAINS_PATTERN` | 正则匹配 | `pattern`, `path`(可选), `scope`(可选,`"transcript"` 只查文本输出不碰文件) |
| `NOT_CONTAINS` | 反向匹配 | `pattern`, `path`(可选), `scope`(可选,同上) |
| `JSON_HAS_FIELD` | JSON 字段 | `filename`, `field`（支持 `a.b.c`） |
| `LINE_COUNT` | 行数范围 | `min`/`max`(至少一个) |
| `TOOL_CALLED` | 检查工具调用（看 tool_trace） | `tool_name`, `min_count` |
| `STEP_COUNT` | agentic 步数范围（Claude 跑了几轮 = `num_turns`，可客观计数，故归 Tier 1） | `min`/`max` |
| `CUSTOM_SCRIPT` | 自定义脚本 | `script`, `args` |
| `CUSTOM_PLUGIN` | 领域特定插件断言 | `plugin_name` |

设计哲学是「**能程序化查的就程序化查**」——凡是能客观验证的（文件在不在、内容匹不匹配、工具调没调），一律用断言，不浪费 LLM、也不引入阅卷噪声。

> **`scope:"transcript"` 给只读 / 纯建议 agent**：不产文件的 agent（如审 PR 给建议的 pr-reviewer），它的「答案」是回答文本（transcript），不在任何文件里。若断言带 `path` 指输入 fixture 或省 `path` 走递归，grading 会去读**输入文件**而非 agent 的回答 → 模式碰巧在输入里就恒 PASS、模式只在回答里就恒 FAIL，判卷失效。`scope:"transcript"` 把判卷对象从「文件」拨到「agent 的回答文本」。产文件的 agent（L2）则用 mixed 断言：`FILE_EXISTS` + `path` 查产物 + `scope:"transcript"` 查调查过程。出题（`init --kind agent`）自动产对应形态。详见 [evals-format.md](evals-format.md)。

### 7.2 LLM 语义判分

LLM 判的有**两条独立的线**，别并成一个「Tier 3」：

- **（a）expectations**（自然语言预期语句）：题目自带的人话预期（「输出文件存在」「解释清晰」……）——LLM 读 transcript + 产物判每条过没过。这条**跟 tier 无关，所有 expectations 一律 LLM 判**。
- **（b）Tier 3 断言**（结构化语义断言）：assertions 里那些没法客观验证的语义类型——`LLM_GRADE`（语义评分）、`LLM_FACT`（事实核查）、`LLM_NOT_DELEGATE`（查模型有没有把不该委托的子任务甩出去）。判官用结构化输出 + 强制 evidence 引用，每条必附证据；结构化输出走 schema 硬约束（forced tool_use + inline schema，验证过的对象代码直接解析，不从散文里抠 JSON）。

两条都进 LLM 侧（`source=llm`），区别只在：expectations 是题目里的自然语言预期，Tier 3 是出题时显式声明的结构化语义断言。

### 7.3 三种评分模式

`eval --scoring <mode>`：

| 模式 | 说明 | 调 LLM | 适用 |
|------|------|--------|------|
| `hybrid`（默认） | 先程序化断言，再 LLM 语义评分 | 是 | 通用 |
| `programmatic` | 仅确定性断言 | 否 | 输出结构化、可机械验证（省钱） |
| `llm` | 仅 LLM 语义评分 | 是 | 输出需要语义判断 |

`programmatic` 模式零 LLM 评分成本，盲比较也会自动跳过（盲比较只服务主观题）。

### 7.4 双率怎么算出来

每条 expectation/assertion 判完带一个 `verdict`（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`）和一个 `source`（`programmatic`/`llm`）。`grading.json` 的 `summary` 按 source 分别统计：

```json
"summary": {
  "passed": 3, "failed": 1, "total": 4, "pass_rate": 0.75,
  "passed_programmatic": 2, "total_programmatic": 2, "pass_rate_programmatic": 1.0,
  "passed_llm": 1, "total_llm": 2, "pass_rate_llm": 0.5
}
```

某类没有 expectation 时，对应字段为 `null`（不报假 0）。评分是**评分五态** verdict（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`），不是只有通过/失败：UNEVAL（评不出来，如 transcript 截断）/TIMEOUT/ERROR 在报告里与 FAIL **区分显示**（?/⏱/!，不混成红 ✗），让你分辨「没评出来」和「真错了」。**但注意 pass_rate 数值上它们仍算「未通过」**——`total` 含全部条数（含 UNEVAL/超时/出错），`passed` 只数 PASS，`failed = total − passed`，所以这些条会和 FAIL 一样拉低通过率；区分只在显示层，不在计数层。（这是**评分五态**；遵循性侧另有一套**遵循性五态** `PASS`/`FAIL`/`N/A`/`UNRESOLVED`/`INDETERMINATE`，见第十一章，两套别混。）

### 7.5 cache_key：内容感知续跑

重跑同一声明时，sift 不会盲目重跑所有题。它给每个 (eval, run) 槽算一个 `cache_key`：

```
cache_key = sha256(声明正文 + eval 全身 + 模型/配置 + sift 代码版本)[:16]
```

其中「sift 代码版本」= sift 自身源码（`src/sift/**/*.py`）的内容 hash（进程内只算一次）；`[:16]` 取 sha256 十六进制串的前 16 个字符。

重跑时，只有 `cache_key` 与本次输入**完全一致**且 `summary.total > 0` 的槽才跳过重跑（省 token）。这意味着：

- 改了声明正文 / evals / model / scoring 配置 → key 变 → 自动失效重跑；
- 升级了 sift 自身（代码版本 hash 变）→ key 变 → 重跑；
- without 侧不含 skill 正文，所以改 SKILL.md 只重跑 with 侧。

> 已知缺口：eval 引用的 `source_dir` / `references/` 文件**内容**改动不检测。改这些请用新 `-o` 强制重跑。

---

## 第八章 盲比较

盲比较（BlindComparator）是主观侧的主位信号，回答「两份产出，抛开标签，哪个更好」。**盲比较是 skill A/B 专属**——agent 评测没有 A/B 对照（单 config 绝对质量），所以也没有盲比较。

### 8.1 为什么需要盲比较

盲比较治的是三层噪声里的**阅卷噪声 A**，而且专治其中独立打分抹不掉的那一面。

阅卷噪声 A 有两面：一面是**随机抖**（同一个判官今天判 PASS、明天判 FAIL）——这个 `--runs` 多跑几次、或客观题交给代码就能平均掉；另一面是**系统性偏差**（判官这一整批偏严或偏松），它才是麻烦：with 和 without 各自独立打分时，这个「偏严」不会在两次独立打分之间抵消，而是夹带进 delta，让你以为 skill 有贡献、其实只是判官今天手紧。

盲比较正是来补这个缺口：**同一个 LLM、同一次调用**同时看两份产出（with 和 without，或 v1 和 v2），判谁更好。同一把尺子量两份，系统性偏差在「谁更好」这个问题上自动抵消（尺子偏严也对两份一视同仁），随机抖也不再是两组独立叠加。所以盲比较比 per-run 独立打分稳——它不是因为 per-run delta 不稳才需要（`--runs 3` 下其实够用），而是结构上更稳，且结论更直观（「with 胜率 73%」比「+18%」更直接回答 skill 帮不帮）。

### 8.2 位置随机化 + 语义 winner

为了彻底消除位置偏差（LLM 可能偏好首位 / 末位），盲比较每次会**抛硬币式（50/50 随机）决定摆位**——哪边放在 A 位、哪边放在 B 位是随机的，判官只看到 position A/B，不知道背后是哪一侧。

报告怎么知道「A 赢了」是哪一侧？靠存**语义标签**而非裸 A/B：两侧一开始就各有名字 `label_a` / `label_b`（默认 `label_a` = with = 候选、`label_b` = without = 基线；v1/v2 模式 `label_a` = 位置参数那个 skill = 候选、`label_b` = `--skill-b` 那个 = 基线，标签默认取目录名）。判官返回裸的 A / B / tie 后，盲比较按**当次**的随机摆位把它翻回语义侧——这次 with 恰好摆在 A 位、判官判 A 赢 → winner 存成 `"with"`。所以 `ComparisonResult.winner` 存的就是**赢的那一侧的名字**，报告直接读它，不用自己反推 A/B 映射。

这套机制的核心是一句分工：**评判者（LLM）全程盲，编排者（代码）全程知道**。代码随机决定摆位、记住映射、最后把判官返回的裸 A/B 翻回语义侧；LLM 只看到两份抹掉身份的产出，只管判哪份好。一边用「盲」消掉 LLM 的偏见（不知道谁是谁），一边用「不盲」保住可读的结果（报告里直接是「with 赢了」）——盲而不乱。

每条 `ComparisonResult` 除了 `winner`，还携带这些字段（多数按侧拆，两侧各一份）：

| 字段 | 装什么 |
|------|--------|
| `reasoning` | 判官的判决理由（文字） |
| `rubric` | 两侧各自的打分：`content`（correctness / completeness / accuracy）+ `structure`（organization / formatting / usability）+ 汇总 `content_score` / `structure_score` / `overall_score` |
| `output_quality` | 两侧各自的 `score` + `strengths[]`（优点）+ `weaknesses[]`（缺点） |
| `expectation_results` | 两侧各自的 `passed` / `total` / `pass_rate` + 逐条 `{text, passed}` 明细 |
| `pos_a_label` / `pos_b_label` | 揭盲映射：position A / B 各自对应哪一侧（报告据此在 reasoning 旁标注，否则 reasoning 里的 A/B 和去匿名化的 winner 对不上） |

报告逐条渲染这些——既看到「谁赢」，也看到两侧各自的 rubric 分、优缺点和逐 expectation 通过情况。

### 8.3 comparison_summary 与胜率两版

盲比较聚合成 `comparison_summary`：

```json
"comparison_summary": {
  "total": 10, "a_wins": 6, "b_wins": 2, "ties": 1, "failed": 1,
  "valid": 9, "win_rate": 0.67,
  "win_rate_ci_lo": 0.35, "win_rate_ci_hi": 0.88,
  "label_a": "with", "label_b": "without"
}
```

这里有个容易踩的细节——**胜率有两个版本**：

| 版本 | 公式 | 用在哪 |
|------|------|--------|
| tie-**计入** | `a_wins / valid`（valid = a_wins + b_wins + ties） | `comparison_summary.win_rate` |
| tie-**排除** | `a_wins / (a_wins + b_wins)` | `gate --min-win-rate` |

两者在阈值边界会让门禁判决翻转，所以 gate 自行计算 tie-排除版，不复用 summary 字段。另外 `failed`（调用失败）排除出分母，`ties`（真平局）计入分母。

还有一对字段 `win_rate_ci_lo` / `win_rate_ci_hi` 是胜率的 **Wilson 95% 置信区间**（以 `a_wins / valid` 算）：比得太少时区间会很宽、甚至把 0.5 也框进去——这就意味着「现有样本不足以断定 with 真的更强，建议多比几次」。上面这个例子正是一例：9 次里赢 6 次看着胜率不低，但区间 `[0.35, 0.88]` 含 0.5，严格说还不显著。

### 8.4 什么时候跳过

盲比较只服务主观题——客观题 programmatic 已经把噪声消到 0，没必要再盲评。所以在 `programmatic`-only 评分模式下，盲比较自动跳过。

### 8.5 比较对象可换：v1/v2 回归门禁是杀手级应用

盲比较的关键不是「又比了一次 with/without」——现有评分早就在比 with vs without 的 delta。它的价值是**一个稳定的比较器，而且比较对象可以换**。换比较对象，回答的问题就变：

| 比较对象 | 回答什么 |
|---------|---------|
| `with vs without` | 这个 skill 到底帮不帮 Claude 把事做好（skill 存在的意义） |
| `v1 vs v2`（同一 skill 两个版本） | 这次更新有没有正收益 |

v1/v2 是盲比较的**杀手级应用——CI 回归门禁**：skill 提 PR（v1 = main 旧版、v2 = PR 新版），CI 拉同一份冻结的 eval 基线，分别 spawn with v1 / with v2 各跑 runs N，每条 eval 盲比较两版产出，汇总 v2 胜率——v2 胜率明显高 → 正收益（CI 绿）；v1 胜 → 回归（CI 红）；大量平手 → 改了寂寞（更新没触及实际产出）。

这正是第十章 gate 把盲比较胜率做成可选门禁轴（`--min-win-rate`）的用意：它天然适合「PR 不许回归」的 CI 用法。换 `--skill-b` 指向另一个 skill 路径就切到 v1/v2 模式（见 5.1）。

---

## 第九章 校准：结果×过程

校准（Calibration）是把前两层（评分 + 遵循性）的矛盾**暴露**出来的那一层——它回答「这次执行的**结果**与**过程**协不协调」。

**产出的前提**：校准依赖遵循性（compliance）——有 compliance 才产 calibration。compliance 在 eval 里**默认开**（注入声明即跑；它审的是 cc 已产出的 transcript、不 re-spawn cc，单 run 本就有效），calibration 跟着出（compliance 一有它就能算）。想跳过用 `--no-compliance`（如快速冒烟测试）。**skill 和 agent 条件相同**——都默认产出校准（per-run 分诊、单 run 有效）。

### 9.1 结果×过程 coherence：为什么是这两个轴配一对

为什么要拿「结果」和「过程」这两个轴来配？因为「一次执行可不可信」恰好有这两个**互相独立**的维度：

- **结果轴**——输出对不对（有没有达到通过线）。
- **过程轴**——过程规不规矩（有没有违反硬性规则）。

它们独立到可以任意组合：你可以**输出对了但过程错了**（抄近路、跳过校验、碰巧蒙对——声明的关键步骤没真执行，答案却对了），也可以**输出错了但过程对了**（声明逻辑没问题，只是执行失败）。**只看结果**会漏掉前者（「蒙对答案」看着像满分，却不能归功于声明）——这正是校准要抓的危险情况。所以把这两个轴交叉，专门把「结果对 + 过程错」这一格标出来（`HIGH_SCORE_SUSPECT`），就是校准存在的理由。换别的组合（比如结果 × 工具调用数）就没这层意义。

矩阵是结果轴（对 / 错）× 过程轴（对 / 错 / 未知），折叠成 5 态。它**不是**凑出来的置信度数，而是分诊信号。

**这两个轴怎么「配成一对」——不是逐条对账。** 容易想成「第 N 条期望 ↔ 第 M 条指令」逐条配对，但代码并不这么做（也没有公共键可配：期望来自 eval、规则来自声明，是两份不同文档）。它把结果轴压成一个聚合率（输出达标没），过程轴压成「哪些硬类规则被违反」，两个**聚合信号**在 2×2 矩阵里交叉。所以「结果对了但过程错了」只是观察到**这次执行：输出达标 AND 碰了某条硬规则**这两个事实共存——**因果没建立**（不判断「输出对是*因为*还是*尽管*碰了那条规则」）。这正是那一格叫 `HIGH_SCORE_SUSPECT`（存疑、值得看一眼）而不是「已证矛盾」的原因：它只报「两个面这次没对齐，人来看一下」，不下因果结论。

### 9.2 两轴都押在实测高精度的信号上

为了让判决稳，两轴都尽量避开方差大的信号：

- **结果轴**：优先押**客观** `pass_rate_programmatic ≥ 0.8` → 结果对。`0.8` 与 report 里 pass_rate 的「绿档」徽章同值（`RESULT_PASS_THRESHOLD` 常量，非 CLI 可配），是条经验线——足够严格、又给抖动留了容差。纯主观题（无客观锚）才回退主观率，并在 `scoring_rate_basis=mixed` 标注「基于主观判分」。
- **过程轴**：只看遵循性里 **prohibition（禁令）/ output（输出格式）两类**违规——这两类**实测精度高**（prohibition 是 LLM 主动找违规、判定较硬；output 是确定性子串匹配、零方差），一旦 FAIL 就值得翻判决。有硬类违规 → 过程错；无 → 过程对；compliance 缺失 / 无可判定 → 过程未知。而 step / conditional 判的是「走过没 / 该不该触发」，**实测方差更大**，FAIL 只记进 `soft_notes`、不翻判决（否则一次 LLM 抖动就推着 verdict 摆）。

> 关键：硬 / 软的分界**不是「程序判 vs LLM 判」**（prohibition 其实也是 LLM 判），而是**实测精度**——prohibition 的「主动查违规」和 output 的「子串匹配」都够稳，step / conditional 的「走过没」够噪。这分类来自实测，不是按判定方式硬分。

### 9.3 五态矩阵

| verdict | 结果 | 过程 | 含义 |
|---------|------|------|------|
| `TRUSTED`（可信） | 对 | 对 | 未发现过程问题 |
| `HIGH_SCORE_SUSPECT`（高分存疑） | 对 | 错 | 输出对了但过程违反硬性规则，建议核对（列 evidence） |
| `FIXABLE`（可修复） | 错 | 对 | 输出未达标，过程无明显违规 |
| `SKILL_FAILED`（技能失效） | 错 | 错 | 输出未达标且过程违反硬性规则 |
| `PROCESS_UNKNOWN`（过程未知） | — | 无可对账 | compliance 缺失或无可判定项（如纯推理题） |

`HIGH_SCORE_SUSPECT` 是校准最有价值的一态——它标出「抄近路蒙对答案」的情况（表里的「高分」指结果轴通过了，但过程违反了硬性规则）。

`calibration.json` 字段：`verdict` / `result_pass` / `process_status`（ok/violated/unknown）/ `hard_violations`（每条 category+instruction+evidence）/ `soft_notes` / `scoring_rate` / `scoring_rate_basis` / `scoring_rate_programmatic` / `scoring_rate_llm` / `compliance_rate`。

**跑出来长啥样——同题多 run，verdict 会摆。** 同一道题跑多个 run，verdict 常在两态间摆：有的 run 结果没过 0.8、过程干净 → `FIXABLE`；有的 run 同样没过 0.8，但那次 cc 碰巧犯了某条 prohibition 硬禁令 → 过程 `violated` → `SKILL_FAILED`。差别**不在结果轴**（几个 run 的客观率都摆不大、都判 `result_pass=False`），**在过程轴**——硬违规在不在，verdict 就跟着翻。这就是「校准是 per-run 分诊、不是稳定声明判决」的活例子，也是它不进 gate **decision** 的直接原因（但 HIGH_SCORE_SUSPECT 会作 soft warning 显出）。

### 9.4 为什么不进 gate 的 decision（只作 soft warning）

校准是 **per-run 分诊**，不是稳定的声明级判决——同一道题，verdict 会跨 run 摆（结果轴相对稳，过程轴的硬违规在不在会摆，verdict 跟着摆）。所以校准**不进 gate 的 decision**（门禁的 PASS/FAIL 走稳定的 `delta.pass_rate_programmatic` + 盲评胜率）；但它的 HIGH_SCORE_SUSPECT 会作 **soft warning** 聚合进 `calibration_summary`、显在 gate.json + viewer（分诊辅助人判断，**不改 decision/exit_code**）。只在注入了声明的 side 产（with/without 模式 = with_skill 侧；v1/v2 模式两边都产）。

更根本的原因是**校准成立有个前提，而工具不验证它**。结果轴查的是 eval 关心的、过程轴查的是声明要求的——只有当 eval 测的正是声明该干的活，「结果对 + 过程错」才是真矛盾（该守的硬规矩没守、却侥幸输出对了 = 潜在 bug）。但工具**假设**相关、不去检查「这条硬规则和这个 eval 的成功标准相不相关」。毫不相关时（eval 测 SQL 正确性、声明某硬规则却管「输出编码格式」），cc 输出对了 SQL 又没守格式 → 机械落 `HIGH_SCORE_SUSPECT`，但这只是两个独立事实共存、不是 coherence 矛盾，人看一眼 evidence 就能 dismiss。所以校准只能当**分诊**（让人核 evidence、判断那条硬规则和这次输出到底有没有关系），不能当**定罪**——门禁走的客观 delta + 盲评胜率都不依赖这个「相关性未验证」的弱 coherence，故校准再保守/激进都不会让 CI 误绿误红。

---

## 第十章 判决与展示：gate + report

这一章把「出判决」（gate）和「看结果」（report）合在一起讲——它们都是只消费 eval 产物、不重新跑题的事后处理。`eval`（生产者）和这两个消费者的分工如下：

| | 角色 | 干什么 | 碰 cc 吗 |
|---|---|---|---|
| `eval` | **生产者** | spawn cc 跑题 → 产出 grading / compliance / transcript 等产物 | 费 token、慢、有方差 |
| `gate` | **消费者** | 读 `benchmark.json` → 出 PASS/FAIL 判决 + gate.json | 不碰（毫秒级） |
| `report` | **消费者** | 读 benchmark.json + gate.json + previous/ → 渲染 report.html | 不碰（纯渲染，毫秒级） |

`gate` / `report` 的共性 = **事后（跑在 eval 之后、以 eval 产物为输入）+ 不 spawn cc 跑题 + 可反复跑（改阈值/参数重判不必重跑 cc）**。

### 10.1 gate：事后门禁

`gate` 对已有 benchmark.json 出 PASS/FAIL 判决。它是 **事后纯函数**——不重跑 Claude、毫秒级、可反复改阈值重判。核心卖点是改门禁阈值不用重跑 cc、毫秒级重判。

```bash
sift gate output/benchmark.json                        # 仅客观回归守卫
sift gate output/benchmark.json --min-win-rate 0.5     # 加主观 gate
sift gate output/benchmark.json --no-programmatic-guard --min-win-rate 0.6
sift gate output/benchmark.json --must-pass 3,5,7      # 必过题:candidate 侧这些题任一挂 → FAIL
sift gate output/benchmark.json --baseline freeze/baseline.json --strict-drift  # 题集漂移守卫
```

四轴判决：

| 轴 | 默认 | 判法 |
|----|------|------|
| 客观侧 programmatic 回归守卫 | **常开** | `delta.pass_rate_programmatic < 0` → FAIL；`≥ 0` → PASS；纯主观题（无客观断言 → 该值为 null）→ 此轴不适用 |
| 主观侧 comparison | opt-in（`--min-win-rate`） | tie-排除胜率 ≥ 阈值 → PASS；< 阈值且决定性比较数（= a_wins + b_wins，排除平局和失败，默认 ≥ `--min-valid`=3）→ FAIL；样本不足 → UNCLEAR |
| 必过题 must_pass | opt-in（`--must-pass`） | candidate 侧这些 eval 的**所有 run** 都得全过（`passed==total`），任一挂 → FAIL；未指定 → 此轴不适用。看 candidate 侧（= with skill）：关键题用了 skill 必须成 |
| 题集漂移 drift | opt-in（`--baseline`） | 对比基线 eval id 集合 vs benchmark.runs 的 eval id 集合，增删 → 报告。默认只 warning（不进 decision）；`--strict-drift` → 有增删升 FAIL。防「删掉挂的题拉高 pass rate」 |

### 10.2 AND + exit code

这四轴的判决**一票否决**，最终结果四选一：

- 任一**适用轴** FAIL → **FAIL**（要 PASS，所有适用轴都得过）
- 没 FAIL 但有适用轴**信号不足** → **UNCLEAR**（判不准）
- **根本无适用轴**（守卫全关 / 没请求 / 无信号）→ **N/A**（不适用）
- 否则 → **PASS**

「适用轴」= 开了或有信号的轴；opt-in 的轴（没给 `--min-win-rate` / 没给 `--baseline` 等）不参与否决。

为什么 N/A 也 exit 2 不放行？因为不适用常意味着门禁根本没评上（如配置漏：纯主观题 + 没给 `--min-win-rate` + 关了 programmatic 守卫）。放行会让「没评」伪装成「通过」，有风险。

exit code：

```
0 = PASS
1 = FAIL
2 = N/A（不适用）/ UNCLEAR（判不准）/ 使用错误（文件/解析错误）
```

N/A、UNCLEAR、使用错误都退 2，靠 `gate.json` 是否存在 + 其 `decision` 字段区分。

### 10.3 CI 集成

```bash
sift eval v2/ --skill-b v1/ --evals baseline.json --runs 3 -o out/
sift gate out/benchmark.json --min-win-rate 0.5
echo $?   # 0=PASS(放行) / 1=FAIL(卡合并) / 2=N/A 或 UNCLEAR，消费方定
```

门禁阈值与 CI 落点（分支保护、check 名、N/A/UNCLEAR 是否阻塞）归消费方配置——sift 只提供机制（三态 exit + gate.json）。

### 10.4 gate.json 怎么读

`gate` 跑完写一份 gate.json 到 benchmark.json 同目录（或 `-o` 指定）。它是判决的**完整账本**——CI 靠它分清 exit 2 到底是 N/A 还是 UNCLEAR，人靠它看「为什么这么判」。这是**读者第一次跑最常见的样子**（只给了客观守卫、没给 `--min-win-rate`）：

```json
{
  "decision": "PASS",
  "exit_code": 0,
  "reason": "客观侧无回归(+0.27)",
  "candidate": "with_skill",
  "baseline": "without_skill",
  "thresholds": {"min_win_rate": null, "min_valid": 3, "programmatic_guard": true,
                 "must_pass_ids": null, "baseline_ids": null, "strict_drift": false},
  "axes": {
    "programmatic": {
      "verdict": "PASS",
      "applicable": true,
      "delta": 0.27,
      "reason": "客观侧无回归(+0.27)",
      "win_rate": null, "decisive": null, "a_wins": null, "b_wins": null, "ties": null, "failed_ids": null
    },
    "comparison": {
      "verdict": "N/A",
      "applicable": false,
      "reason": "主观 gate 未请求(未给 --min-win-rate)",
      "delta": null, "win_rate": null, "decisive": null, "a_wins": null, "b_wins": null, "ties": null, "failed_ids": null
    },
    "must_pass": {
      "verdict": "N/A",
      "applicable": false,
      "reason": "必过题未指定(--must-pass)",
      "delta": null, "win_rate": null, "decisive": null, "a_wins": null, "b_wins": null, "ties": null, "failed_ids": null
    }
  },
  "warnings": []
}
```

读懂它抓 **3 个判断点**：

**① `decision`（4 态）vs `exit_code`（3 值）的映射**——§10.2 说过「N/A 和 UNCLEAR 都退 2、靠 decision 区分」，这里收口：

| decision | exit_code |
|---|---|
| PASS | 0 |
| FAIL | 1 |
| N/A（不适用） | 2 |
| UNCLEAR（判不准） | 2 |

CI 脚本只读 exit code 分不清「N/A」和「UNCLEAR」，得读 gate.json 的 `decision` 字段——这正是 §10.2 那句「靠 gate.json 区分」的落点。

**② `axes` 里 `applicable` 是读懂产物的钥匙**——为什么上面 comparison 是 `N/A`、一堆 `null`？因为 `applicable: false`（没给 `--min-win-rate`，主观轴根本没介入）。`applicable=false` 的轴**不参与 AND 合并**（§10.2），verdict 恒 `N/A`、数值字段恒 `null`；`applicable=true` 但样本不足 → `UNCLEAR`。所以看到某轴 `N/A` 先查 `applicable`：`false` = 你没开这轴，`true` = 开了但判不准。reference 里的样本两轴都 `applicable: true` 都 PASS 是理想态；现实里只跑客观守卫时 comparison 几乎总 `N/A`，别被 null 吓到。

**③ `thresholds` 回显**——你传给 `gate` 的三个阈值原样记进 gate.json：可复现（同一 benchmark + 同一组阈值 → 同一判决），改阈值重判 thresholds 跟着变。`min_win_rate: null` = 没给主观阈值。

> 完整字段（含 `warnings` / `artifact` 等）见 [reference](sift-reference.md) 的「gate.json」段。

### 10.5 report HTML 布局

`sift report output/ --open` 生成自包含的 report.html（eval 末尾也会自动产）。它读 `gate.json`（若存在）在标题旁渲染门禁徽章，读 `previous/benchmark.json` 显「vs 上次」卡。两个标签页：

**单次执行 tab**——浏览单个 run 的 prompt、输出文件、评估详情（打分 + 遵循性 + 校准三层）。顶部下拉按 eval 分组，可直接跳到指定 run；方向键逐条翻。

**整体汇总 tab**——看全局，自上而下（门禁徽章在标题栏、两 tab 都见，见本节开头）：

1. **Dashboard 卡片行** — 一排卡：通过率变化 Δ 卡（双率 delta；agent 绝对质量模式换「绝对通过率」三率卡）+ 盲评胜率卡（Wilson CI + 显著性）+ vs 上次卡（纵向 delta，仅 fresh 重跑产 previous/ 时）+ 执行规模卡 + Skill / Agent 名卡 + 模型卡（三角色同源标「同源」）。
2. **异常检测** — `anomaly_report`（超时 / 离群），有异常才显。
3. **失败根因** — 聚合各 run 的 `error_root_cause`（timeout / rate_limited / ...），有失败才显。
4. **静态分析记分卡** — 读 scorecard.json（11 项静态体检）。
5. **安全审查** — 读 vet-report.json。
6. **通过率对比** — 双率（客观 / 主观）图表。
7. **LLM token 用量** — 各 config / eval 的 token。
8. **耗时** — 各 config / eval 耗时。
9. **遵循性聚合** — 整个考试聚合的遵循性（per side 聚合 per-run findings），含**声明覆盖**一行 + 未覆盖折叠清单（实测口径：哪条声明规则至少被一张考卷触发过；covered=任一 run PASS/FAIL、未覆盖=全 N/A，反推指令不计——与 audit 报告 ③b 同一聚合）。
10. **校准摘要** — `HIGH_SCORE_SUSPECT` 高分低遵循警告（5 态判决聚合）。
11. **盲评对比** — `comparison_summary` 胜率 + 逐条比较（rubric / output_quality / expectation_results），揭盲标注 A / B 各是哪侧。
12. **深度分析** — `analyze_comparison` 产的赢家侧 strengths / improvement_suggestions，只在有清晰赢家时显（详见下文）。
13. **检查点总览** — per-eval 的程序率 / LLM 率 / 总率（这三个数即「3-dim」）一览（mean across runs），点行跳明细；flaky 题（跨 run 忽过忽挂）标红旗。
14. **逐项评测明细** — per-run 的 3-dim + token / 耗时列 + 检查项 ✓/✗ 矩阵。
15. **分析备注** — `analyze_benchmark` 产的整体备注（analysis-notes）。

> report 里有两套 `runs` 数据来源容易混：`benchmark.runs`（聚合器产出，compliance 恒 null）和 viewer 自己 `find_runs(workspace)` 扫描的 per-run compliance.json。遵循性聚合面板走的是后者（逐个读 per-run compliance.json 再聚合），所以它能在 benchmark.runs.compliance 为 null 时仍然显示遵循性。

**深度分析（analysis）**——benchmark.json 的 `analysis` 字段来自两个 LLM 分析步骤：

- **analyze_benchmark** → `analysis-notes.json`（整体分析备注）。
- **analyze_comparison** → 深度分析栏（赢家侧的 strengths / improvement_suggestions，只在有清晰赢家时产）。

**赢家怎么定**：winner 取**整体多数票**（所有有效比较里哪侧赢得多，排除 tie/failed；平局或无有效胜负 → 没清晰赢家 → 不产深度分析），代表判词也取整体胜者侧赢的一个比较——不用可能反整体的 `comparisons[0]`（它只是 `(eval_name, run_n)` 字典序第一对，不代表整体）。所以深度分析反映**整体趋势**，不是某一道题。（局限：比较结果不带 eval/run 回指，代表比较的精确 transcript 取不到，分析用的是 config 第一个 run 的 transcript。）

深度分析栏只在 `analysis && (winner_strengths || improvement_suggestions)` 时渲染——所以它出现需要两个条件：① 盲比较有清晰赢家（tie/空不算）；② 那次 analyze_comparison LLM 调用成功。没出现不一定是 bug，可能是 tie 或 LLM 步骤没成功。


---

# 第二部 审计 —— skill / agent / 编排者的执行

审计回答「过程」问题：一份声明（skill / agent / 编排者）执行时有没有真照声明走，而不只看输出对不对。它是 sift 的原创层，也是和 skill-creator 这类工具最大的差异点。

审计是**声明驱动**的——判定的依据永远是声明（SKILL.md / agent 定义 / 编排者声明），transcript 只提供场景和事实，两者交叉才有可靠判断。引擎跨评测和审计共享：eval 内置的过程对账（compliance）和独立的 `audit` 命令用的是同一套引擎，评测在每次执行后顺手查一遍过程，audit 则把这套对账独立深化、做成能审任意会话（含历史会话、真任务 trace、opencode session）的产品。

这一部按「怎么判 → 能信多少 → 审谁」三步展开：第十一章讲判定机制（声明驱动、指令提取、五态判决、eval 内 compliance 与 audit 共享引擎），第十二章讲可信度与噪声（噪声地图、refine、宁少报、noise_label、流水线、HTTP 并发），第十三章讲作用域与 transcript（审 skill / agent / 编排者、cc/opencode 双源、transcript 重建、turn 定位）。

---

## 第十一章 审计的判定机制

### 11.1 为什么需要审计

输出对了 ≠ 过程对了。模型可能「抄近路」蒙对答案——比如声明要求「运行 validate.py 校验」，模型跳过校验直接给了个恰好正确的输出。单看评分，这是满分；但这次满分不能归功于声明，因为声明的关键步骤被跳过了。审计就是把「有没有真按指令做」单独测出来。

### 11.2 6 类指令 → 4 个语义类别

审计判定的**出题依据是声明**（skill 的 SKILL.md / agent 定义 / 编排者声明，不是 eval）。`InstructionExtractor` 从声明正文静态抽出 6 类可检查指令：

- `HEADING_STEP`（应走某步骤）
- `TOOL_REQUIREMENT`（该用某工具/运行某脚本）
- `REFERENCE_USAGE`（该读某参考）
- `CONSTRAINT`（约束/禁令）
- `ANTI_FABRICATION`（反捏造规则）
- `OUTPUT_FORMAT`（输出格式要求）

然后按**语义**归到 4 个类别，每类配方向正确的判法：

| 类别 | 来自哪些指令 | 怎么判 |
|------|------------|--------|
| 条件性 CONDITIONAL | TOOL_REQUIREMENT / REFERENCE_USAGE | 命中 tool_trace（**含参数级断言**——声明点名的 CLI 参数也得对）→ PASS（**程序判定**）；未命中 → LLM 判该不该触发 |
| 禁令 PROHIBITION | CONSTRAINT / ANTI_FABRICATION | LLM **主动检测这次有没有违规**（不是判「遵守没」） |
| 步骤 STEP | HEADING_STEP | LLM 判走过 / 该走没走 / 不涉及 |
| 输出格式 OUTPUT | OUTPUT_FORMAT | **确定性子串匹配**（程序判定，不调 LLM） |

> **为什么 audit 用正则抽指令，评测却用 LLM 出题**：两者源头都是声明，但产物不同——extractor 抽的是声明里**已有的指令**（步骤 / 禁令 / 工具要求，逐条对账用），init 生成的是**新评测题**（input + expectations，测能力用）。审计要确定性、可重复（同一份声明每次抽出同样的指令，对账才稳），所以用正则静态抽；评测要语义和创造性，所以 init 用 LLM 生成。正则的代价是抽不准——教程型声明的示例文字会被误抽成指令，后面「地基瓶颈」展开。

> **条件性的「程序判定」现在看参数**：TOOL_REQUIREMENT 命中工具名只是第一步——声明点名的 CLI 参数（如要求跑 `pytest --tb=short`，参数就是 `--tb=short`）也得出现在 agent 实际命令里，才算程序判定 PASS。漏参数不算 PASS，**也不直接 FAIL**，而是走 LLM 判「这场景到底该不该要这个参数」。所以你会看到「agent 明明跑了 pytest、条件性却没给程序 PASS」——多半是它漏了声明点名的参数，交给 LLM 定夺了。匹配口径：工具名、路径、CLI 参数都**大小写不敏感**（双侧 lower）；路径另做规范化比对（解相对/绝对差异 + 分隔符边界），参数走子串匹配（粗召回宁松，`--tb=short` 整串、`tests/` 这类 token 都算命中）。参数上哪找也有讲究：agent 的命令常不是自己敲的，而是写在派给子 agent 的任务描述里——200 字的调用摘要常把这些命令截掉，所以「工具/参数出现过吗」这道检查除了摘要、还搜这条调用的**完整 input**，不然明明派发了却搜不到。另一头，声明写 `docs/{OP}/` 这种**模板占位符的参数不参与比对**——trace 里记的是真实值（如 `docs/conv/`），拿模板对实值必然比不过。抽不到参数的指令（指令里没点名具体参数）行为不变。

> **输出格式（OUTPUT_FORMAT）怎么知道匹配什么**：extractor 从声明抽出输出格式要求（比如规定产物要含某个标题 / 标记 / 模板），audit 拿它去和实际输出做**子串匹配**——命中 → PASS，未命中 → FAIL（这类没有 N/A / 没判成 / 判不准，因为是纯字符串判定）。所以声明里把输出格式写得越具体（点名某个 marker），越容易被抽到、查得越准。

> 禁令的判法是**反着来**的：其它三类问「该不该做/做没做」（正面遵循），禁令问「这次执行里有没有发生**违反**这条禁令的行为」——主动找违规。例如 `NEVER fabricate error codes`，模型 hallucinate 一个错误码 → `violated=true` → FAIL。

> **判官看什么 + 两处结构性噪声的防呆**：三个 LLM 判官各看不同的执行上下文——条件性看「场景」（`digest.user_turns`，任务上下文，判指令该不该触发）、禁令 / 步骤看「执行记录」（`transcript_text`，assistant 文字 + 工具调用渲染）。两处防呆治实测的两条主噪声：① **条件性场景回退**——`--kind skill/agent` 切出来的段只有执行 turn、没有 user turn（任务在父作用域），条件性判官原拿空场景只能一律弃判 INDETERMINATE（实测占 22%–55%）；现空场景时回退用 `transcript_text`（执行本身揭示任务上下文）当场景，判官方能判 applicability 而非弃判。② **头 + 尾窗口**——执行记录远超 prompt 预算时（实测单段 60–150KB），旧裸 `[:8000]` 只留头、尾部（最终结果 / 总结 / 收尾）判官全盲 → 禁令类「未见收口」假 PASS 或弃判；现取头 + 尾各半 + 中段截断标记，token 成本不变、覆盖从单端变双端。步骤类另有完整 `tool_trace`（不截断）兜底，所以「有没有调某工具」不受截断影响。

> **extractor 的地基瓶颈**：声明正文是给人读的自由文本——讲解、示例、真步骤混在一起，没有结构标记区分「这行是指令」还是「这行是示例」。extractor 抽指令只能靠正则死规则（标题下、大写开头 ≈ 步骤），于是教程型声明里的示例文字（如 `3 agents dispatched in parallel`、`6 failures across 3 files`）会被误抽成「指令」喂给判官 → 卷子大半是废题 → 判官再准也只能大量判 N/A（伪指令判不准）。这就是为什么审计常出很多 N/A、为什么教程型声明特别难审（评测内 compliance 和独立 audit 共用的地基问题）。根治两路：① **结构化声明**——声明作者在 frontmatter 声明 `required_tools`/`required_reads`/`required_outputs`，extractor 直接查声明、不挖散文（最稳，但要声明作者配合改格式，待做）；② **refine**——正则粗召回后用 LLM 精筛伪指令（工具侧改进，不改声明格式），**默认开**，详见 §12.2。

### 11.3 五态 verdict + method 双维度

每条 finding 有两个**正交**维度：

**verdict（结果，五态）**：

| 态 | 含义 | 重跑有用吗 |
|----|------|-----------|
| `PASS` | 已判明通过 | — |
| `FAIL` | 违规 | — |
| `N/A`（不适用） | 规则存在但本次场景不触发 | 不用 |
| `UNRESOLVED`（没判成） | LLM 调用/解析失败/未提及/畸形值 | **可重跑救回** |
| `INDETERMINATE`（判不准） | 证据真模糊（矛盾/缺失） | 重跑无益 |

**method（怎么查的）**：`PROGRAMMATIC`（tool_trace 命中 / 子串匹配，不调 LLM）或 `LLM`（语义判定）。

遵循度 = `pass / (pass + fail)`，排除 N/A + UNRESOLVED + INDETERMINATE（三者都不进分母）。

这三态必须分清，是看审计结果最容易混的地方：

- **UNRESOLVED（没判成）** = LLM 没出判定（操作性失败）→ 可重跑、汇总进「建议重跑」警告；
- **INDETERMINATE（判不准）** = LLM 出了判定但本身是「证据模糊判不了」（语义失败）→ 重跑无益，必须带具体 reason；
- **N/A（不适用）** = 规则存在但本次场景不触发（如声明写「读 references/foo.md」，但本次任务根本不涉及该条件）→ 已解决的有效结论，不进警告。

> **N/A 还有一个来源：记录窗口外**（步骤/条件/数量类都会用到）。给的执行记录是有尽头的——记录止于某处、其后排着的计划任务从未开始，这些指令不是「该走没走」，是「证据根本不覆盖」，判 N/A。两条边界别越：任务/阶段**开了头**、中途漏掉的步骤仍是 FAIL，不得借这条洗成 N/A；记录**中间**的空洞——工具记录从某条直接跳到后面、中间没有这一步的任何痕迹——就是没走，判官不得猜「中间的调用没抄进来、应该做了」。只有记录**末尾之后**的步骤，才轮得到窗口 N/A。

> 注意：审计这套**遵循性五态**（`PASS`/`FAIL`/`N/A`/`UNRESOLVED`/`INDETERMINATE`）和评分侧的**评分五态**（`PASS`/`FAIL`/`UNEVAL`/`TIMEOUT`/`ERROR`，见 §7.4）是两套不同的状态集，别混。

每条 `AuditFinding`（两个正交维度 `verdict` × `method`）的字段：

| 字段 | 含义 |
|------|------|
| `instruction_text` | 指令原文 |
| `source` | 规则类型（`InstructionSource`：CONSTRAINT / HEADING_STEP / TOOL_REQUIREMENT / REFERENCE_USAGE / ANTI_FABRICATION / OUTPUT_FORMAT） |
| `section` | 声明分组标题（`source_section` 回填的真实标题 or 伪节名兜底），覆盖视图分组用、判定不读；缺省空（旧产物） |
| `verdict` | 五态：`PASS` / `FAIL` / `N/A`（不适用）/ `UNRESOLVED`（没判成，可重跑）/ `INDETERMINATE`（判不准，重跑无益） |
| `method` | `PROGRAMMATIC`（程序对账）/ `LLM`（语义判定）——「怎么查的」，与 verdict 正交 |
| `category` | 语义类：`conditional` / `prohibition` / `step` / `output` |
| `evidence` | 判定理由（INDETERMINATE 时即判不准的理由） |
| `transcript` | 对应 transcript 文件名 |
| `derived_from_transcript` | 是否反推声明来的（见 §13.6） |
| `synthetic` | 这条 finding 不是声明里抽出来的原文指令、是 audit 自己**拼出来的关系描述**（ORDER 的 `(顺序)…` 文案，见 §11.5）时为 True——不算真实指令，不进覆盖视图 / by_instruction 汇总，也不参与噪声判别；旧报告恒 False |
| `control_flow` | 控制流判官标记（纯显示）：`quantity` = 数量指令移交控制流判官判（程序计数或 LLM 数量判定），CLI/HTML 据此挂「数量」记号；判定/聚合都不读；缺省空（旧产物） |
| `seq` | `#N` 序号（去重后编排，供 `related` 引用 + 报告定位） |
| `sources[]` | 同文去重合并的全部来源（单条 = `[source]`） |
| `related[]` | 同行为跨类关联的其它 finding 序号（如 `["#9"]`） |
| `conflict_with[]` | 同源矛盾对端序号：同一声明行（同 `source_excerpt`）被判出一 PASS 一 FAIL 时互标（如 `["#10"]`），HTML 挂「⊘ 判定矛盾」徽章、warnings 报组数；标注不仲裁——谁对谁错人工看两端 evidence；无矛盾留空 |

### 11.4 eval 内的遵循性 = audit 命令

重要：**eval 里的遵循性判定和独立的 `audit` 命令是同一套引擎**。eval 直接调它喂单条 transcript.jsonl，产出的 `compliance.json` 就是 AuditReport，schema 和 audit 命令的 `audit-report.json` 一致。所以第十一章–第十三章讲的判法对 eval 内 compliance 和独立 audit 都成立——eval 是「执行后顺手查一遍」，audit 是「把这套对账独立深化、能审任意会话」。

### 11.5 控制流判官：顺序（ORDER）+ 数量（QUANTITY）

条件性、禁令、步骤这三个判官（第四类指令「输出格式」不走 LLM，是程序比对）都是**逐条**判「这一条做了没」。有两件事这么判天生覆盖不到，因为看的不是单条指令、是**指令之间的关系**：一是**顺序**——声明写「先备份再迁移」，执行却先迁移后备份，每一步单看都做了、顺序是反的；二是**数量**——声明说「每个错误都要引用原文」，3 个错误只引了 2 个。于是加了第 4 个 LLM 判官（**控制流判官**）补这两块。注意：它**不新增指令类别**——声明抽出来的指令还是条件性 / 禁令 / 步骤 / 输出那几类，变的只是谁来判：ORDER 把两条相邻的步骤**配成对**、审它们之间的先后关系；QUANTITY 把原有指令里带数量词的那些**从原判官手里接过来**判（指令本身一字不变）。声明里没有这类信号时它根本不出场（零 LLM 调用，行为跟没有这个功能一样）：

- **顺序（ORDER）**：同一个小节里**紧挨着**的两条步骤，文字里带**明说的**顺序词（先 / 然后 / 再 / 接着 / 随后 / 之后 / 其次 / 下一步 / 最后……；「优先」「预先」「不再」这类不算）才当成一对「A 必须在 B 前面」。配对是程序做的，判给 LLM（看执行记录 + 工具调用的先后）。判得保守：只有 B 的证据**全部**都在 A 之前才算违序——A、B 交错着做算过；A 或 B 压根没做算 N/A。**步骤写成 1. 2. 3. 编号列表不算顺序声明**——编号只是排版习惯，倒着做常常也无害，把编号当顺序会大量冤枉人。
- **数量（QUANTITY）**：指令里带数量词的（「每个 / 所有 / 逐个」是一类，逐项检查；「至少 N 次 / 不超过 N 次」是一类，带具体数字）不再交给原来的判官，改由控制流判官一起判——每条指令照样只有一个结论，不会两个判官各判各的打起来。判的口径：该做的没做、数量不够，一律 FAIL；N/A 只留给「没有可数的东西」的场景（比如整场就没发现错误，谈不上「每个错误都引用」）。三条细则：

  - **没开始的算 N/A，不算漏做**：所属任务/阶段在记录里从未开始（记录止于某处、它排在后面），或「据 X 的失败报告修复」这类**前置发生了才轮得到做的活儿、前置从头到尾没发生**——都判 N/A。「整体没做完」这件事由完成度条目兜底报出（见下一条），逐条再 FAIL 是重复报。但阶段开了头、中途漏掉这条的躲不掉，仍 FAIL。
  - **完成度以这份记录为全集**：「逐一完成 / 全部完成其中的 Tasks/项」这类条目，记录结束时没做完就是数量不足——判官不得猜「记录之外可能还会继续」（对账对象就是给的这份记录，它是不是切片不是判官该猜的）；「按顺序」与「完成全部」同现时，完成度是主判据。
  - **少数情况走捷径**：带具体次数、又写了工具名的，由**程序**直接数 tool_trace 里的调用次数（不走 LLM，evidence 里给实测数字）。

报告里怎么认它们：ORDER 的 finding 不是声明里的原文，是 audit **自己拼出来**的一句关系描述（`(顺序)「A」须先于「B」`，带 `synthetic` 标记）——它不是真实指令，所以不进覆盖视图、不进 by_instruction 汇总，也不参与噪声判别；它的 `related` 指向 A、B 两条真实 finding，点得过去。数量 finding 正相反，用的就是原指令原文，聚合一切照常；唯一差别是带 `control_flow="quantity"` 纯显示标记——CLI 违规表在指令前显 `(数量)`、HTML 挂「数量」徽章，不然它长得跟普通 conditional/step 判定一模一样，在报告里认不出来。turn 定位跟别的判官一样受 `--turn-refs` 开关控制（§13.7）；判官调用失败也不会让 audit 崩——相关条目标 UNRESOLVED，报告照常出。

---

## 第十二章 审计的可信度与噪声

审计是诊断工具——它的结果不像评测那样能靠「消噪」磨出干净数字，而是靠「诚实标注哪些不可信」让你判断能信多少。

### 12.1 audit 的噪声地图（先看全貌，逐条展开见下）

下表把噪声来源和各自怎么治 / 诊断一眼列全，后面各节逐条展开。

| 噪声来源 | 长什么样 | 怎么治 / 诊断 | 详见 |
|---|---|---|---|
| extractor 误抽（地基瓶颈） | 教程型声明里的示例 / 占位文字被当成指令 → 大量 N/A | `refine`（判定前 LLM 砍伪指令，默认开）+ 结构化声明（待做） | §11.2 末 / §12.2 |
| LLM 判官判定抖 | 同一条指令这次 PASS、下次判不出或判反 | 五态诚实标注（`UNRESOLVED` 可重跑、`INDETERMINATE` 带证据）+ 宁少报（没判成 → `UNRESOLVED`，不保守 FAIL） | §11.3 / §12.4 |
| 头尾窗口盲区 | 执行记录太长，旧的只留头部 → 尾部收口被漏看 → 假 PASS | 头 + 尾各留一半 + 中段截断标记 + 完整 `tool_trace` 兜底 | §11.2 末 |
| 条件性场景空 | `--kind` 切出的段没有 user turn → 条件性指令一律弃判 | 空场景时回退用 `transcript_text` 当场景 | §11.2 末 |
| 工具噪声混进 FAIL | FAIL 里混着工具的洞和真声明问题 | `noise_label` 逐条打标（tool_noise / skill_issue / uncertain） | §12.5 |
| `--turn-refs` 条件分支过严 | 开 `--turn-refs` 后，本该跳过的条件分支被误判 FAIL | 默认关，要定位时再 opt-in 开 | §13.7 |

一个根本差异：**评测的三层噪声是「消」**——programmatic 归零、盲比较抵消、`--runs` 聚合，每层都有手段把噪声磨掉；**审计的噪声大多是「标」**——磨不掉，只能诚实标出哪些不可信（`UNRESOLVED` / `INDETERMINATE` / `noise_label`），告诉你哪些结果能信、哪些要重跑或人工复核。所以读 audit 结果，看的是**可信度标注**，不是数字本身——这也是 §12.4 那句「0 FAIL ≠ 全过」的由来。

### 12.2 refine：extractor 粗召回后的 LLM 精筛

§11.2 末尾提到 extractor 的地基瓶颈——它靠正则死规则从自由文本里抽指令，保召回但不分「真指令」还是「示例/占位」，于是教程型声明里的示例文字会被误抽成指令喂给判官，判官再准也只能大量判 N/A。refine 就是治这个的工具侧方案（不改声明格式）。

**两段式**：extractor 正则**粗召回**（宁可多抽、不漏真指令）→ refine 在判定前用 LLM 逐条**精筛**「这是真实可检查的指令，还是示例/占位文字」，只砍明确判为伪指令的。

**精度 > 召回**（关键哲学）：refine 只砍 LLM **明确**判 `is_real=False` 的；没判成 / 判不准 / 调用失败一律**保留**——宁可留伪指令让下游判 N/A，也绝不砍真指令。误砍一条真指令 = 漏审一条规则（不可逆），误留一条伪指令 = 多一条 N/A（可容忍）。实测误砍率 0（4 个结构差异声明 × 139 条指令，砍的是表头/占位/资源描述，留的是步骤/禁令/约束）。

**默认开**：`--refine-instructions` 默认开（`--no-refine-instructions` 关），`audit` / `eval` 都支持。砍掉的条数和来源写进 report 顶部 warnings（如 `refine: 滤除 9 条疑似示例/占位伪指令(HEADING_STEP 7, …)`）。`OUTPUT_FORMAT` 不精筛（纯程序子串匹配，精筛无收益）。

**per-声明 hash 缓存**：refine 只看声明指令、不看 transcript，所以同一份指令只算一次。缓存键 = 指令内容（每条的来源/标题/文本）+ model + prompt 版本的 hash，落盘 `~/.cache/sift/refine/`，跨 audit() 调用、跨进程复用。这解决了 runner 每 eval×config×run 调一次 audit、同一份声明反复 refine 实测 10+ 分钟的问题——首次算完落盘，后续命中秒回。指令内容 / model / prompt 版本任一变 → 键变 → 自动失效重算；删 `~/.cache/sift/refine/` 可强制清。

**边界**：extractor 本身冻结在固定版本、纯正则不调 LLM（保证可复现、不把 LLM 方差引入抽取层）；refine 是 audit 层加的独立步骤，不破坏 extractor 的 layering。另一条根治路——结构化声明（frontmatter 声明 `required_tools`/`required_reads`/`required_outputs`，extractor 直接查声明）——更稳但仍待做（要声明作者配合改格式）；在那之前，refine 是默认兜底。

### 12.3 headline：指令显示的人话标题（判定不读）

§11.2 的 extractor 地基瓶颈还有个显示副作用：按 markdown 结构切指令时，谓语常被切到独立片段、主语留在列表项首行（如「必须存在」丢了 `design_doc_cases.json`），读起来晦涩。但判定吃 instruction_text + 整段 scene、不依赖主语——**主语丢失只伤显示可读性、不伤判定**，所以治显示不必动判定。

两步纯显示增强（judge 始终吃 instruction_text 不变）：

- **`source_excerpt`**：extract 时记下指令所属 bullet（markdown 列表项 + 续行）的完整原文，带回被切的主语。确定性正则（`_bullet_excerpt` 回溯列表项首行），无 bullet 时回退所在行。
- **`--headline`**（默认开，`--no-headline` 关）：refine 之后、判定之前的**独立 LLM 步骤**，把 instruction_text + source_excerpt 去噪 + 补主语 + 压成一句人话标题。**关键：没塞进 refine**——refine 判 is_real 决定哪些指令进 judge，把 headline 塞进去会让 is_real 漂移→滤指令集合变→判定间接变；做成独立步骤（自己的 prompt + 缓存）才真正做到判定零影响。
- **`source_section`**：body 级 extractor（约束/防编造/参考文件，跨章节扫描）在 match 偏移处回填的**真实 markdown 标题**——它们的 `section` 字段是伪节名（"Constraint" 等）且**保持不动**（伪节名在 refine/headline 缓存键里，改值全量失效缓存），真实归属由这个纯增字段带回：`finding.section = source_section or section`，覆盖视图按它分组。判定与缓存键都不读它；旧 refine 缓存条目缺此字段 → 回退伪节名，SKILL.md 变更后自然失效重算。

显示回退顺序 `headline > source_excerpt > instruction_text`：headline 优先、source_excerpt 兜底、最后才是无主语谓语；audit-report.html 都保留 instruction_text 作「判定锚点」折叠核对（judge 实际对账的就是它）。per-声明 hash 缓存（`~/.cache/sift/headline/`，键含 source_excerpt——extract 改了 bullet 回溯会失效重算），跨 audit() 复用。

### 12.4 「宁可少报不乱报」

审计是诊断工具，**精度 > 召回**——假 FAIL 一多，声明作者就不信这工具了。所以：

- **FAIL 只在 LLM 明确判「违规/该做没做」时报**；
- LLM 没判成（超时/异常/解析失败/未提及/畸形值）→ 报 UNRESOLVED，**不保守 FAIL**；
- 真模糊 → 报 INDETERMINATE（必须带具体矛盾/缺失证据，空 reason 降级回 UNRESOLVED）；
- audit 汇总 UNRESOLVED 条数 > 0 时在报告顶部出「建议重跑」警告。

**每条 UNRESOLVED 带根因**（`unresolved_cause`，纯诊断）：`timeout`/`rate_limited`/`parse_failed`/… = **基础设施失败**（重跑常能救回）；`unmentioned` = transcript 未覆盖 / LLM 未提及（**重跑可能无益**）；`judge_error` = 判定器自身异常。终端 + HTML（「没判成根因」块）显根因分布，顶层 `unresolved_breakdown` 聚合——把笼统的「建议重跑」细化成「该重跑 vs 重跑无益」。

记住一句话：**「0 FAIL ≠ 全过」**——要看警告里 UNRESOLVED 是不是 0。没判成的条可能藏着真违规。

### 12.5 逐条判别：某条 FAIL 是工具噪声还是声明问题

fixture 下审计的 FAIL 混着工具噪声和真声明问题。报告现在给每条 FAIL **自动标**一个启发式判别（per-run findings、整体汇总 by_instruction、单次 audit-report 的违规表都带徽章 + hover 理由）：

| `noise_label` | 含义 | 典型信号（命中即标）|
|---|---|---|
| `tool_noise`（疑似工具噪声）| 改声明治不了，根因在工具 | `source=HEADING_STEP` 且指令像资源链接（资源描述被当步骤）；evidence 自相矛盾（称「不涉及」却判 FAIL）；LLM 判定带跨类关联（`related` 非空）；聚合 NA 多 FAIL 少（条件性资源被当无条件步骤）|
| `skill_issue`（疑似声明问题）| 大概率真声明没做到 | 硬约束（`CONSTRAINT`/`ANTI_FABRICATION`）+ 程序判定 FAIL（确定、可复现）|
| `uncertain`（待确认）| 单条信号不足 | 其余 FAIL —— 看汇总分布或人工复核 evidence |

**怎么用**：看到 FAIL 先看徽章 + 理由（hover）。`tool_noise` → 别急着改声明，先想 extractor/判定/evidence 是不是工具的洞（典型：声明「详细资源」段的链接描述被抽成步骤）；`skill_issue` → 这才是改声明的信号；`uncertain` → 人工看 evidence。优先级 `tool_noise > skill_issue > uncertain`（取最严：有一条噪声信号就归 tool_noise，**避免据噪声误改声明**）。

**诚实说**：这是**启发式 + 疑似 + 理由，不是权威判决**（可能假阳/假阴）。它**治标**（帮解读现有噪声），没让审计更准 —— 治本仍 `--refine-instructions`（判定前 LLM 砍伪指令，默认开，§12.2）/ 结构化声明（frontmatter 声明 required_*）/ 真实 transcript audit。**别以为「标了 tool_noise = 审计可信了」**。打标本身也不调 LLM——纯程序按每条 finding 上已有的信号（来源 / 类别 / evidence 特征）算出来的（源码 `audit/noise.py`，不重新读 transcript），不花 token、跑多少遍结果都一样，别担心判别自己又添一层抖动。

### 12.6 声明覆盖视图（coverage）：这批会话碰到了声明的多少

audit 的判决只对「碰到的指令」负责；覆盖视图退一步看全貌：**声明的全部指令里，这批会话真实碰到了多少**。报告（audit-report 的 coverage 区块 + CLI 摘要一行）按声明的章节分组数给你：每组指令多少条、覆盖几条、没触达几条。它回答「skill 内容被实际用到什么程度」这个问题，是独立于违规判定的诊断面，不参与判罚。

每条指令三态：

| 状态 | 意思 |
|---|---|
| covered（触达）| 任一 transcript 判过 PASS 或 FAIL——会话碰到了这条规则，不管过没过 |
| uncovered（未触达）| 全部 N/A——规则在、场景没触发，这批会话压根没锻炼到它 |
| unknown（未知）| 只剩 UNRESOLVED / INDETERMINATE——先治「建议重跑」（§12.4） |

`coverage_rate = covered / total`（unknown 计入分母——没判成的不装成已覆盖，诚实低估）；从 transcript 反推出来的指令（derived）不是声明原文，不进分母。

**怎么读**：低覆盖 ≠ 声明差——条件式指令（「遇到 X 才做 Y」）场景没触发很正常；它真正告诉你的是，上面的 PASS / FAIL 结论只覆盖了触达的那部分，没触达的指令这批会话没说话。uncovered 集中在某个分组，就是这个声明面没被这批会话锻炼到（fixture 下常源于 extractor 误抽，见 §12.2——先想工具的洞，再想声明）；想验证就补跑对口场景的 transcript 再对账。

**独立取用**：coverage 的汇总在 audit-report.json 里是独立字段（`coverage`，自带 sections 分组），机器直接读这一块就能回答覆盖情况，不用解析 findings、不用管违规明细。比如攒一批真实会话定期跑 audit、只取 coverage：长期没被触达的声明段，就是「写了但没人用」的清理候选——将来 CI 里的周期性 skill 清理任务接这里就够了。

### 12.7 工具对账视图（boundary）：会话动的工具，声明里提没提

工具对账视图回答一个反方向的问题：**会话里真调过的领域工具，声明里提没提**。报告（audit-report 的 boundary 区块 + CLI 摘要一行）把会话里出现过的领域工具一个个列出来：声明里写了的标「已声明」（domain，附声明出处），没写的标「未声明」（undeclared-domain）。read / bash / grep 这类 agent 自带的内置原语（builtin）不进表——列了全是噪声。

它和 §12.6 的覆盖视图正好一正一反（声明→执行 / 执行→声明），但各答各的问题、各自独立成块，数据也各自独立取用。和覆盖视图一样**只陈列、不定罪**：不产生 finding、不影响判定。因为「用了声明没提的工具」不等于违规——skill 的声明一般不会把工具列全——积累够基线之前就判罚一定乱判。它的用处：一眼看出这次会话动了哪些声明之外的 MCP / 领域工具；表里的会话数 / 调用数列就是在攒 per-skill 行为基线数据，将来基线够了才升级成判定。

声明里认「已声明」看四个地方：工具要求指令的 `tool_name` 参数 / 指令文本里的 `mcp__server__tool` 全名 / 反引号代码字 / **声明 frontmatter 的工具白名单**（agent 的 `tools:`、skill 的 `allowed-tools:`——agent 常把工具只写在 frontmatter、正文一个字不提，不读它会把声明过的工具全标成「未声明」；CLI 和 run 内 compliance 都会自动从声明文件取好，报告「声明出处」列显 `frontmatter: tools`;带权限限定的写法 `Tool(…)` 也认——取括号前的裸名比对，trace 里记的就是裸名）。这块也不像 refine / 判官那样调 LLM——纯程序数出来的（源码 `audit/boundary.py`）：数工具调用、跟声明文本对表都是死规则，不花 token、跑多少遍结果都一样，将来拿它攒行为基线，基线本身也不会抖。

### 12.8 这一层的边界：fixture 下边际价值低，真实 transcript 下价值高

诚实说，审计在不同来源下价值差别很大：

- **评测内 compliance（fixture）**：边际价值偏低。fixture transcript 是按剧本 spawn 出来的，模型的行为高度受 sift 的 prompt 框定；这里 compliance 查出来的「违规」，有相当一部分根因不在声明而在工具自身——典型就是上面讲的 extractor 把示例文字误当指令，导致大量 N/A。换句话说，fixture 下 compliance 更多暴露「工具的洞」，而非「声明的洞」。要「找声明问题 → 改声明」，主信号是 **scoring（A/B delta）+ 静态分析（analyze/vet）**，不是 compliance。
- **独立 audit（真实 transcript）**：价值高。真实会话里的违规是**诊断资产**——它如实反映声明在实际使用中被遵循成什么样，反过来暴露声明指令设计清不清晰、容不容易被遵循。这里的违规不是噪声，正是你想看的。

所以：做 fixture A/B 评测，别对 compliance 的数字过度解读（尤其 N/A 多时，先想 extractor 误抽）；要评估声明的真实遵循质量，拿真实会话跑 audit。

### 12.9 audit 的阶段流水线（对应报告的「阶段耗时 / token」表）

audit 跑一次声明 × transcript 对账，内部是一条 **6 阶段顺序流水线**（各阶段非重叠，秒数加总 ≈ 总墙钟）。报告里的「阶段耗时 / token」表就是这 6 行（末尾多一行 `合计` = 各阶段 token/秒数之和，**不是阶段**）。每阶段：

| 阶段 | 类型 | 干什么 |
|---|---|---|
| 抽取（extract） | 程序 | 从声明静态正则抽全部候选指令（§11.2 说的地基瓶颈在这） |
| 精筛（refine） | LLM | 判定前用 LLM 砍掉 extractor 误抽的示例/占位伪指令，精度 > 召回、per-声明缓存（§12.2） |
| 标题（headline） | LLM | 把指令改写成人话标题，**仅显示、判定不读**，per-声明缓存（§12.3） |
| 切片（rebuild） | 程序 | 把 transcript 重建为 SessionDigest + 按作用域（`--kind`）切执行段（§13.8） |
| 判定（judge） | LLM | 三类判官（条件性 / 禁令 / 步骤）逐批调 LLM 判指令遵循性；声明里有顺序对 / 数量指令时，控制流判官跟着一起跑（§11.5） |
| 聚合（postprocess） | 程序 | **判定后的汇总**：跨 transcript 按指令聚合 findings（`by_instruction` 的 fail%）+ 噪声判别标注 + 声明覆盖视图（`coverage`：按声明分组统计指令被触达多少，§12.6）+ 工具对账视图（`boundary`：会话里调过的领域工具 × 声明，只陈列不判罚，§12.7） |

要点：

- **只有 LLM 阶段（精筛 / 标题 / 判定）有 token + calls**；程序阶段（抽取 / 切片 / 聚合）恒 0——表中看到 0 不是漏跑，是本地计算阶段。
- **判定是性能主战场**：三类判官并发跑（全局在飞 LLM 调用由 AIMD 自适应限制器动态调，适配不同后端容量；`--max-concurrent` 固定向后兼容），墙钟主要花在这；精筛 / 标题有 per-声明缓存（命中秒回），只有首次才花时间。
- **聚合（postprocess）就是「判定之后那个汇总阶段」**：它不调 LLM（程序阶段），所以 token=0、秒数极小，但产出 `by_instruction` 跨会话聚合表 + 每条 FAIL 的噪声判别徽章——别把它和表末的 `合计` 行混（`合计` 是算出来的和、`聚合` 是真阶段）。

### 12.10 这些 LLM 调用怎么发出去：HTTP 直连 + 自适应并发

§12.9 表里标「LLM」的三个阶段（精筛 / 标题 / 判定）一次 audit 要调很多次模型（十几次起）。这节讲这十几次调用**怎么发出去**——它直接决定 audit 跑一遍要多久。

**以前慢在哪：每次调用都「开一次机」。** 早先每调一次 LLM 就启动一个 claude 命令行子进程，等它跑完再读结果。这好比每问一句话都得重新开机：光启动就好几秒，进程内部还要多轮对话才吐出结构化结果，单次就要十几到三十秒。十几次叠一起，跑一遍十几分钟不奇怪。

**现在：HTTP 直连，「开着机一直问」。** 改成直接发 HTTP 请求给模型接口，不再每次起进程。省掉了开机开销，而且一次就要回结构化结果（不用多轮来回），单次调用落到几秒。

**并发：能同时发，但「发几个」不好定。** 这十几次调用彼此独立，本可以同时发、不用排队。但同时发太多会被服务端限流挡掉（HTTP 429，「你太快了」），发太少又白白等着。写死一个数也不行——不同模型服务的容量差很多，写死成 6 在这个服务上偏保守、换个宽松的服务又浪费。

**解法：自适应并发（AIMD，借自 TCP 拥塞控制）。** 一句话——「连续成功就慢慢加，一撞限流就砍半」：

- 一连成功若干次 → 并发数 +1（试探着往上加）；
- 一撞限流 → 并发数砍半（立刻收手）。

**砍半不是终点**：砍完继续按上一条慢慢加回去，涨到再撞限、再砍半，如此反复——并发数在后端容量的真实边界附近**锯齿振荡**（涨 → 撞 → 砍半 → 涨），不停在某个固定值。「砍半」是快速退避，不是把并发永久压到一半，稳态平均约容量的 3/4。

为什么是「慢涨快降」而非对称加减：撞限流说明当前并发已超过后端容量，得果断大幅退避回安全区（只减 1 可能还远在容量之上、接着撞）；往上涨则保守（每 8 次成功才 +1），避免反复撞。代价是振荡——砍半到回升那段时间确实只按约一半并发、吞吐暂时偏低；换来的是**不用预知后端容量**（sift 跑各种后端，容量各异且不公布，AIMD 自己探测）和不持续撞限（持续撞 429 触发重试退避，反而更慢）。严格说 AIMD 不是吞吐最优（稳态利用率约 3/4），是「未知容量下的鲁棒收敛」。默认自适应；想锁固定值用 `--max-concurrent N`。

> **凭据从哪来**：HTTP 直连得自己找 API 凭据和模型名。原先只看环境变量，可有些环境（比如 IDE 拉起的子进程）里压根没有这些变量，于是所有调用悄悄失败、报告一片「没判成」。后来改成跟 claude CLI 同源、从 `~/.claude/settings.json` 读，同一台机器上 CLI 能跑的地方 audit 也能跑了。

一句话总结：HTTP 直连省掉「每次开机」的开销（单次快几倍），自适应并发把「同时发几个」交给算法自己摸（既不撞限流也不多等）。报告「阶段耗时 / token」表里的秒数，就是这套机制跑出来的实测值。


---

## 第十三章 审计的作用域与 transcript

这一章讲审「谁」、transcript 从哪来/怎么变成可对账的 digest——也就是 audit 命令的用法层。

### 13.1 audit 命令：对账历史 transcript

audit 是**对账**，不是单看 transcript——背后有个根本前提：**transcript 只是执行事实，不是规范**。它只记「做了什么」，给不出禁令（如「禁止编造数据」）、也说不出遗漏（「该做没做」）。若把 transcript 单方面当标准答案，会出两类错：把违规固化成期望（做过就当对）、把违规洗成 N/A（没判违规就算过）。所以规范权威永远是**声明**，transcript 只提供场景和事实，**两者交叉**才有可靠判断——冲突点由声明仲裁，trace 始终只当证据。

audit **不 re-run Claude**，直接拿已有会话的 transcript 跟声明对账。被审声明（首参）可以是 skill 目录（找其下 SKILL.md）、SKILL.md 文件、或任意 agent / workflow 的 .md 文件（直读）。`--transcript` 吃两种格式(按后缀自动分派):cc 会话 `.jsonl`(`run --task` 两种执行器都支持,默认 `cc` 产 `.jsonl`,`--executor opencode` 产 `.json`)或 opencode session `.json`(`opencode export <sessionID>` 导出,顶层 `info`/`messages`、parts 嵌在 message 里;生产:`opencode session list` 找 id → `opencode export <id> > s.json`)。**拿到一个 .db 文件**(别处拷来的库)直接 `audit --db handed.db`(内部自动 list/export/audit;单 session 自动选,多个列出挑;底层三步见 reference audit 段)。可重复,多条 transcript 做指令级聚合(跨会话看 fail%)。

```bash
sift audit path/to/decl/ --transcript run-1/transcript.jsonl -o audit-out/   # decl = skill 目录 / SKILL.md / agent.md
sift audit path/to/decl/ --transcript a.jsonl --transcript b.jsonl            # 多 transcript 聚合
sift audit path/to/decl/ --transcript opencode-session.json                   # opencode session(.json)
```

判定要逐批调 LLM（每批约 40–257s），跑期间实时打印进度（`[条件性/禁令/步骤] 判定 N 条…`），三个 LLM 类别判定器（条件性 / 禁令 / 步骤；输出格式走程序判定不调 LLM）并发跑（约 2× 提速）；声明里有顺序对 / 数量指令时，控制流判官作为第 4 个跟着一起跑（§11.5）；全局并发由 AIMD 自适应限制器动态调（机制见 §12.10）。看到 `[类别] …` 滚动就是在跑，别误以为卡死。

**对账作用域（两层，skill/agent/root 一视同仁）**：

- **默认（不带 `--kind`）= 宽松全 session 模式**：拿**整条 transcript** 对声明 body 对账、不切分——**skill / agent / root 三者共同默认**（不是 skill 专属捷径）。整段当一个袋子判，别单元的行为会混进报告：skill 指令多「做事方式」类、不活跃段多落 N/A（误伤轻），故 skill 宽松模式常可接受；但 agent / root 的角色专属 / 编排指令会扣到不相干 record 上（agent 尤甚 → 误 FAIL）。
- **`--kind skill/agent/root` = 精确收窄**：切到声明单元的真实执行作用域再对账——三种锚点各切各的、**不并集**（skill 按 `Skill` 调用、agent 按 `agent` 归属、root 按主作用域）。该单元没以所选锚点在 transcript 出现 → exit 2（不静默跑空）。

**对称提示**：不带 `--kind` 但 transcript 含可收窄锚点（本 skill 被调用 / `agent==name` 段 / 子 agent 作用域）时，audit 打黄字提示可加哪个 `--kind`（skill/agent/root 一视同仁、**非阻断**——宽松模式照跑）。opencode 单锚点（无 `Skill` tool_use），`--kind skill/agent` 的区分对它无意义、照按 per-message `agent` 字段切；提示也只对 cc / structured transcript 触发。

三种作用域各成一节（平权，`--kind` 三选一）：**审 skill → §13.2**；**审 agent → §13.3**；**审 root → §13.4**。

读结果（别只数 fail、来源徽章 / 同源关联）见 **§13.5**；transcript 反推出的额外检查见 **§13.6**；违规 turn 定位见 **§13.7**；transcript 怎么重建见 **§13.8**；transcript 从哪来见 **§13.9**。

### 13.2 审 skill（`--kind skill`）

skill 按 transcript 里的 `Skill` 工具调用切——**scope-aware last-invoked-owns**：每个 `Skill`（target）调用起一段、归该 skill 到同作用域下一次 `Skill` 调用或作用域结束；子 agent（`Agent` 工具）是独立作用域，其 record 带 `parent_tool_use_id`、不污染主 skill 归属。target 调多次 → 多段 → 多 transcript 聚合。

```bash
sift audit path/to/skill/ --transcript big-multi-skill.jsonl --kind skill   # 多 skill 会话：只对账本 skill 被调用段
```

两条固有限制：① skill 无「返回」信号，「调 A→调 B→不重调 A 接着干 A」那段会归给 B（不可检测，解法是 agent 显式重调 A）；② 把委派子 skill 的段折进来审（inclusive）需用户声明、暂不支持。

### 13.3 审 agent（`--kind agent`）

审**被 dispatch 的 subagent**——拿 subagent 的定义 .md 去验它在会话里有没有按定义执行。agent 与 skill 是两种不同的执行单元：skill 被 `Skill` 工具调用、agent 被 `Agent`/`Task` 工具 dispatch，落进不同的切段（所以审 agent 用 `--kind agent`，不能用 `--kind skill`）。

**怎么用**：第一个参数给 agent 声明（fixture 式目录含 `SKILL.md`，如 `test-runner/`；或直接给 agent 的 `.md` 文件，如 `agents/developer.md`），加 `--kind agent`：

```bash
sift audit path/to/test-runner/ --transcript session.json --kind agent   # 审被 dispatch 的 subagent（给 agent 定义目录）
```

切片器按 `record.agent == <name>` 切出该 agent 被 dispatch 期间的连续段，再对账它的声明指令。**name 契约**：agent .md frontmatter 的 `name` 要对上 transcript 里的 `agent` 字段（如 `name: test-runner` ↔ `record.agent == "test-runner"`）——对不上就切不到段、exit 2。

**和 skill 对账的三处区别**：

- **锚点不同**：skill 按 `Skill` 工具调用切（scope-aware last-invoked-owns）；agent 按 `agent` 归属字段切（dispatch 锚点，无 `Skill` 调用）。
- **扁平切、无父子作用域**：agent 切片只看 `record.agent == target`、**不读 `parent_tool_use_id`**。target 的 run 里若嵌套了别的 agent 的 turn（A dispatch B 后 resume A，record 序 A…B…A），A 的 run 被 B 断成两段。这是上游数据模型的被迫选择（`buildStructuredRecords` 不合成 `Agent` tool_use、无「agent 名 → dispatch id」映射，无法像 skill 路那样按作用域归段）。**断段无害**：每段独立喂 auditor，findings 全量 pool 后按指令聚合，verdict 与合成 1 段等价。
- **dual-nature 同名**：同一个名字既是 skill（被 `Skill` 调用）又是 agent（被 dispatch）时（如某个同名既作 skill 调用、又作 agent dispatch 的名字），`--kind skill` 审 skill 那面、`--kind agent` 审 agent 那面，各切各的、不串味。**kind 选锚点、不并集**——并集会把 agent 执行段塞进 skill 正文对账、对象错位。

现成示例：`fixtures/example-agents/`（test-runner / lint-reviewer，合成违规 transcript + 期望该抓的违规逐条列明），见 [example-agents/README.md](../fixtures/example-agents/README.md)。

> opencode transcript 单锚点（无 `Skill` tool_use），`--kind skill/agent` 的区分对它无意义、照常按 per-message `agent` 字段切——cc 的 skill/agent 双锚点区分只对 cc / structured transcript 有意义（`--kind root` 在 opencode 取 `session.info.agent`）。

### 13.4 审 root（`--kind root`）：对账顶层主 agent / 编排者

与 agent（被 dispatch 的子 agent）对称，root 切的是**顶层主 agent**的作用域——审主 agent 的**编排动作**（dispatch 了谁、何时、怎么接返回），而非子 agent 内部。它是整 session / workflow 审计的地基：主 agent 的声明常是一份 workflow 级 SKILL.md（管主 agent 怎么编排，而不是某个子 skill / 子 agent 的行为），root 就是拿这份声明去对账主 agent 作用域的执行。

**双平台定义**：

- **cc / 结构化 records** = `parent_tool_use_id` 为空的全部 record（主作用域）。主 agent 自己的 turn + 它发起的 Agent dispatch tool_use / tool_result 都在主作用域（这些是主 agent 的编排动作，**包含**）；子 agent 内部 record 的 `parent_tool_use_id` 指向某 Agent 调用（**排除**——那些归各自 `kind="agent"` 单元审）。
- **opencode** 无父子作用域，root 取 `session.info.agent`（declared 主 agent），复用 `slice_opencode_by_agent` 按连续同 agent message 切段。

**断段差异**（同源于 §13.3 agent 路的「扁平切」）：cc 主作用域过滤后是连贯的主执行流，作为**单 span** 喂 auditor（即使主 record 在原序列里被子 agent record 物理夹断，过滤后仍连贯一段，区别于 agent 路的同名 run 断段）；opencode 按连续 message 切，主 agent 的 message 若被子 agent 的 message 夹断（主…子…主…子…主），会被切成**多段**——这无害，每段独立喂 auditor，findings 全量 pool 后按指令聚合（`aggregate_by_instruction` / `aggregate_coverage` / `aggregate_boundary` 同为 pool 后的纯聚合，断段等价性同理成立），verdict 与合成单 span 等价（与 agent 路断段无害同理）。

**target 名对 root 无意义**：skill / agent 路靠声明名（`name` 契约）切，root 只认作用域（cc 的 `parent_tool_use_id` 是否为空 / opencode 的 `info.agent` 字段）——切主 agent 不看它叫什么、target 名忽略。这也是 root 能成为整 session 审计地基的原因：它不依赖某个子单元被点名调用，而是恒定的顶层作用域。

exit code 不变（root 不引入新 gate）：cc 主作用域在**非空 transcript** 下恒非空（至少起头 user turn 在主），never-invoked 不触发 exit 2；**完全空 transcript 仍触发 exit 2**（无 record → 主作用域也为空，never-invoked）；opencode 缺 `session.info.agent`（无法定主 agent）→ exit 2。

### 13.5 怎么读 audit 结果：别只数 fail + 来源徽章

读 audit 结果时记住一句话：**别只数 fail 条数**——每条 finding 带序号。**来源徽章**显示这条 finding 是从哪个 / 哪些 `InstructionSource` 抽出来的（CONSTRAINT / ANTI_FABRICATION / HEADING_STEP / TOOL_REQUIREMENT / REFERENCE_USAGE / OUTPUT_FORMAT）。当同一句声明文本同时匹配多个抽取模式（如同句既是约束、又是反捏造）、且同 transcript 同 verdict 时，会**合并成一条**，徽章就显示两个来源（如 `CONSTRAINT + ANTI_FABRICATION`）——证明同文重复已去重、没有虚增 fail。而同一行为跨两条**不同**规则（如没读某 reference 同时违反「条件性」+「步骤」两条）仍会是两条，但互标「↔ 同源 #N」。所以 fail 条数可能 > 真实问题数，逐条看 evidence 对照「哪个行为」，别数条数。

### 13.6 derived_from_transcript（反推）

findings 里 `derived_from_transcript: true` 的不是从声明抽出来的，而是从 transcript 实际行为**补**出来的检查（**只增不减** = 只新增 finding，不会替换或删掉从声明抽出来的指令）。**意图是堵 extractor 漏抽**——声明其实该有这条（语义上），正则没抽出来，transcript 印证 agent 做了，补上。注意别读成「transcript 偏离了声明」：声明**没写**要 read 某文件 ≠ agent 违规，compliance 只判声明**要求**的做了没 + **禁止**的犯了没，声明未提的领域 agent 自主查阅既不算遵循也不算违规。

**目前只补一类**：transcript 里 agent 真实 Read 过的输入/参考文件 → 各补一条「读取 \<path\>」（REFERENCE_USAGE，构造上即命中 → PASS，只补对、不挑错、不膨胀 fail）。另外两类不反推——工具调用反推（`required_tools`，Bash 命令分不清业务专用还是通用）和产物文件反推（`required_outputs`，历史 trace 的产物未必还在盘，且「产出文件检查」属评分断言、不属遵循性指令），因为映射不干净、容易误判。

### 13.7 turn 定位（`turn_refs`）：可选的违规 turn 定位，默认关

audit 的 finding 默认告诉你「违反了什么指令 + evidence（中文理由）」，但**不告诉你在哪个 turn**——几百 turn 的 session 里手动翻找不现实。`turn_refs` 补这个：FAIL / INDETERMINATE 的 finding 带 `turn_refs` 列表（全局 turnIndex），能 link 到 turn 的消费方（如有 turn 浏览器的下游工具）拿它能直接跳到对应 turn 看上下文。

**但默认关（`--no-turn-refs`），要 `--turn-refs` 显式开——不是没做完，是开它有代价。**

**做了什么**：开 `--turn-refs` 时，digest 重建给每条 assistant record 文本加全局 `[T{idx}]` 前缀（`[T142] assistant: …`），judge prompt 据此要求 LLM 在 FAIL / INDETERMINATE 时输出 `turn_refs`。段级缺席（「全程未发生」，如「调用 subagent」全程没调）→ 留空；turn 级违规（某具体 turn 做错）→ 给 seq。`locate_turns` 参数贯穿 rebuild（控前缀）→ judge（控 prompt 的 turn 引用规则）→ audit CLI 开关。

**对结果的影响：引入条件分支过严**——加 `[T{idx}]` 前缀 = 动了喂 LLM 的 prompt，实测让判定**变严**：step / prohibition 类里「条件不满足→本就该跳过」的指令（如「读取 PLAN.md」「读取 probe」，本 session 条件没触发、该判 N/A），LLM 见到 turn 信号就倾向 FAIL。实测加前缀后 FAIL 数会明显上涨，多出的**不是新违规、是条件分支误判**（真违规判定没退化：原本就成立的 turn 级违规 FAIL + turn_refs 全保留）。

故默认关：多数场景（尤其 CLI 直接看报告、没有能 link 到 turn 内容的消费方）开了只多收条件分支误判噪声、拿不到定位收益；要定位且能接受多报时才 opt-in 开。

**三轮「判定层纠偏」尝试皆败（教训，勿重蹈）**——发现过严后，试过三轮在**判定层**修正它，全败：

| 尝试 | FAIL | 为什么败 |
|---|---|---|
| 调松 judge prompt 措辞 | 17（过宽） | 丢真违规（如「最多重试 2 次」→N/A） |
| 加 `premise_held` 独立二判 | 30 | LLM 把 premise_held 当捷径、25 PASS→N/A + 丢真违规 |
| 判后纠偏 pass（rectify） | 36（最接近） | **漏报**：喂去 `[T{idx}]` 的 stripped transcript 再判，但「条件分支该跳过」与「真违规该做没做」在 stripped 文本里长得一样（都 = 没出现），LLM 分不清、全判 N/A，误伤 ~10 真违规段级缺席 |

**根因**：判定层二判无法区分两类「缺席」——stripped 掉 turn 信号后，「本就不该做」和「该做却没做」都是「没出现」，必漏报。审计最忌漏报（违规被掩盖、带「纠偏」标记还误导用户以为「不涉及」），比多报（可识别排除）危险得多。

**最终落地：做成可选开关，而非判定层纠偏。** 判定层没法「既保 turn_refs 定位又免条件分支过严」，故不在判定层二选一——两个形态都保留交用户选：默认给干净的（no-seq），要定位且能接受多报时 opt-in 开 seq。比任何判定层纠偏都干净。

**开 `--turn-refs` 时怎么读报告（triage）**：

- **`turn_refs` 空** = 段级缺席（全程未发生）。两可能：条件分支没触发（本就该跳过 → 其实 N/A，属多报）／真违规该做没做（真问题）。需复核 evidence。
- **`turn_refs` 有 seq**（如 `[142]`）= 确定的 turn 级违规，直接定位那个 turn。

条件分支误判集中在「empty turn_refs」那批，看 seq 能快速分流。

**已知限制 / 后续**：

- **opencode 切片的 turn_refs 是段内 local、非全局**：opencode 切片经 message 子集往返重建、按段内位置 re-stamp；cc / structured records 不受影响（dict 引用子集，`_turn_index` 经 JSON 往返保全局）。opencode 路开 `--turn-refs` 时 seq 是段内的。
- **OUTPUT / CONDITIONAL 类不带 turn_refs**：`--turn-refs` 只让 prohibition / step 类（走 transcript_text + turn 引用的 LLM judge）输出 turn_refs。OUTPUT 是程序化子串匹配、CONDITIONAL（TOOL_REQUIREMENT / REFERENCE_USAGE）的 FAIL 是段级缺席（trace 全程未调用 / 未读取 X）——两者都无具体 turn 可定位、恒空；只有 prohibition / step 的 turn 级违规才带 seq。
- **想减条件分支误判**：别再试判定层纠偏（三轮皆败）。靠报告层 triage（empty/有 seq），或改单轮 prompt / conditional 判定器本身的「条件不满足→N/A」引导。

### 13.8 transcript 怎么变成可对账的 digest：三种来源同一条重建路

audit 对账前，先得把 transcript **重建**成一个结构化 digest——把「模型读了哪些文件、写了什么、改了什么、调了哪些工具」从原始会话记录里还原出来。这里有个关键架构：**cc 会话、opencode 会话、结构化输入三种来源，归一到同一种中间表示，再走同一个重建器**，所以重建逻辑只维护一份。

```
cc session (.jsonl)            opencode session (.json)         结构化 records (.json)
逐行 JSON record               opencode export                  friendly JSON
tool_use / tool_result         {info, messages, parts}          {role, tool_calls}
分两条记录，靠 tool_use_id 连   tool 的 input+output 在一个 part
       │                              │                                │
  _records_from_cc           _records_from_opencode          load_structured_records
  (近恒等)                   (转换：见下表三件事)             (工具名归一)
       │                              │                                │
       └─────────────► list[cc-record 形] ◄─────────────────────────────┘
                         （统一中间表示 / canonical）
                                 │
                                 ▼
                     _digest_from_records（共享重建器，单处定义）
                     · pass1：tool_use_id → result 映射
                     · pass2：按序演进文件状态
                         Read → 初始内容；Write → 覆盖；
                         Edit/MultiEdit → old→new 替换（找不到 → demoted）；
                         Bash → 记命令 + bash_touched
                     · 收 tool_trace / user_turns / transcript_text
                                 │
                                 ▼
                          SessionDigest
                                 │
                                 ▼   ← opencode 特例：skill_used 从 info.agent 补回
                                     （它没 Skill tool_use，重建器抽不到 → opencode 侧补）
                                 │
                                 ▼
                   TranscriptAuditor.audit()（对账）
                   · 多单元会话已按 --kind 先切分 → 每段各自重建、audit 跨段聚合
                     （cc 按 Skill tool_use / agent 归属 / 主作用域；opencode 按 agent）
                   · 从声明提指令（6 类 → 4 类，见 §11.2）
                   · 每条指令：命中 tool_trace → PASS（程序，不调 LLM）；
                                否则 → LLM 语义判（transcript_text）
                   · 五态 verdict（见 §11.3），跨 transcript 聚合违规率
```

**为什么是这个形状**：cc-record 形（`{message:{role, content:[块]}, parent_tool_use_id}`）是仓库里早就有的 canonical 中间表示——结构化输入 importer 一直在用（把各种格式归一成它再喂重建器）。cc 近乎恒等（它本来就是这形状）；opencode 一度是唯一没接上、自己 inline 了一份弱化重建的（只 read/write/bash，无 edit 重建、无 unreconstructed 告警——技术债）。修法不是推倒重来，而是让 opencode 也走那条已验证的路（`structured.py` 是现成模板）。

opencode 的转换器（`_records_from_opencode`）做三件事抹平格式差异：

| opencode 的样子 | 转成 cc-record 形 |
|---|---|
| 工具名小写 `read`/`write`/`edit` | 归一大写 `Read`/`Write`/`Edit`（`_norm_tool`，共享映射表）|
| 字段 camelCase `filePath`/`oldString`/`newString`/`replaceAll` | 映回 snake_case `file_path`/`old_string`/...（`_map_opencode_input`）|
| tool 的 input+output 同在一个 part | 拆成 tool_use 块 + 尾随 tool_result 块，合成 tool_use_id（凑成 cc 的两趟关联形态）|

**read 输出的清洗不归转换器、归共享重建器**：真 opencode 导出 / 库里 read 存成 `<path>…</path>\n<type>file</type>\n<content>\nN: line…</content>`（带 XML 包裹 + `N: ` 冒号行号），不剥会污染 `input_files` 内容、还会让多行 edit 的 `old_string` 匹配不上而误判不可重建。这步清洗（`_digest_from_records` 里的 `_clean_read_result`）按输出**以 `<path>` 起手** + `<content>` 包裹触发（opencode read 恒以 `<path>` 起手、cc 恒以 `\d+\t` 起手，首字符就分化，比「含某字面」稳、假阳趋近零，且不绑 `<type>` 值）——**三种来源都走同一个**，所以结构化输入（观测平台 / 自建 harness 等）直喂这种 opencode 形 read 也自动洗净；cc / 裸内容（不以 `<path>` 起手）只剥自己的 `\d+\t` 行号，正文里合法的 `N: ` 文本、乃至含 `<content>`/`<path>` 字面的文件正文都不误伤。

抹平之后，Read/Write/Edit/MultiEdit 重建 + demoted/unreconstructed 推导（含上面的 read 清洗）对三种来源完全一致。**一个例外**：opencode session 没有 `Skill` tool_use（它的 skill 标识在 `info.agent`），所以 `skill_used` 由 opencode 侧从 `info.agent` 补回（重建器只从 `Skill` tool_use 抽，抽不到）。

**多单元切分（`--kind`）**也在归一之后口径统一：cc 按 `Skill` tool_use / `agent` 归属 / 主作用域切（skill/agent/root 三锚点），opencode 按 per-message `agent` 切连续段（扁平，在转换前切原始 session）。切完每段各自重建 → 各自对账。

> **对 audit 判定无影响**：重建出的 `produced_files`/`input_files` 内容、`unreconstructed` 这些字段，audit 其实**不读**——audit 只读 `tool_trace`（程序匹配：工具名/路径/参数都大小写不敏感）+ `transcript_text`（LLM 语义判）+ `user_turns`（场景）。所以 opencode 接到共享路之后 digest 更完整（edit 过的文件也重建、内容干净、不可重建的会告警），但 audit 的 verdict 不受影响——变的是 digest 的「保真度」，不是判定结果。

**结构化输入（structured records）——给外部 producer 的通用入口**

上面三种来源里，cc 和 opencode 各绑一种原生格式（cc `.jsonl`、opencode export `.json`）。**结构化输入**是第三条路：一个**故意做「友好」的通用 JSON 格式**，给任何**已经把会话归一成 turn + 工具调用**的外部系统用——它不必伪造 cc 的 jsonl、也不必伪造 opencode 的 `{info,messages,parts}`。形状（顶层 `format == "sift-records"` 作识别，跟 opencode 的 `{info,messages}` 区分）：

```json
{"format": "sift-records",
 "records": [
   {"role": "user", "text": "..."},
   {"role": "assistant", "agent": "...", "parent_tool_use_id": null, "text": "...",
    "tool_calls": [{"name": "skill", "input": {"name": "x"}, "result": "..."},
                   {"name": "read",  "input": {"file_path": "..."}, "result": "..."}]}
 ]}
```

**它最通用、最不绑任何框架**：cc 绑 Claude Code、opencode 绑 opencode export，结构化输入只要求「有 turn + 工具调用」——任何 agent 框架 / 观测平台 / 自建 harness / 别的会话存储（像某些观测平台那样把原生格式转成自己 schema 的）都能吐。已有实例在用，但**不专属任何一家**：producer 不用拼 cc 的 tool_use / tool_result 拆分、不用管 `Skill` 大小写、不用管 read 输出的 `<content>` 包裹——这些归一全由 sift 这边做（`load_structured_records` 展开成 cc-record 形；read 清洗、Edit/MultiEdit 重建都走共享重建器），producer 只管按 turn 吐真实数据。

**对接要点**（给想接的 producer）：每个 turn 一条 record；工具调用放 `tool_calls`（`name` 用原始小写即可，sift 归一）；skill 调用参数键用 `input.name` 或 `input.skill` 都行（reader 归一到 cc 认的 `input.skill`）；`parent_tool_use_id` 是**作用域标记**——主 turn 填 `null`，子 agent 里跑的 turn 填该子 agent 的稳定 id。

**`parent_tool_use_id` 决定多 skill 切段的作用域归并**：`--kind skill` 的切片器（`slice.py:_scope_of`）按它把 record 归作用域——`null` = 主，其他值 = 某子 agent 的独立作用域；作用域内 last-invoked-owns。**子 agent 里调用过 skill 的 session 必须填它**：若子 agent turn 也填 `null`，它们会和主 turn 挤进同一个主作用域，主作用域的噪声（别的工具调用 / 别的 skill）就按 last-invoked-owns 混进本 skill 的段 → 假证据（实测跨 scope 噪声 40%+）。填对之后，子 agent 的 skill 段被干净隔离进自己的作用域。

**`tool_calls[].result` 是可选的——做对账(audit)的 producer 建议不传**：`result`（工具返回值，如 Read 整个文件、skill 正文、大段命令输出）在真实 session 里常达 10MB+，而 **audit 的判定根本不读它**——auditor 只取 `digest.user_turns`(场景)+ `tool_trace`(工具调用的**输入**)+ `transcript_text`(assistant 文本)，三项都不含工具结果；连「反推指令」(`derive_declarations`)也只读 `input_files` 的 **路径**(路径来自 Read 的 `file_path` **输入**，不靠 result)。所以全量回传 `result` 是纯死重量：会让 `--transcript` 的 session.json 撑到 20MB+，序列化 / 落盘 / sift 解析全慢。做 audit 的 producer 因此**不传 `result`**（payload 实测 18MB→7MB）。reader 对缺 `result` 的 tool_call 本就容错（不产 tool_result 块，`input_files` 内容空但路径在、tool_trace 完整），对账 verdict 不变。仅在需要重建文件**内容**(produced_files 还原、非对账)时才传 `result`。

### 13.9 audit 吃的 transcript 哪来

audit 不 re-run cc、只对账已有 transcript，所以「transcript 从哪来」是关键。三个来源：

- **历史会话**（最常见）：直接拿真实会话的 transcript——cc 的 `.jsonl`（`~/.claude/projects/...` 里）、或 opencode 导出的 `.json`（`opencode export <sessionID>`）。这是审计最有价值的来源：真实使用里的违规是诊断资产，反映声明在实际中被遵循成什么样（见 §12.8）。
- **`run --task` 真任务模式产的 trace**（见 §5.5）：真任务模式 spawn 执行器在真实代码库/数据目录跑一个真任务，产 `run-*/transcript.jsonl`（cc）或 `run-*/transcript.json`（opencode）。这份 trace 直接喂 audit——这就是评测和审计的产→审闭环：run 产真任务 trace，audit 审这份 trace 有没有照声明做。`--task` 必须对口被审声明（不对口 → audit 假 FAIL）。
- **结构化输入**（外部 producer）：观测平台 / 自建 harness / 别的会话存储把会话归一成 `sift-records` 格式直喂，不必绑 cc 或 opencode 的原生格式。

三种来源归一到同一个 canonical 中间表示、走同一个重建器（§13.8），所以 audit 对它们的判定口径完全一致——差别只在 transcript 怎么来的，不在怎么判。

---

### 13.10 文件归因证据审计

Opsbot 负责将文件归因证据输出为对应的 JSON 文件，Sift 侧负责导入和审计；Sift 的既有 Markdown 指令抽取、四类 Judge、coverage、boundary 和报告体系不变。

设计按证据层、确定性行为分析和语义审计分阶段交付：

| 阶段 | 交付内容 | 当前状态 |
|---|---|---|
| 1. 内部证据模型 | v2 reader、blob 校验、capture/effect 归一化、lineage 透传、digest timeline | 已合入 |
| 2. 确定性 Analyzer | evidence health、attribution integrity、file churn、actor ownership、verification closure | 已合入 |
| 3. 独立 Finding | `ProvenanceFinding` / `ProvenanceAuditResult` 与 `AuditFinding` 分离 | 已合入 |
| 4. audit 使用 | v2 自动运行 analyzer；v1/session 保持原行为 | 本次改动 |
| 5. 报告展示 | Provenance Overview、Behavior Findings、File Timeline | 后续 PR |
| 6. 语义审计 | 受限 LLM 检查最终回答、变更完整性和替代路径 | 单独评估（TODO） |

#### 内部证据模型

把 Opsbot 提供的文件归因证据（`sift-records` v2）带入现有审计流程，内部模型分成三层：

原始 v2 bundle 在 reader 边界先解析为 `SiftRecordsV2` 及其嵌套 BaseModel。BaseModel 负责协议字段的严格类型校验和跨对象完整性校验；生成 canonical cc-record 之前不会将 v2 证据整体降回普通字典。

| 模型 | 作用 | 关键内容 |
|---|---|---|
| `ToolCallRecord` | 现有工具轨迹的扩展 | 原生 tool ID、工具输入/结果、状态、时间、capture ID、agent/session lineage、evidence refs |
| `FileEffectRecord` | 单个文件 effect | path、before/after blob、增删统计、quality、conflict、actor、工具和有限内容 |
| `ProvenanceEvent` | capture 级时间线事件 | capture/tool 关联、actor/agent、生命周期、runtime/model、git head、effects 和稳定 sequence |

#### 关键步骤

1. `load_structured_records()` 按 `version` 分派：缺省、`null`、整数 `1` 和字符串 `"1"` 都进入原有 v1 loader；只有真正的整数 `2` 进入 v2 loader。
2. v2 loader 调用 `SiftRecordsV2.model_validate()`。模型负责 `records`、`captures`、`blobs` 及嵌套字段的严格类型校验，并在 model validator 中检查 capture/blob 引用、capture key 一致性、base64 解码、SHA-256 和 size；失败统一转换为 `InputError`，不生成部分可信输入。
3. loader 从 bundle session 构建一次 `session_agents`，显式传给 `_build_v2_context()`。context 在已验证的 BaseModel 上计算关联质量：唯一引用为 `association_quality=exact`；多次引用或 capture 自身为 `ambiguous` 为 `ambiguous`；零次引用为 `unmatched`。
4. 在 canonical record 之前，records、captures、effects 和 blobs 仍是 BaseModel。只有写入现有 canonical record 时才 `model_dump()`：assistant/user 文本进入 `message.content`，工具调用进入 `tool_use`，工具结果进入 `tool_result`，关联 capture 进入 `_sift_evidence`，unmatched capture 进入 standalone record。
5. `SessionDigest` 会继续填充原有 `user_turns`、`input_files`、`produced_files`、`commands` 和 `transcript_text`，并额外保存 `evidence_refs`、`evidence_warnings`、`evidence_metadata`、`provenance_timeline`。因此 **v1 和普通 session 的字段和判定行为不变**，v2 只是增加可供后续 analyzer/Judge/report 使用的证据。

```text
sift-records v2 JSON
        │ load_structured_records()
        ▼
临时 canonical JSONL
        │ slice_transcript(kind=root/skill/agent)
        ▼
_digest_from_records()
        ├─ tool_trace（包含 capture/evidence 元数据）
        ├─ evidence_refs / evidence_warnings / evidence_metadata
        └─ provenance_timeline
```

#### 确定性 Provenance Analyzer

入口为 `analyze_provenance(digest: SessionDigest) -> ProvenanceAuditResult`，输入为已经校验过的 `SessionDigest`，不调用 LLM。五个维度及默认语义如下：

| 维度 | 检查内容 | 默认输出 |
|---|---|---|
| Evidence Health | quality 分布、unmatched/ambiguous/conflicted/incomplete、缺失 capture、coverage warnings、无法重建文件 | `WARNING/INDETERMINATE` |
| Attribution Integrity | 写工具无文件变更、错误或失败工具产生变更、只读工具产生变更、轨迹字段不一致 | `WARNING/INDETERMINATE` |
| File Churn | trusted effect 的重复修改、恢复旧内容哈希、完整覆盖 | `INFO/WARNING` |
| Actor Ownership | 每个路径的执行者、最终写入者、明确不一致的 parent lineage、执行者信息缺失 | `WARNING` |
| Verification Closure | 最终修改后的测试证据、最终修改未验证 | `WARNING` |

验证闭环只针对通常需要执行测试的代码文件；`.md`、`.markdown` 和 `.mdx` 文档默认视为验证豁免路径，不会因为没有测试调用而产生 `FINAL_MODIFICATION_UNVERIFIED`。工具的 `is_error` 表示结果是否被标记为错误，`succeeded` 表示是否明确报告成功；两者同时存在时，错误标记优先，只有 `succeeded=true` 且 `is_error` 不是 `true` 才视为成功。

实现位于 `src/sift/audit/provenance.py`，结果数据模型位于 `src/sift/models/provenance_audit_models.py`。`TranscriptAuditor.audit()` 现在会对每个成功重建的 transcript 自动运行 Analyzer，并将可用结果按 transcript 文件名写入 `AuditReport.provenance`；该区块独立于既有 findings，不改变 audit 的 exit code、声明 coverage 或 PASS/FAIL 数量。对普通 v1/cc/opencode session，`provenance` 保持为空；对 v2 即使没有 mutation，也会写入 `available=true` 的结果，因为“工具执行但没有文件变化”本身是可审计事实。阶段 5 的 HTML 交互式展示仍单独推进。

独立 Provenance Finding 不复用 `AuditFinding`，以免污染声明覆盖率或改变现有 pass/fail 数量；`ProvenanceAuditResult.file_timelines` 使用 `ProvenanceTimelineEntry` 表示每次文件 effect。完整类型、默认值和取值约定见 [reference 中的字段定义](sift-reference.md#provenancefinding)。

#### audit 使用

```bash
sift audit path/to/agent.md \
  --transcript opsbot-audit-<session>.json \
  -o audit-output
```

#### 相较原有审计的新增内容
##### 审计证据增强

原有审计的输入是 Markdown 声明和 agent transcript，重点回答“声明中的条件、禁令和步骤是否被遵守”，并由现有 Judge 产出 `AuditFinding`、coverage 和 PASS/FAIL 等遵循性 verdict。它主要依据消息文本、工具调用和工具结果判断，不直接验证工作树中的实际文件状态。

文件归因审计增加了一层可校验的事实证据：

| 对比项 | 原有审计 | 文件归因增强后 |
|---|---|---|
| 输入 | Markdown + transcript | 在原输入之外增加 `sift-records` v2 证据包 |
| 文件事实 | 主要根据工具文本或结果推断 | 根据 capture、mutation、before/after blob 直接确认，并在 `AuditReport.provenance` 独立区块输出 |
| 关联粒度 | tool call / turn | tool call + `capture_id` + path + actor/agent lineage |
| 时间信息 | transcript turn 顺序 | 带 timestamp、iteration、event/capture ID 的稳定 provenance timeline |
| 可信度 | 主要由 Judge 对文本证据判断 | 保留 `observed`、`incomplete`、`conflicted`、`ambiguous`，并校验 blob 引用和 SHA-256 |
| 输出范围 | 声明遵循性 finding 和 verdict | 在其旁边增加文件 effect、evidence refs，以及后续的 Provenance Finding |

##### 独立 Analyzer 校验报告

Provenance Analyzer 的结果作为报告中的独立 `provenance` 区块输出，不并入原有报告。

报告包含三个层次：

1. **Provenance Overview**：显示 `available`、输入证据 SHA-256、Opsbot session ID、capture/effect 总数、涉及文件和 actor 数量、quality 分布、exact/ambiguous/unmatched 关联数、冲突数、coverage warnings，以及可重建和不可重建的文件数。`available=false` 或证据包校验失败时，报告明确显示“无可用文件归因审计”，不伪造结果。
2. **Behavior Findings**：每条 finding 展示 `dimension`、`code`、`severity`、摘要和解释，并列出 path、actor、tool use ID、turn、capture ID、`evidence_ref`。`WARNING` 以风险提示呈现，表示事实已确认但不自动等价于声明违规；`INDETERMINATE` 以证据不足呈现，标出缺失/冲突/歧义原因，不给出确定的 PASS/FAIL。
3. **File Timeline**：按路径展开 `before → capture/effect → after/final`。每个节点标记 timestamp、actor、tool、quality 和 evidence ref；`observed` effect 可作为可信最终状态，其他 quality 标记为 `unreconstructed`，不会覆盖最后一个可信状态。文本文件只展示受预算限制的 unified diff/excerpt，二进制只展示 path、size 和 hash。

### 13.11 改进建议（`--suggest`，可选）：FAIL 分三类 + 起草修改建议

audit 报出 FAIL 之后，「哪些该回去改 skill、哪些其实是声明本身写得脱离实际」要靠人自己琢磨。`audit --suggest` 让 LLM 帮你做这一步：把 FAIL 归因、起草对应的修改建议。**默认不开**——audit 的本分是判对错，建议是你明确要了才给的附加品（建议不进任何 gate、不影响 exit code）。agent / 编排（`--kind agent` / `--kind root`）审计同样可用——`--kind` 只切 transcript 侧的执行段，建议归因吃的是被审的那份声明正文（.md）；归因枚举名 `skill_fix` 是 skill 时代起的，对 agent 语义就是「改 agent 正文」。

建议按归因分三类（归哪类决定给不给建议）。前两类的名字容易读成「改两个不同的文件」——对 skill 来说声明就是 SKILL.md，两类建议最后都落在**同一份正文**上；分两类的不是改哪里，是**「谁的错」的判决相反**，修法方向跟着相反：

- **改 skill**（`skill_fix`）：判决「错在执行」——声明要求的事本身是对的，但执行端一直做不到（多个会话反复栽在同一条、或程序判定钉死）→ 修法是把这段写法改得**更容易被照做**：措辞更硬、加显式约束、把容易漏的拆成步骤。该做的事不变，变的是正文的「操纵力」。
- **改声明**（`declaration_fix`）：判决「错在声明」——执行做的才是更合理的事，声明过时或太死板（字面违反了、行为其实对，如跳过了冗余步骤）→ 修法是**把正确实践写回声明**：改写、放宽或删掉那条过时的要求，让声明跟现实对齐。
- **模型 / 环境问题**：偶发失败、工具噪声 → **不给建议也别硬编**，只报一个数（「另有 N 条 FAIL 归因模型/环境」），让你知道有这么些条不属于前两类。

「反复失败」还是「偶发失败」要多个会话才分得出来；只审一条 transcript 时每条失败都是 1/1，这个信号用不了——这种情况就明确告诉 LLM「别按偶发归类」，不然所有 FAIL 都会被一句「仅出现 1 次」推给「模型问题」。

给 LLM 看什么：一次调用、一个 prompt——全部待归因的 FAIL（每条带编号 + evidence + 判定方式 + 噪声标注）放在一起，旁边附**一份**「哪些指令全 PASS」的清单（整个 prompt 只有一份，所有 FAIL 共用，不是每条 FAIL 挂一份）。这份清单是给建议上保险用的：LLM 起草「改 skill 正文」的建议时，要对着它自查一句「我要改的这段，会不会正好是现在执行得不错的部分」（`keeps_pass`）——撞上了就说明这段写得没问题、是别的会话都遵循的，改它可能把好的改坏。可能撞车的建议在报告里带 ⚠ 标出来，提醒你改之前多看一眼。**不给完整会话记录**——evidence 已经是把执行事实提炼过的版本，喂原文又贵又把注意力冲散。

省着跑、跑挂不伤人：全 PASS 就不跑（没有 FAIL，没什么可建议）；「没判成」占比超过 30% 不跑（证据残缺时建议必然瞎说，先重跑 audit）；一次最多喂 10 簇，输出上限按簇数从宽给够（6000 + 每簇 2500——这类深度归因的思考量就在这个量级，给死小帽必截断），批与批并行跑（几十个失败的墙钟 ≈ 最慢一批，不随批数翻倍）。这一步出任何错都不把异常抛出去——它跑在报告落盘之前，一炸会把整份报告一起带走；失败就留一句 `skipped_reason`，报告照常写。

明确不做：自动改写声明（只出建议文本，改不改你拍板）；建议进任何 gate；按单条 transcript 给建议（要跨会话才看得出「反复」还是「偶发」）。

## 附录

### A. 命令速查

按工作流阶段分组（与 `sift --help` 分区一致）：

**静态体检（不跑 cc）**

| 命令 | 作用 | 成本 |
|------|------|------|
| `analyze` | 11 项静态体检（skill） | 零 API，<1s |
| `vet` | 4 扫描器安全审查（skill） | 零 API（内置） |
| `critique` | 内容质量诊断（正文 4 + description 2 维，skill + agent） | LLM（两次，http） |

**出题**

| 命令 | 作用 | 成本 |
|------|------|------|
| `init` | 生成 evals.json（LLM/transcript/模板；skill + agent） | LLM（除非纯模板） |
| `add` | 追加单道 eval（skill + agent） | LLM（--idea）或零 |
| `inspect` | 题集体检，不下判决 | 零 API |
| `freeze` | 冻结基线快照 | 零 API |
| `coverage` | 漏测诊断（eval 覆盖声明内容点，skill + agent，诊断、无门禁） | LLM（一次映射） |

**评测**

| 命令 | 作用 | 成本 |
|------|------|------|
| `eval` | 评测主流程（A/B skill / 绝对质量 agent） | Claude + LLM |

**真任务**

| 命令 | 作用 | 成本 |
|------|------|------|
| `run` | 真任务产 trace 喂 audit（审计上游） | Claude / opencode |

**审计**

| 命令 | 作用 | 成本 |
|------|------|------|
| `audit` | transcript 对账（skill / agent / 编排者） | LLM |

**判决**

| 命令 | 作用 | 成本 |
|------|------|------|
| `gate` | 事后门禁判决 | 零 API，毫秒 |

**触发**

| 命令 | 作用 | 成本 |
|------|------|------|
| `trigger` | 触发准确度（第 4 维，skill 专属诊断、无门禁） | Claude spawn |

**看结果**

| 命令 | 作用 | 成本 |
|------|------|------|
| `report` | 生成 HTML 报告 | 零 API |

### B. exit code 全表

| 命令 | 0 | 1 | 2 | 3 |
|------|---|---|---|---|
| `gate` | PASS | FAIL | N/A / UNCLEAR / 使用错误 | — |
| `eval` / `run` | 产数据成功 | （`--security-gate` 触发） | 输入错误 | 执行错误 |
| `audit` | 跑完 | — | 输入错误 / 段切空（never-invoked） | 执行错误 |
| `trigger` | 跑完（诊断，无门禁） | — | 输入错误 | 执行错误（cc 缺失/超时） |
| `coverage` | 跑完（诊断，无门禁；LLM 失败降级空报告） | — | 输入错误 | — |
| `critique` | 跑完（诊断，无门禁；LLM 失败降级空报告） | — | 输入错误 | — |
| `vet` | SAFE / REVIEW_NEEDED | BLOCKED | 输入错误 | — |
| `analyze` | score 满 | score 未满 | 输入错误 | — |
| `init`/`add`/`inspect`/`freeze`/`report` | 成功 | — | 输入错误 | — |

### C. 三层在 run 结果里怎么看

- **结果面（scoring）** → `grading.json` 的 `summary`（双率）；
- **过程面（compliance）** → `compliance.json` 的 `summary`（五态计数）+ findings；
- **结果×过程协调（calibration）** → `calibration.json` 的 `verdict` + `hard_violations`；
- **默认全开**：compliance + calibration 在 `run` 里默认就带（任何 runs 数、不绑 ≥3——单 run 也产）；`--runs 3` 只是统计更稳，不是「才开」。要跳过用 `--no-*` 家族：`--no-compliance`（跳遵循性）/ `--no-calibration`（跳校准）/ `--no-static`（跳静态评分卡）/ `--no-vet`（跳安全审查）/ `--no-report`（跳 HTML 报告）。

### D. 第一次跑的最小路径

```bash
# 1. 静态体检 + 安全（零成本）
sift analyze path/to/skill/
sift vet    path/to/skill/

# 2. 出卷
sift init path/to/skill/

# 3. A/B 跑 3 轮（统计更稳；遵循性 + 校准 + report 默认带）
sift eval path/to/skill/ --runs 3 -o out/

# 4. 看报告
sift report out/ --open

# 5. （CI）出判决
sift gate out/benchmark.json --min-win-rate 0.5
```

### E. 常见坑

- **delta 接近 0 别急着下「skill 没用」**：先排查 references/`source_dir` 有没有正确拷进工作区（skill 依赖读不到 → 开关 skill 效果一样）。
- **expected_output 不要写进 prompt**：它是阅卷参考，拼进发给 Claude 的 prompt = 答案泄露，A/B 失真。
- **审计「0 FAIL」≠ 全过**：看报告顶部 UNRESOLVED 警告，没判成的条可能藏真违规，建议重跑。
- **改了 references 内容要强制重跑**：cache_key 不检测引用文件内容改动，用新 `-o` 重跑。
- **门禁 N/A 不是通过**：exit 2 可能是「根本没评上」（配置漏），别当放行。
- **深度分析栏没出现**：通常是盲比较 tie 或那次 LLM 分析没成功，不一定是 bug。
