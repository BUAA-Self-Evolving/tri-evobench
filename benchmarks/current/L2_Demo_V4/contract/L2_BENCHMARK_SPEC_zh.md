# L2 TaskStream 数据合同 V1

## 1. 范围与继承

L2 TaskStream 用于评测 Agent 能否在一条持续任务流中，从前序任务、执行结果和有限失败反馈中形成可迁移能力，并在后续同族或相关任务中改善表现。

V1 继承 L1-Life 的以下语义：

- Benchmark 由相互隔离的 Stream 组成；
- Stream 由按 `turn_index` 排序的 Turn 组成；
- 一个 Stream 只拥有一个持久 `agent_workspace`；
- Task Agent 只看到 `agent_input`、当前 Workspace、工具结果和 User Simulator 消息；
- `evaluation`、GT、rubrics、verifier 和 future Turns 对 Task Agent 密封；
- Judge 使用完整执行证据逐项评分，框架确定性重算总分。

L2 新增 capability relation、Agent-visible assets、动态 `workspace_transition`、失败后 LLM User Simulator，以及作为 Judge 权威证据的强制 native verifier。

## 2. 术语

### Benchmark

一个版本化发布数据集。

### Stream

一组按固定顺序执行、共享同一 Workspace 和 learner state 的相关任务。不同 Stream 不共享可写状态。

### Turn

一个需要最终完成并评分的 Task。Turn 不是一次模型调用，也不是一次重试。

### Interaction

Task Agent 收到一条用户来源输入后进行的一次完整任务尝试。初始 Task message 和 User Simulator 生成的 retry message 都会开启一个 Interaction。工具返回、native verifier、Judge 和 User Simulator 自身的模型调用不单独计为 Interaction。

### User Simulator

仅在一个 Interaction 完整执行并评测失败后调用的 LLM Agent。它根据公开轨迹与 bounded feedback 生成下一条模拟用户输入；它不是实时真人、澄清等待器或 Judge。

### Native verifier

由源 Benchmark 的测试、状态比较、指标函数或确定性规则适配而来的 evaluator-private 程序。每个 Reference Task 必须提供。

## 3. 发布目录

```text
l2_release/
├── manifest.json
├── tools.json
└── streams/
    └── <stream-directory>/
        ├── stream.json
        ├── agent_workspace/
        ├── turn1/
        │   ├── task.json
        │   ├── assets/
        │   └── evaluation_assets/
        ├── turn2/
        │   └── ...
        └── ...
```

规则：

- 运行时入口只依赖发布包内路径；upstream 路径仅用于 provenance。
- `assets/` 是可经 `workspace_transition` 投影给 Agent 的材料。
- `evaluation_assets/` 是 evaluator-private；禁止挂载给 Task Agent、learner 或 User Simulator。
- 不允许绝对路径、`..`、符号链接逃逸或跨 Stream 路径。

## 4. 根 `manifest.json`

必填字段：

| 字段 | 含义 |
|---|---|
| `schema_version` | 固定为 `l2_taskstream/1.0` |
| `benchmark` | Benchmark 名称 |
| `source_datasets` | 使用的源 Benchmark 名称列表 |
| `stream_count` | Stream 数量 |
| `total_turn_count` | Turn 总量 |
| `aggregation_profile` | 固定为 `l2_evolution_aggregation/1.1`，指向全 Benchmark 共用的 learning-curve 与 evolution-gain 规范 |
| `judge_prompt_version` / `judge_prompt_versions` | 单 Prompt 发布使用前者；同时包含 General V1 与 AutoResearch V2 的混合发布使用版本列表。二者必须且只能出现一个 |
| `user_simulator_prompt_version` | 通用 User Simulator Prompt 版本 |
| `tools_version` | Tool catalog 版本 |
| `judge_output_version` | Judge 标准输出版本 |
| `bounded_feedback_version` | 有界失败反馈版本 |
| `native_verifier_request_version` | verifier 标准请求 envelope 版本 |
| `native_verifier_result_version` | verifier 标准输出版本 |
| `streams` | Stream 索引 |

`streams[]` 至少包含 `stream_id`、`directory`、`turn_count` 和 `path`。

`aggregation_profile` 是根级统一路由，不在每条 Stream 重复声明 `analysis_plan`。具体算法见 `L2_EVALUATION_SPEC_zh.md`；Runtime 必须按该版本实现或复用 `evolution_reference.py` 验证结果。

### 根 `tools.json`

`tools.json` 必须通过 `l2-tools.schema.json`。`tools` 以稳定 tool ID 为键，每项声明 `description`、参数对象 `arguments`，以及可选的 `requestor`。`task.json.agent_input.available_tools` 只能引用此 catalog 中存在的 ID；具体工具实现仍由 Runtime/Baselines 提供。

## 5. `stream.json`

### L1-compatible 基础字段

```text
schema_version
stream_id
directory
profile
theme
turn_count
task_layout
agent_workspace
turn_type_counts
fact_thread_count
ordering
turns
```

`fact_thread_count` 在不使用事实线程的 L2 Stream 中为 `0`，不得被改作 capability 数量。

### `ordering`

```json
{
  "method": "fixed",
  "seed": null,
  "turn_order": ["turn1", "turn2"],
  "note": "Pilot preserves the declared source-item order."
}
```

`turn_order` 必须与 `turns[]` 和实际目录顺序完全一致。`method=fixed_seed_shuffle` 时 `seed` 必须是整数；不得在执行时临时重排。

### L2 新增 `capability`

```json
{
  "capability_id": "dataframe_visualization",
  "family": "data_analysis",
  "relation_type": "same_capability_different_input_format",
  "description": "Turns reuse the same visualization capability over different file formats."
}
```

`relation_type` 必须解释 Turn 为什么属于同一 Stream，不能只写 `mixed` 或 `random`。

### L2 新增 `execution_requirements`

```json
{
  "network": "disabled",
  "path_scope": "agent_workspace",
  "max_interactions": 3,
  "max_tool_calls_per_interaction": 20,
  "turn_timeout_seconds": 900,
  "max_output_tokens": 12000
}
```

这些字段是 Runtime 必须遵守的运行边界，不表示 L2 数据组实现 Sandbox。

### `turns[]`

每项至少包含：

```text
turn_id
turn_index
turn_type
turn_origin
sequence_role
source_task_id
```

数组顺序、目录名、`turn_id` 和 `turn_index` 必须一致。

`sequence_role` 在 V1 中收紧为：

| role | 含义 | 当前 V3 Demo 用途 |
|---|---|---|
| `learning` | Stream 中首次建立或暴露该能力的 Turn | 每条 Stream 的第一个 Turn |
| `application` | 后续同能力普通应用 Turn | 主体任务序列 |
| `probe` | 有意放置的 held-out sibling / controlled variant / 周期检查，用于观察前序经验是否迁移 | 当前 V3 每个完整 10-Turn bin 的末尾放置一个 probe |
| `replay` | 未来用于显式 replay/retention 检查的重复或近重复任务 | V3 Demo 暂不使用 |
| `noise` | 未来用于显式干扰项或无关任务 | V3 Demo 暂不使用 |

当前合同不要求每个 Stream 都包含 `replay` 或 `noise`。若未来要正式测 forgetting/retention，必须先定义对应任务构造与统计口径，再把相关 Turns 标为 `replay`；不得只靠改标签声称已经测了 forgetting。

`sequence_role` 是序列分析标签，不自动选择 Judge Prompt 或评分 Rubric。Turn 的客观正确性仍由自己的 native verifier、`evaluation.checks`、rubrics 与 GT 决定。`probe`/`replay` 必须通过既有 `capability_relation.parent_turn_ids` 引用更早 Turn；V1 不新增重复的 `probe_spec`，也不定义 forgetting 指标。

## 6. `task.json` 顶层字段

```text
schema_version
stream_id
turn_id
turn_index
turn_type
turn_origin
sequence_role
interaction_arm
source_basis
capability_relation
agent_input
workspace_transition
user_simulator
evaluation
```

除 `capability_relation` 和 `workspace_transition` 外，其余基础字段沿用 L1 语义。

`evaluation` 可以显式路由评分类型：

- 未声明 `evaluation_profile`：兼容既有 General Task，使用 `l2_judge/1.0`；
- `evaluation_profile=general`：新建或迁移后的普通任务；
- `evaluation_profile=auto_research`：连续 native metric 驱动的研究任务，必须提供 `metric_profile`、Judge Prompt V2、AutoResearch Domain Rubric route 和单一 `auto_research_overall` ordinal check。

Profile、Rubric 和 metric 的完整字段与组合规则见 `L2_EVALUATION_SPEC_zh.md`。这些字段属于 evaluator-private `evaluation`，不能进入 Task Agent、learner 或 User Simulator 的可见上下文。

## 6.1 Reference run conditions 与 Workspace 语义

为了与 L1 的 learning curve / evolution gain 语义对齐，Runtime 至少需要支持三类 reference run condition：

| condition | SUT persistent state | L2 Workspace / Sandbox | 用途 |
|---|---|---|---|
| `reset` | 每个 Turn 前清空，不保留 memory、skill、learner state 或 harness update | 每个 Turn 前从该 Stream 的初始 `agent_workspace` 重建，并只应用当前 Turn 声明的 `workspace_transition` | 排除顺序和任务难度影响，得到无自进化参照 |
| `frozen` | Stream 开始时固定，之后不允许更新；可以读取同一份初始能力/提示/工具配置 | 同一 Stream 内继续保留 Workspace 变化，并按 Turn 应用 `workspace_transition` | 区分“环境持续变化”与“SUT 自进化” |
| `evolving` | 同一 Stream 内允许按被测方法更新 memory、skill、策略或 harness state | 同一 Stream 内继续保留 Workspace 变化，并按 Turn 应用 `workspace_transition` | 被测 self-evolving 主条件 |

这里的 `reset` 是 L2 特化定义：不只是清空 SUT memory，也要从冻结的 Stream seed 重建一份干净 Workspace，再为当前 Turn 重新初始化。若某个 Turn 的任务语义依赖前序文件副作用，它必须把当前 Turn 所需输入通过 `workspace_transition` 声明出来；否则该 Turn 不能用于严格的 Reset 对照。

`frozen` 与 `evolving` 都使用同一条固定 `turn_order`。区别只在 SUT persistent state 是否允许更新；Benchmark Harness 不得替 frozen condition 执行学习、记忆写入或 skill update。

## 7. `source_basis`

必填：

```text
source_dataset
source_task_id
parent_source_task_ids
mapping_rule
source_revision
source_path
source_selector
source_metadata
```

规则：

- 直接映射源任务时，`source_task_id` 非空；
- 派生 Turn 时，`source_task_id=null`，`parent_source_task_ids` 非空；
- `source_revision` 应是不可变 revision；
- `source_path` 只用于 provenance，不得成为运行依赖；
- 对外 Task schema 不要求 license 字段。

## 8. `capability_relation`

```json
{
  "capability_id": "dataframe_visualization",
  "relation_type": "application",
  "parent_turn_ids": ["turn1"],
  "expected_transfer": "Reuse chart construction and validation lessons while adapting to a new input format."
}
```

`parent_turn_ids` 只能引用当前 Stream 内更早的 Turn。

`learning` Turn 可以使用空 `parent_turn_ids`。`probe` 与 `replay` Turn 必须至少引用一个更早 Turn；`relation_type` 和 `expected_transfer` 必须解释后续任务在测试前序哪项能力，不能只重复 `sequence_role` 字面值。

## 9. `agent_input`

必填字段：

```text
agent_workspace
workspace_persistence
messages
available_tools
assets
```

### Messages

`messages[]` 包含 `role=user|assistant` 与非空 `content`。初始 Interaction 使用 messages 中最后一个 user message 作为当前用户输入。

### Tools

`available_tools` 为 `tools.json` 中的工具名数组。工具数量不需要新字段；调用上限由 Stream 的 `execution_requirements` 决定。

### Assets

```json
{
  "path": "agent_workspace/data/train.csv",
  "kind": "file",
  "mime_type": "text/csv",
  "sha256": "<64 lowercase hex>",
  "description": "Training split visible to the Task Agent."
}
```

规则：

- `path` 必须位于 `agent_workspace/`；
- 文件必须已经在 seed Workspace 中，或由当前 Turn 的 transition 创建；
- 大文本、多文件和二进制材料使用 assets，不强制内嵌进 message；
- asset 不能指向 `evaluation_assets/`。

## 10. `workspace_transition`

### `continue`

```json
{
  "mode": "continue",
  "operations": [],
  "preserve_unrelated_files": true
}
```

### `continue_with_patch`

```json
{
  "mode": "continue_with_patch",
  "operations": [
    {
      "op": "copy",
      "source": "assets/test.csv",
      "target": "agent_workspace/data/test.csv"
    }
  ],
  "preserve_unrelated_files": true
}
```

V1 只允许：

| 操作 | 规则 |
|---|---|
| `copy` | source 必须是当前 Turn `assets/` 下的文件或目录；target 必须不存在。目录复制保留内部相对路径和完整内容 |
| `create` | 创建 UTF-8 文件；target 必须不存在 |
| `apply_patch` | target 必须已存在；patch 必须唯一、完整应用 |

安全默认值：

- 禁止隐式覆盖；目标存在即 transition error；
- 禁止 delete、move、rename 和执行脚本；
- 所有 target 必须位于当前 Stream `agent_workspace/`；
- 操作按数组顺序执行；任何失败使整个 transition 失败；
- transition 是 evaluator/runtime 初始化，不是 Agent 行为，不计入 Agent 得分；
- Runtime 必须保存 transition 前后 manifest/hash 和 applied operations；
- 未声明文件必须保留。

## 11. Interaction 状态机

一个 Turn 最多 3 个 Interaction：

```text
Interaction n 开始
  ← 初始 user message（n=1）或 User Simulator retry message（n>1）
Task Agent 完整执行
冻结可评测结果
强制 native verifier
LLM Judge
确定性聚合
  ├─ success：Turn 终止
  ├─ failure 且 n < max：User Simulator → Interaction n+1
  └─ failure 且 n = max：Turn terminal failure
```

上图中的 `failure` 是 General Profile 的默认 continuation 条件。AutoResearch Profile 使用 `invalid_or_unverifiable_submission_only`，并把 `user_simulator.trigger` 改为 `submission_invalid_or_unverifiable_and_budget_remaining`：合法但 native metric 较低的 submission 直接结束 Turn 并记录离散分，不因低分进入 User Simulator；submission 缺失、非法或不可验证且仍有预算时才进入下一 Interaction。

不存在 `awaiting_user`。如果 Agent 只提出问题而未完成任务，该 Interaction 仍然结束、评测并通常失败；User Simulator 可在下一 Interaction 的用户输入中回应。

工具 calls/results 属于当前 Interaction。API transport retry、verifier retry、Judge repair 和 Simulator 模型 retry 不计 Benchmark Interaction。

Evaluator 控制面错误不属于 Agent failure。Verifier/Judge/Simulator 在允许的 Runtime retry/repair 耗尽后，Runtime 必须记录 `evaluation_error`，令当前 Turn 的分数为 `null`，不得调用 User Simulator，也不得把该错误改写为 Agent 的 `0` 分。当前 Run 是停止还是继续由 Runtime run configuration 决定；无论选择哪种策略，都必须保留错误阶段、重试次数和原始错误记录。

## 12. LLM User Simulator

```json
{
  "type": "llm_post_failure",
  "trigger": "interaction_unsuccessful_and_budget_remaining",
  "prompt_version": "l2_user_simulator/1.0",
  "simulator_prompt": "<full common prompt>",
  "inputs": [
    "agent_input",
    "interaction_transcript",
    "latest_agent_response",
    "bounded_evaluation_feedback"
  ],
  "temperature": 0,
  "output_schema": "l2_user_simulator_output/1.0",
  "max_generated_messages": 2
}
```

规则：

- 只在失败且尚有 Interaction 预算时调用；
- 每次调用只输出一条用户角色消息，Runtime 自动开始下一 Interaction；
- `max_generated_messages=2` 是一个 Turn 内的累计上限，而不是单次调用的输出条数；它对应初始 Interaction 之后最多两次自动 retry；
- 不允许访问 GT、rubric 权重、hidden tests、private verifier diagnostics 或未来 Turn；
- 不得给出精确答案、代码 patch、gold 参数或逐步修复方案；
- 不得改变原任务目标或增加无依据的新要求；
- Prompt 在所有 Turn 中相同，Task-specific 信息来自公开输入和 bounded feedback。

## 13. 可见性边界

### Task Agent 可见

- `agent_input`；
- 当前可写 `agent_workspace`；
- 本 Interaction 的 tool results；
- User Simulator 生成的用户消息。

### User Simulator 可见

- `agent_input`；
- 已完成的公开 interaction transcript；
- 最新 Agent response；
- bounded evaluation feedback。

### Evaluator/Judge 可见

- 完整 `evaluation`；
- native verifier 与 private evaluation assets；
- 全部 response/trace/tool/state/workspace evidence；
- User Simulator 原始输入输出日志。

### 严禁泄漏

GT、rubrics、weights、expected evidence、private diagnostics、Judge rationale、source reserve 和 future Turns 不得进入 Task Agent、learner 或 User Simulator 的 prompt/state/workspace。

## 14. 数据组与 Runtime 组边界

### L2 数据组交付

- versioned JSON 数据与 schema；
- 通用 Judge/User Simulator prompts；
- Workspace seed/transition；
- Agent-visible assets；
- checks、rubrics、GT 和 expected evidence；
- native verifier 程序或可执行适配资产及标准接口声明；
- known-correct/known-broken verifier fixture；
- provenance 与内部合规记录。

### Runtime/Baselines 组实现

- Sandbox/容器与进程隔离；
- Harness、tools 和模型调用；
- transition executor；
- verifier executor；
- Judge/Simulator prompt 渲染与 API 调用；
- feedback projection；
- score aggregation、run logs 和 baseline 实验。

## 15. 数据验收

正式 Demo 必须满足：

1. manifest、stream、task 全部通过 JSON Schema；
2. 每个 Turn 有 native verifier 和 known-correct/known-broken fixture；
3. checks 权重之和为 1；
4. native verifier 必须先产生合法标准 result，且完整 result 进入 Judge context；
5. Judge 与 Simulator prompts 的全部占位符可渲染；
6. public/private 路径无泄漏；
7. transition source 存在、target 合法且无隐式覆盖；
8. parent Turn 只指向更早 Turn；
9. 所有发布运行资产 package-local；
10. Runtime 组能够加载至少两个代表性 Turn 做接口 smoke。

## 16. Contract V1 Pilot 的 Prompt 路由范围

本版交付只定义两条可执行路由：

- `evaluation_profile=general` 必须使用 `l2_judge/1.0`，且不使用 `metric_profile`；
- `evaluation_profile=auto_research` 必须使用 `l2_judge/2.0`、结构化 `metric_profile` 与 AutoResearch Domain Rubric。

General Profile 若未来需要 Domain-specific Rubric，必须先在后续兼容版本中补齐对应 schema、rubric route 和验证样例，不能在 Contract V1 Runtime 中自行组合 General + Judge V2。
