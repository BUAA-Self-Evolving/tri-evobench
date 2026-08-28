# L1-Life：自包含的 Agent 自进化事实流数据

L1-Life 是 Tri-EvoBench 的 L1 FactStream 数据集，用于评测 Agent 是否能够在连续任务流中学习、保存、修正和再次使用用户事实。数据包含 **10 条相互隔离的 stream，每条 60 个 turn，共 600 个 turn**。

数据基于 `tencent/CL-bench-Life` 的 405 条任务构造，正式使用其中 380 条，覆盖率为 93.8%；剩余 25 条冻结为后续 hidden/calibration reserve。每条被选中的原始任务严格映射到一个 `turn_origin=source_task` 的 turn，另外 220 个 turn 是基于前序任务构造的事实修正、延迟问答和长延迟 replay，不会重复消耗新的源任务。

## 数据设计

L1-Life 是完全自包含的数据包：

- 没有 `source_records/` 目录；
- 没有 `source_record_refs` 或 `source_rubrics_from`；
- 每条 stream 包含 `turn1` 至 `turn60`，每个 turn 文件夹内有一个自包含的 `task.json`；
- 原始上下文、对话、当前用户请求、rubrics、评分 checks、期望状态和工具终态均直接内嵌在对应 turn 中；
- 每个 turn 都内嵌完整英文 `evaluation.judge_config.judge_prompt`；
- 每个 turn 都有 behavioral rubrics；需要回答源任务的 turn 同时内嵌原始 source rubrics；
- 每个 turn 都有 `evaluation.gt`；fact QA 和 correction 提供 canonical response，其余开放任务提供完整结构化 GT 与明确占位说明；
- Agent 可见的 `messages` 使用正常用户语言，不出现数据集名称；
- 每条 stream 拥有一个独立且持久的 `agent_workspace`。

## 数据规模

| 项目 | 数量 |
|---|---:|
| Streams | 10 |
| Turns / stream | 60 |
| 总 turns | 600 |
| 一对一源任务 turns | 380 |
| 自进化衍生 turns | 220 |
| 事实线程 | 60 |
| 使用的源任务 | 380 / 405 |
| Reserve 源任务 | 25 |

每条 stream 的任务配额为：

| Turn 类型 | 每流数量 | 作用 |
|---|---:|---|
| `fact_disclosure` | 6 | 首次披露并学习 scoped fact |
| `fact_correction` | 6 | 新值 supersede 旧值 |
| `preference_application` | 12 | 在新任务中选择性激活长期偏好 |
| `fact_qa` | 12 | 不重新提供历史上下文的延迟问答 |
| `fact_dependent_tool` | 12 | 将长期事实转换为工具参数和 workspace 操作 |
| `distractor` | 8 | 完成一次性任务，同时避免长期记忆污染 |
| `long_delay_replay` | 4 | 测量长期保留、陈旧值和 replay 能力 |

## 目录结构

```text
l1_life_v2/
├── README.md
├── README_zh.md
├── BENCHMARK_SPEC.md
├── BENCHMARK_SPEC_zh.md
├── EVALUATION_SPEC.md
├── EVALUATION_SPEC_zh.md
├── CALIBRATION_PLAN.md
├── LICENSE.md
├── manifest.json
├── tools.json
└── stream-N/
    ├── turn1/task.json
    ├── ...
    ├── turn60/task.json
    ├── stream.json
    ├── facts.json
    └── agent_workspace/
        ├── state/
        ├── tool_state/
        ├── docs/
        ├── artifacts/
        └── iterations/
```

每个 `stream-N/turnK/task.json` 是一个完整、独立可加载的 turn。运行时按 `turn_index=1..60` 顺序读取这些文件，不需要再打开任何 source 数据文件。

## 原始任务与衍生任务

### 原始任务 turn

`turn_origin=source_task` 表示该 turn 直接内嵌一条唯一源任务。它包含：

- 完整 `agent_input.messages`；
- 唯一 `source_task_id`；
- 原始 JSONL 行号和 SHA-256；
- 原始 category / subcategory；
- 直接内嵌的 source rubrics；
- 当前 stream 所需的事实披露、偏好应用或工具操作要求。

### 自进化衍生 turn

`turn_origin=derived_evolution_turn` 表示该 turn 不消耗新的源任务，而是引用前面已经出现的事实线程。它包括：

- `fact_correction`；
- `fact_qa`；
- `long_delay_replay`。

这类 turn 的 `source_task_id` 为 `null`。如果评分需要历史上下文，历史 messages 和 rubrics 会直接内嵌在密封的 `evaluation.reference_context` 中，但不会发送给 Agent。

## Agent 可见边界

运行时只应向 Agent 提供：

- `agent_input.messages`；
- `agent_input.available_tools` 对应的工具 schema；
- user simulator 在交互中按规则给出的回复；
- 当前 stream 的可写 `agent_workspace`。

以下内容必须对 Agent 和 evolution learner 保持密封：

- `evaluation`；
- `facts.json`；
- source rubrics；
- expected state；
- expected tool call；
- expected workspace effect；
- check weights；
- judge 的完整内部判分。

## Persistent Agent workspace

每条 stream 只有一个 `agent_workspace`，供该 stream 的 60 个 turn 持续使用。不同 stream 的 workspace 不得共享。

- `state/seed_state.json`：不可变初始记忆快照；
- `state/current_state.json`：运行期间可写的长期状态；
- `tool_state/seed_state.json`：工具环境初始状态；
- `tool_state/current_state.json`：文件、提醒、草稿和标签的当前终态；
- `docs/`：真实文件操作目录；
- `artifacts/manifest.json`：产物索引；
- `iterations/manifest.json`：turn 运行日志索引。

Evolving 条件在同一 stream 内持续携带 current state；Reset 条件在每个 turn 前恢复 seed state；Frozen 条件可读取 seed state，但不允许持久写入。

## 文件与工具任务

工具 turns 构成真实的连续 workspace 操作链：

```text
创建文件 → 追加内容 → 定点编辑 → 再次追加 → 重命名
       → 用户修正默认 workspace
       → 在新目录创建文件 → 使用最新项目名再次编辑
```

正式数据包含 `file.write_text`、`file.append_text`、`file.edit_text`、`file.rename` 和 `calendar.create_reminder` 等操作。评分不仅检查工具名和参数，还检查文件是否存在、旧内容是否保留、重命名前后路径状态以及是否误改无关文件。

## 评测

所有语义评分统一使用 LLM-as-judge。每个 turn 都包含带权 checks，judge 只输出单项 `1 / 0.5 / 0 / null` 判断；最终总分应由评测方根据非 `NA` checks 重新计算。详细规则见 `EVALUATION_SPEC_zh.md`。

## 构建与验证

Baseline 和统一评测框架的工程实现应先阅读 `BASELINE_EVALUATION_GUIDE_zh.md`。该文档定义任务投影、可见性隔离、六种 baseline、workspace 生命周期、user simulator、tools、judge prompt 渲染、分数重算、反馈投影、统计和结果文件规范。

数据由 `scripts/build_l1_life_v2.py` 确定性生成：

```bash
python scripts/build_l1_life_v2.py --check
```

验证覆盖 10×60 个 `turnK/task.json`、380 个唯一 source task IDs、Agent messages 数据集名称与原始 `<|TASK|>` 分隔符泄漏、source path refs、workspace 完整性和 JSON 可解析性。
