# L1-Life 数据规范

本文档定义 L1-Life 的目录、stream、turn、消息可见性、持久状态和来源映射规范。数据集是一个不依赖外部 source 文件、可以直接交给任务运行方的自包含 L1 事实流数据包。

## 1. 基本单位

- Benchmark 由 10 条独立 stream 组成；
- 每条 stream 包含 60 个有序 turn；
- 同一 stream 内允许持久状态、工具终态和产物跨 turn 保留；
- 不同 stream 必须从独立空 snapshot 开始，禁止共享状态；
- 数据文件只使用 `stream` 和 `turn` 术语。

## 2. 目录规范

```text
l1_life_v2/
├── manifest.json
├── tools.json
└── stream-N/
    ├── turn1/task.json
    ├── ...
    ├── turn60/task.json
    ├── stream.json
    ├── facts.json
    └── agent_workspace/
```

发布数据不允许存在 `source_records/`。每条 stream 必须包含 `turn1` 至 `turn60`，每个 turn 文件夹内必须有一个自包含的 `task.json`。

## 3. `task.json` 结构

```json
{
  "schema_version": "l1_life/2.0",
  "stream_id": "l1_life_stream_1",
  "turn_id": "turn1",
  "turn_index": 1,
  "turn_type": "fact_disclosure",
  "agent_input": {},
  "evaluation": {}
}
```

目录顺序必须与内容一致：`stream-N/turnK/task.json` 内的 `turn_id` 必须是 `turnK`，`turn_index` 必须是整数 K。统一运行时按 `turn_index=1..60` 加载。

## 4. Turn 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | string | 固定为 `l1_life/2.0` |
| `stream_id` | string | 所属持久任务流 |
| `turn_id` | string | `turn1` 至 `turn60` |
| `turn_index` | integer | 1-based 顺序 |
| `turn_type` | enum string | 披露、修正、应用、问答、工具、噪音或 replay |
| `turn_origin` | enum string | `source_task` 或 `derived_evolution_turn` |
| `sequence_role` | enum string | `learning/application/probe/noise/replay` |
| `interaction_arm` | string | `disclosed/interactive/correction/standard` |
| `source_basis` | object | 内嵌来源 metadata，不含文件路径 refs |
| `agent_input` | object | Agent 可见输入与 workspace/tool 配置 |
| `user_simulator` | object/null | 交互式披露的冻结 simulator 规则 |
| `evaluation` | object | 密封 judge checks、rubrics 和期望终态 |

## 5. 一对一来源映射

`turn_origin=source_task` 必须满足：

- `source_basis.source_task_id` 非空；
- source task ID 在 600 turns 中只作为主任务出现一次；
- 完整源 messages 直接嵌入 `agent_input.messages`；
- 对应 rubrics 直接嵌入本 turn 的 `evaluation.reference_context`；
- 不允许同时绑定两条源任务。

`turn_origin=derived_evolution_turn` 必须满足：

- `source_basis.source_task_id=null`；
- `parent_source_task_ids` 指向先前 source turn；
- 不计算为新使用的一条源任务；
- Agent 只获得当前自然请求，不重新获得已隐藏的历史上下文。

正式数据使用 380 个唯一 source tasks，构成 380 个一对一 source turns；另有 220 个 derived turns。剩余 25 个 source tasks 保留为 reserve。

## 6. `agent_input.messages`

`messages` 使用标准角色序列：

```json
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "..."},
  {"role": "user", "content": "..."}
]
```

Source turn 的完整上下文直接内嵌，stream 新增要求合并到最后一个 user message。Memory-only turn 只包含当前 user message。

Agent 可见 messages 中禁止出现数据集名称或“这是 benchmark task”等元信息。来源名称只允许出现在不可作为用户消息发送的 metadata、README、manifest 和 LICENSE 中。

## 7. Agent 可见性

允许提供：

- `agent_input.messages`；
- `available_tools` 对应 schema；
- simulator 的实际对话回复；
- 当前 stream 的 `agent_workspace`。

禁止提供：

- `evaluation` 对象；
- `facts.json`；
- `reference_context`；
- expected facts、tool calls、workspace effects；
- check 权重；
- source rubrics；
- judge rationale 或 state patch。

## 8. Fact model

每条 stream 有 6 条事实线程：2 条 preference、2 条 work fact、2 条 tool fact。每条线程至少包含：

```json
{
  "fact_thread_id": "...",
  "key": "...",
  "scope": "user:...",
  "v1": {"fact_id": "...", "value": "..."},
  "v2": {"fact_id": "...", "value": "...", "supersedes": "..."},
  "exposure_turn": 1,
  "correction_turn": 27
}
```

Learner 不能直接读取该 gold 结构。它只能从 user messages、工具返回、完整交互轨迹和局部反馈中形成自己的持久状态。

## 9. Agent workspace

每条 stream 必须且只能有一个 `agent_workspace`。所有 60 个 turn 的 `agent_input.agent_workspace` 固定为 `agent_workspace`。

Workspace 中：

- seed 文件是不可变重置基线；
- current 文件是运行时可写状态；
- 工具操作必须更新 current tool state 和 artifact manifest；
- 文件写入必须限制在本 stream workspace 内；
- 不允许跨 stream 访问或修改文件；
- `facts.json` 位于 workspace 外，是密封 evaluator truth。

## 10. 文件操作依赖

文件 turns 是有依赖的状态链，不是彼此独立的假调用。后续 append/edit/rename 必须读取前面产生的文件终态。Localized edit 必须保持未编辑正文；rename 必须保证旧路径消失、新路径出现且内容不变；workspace 默认值修正后，后续新文件必须进入新目录，同时保留旧目录的 archived 文件。

## 11. Stream 条件

- Reset：每个 turn 前恢复 seed state；
- Frozen：允许读取 seed，但拒绝持久写入；
- Raw-Replay：允许保留原始历史或检索记录；
- Evolving：允许 learner 在 turn 之间更新持久状态；
- Matched-Search：匹配额外搜索预算；
- Oracle-Update：注入评测侧构造的正确更新，用于测量可学习上限。

这些条件属于运行配置，不改变 L1-Life 的任务数据和 turn 顺序。

## 12. 根目录 `manifest.json` 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema_version` | string | 是 | 当前发布 schema 固定为 `l1_life/2.0` |
| `benchmark` | string | 是 | 固定为 `L1-Life` |
| `source_dataset` | string | 是 | 数据来源名称，仅作 provenance，不进入 Agent messages |
| `source_dataset_record_count` | integer | 是 | 上游总任务数，当前为 405 |
| `used_source_task_count` | integer | 是 | 正式使用的唯一源任务数，当前为 380 |
| `source_coverage_ratio` | number | 是 | `used / total`，当前为 0.938272 |
| `reserved_source_task_count` | integer | 是 | 冻结 reserve 数量，当前为 25 |
| `reserved_source_task_ids` | string[] | 是 | 没有进入正式 turns 的源 task IDs |
| `stream_count` | integer | 是 | 当前为 10 |
| `turns_per_stream` | integer | 是 | 当前为 60 |
| `total_turn_count` | integer | 是 | 当前为 600 |
| `source_task_turn_count` | integer | 是 | 一对一 source turns 数，当前为 380 |
| `derived_evolution_turn_count` | integer | 是 | 衍生 turns 数，当前为 220 |
| `judge_prompt_version` | string | 是 | 内嵌 judge prompt 版本 |
| `judge_prompt_language` | string | 是 | 固定为 English |
| `gt_version` | string | 是 | 结构化 GT schema 版本 |
| `source_task_ids` | string[] | 是 | 380 个正式主 source IDs，必须无重复 |
| `streams` | object[] | 是 | 10 条 stream 的摘要和路径信息 |

必须满足：

```text
used_source_task_count + reserved_source_task_count = source_dataset_record_count
source_task_turn_count + derived_evolution_turn_count = total_turn_count
stream_count × turns_per_stream = total_turn_count
```

## 13. `stream.json` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | `l1_life/2.0` |
| `stream_id` | string | 与该目录全部 turns、facts、workspace 一致 |
| `directory` | string | `stream-1` 至 `stream-10` |
| `profile` | string | 该 user lifetime 的高层场景说明，不作为 gold fact |
| `theme` | string | Source task 选取主题 |
| `turn_count` | integer | 固定为 60 |
| `task_layout` | string | 固定为 `turn{1..60}/task.json` |
| `agent_workspace` | string | 固定为 `agent_workspace` |
| `turn_type_counts` | object | 该流各 turn type 的数量 |
| `fact_thread_count` | integer | 固定为 6 |
| `turns` | object[] | 轻量索引，不重复完整 task 内容 |

`stream.json.turns[]` 中每项至少包含 `turn_id`、`turn_index`、`turn_type`、`turn_origin`、`sequence_role` 和 `source_task_id`。该数组顺序必须与目录中的 60 个 `task.json` 一致。

## 14. `source_basis` 字段

| 字段 | Source turn | Derived turn | 说明 |
|---|---|---|---|
| `source_dataset` | string | string | 来源 provenance |
| `source_task_id` | 非空 string | `null` | 是否直接消耗一条源任务的判定字段 |
| `parent_source_task_ids` | `[]` | 非空 string[] | Derived turn 的父 source turns |
| `mapping_rule` | string | string | 人类可读映射说明 |
| `source_jsonl_line` | integer | 可存在 | 原始 1-based 行号 |
| `source_sha256` | string | 可存在 | 原始 source record 的 canonical SHA-256 |
| `source_metadata` | object | 可存在 | 原始 task ID、category 和 subcategory |

发布数据中不允许出现 `source_record_refs`、`source_rubrics_from` 或依赖本机绝对路径的 source 字段。

## 15. `agent_input` 字段

| 字段 | 类型 | 必填 | Agent 可见 | 说明 |
|---|---|---|---|---|
| `agent_workspace` | string | 是 | 是 | 固定为 `agent_workspace` |
| `workspace_persistence` | string | 是 | 是 | 描述同一 stream 内的持久化边界 |
| `messages` | message[] | 是 | 是 | 当前 turn 的完整对话输入 |
| `available_tools` | string[] | 是 | 是 | 本 turn 允许使用的工具名称 |

Message 对象必须包含：

| 字段 | 类型 | 规则 |
|---|---|---|
| `role` | enum string | `user` 或 `assistant`；如未来加入 system，必须升级规范 |
| `content` | string | UTF-8 非空文本 |

Source turn 的最后一个 message 必须是 user；stream overlay 应合并到该 message。Derived memory-only turn 通常只有一个 user message。Agent messages 禁止包含原始 `<|TASK|>` 分隔符、source 文件路径、rubrics 或数据集身份提示。

## 16. `user_simulator` 字段

`user_simulator` 可以是 `null`，或包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `policy` | string | 何时允许披露隐藏字段 |
| `scripted_reply` | string | 满足 policy 后返回的 grounded 回复 |
| `grounded_fact_id` | string | 回复所依据的 sealed fact |

Interactive turn 中，`scripted_reply` 不能在初始 `agent_input.messages` 中提前出现。Simulator 只能按 `policy` 回复，不能自行发明事实或改变 gold state。

## 17. `evaluation` 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 是 | 固定为 `llm_as_judge` |
| `protocol` | string | 是 | 当前为 `l1_life_llm_judge_v1` |
| `judge_config` | object | 是 | 每个 turn 内嵌的完整英文 judge prompt 与机器配置 |
| `rubrics` | object | 是 | Source rubrics 与 behavioral rubrics |
| `gt` | object | 是 | 每个 turn 的完整结构化 ground truth |
| `checks` | object[] | 是 | 分项评分定义，权重和必须为 1 |
| `expected_state` | object | 是 | 该 turn 期望的 fact/state 约束 |
| `expected_tool_call` | object/null | 是 | 工具名称、参数与调用次数约束 |
| `expected_workspace_effect` | object/null | 是 | 文件、提醒或产物终态约束 |
| `reference_context` | object/null | 是 | Sealed 历史 messages；不得给 Agent |
| `feedback_policy` | string | 是 | 允许返回 learner 的局部反馈边界 |

`reference_context` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `visibility` | string | 固定为 `sealed_evaluator_only` |
| `messages` | message[]/null | Replay 时内嵌不可重新给 Agent 的历史 context；source turn 可为 null |
| `message_source` | string | Judge 应从何处读取 context |

Rubrics 不放在 `reference_context` 内，统一由 `evaluation.rubrics` 提供。

### `evaluation.judge_config`

每个 turn 都必须完整内嵌 judge 配置，不依赖外部 prompt 文件：

| 字段 | 类型 | 说明 |
|---|---|---|
| `prompt_version` | string | 当前为 `l1_life_judge_prompt/1.0` |
| `reference_model` | string | Reference track 的 judge model ID |
| `temperature` | number | 固定为 0 |
| `max_output_tokens` | integer | Judge 最大输出预算 |
| `template_engine` | string | `literal_placeholder_replace` |
| `score_scale` | array | `[0, 0.5, 1, null]` |
| `score_granularity` | string | `ordinal_components` |
| `judge_prompt` | string | 完整英文可执行 prompt |
| `inputs` | string[] | Prompt 必需输入字段 |
| `output_format` | object | 严格 JSON 输出要求 |
| `aggregation` | object | Rubric component 与 turn score 重算规则 |
| `failure_policy` | object | JSON、ID、索引和非法分数失败处理 |

`judge_prompt` 必须为英文，并包含 turn identity、Agent input/output、interaction transcript、tool calls/results、state before/after/diff、workspace before/after、reference context、rubrics、checks 和 expected evidence 的全部占位符。

### `evaluation.rubrics`

```json
{
  "source_rubrics": [],
  "behavioral_rubrics": [
    {
      "rubric_id": "fact_activation",
      "dimension": "activation",
      "weight": 0.15,
      "scoring": "ordinal",
      "criterion": "..."
    }
  ]
}
```

- `source_rubrics`：原始 source 任务 rubrics。Source turns 和 long-delay replay 非空；没有 source answer 的 correction/fact QA 为空数组。
- `behavioral_rubrics`：每个 turn 必须非空，并与 `evaluation.checks` 一一对应；`rubric_id=check_id`、`criterion=instruction`，dimension、weight 和 scoring 必须完全一致。
- Judge prompt 必须显式读取 `rubrics`。存在 `mean_source_rubrics` check 时，`source_rubrics` 不得为空。

### `evaluation.gt`

每个 turn 都必须存在 GT：

| 字段 | 类型 | 说明 |
|---|---|---|
| `gt_version` | string | 当前为 `l1_life_gt/1.0` |
| `status` | string | 当前为 `complete_structured_gt` |
| `answer_gt` | object | 自由文本答案模式、canonical response、可接受值与占位原因 |
| `rubric_gt` | object | 必须与 `evaluation.rubrics` 完全一致 |
| `state_gt` | object | 必须与 `expected_state` 一致 |
| `tool_gt` | object/null | 必须与 `expected_tool_call` 一致 |
| `workspace_gt` | object/null | 必须与 `expected_workspace_effect` 一致 |
| `negative_constraints` | object | stale、额外工具、无关写入和无证据执行声明约束 |
| `notes` | string | GT 使用边界说明 |

`answer_gt` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `mode` | string | exact fact、correction、rubric-defined source answer 等 |
| `canonical_response` | string/null | Fact QA/correction 非空；开放式任务为 null |
| `acceptable_answer_values` | array | 必须准确出现或语义等价的值 |
| `normalization` | string | 答案规范化与等价规则 |
| `placeholder_reason` | string/null | 无唯一自由文本答案时必须非空 |

Fact QA 的 GT 必须包含当前 active fact value；correction 的 GT 必须包含新 active value 和 supersedes 语义。工具 turn 的 canonical free text 可以为空，但 tool/workspace GT 必须完整。Source answer 不应编造唯一参考文案，应由 source rubrics 定义。

## 18. `evaluation.checks[]` 字段

| 字段 | 类型 | 规则 |
|---|---|---|
| `check_id` | string | 单 turn 内唯一 |
| `dimension` | string | 如 `task_success`、`activation`、`stale_control` |
| `weight` | number | `(0,1]`，同 turn 合计必须为 1 |
| `scoring` | enum string | `ordinal` 或 `mean_source_rubrics` |
| `instruction` | string | Judge 可执行、可观察、无歧义的检查说明 |

`mean_source_rubrics` check 必须存在非空 `evaluation.rubrics.source_rubrics`。`ordinal` check 使用 `1 / 0.5 / 0 / null`。自然语言声称不能替代 state/tool/workspace 证据。

## 19. Expected state/tool/workspace 字段

### `expected_state.target_facts[]`

| 字段 | 类型 | 说明 |
|---|---|---|
| `fact_id` | string | 目标 active fact version |
| `key` | string | Stable fact key |
| `value` | any JSON value | 当前期望值 |
| `scope` | string/缺省 | 写入或修正 turn 的作用域 |
| `supersedes` | string/缺省 | Correction 新版本指向旧版本 |
| `stale_value` | any/null/缺省 | 后续 probe 不应作为 current 使用的旧值 |

### `expected_tool_call`

| 字段 | 类型 | 说明 |
|---|---|---|
| `tool` | string | 必须位于 `agent_input.available_tools` |
| `arguments` | object | 必须满足的参数；`non-empty` 表示语义占位约束 |
| `must_call_exactly_once` | boolean | 是否要求恰好一次调用 |

### `expected_workspace_effect`

该对象随工具操作变化，常见字段包括：`operation`、`path`、`old_path`、`new_path`、`must_exist_after`、`old_must_exist_after`、`content_must_be_unchanged`、`prior_content_must_be_preserved`、`preserve_unrelated_files` 和 `former_workspace_files_must_be_preserved`。

## 20. `facts.json` 字段

根对象包含 `schema_version`、`stream_id`、`visibility` 和 `facts`。`visibility` 固定表明只供 evaluator/user simulator 使用。

每条 fact thread：

| 字段 | 类型 | 说明 |
|---|---|---|
| `fact_thread_id` | string | stream 内唯一线程 ID |
| `key` | string | 稳定键 |
| `label` | string | 自然语言名称 |
| `kind` | enum string | `preference`、`work_fact`、`tool_fact` |
| `scope` | string | 当前采用 `user:<stream_id>` |
| `v1` | object | 初始版本 |
| `v2` | object | 修正版本，包含 `supersedes` |
| `exposure_turn` | integer | 首次披露位置 |
| `correction_turn` | integer | 修正位置，必须晚于 exposure |

## 21. Agent workspace JSON 字段

`state/seed_state.json` 与初始 `current_state.json` 必须完全一致，包含：

- `schema_version`；
- `stream_id`；
- `snapshot`；
- `revision`；
- `facts`；
- `fact_history`；
- `memory_notes`。

`tool_state/seed_state.json` 与初始 `current_state.json` 必须完全一致，包含：

- `files`；
- `calendar_reminders`；
- `message_drafts`；
- `artifact_tags`。

运行后 current state 可以变化，但 seed state 不得改变。所有实际 side effects 必须能够通过 current tool state、真实文件或 artifact manifest 复核。

## 22. 跨文件不变量

发布或交给 L2 团队前必须检查：

1. 10 条 stream，每条正好 60 个 `turnK/task.json`；
2. 目录 K、`turn_id=turnK`、`turn_index=K` 三者一致；
3. 所有 `stream_id` 在 task、stream、facts、workspace 内一致；
4. 380 个 `source_task_id` 全部唯一且各映射一个 source turn；
5. 220 个 derived turns 的 `source_task_id` 全为 null；
6. Parent source task 必须早于其 probe/replay；
7. Fact exposure 必须早于 correction，correction 必须早于依赖新值的 probe；
8. 每个 turn 的 check weights 等于 1；
9. 工具名称必须存在于 `tools.json`；
10. Agent messages 中不存在 source refs、sealed rubrics、数据集名称或 `<|TASK|>`；
11. 每流恰好一个 workspace，且不同 stream 不共享路径；
12. manifest 计数与实际文件扫描完全一致。

## 23. 版本与兼容性

任何改变字段语义、可见性边界、turn 索引规则、source 映射或评分权重解释的修改，都必须升级 `schema_version`。仅修正文案且不改变机器语义时可以保持 `l1_life/2.0`，但应更新发布 manifest 或变更记录。L2 团队可以新增 capability 字段，但不应重新解释 L1 的基础字段。
