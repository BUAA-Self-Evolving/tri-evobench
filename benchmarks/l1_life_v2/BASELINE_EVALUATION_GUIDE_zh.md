# L1-Life Baseline 与评测框架实施指南

本文档面向负责实现 baseline、统一 runtime、LLM judge、结果聚合和实验复现的工程同学。目标是确保不同方法在完全相同的 stream、turn、预算、workspace 和评分协议下运行，避免把额外上下文、gold 泄漏、重复尝试或不一致的环境重置误算为 Agent self-evolution。

数据字段的权威定义见 `BENCHMARK_SPEC_zh.md`，LLM judge 的权威定义见 `EVALUATION_SPEC_zh.md`。本文只说明评测框架应如何正确使用这些数据。

## 1. 评测对象与基本循环

L1-Life 的统计单位是独立 stream，时间步是 turn：

```text
加载 stream seed workspace
  ↓
按 turn_index=1..60 顺序运行
  ↓
当前 Task Agent 与 user simulator / tools 交互
  ↓
保存 final response、完整轨迹、工具调用、state/workspace diff
  ↓
用密封 LLM judge 评分
  ↓
只生成允许返回 learner 的局部 feedback
  ↓
Evolution learner 在 turn 结束后更新一次持久状态
  ↓
进入下一个 turn
```

同一个 turn 内可以有多轮用户澄清和多次工具调用，但 learner 的持久更新只能发生在 turn 完成之后、下一个 turn 开始之前。不能把同题重试当作新的学习 turn。

## 2. 正式数据入口

根目录关键文件：

| 文件 | 框架用途 |
|---|---|
| `manifest.json` | 冻结数据规模、stream 列表、source 覆盖与 prompt/GT 版本 |
| `tools.json` | Mock tool 名称、参数 schema 和操作语义 |
| `stream-N/stream.json` | 该 stream 的顺序索引和任务类型配额 |
| `stream-N/turnK/task.json` | 当前 turn 的完整 Agent 输入与密封评测数据 |
| `stream-N/facts.json` | User simulator / evaluator 的密封事实真值 |
| `stream-N/agent_workspace/` | 该 stream 的 seed/current memory、tool state 和文件系统 |

框架必须通过 `turn_index` 排序，不应使用字符串字典序。错误示例：`turn1, turn10, turn11, ..., turn2`。

推荐加载逻辑：

```python
for stream_no in range(1, 11):
    stream_dir = root / f"stream-{stream_no}"
    tasks = []
    for turn_index in range(1, 61):
        task = load_json(stream_dir / f"turn{turn_index}" / "task.json")
        assert task["turn_index"] == turn_index
        assert task["turn_id"] == f"turn{turn_index}"
        tasks.append(task)
```

## 3. Agent 可见性隔离

Task Agent 和 evolution learner 只能看到：

- `task.agent_input.messages`；
- `task.agent_input.available_tools` 对应的工具 schema；
- 当前 stream 的可写 `agent_workspace`；
- user simulator 在真实交互过程中返回的回复；
- 框架投影后的局部 feedback。

以下字段必须在独立 evaluator process 中读取，不能进入 Agent prompt、memory、skill、retrieval index 或 workspace：

- `task.evaluation` 整体；
- `task.evaluation.rubrics`；
- `task.evaluation.gt`；
- `expected_state`；
- `expected_tool_call`；
- `expected_workspace_effect`；
- `reference_context`；
- `stream-N/facts.json`；
- judge 的完整输出和 rationale；
- reserve source task IDs。

推荐使用两个进程或容器：

```text
Agent runtime process
  只挂载 Agent-visible task projection + writable workspace

Evaluator process
  挂载完整 task.json、facts.json、judge credentials 和只读结果日志
```

只是在代码中约定“Agent 不读 evaluation”不够。如果 Agent 拥有仓库级文件访问权限，它可以直接读取 GT。正式实验必须做文件系统或进程级隔离。

## 4. Agent-visible task projection

框架应从 `task.json` 构造以下只读任务对象：

```json
{
  "stream_id": "l1_life_stream_1",
  "turn_id": "turn5",
  "turn_index": 5,
  "messages": [],
  "available_tools": [],
  "agent_workspace": "<isolated writable path>"
}
```

不要把 `source_basis`、`turn_origin`、`sequence_role` 或 `turn_type` 自动写入用户消息。这些是分析标签，Agent 是否可见应由公开赛道规则统一决定；reference track 建议不提供，以避免 Agent 根据固定位置或标签投机。

`messages` 已经是最终完整输入：

- Source turn 包含完整上下文和最后一个用户问题；
- Stream 的事实/工具要求已经合并进 terminal user message；
- Derived turn 只包含当前自然问题；
- 不需要再拼 source 文件或添加 `<|TASK|>`。

## 5. Workspace 初始化与复制

绝对不能直接在发布数据目录内运行。每次独立 run 都应创建新的工作副本：

```text
run_root/
└── <condition>/
    └── <model>/
        └── stream-N/
            └── agent_workspace/
```

启动一条 stream 时：

1. 递归复制 `stream-N/agent_workspace/`；
2. 确认 `state/current_state.json == state/seed_state.json`；
3. 确认 `tool_state/current_state.json == tool_state/seed_state.json`；
4. 记录 seed workspace 的目录树与文件 hash；
5. 为当前 run 创建独立日志目录。

不同 model、condition、stream seed 和重复 rollout 不得复用同一个 writable workspace。

## 6. Baseline 条件的精确定义

所有条件必须使用完全相同的 Task Agent、模型版本、工具权限、turn 顺序、单 turn token/tool/time budget 和 judge。差别只能来自声明的持久状态策略。

### 6.1 Reset

目的：测量没有跨 turn 持久信息时的逐题能力。

- 每个 turn 前将 memory state 恢复为 `state/seed_state.json`；
- 每个 turn 前将 tool/file workspace 恢复为 tool/workspace seed；
- 不保留历史 transcript、memory note、fact、skill 或 artifact；
- 当前 turn 内允许正常交互和工具操作；
- 当前 turn 结束后保存结果，但下一 turn 不继承。

Reset 会在文件 append/edit/replay 等依赖任务上自然失败，这正是无生命周期状态的对照，不得由框架偷偷补齐前序文件。

### 6.2 Frozen

目的：区分环境连续性与 learner 更新收益。

- Tool/file workspace 可按 stream 顺序保留真实 side effects；
- Learner memory/skill/harness 保持 seed 或只读；
- 禁止将轨迹总结写入持久 learner state；
- 允许 Task Agent 使用当前环境中真实存在的文件，但不能读取 evaluator truth。

报告中必须明确 Frozen 是否保留 tool environment；不得把保留与不保留的结果混为同一 baseline。

### 6.3 Raw-Replay

目的：测量不做结构化自进化、只保留原始历史的效果。

- 同一 stream 的历史 messages、tool results 和 final responses可以进入 replay buffer；
- 不允许手工写入 gold facts、规则或结构化 skill；
- 检索算法和 top-k 必须冻结；
- 计入检索 token、索引大小和延迟；
- 不能把 sealed feedback、rubrics 或 GT 放入 replay buffer。

### 6.4 Evolving

目的：评测被测 self-evolution learner。

- Learner 在每个 turn 结束后读取完整公开轨迹和允许的局部 feedback；
- 每个 turn 只能进行一次持久更新阶段；
- 可更新的 carrier（memory、skill、harness 或 parameter）必须在 run metadata 中声明；
- 保存 state before、state after 和 machine-readable state diff；
- 下一 turn 使用更新后的状态；
- 不得读取 hidden judge 输出、GT 或未来 turn。

### 6.5 Matched-Search

目的：排除 Evolving 只是使用更多 token、rollout 或搜索次数。

- 与 Evolving 匹配累计输入/output token；
- 匹配 tool calls、judge/verifier calls、wall-clock 或 rollout 数；
- 不允许跨 turn 持久化 learner update；
- 预算匹配规则必须在结果 metadata 中记录。

### 6.6 Oracle-Update

目的：验证任务是否存在可学习 headroom，而不是正式参赛方法。

- 只在 turn 结束后使用 `evaluation.gt/state_gt` 构造正确持久更新；
- 只注入当前已经披露或修正的事实；
- 不得注入未来事实、source answer prose、未来 replay 内容或未发生的工具终态；
- 下一 turn 的 Task Agent 仍然不能看到 rubrics/GT；
- Oracle 的 update format 应尽量匹配 Evolving carrier 的表示能力。

## 7. User simulator 执行

当 `task.user_simulator=null` 时，直接执行 messages 中的当前请求。

当存在 simulator 配置时：

1. Task Agent 先看到 `agent_input.messages`；
2. 若 Agent 发出与 `policy` 匹配的相关澄清问题，框架返回一次 `scripted_reply`；
3. 若 Agent 没有询问，不主动披露隐藏事实；
4. 不允许 simulator 改写 scripted truth；
5. 将所有问答按原始顺序写入 `interaction_transcript`；
6. 到达 turn 交互上限后结束，不循环诱导 Agent。

Simulator 不是 judge。它只决定“用户如何披露”，不决定任务是否完成。

## 8. Mock tool 实现

工具 schema 来自 `tools.json`。Reference runtime 应提供确定性、无网络、路径受限的实现。

### 文件工具

- `file.write_text`：默认拒绝覆盖已有文件；
- `file.append_text`：目标必须存在并保留全部旧内容；
- `file.edit_text`：只允许指定 localized edit；
- `file.rename`：旧路径消失、新路径出现、内容 hash 不变；
- `file.read_text`：只读，不产生 state mutation；
- `file.tag_artifact`：只修改 tag/index，不改变文件内容。

所有路径必须：

- 相对于当前 stream workspace；
- 经过规范化后仍位于 workspace 根目录内；
- 拒绝 `..`、绝对路径、符号链接逃逸和跨 stream 路径。

### 日历与消息工具

- `calendar.create_reminder`：记录 calendar ID、title、date，生成唯一记录；
- `message.prepare_draft`：只生成草稿，不发送；
- 未提供发送工具时，任何“已发送”声明都不能形成真实 side effect。

工具调用必须记录：请求、规范化参数、返回结果、错误、调用前后 tool-state diff 和耗时。

## 9. Turn execution record

每个 turn 建议保存一条 JSONL 记录：

```json
{
  "run_id": "...",
  "condition": "Evolving",
  "model_id": "...",
  "stream_id": "l1_life_stream_1",
  "turn_id": "turn5",
  "turn_index": 5,
  "final_response": "...",
  "interaction_transcript": [],
  "tool_calls": [],
  "tool_results": [],
  "state_before": {},
  "state_after": {},
  "state_diff": [],
  "workspace_before": {},
  "workspace_after": {},
  "cost": {
    "input_tokens": 0,
    "output_tokens": 0,
    "tool_calls": 0,
    "wall_clock_ms": 0,
    "persistent_state_bytes": 0
  },
  "error": null
}
```

`workspace_before/after` 可以保存完整 manifest，也可以保存稳定 hash + 文件 metadata + changed files，但必须足以验证 expected workspace effect。

## 10. Judge prompt 渲染

每个 turn 的英文 prompt 位于：

```text
task.evaluation.judge_config.judge_prompt
```

使用 `literal_placeholder_replace`，对以下字段做 JSON 序列化后逐项替换：

```text
stream_id, turn_id, turn_type, turn_origin,
agent_input, final_response, interaction_transcript,
tool_calls, tool_results,
state_before, state_after, state_diff,
workspace_before, workspace_after,
reference_context, rubrics, checks,
expected_state, expected_tool_call,
expected_workspace_effect, gt
```

禁止使用不安全的通用 `str.format()` 直接解析 prompt 中的 JSON 示例花括号。建议只对明确的 `{field_name}` token 做 literal replacement。

渲染时：

- `ensure_ascii=false`；
- 保留数组顺序；
- 不截断 state/tool/workspace evidence；
- `null` 按 JSON null 输出；
- Final response 也应 JSON-escape 后插入，避免 prompt 结构被破坏。

## 11. Rubrics 与 GT 的使用

每个 turn 都包含：

```text
evaluation.rubrics.source_rubrics
evaluation.rubrics.behavioral_rubrics
evaluation.gt
```

### Source rubrics

- Source turns 和 long-delay replay 非空；
- Judge 逐条按 1-based index 评分；
- Fact QA / correction 没有新的 source answer，允许为空；
- 不能把 source rubrics 直接发送给 Task Agent。

### Behavioral rubrics

- 600 个 turn 全部非空；
- 与 `evaluation.checks` 一一对应；
- `evaluation.checks` 是权重单一真相源；
- Judge 使用 behavioral rubrics 了解标准，但聚合时按 checks 权重计算。

### GT

- Fact QA 和 correction 有 canonical response；
- 开放式 source 任务的 canonical response 为 null；
- null 不代表没有 GT，其正确性由 rubric GT、state GT、tool GT 和 workspace GT 定义；
- `placeholder_reason` 必须非空；
- GT 定义验证目标，不是执行证据；
- Oracle 可以使用 state GT 做当前 turn 后更新，普通 baseline 不可见。

## 12. Judge 输出校验

Judge 输出必须是纯 JSON，并覆盖：

- 正确 `stream_id` / `turn_id`；
- 每个 ordinal check 恰好一次；
- 每条 source rubric 恰好一次；
- score 只能是 `1 / 0.5 / 0 / null`；
- 每项包含非空 reason 和 evidence refs；
- 无未知 check ID、重复 ID 或重复 rubric index。

失败处理按 `judge_config.failure_policy`：

1. 首次非法 JSON：带具体 parse error 重试一次；
2. 二次失败：记录 `judge_failure`；
3. Reference track 该 turn 记 0；
4. 不允许静默丢弃失败 turn。

## 13. 分数重算

评测框架不得信任 judge 自报总分。

### Source component

```text
source_component = mean(non-null source rubric scores)
```

### Turn score

对 `mean_source_rubrics` check，使用上面的 source component；其他 check 使用 judge ordinal score：

```text
turn_score
  = Σ(check_weight × applicable_check_score)
    / Σ(applicable_check_weight)
```

如果一个 check 为 null，对权重重新归一化。若全部 checks 都为 null，判定为 invalid judgment，而不是给 0 或 1。

Primary metric 使用连续 turn score。若同时报告 pass rate，必须预先冻结阈值；建议 `turn_score >= 0.8`，并明确标记为辅助指标。

## 14. Feedback 投影

Judge 的完整 `reason`、source rubric 结果、GT 和 expected evidence 只供 evaluator 报告。

允许传给 learner 的 feedback 必须依据 `feedback_policy` 投影为：

```json
{
  "failed_dimensions": ["activation"],
  "observable_deficiency": "The current stored default was not reflected in the tool arguments."
}
```

禁止包含：

- 正确 value；
- source rubric 全文；
- check 权重；
- gold state patch；
- exact tool arguments；
- canonical answer；
- future turn 信息。

## 15. 结果聚合

每个 condition 至少输出：

- 600-turn overall mean；
- 10-stream macro mean；
- per-stream mean；
- per-turn-type mean；
- per-sequence-role mean；
- 每 10 turns learning-curve bin；
- long-delay replay retention；
- stale error rate；
- intrusion error rate；
- judge failure / context overflow rate；
- token、tool、time、state-size cost。

Self-evolution 主增益：

```text
Delta_evo = mean(Evolving) - mean(Reset)
```

同时报告：

- 每条 stream 的 gain；
- 正增益 stream 比例；
- 最坏 stream；
- replay gain；
- intrusion 变化；
- matched-search residual；
- Oracle headroom。

## 16. 统计与置信区间

统计单位是 10 条独立 stream，不是 600 个相关 turns。

推荐：

- 对 stream 做有放回 bootstrap；
- 至少 10,000 次 bootstrap samples；
- 报告 95% confidence interval；
- 成对比较条件时，在同一 stream seed 上计算 paired delta 后 bootstrap；
- 不把同一 stream 内 turns 当作 60 个独立样本计算显著性。

若增加多次 stochastic rollout，应先在同一 stream/condition/model 内聚合，再进行 stream-level 统计。

## 17. 预算与可靠性记录

每次 run 必须冻结并记录：

- Task Agent model/version；
- Evolution learner model/version；
- Judge model/version；
- temperature / seed；
- context window；
- max output tokens；
- 单 turn 与全 stream token budget；
- tool-call budget；
- user interaction limit；
- update budget；
- timeout/retry；
- hardware/runtime commit；
- benchmark manifest hash；
- judge prompt version；
- GT version。

条件之间预算不一致时，不得直接解释为 self-evolution gain。

## 18. Context overflow 与运行错误

- Reference track 不截断、摘要或重排 messages；
- 超出 Task Agent context：记录 `agent_context_overflow`，该 turn 记 0；
- 超出 Judge context：记录 `judge_context_overflow`，该 turn 记 0；
- Tool runtime error：保留错误轨迹并正常评分；
- Framework crash：区别于 Agent failure，修复后从同一 seed 重新运行整个受影响 stream；
- 不允许只删除失败 turns 后重新计算均值。

## 19. 推荐结果目录

```text
results/
└── <release_id>/
    ├── run_config.json
    ├── manifest_sha256.txt
    ├── per_turn.jsonl
    ├── judge_outputs.jsonl
    ├── per_stream.json
    ├── aggregate.json
    ├── costs.json
    ├── errors.jsonl
    └── workspaces/
        └── <condition>/<model>/stream-N/
```

所有结果应能够从 `run_config + frozen data + per_turn logs + judge outputs` 重算，不能只保留最终平均分。

## 20. 最小 Smoke Test

在全量运行前，至少覆盖以下 turns：

- 一个 disclosed fact turn；
- 一个 interactive fact turn；
- 一个 correction；
- 修正前和修正后的 fact QA；
- `file.write_text`；
- `file.append_text`；
- `file.edit_text`；
- `file.rename`；
- workspace 修正后的新文件创建；
- calendar ID 修正前后；
- distractor；
- long-delay replay；
- 一个 source-rubric conflict case；
- 一个 judge invalid-JSON repair case。

Smoke test 必须验证：

- Agent 看不到 sealed 字段；
- state/workspace 生命周期符合 condition；
- judge prompt 所有 placeholders 被替换且无残留；
- rubrics 索引完整；
- 分数可重算；
- feedback 不泄漏 GT；
- 运行两次 seed snapshot hash 一致。

## 21. 常见错误实现

以下实现会使结果无效：

1. 直接把完整 `task.json` 发给 Agent；
2. 允许 Agent 读取 `facts.json` 或 `evaluation.gt`；
3. 只发送最后一句 user message，丢掉源多轮 context；
4. 用字符串字典序运行 turn；
5. 不复制 workspace，导致模型/条件相互污染；
6. Reset 条件仍保留前序文件或 memory；
7. Frozen 与 Reset 的环境持久化定义不一致但混报；
8. 把 source rubrics 或 judge rationale 返回 learner；
9. 将 Agent 的“已写入”文字当作真实 side effect；
10. 不验证 append/edit/rename 的文件终态；
11. Judge 漏评 rubrics 或输出非法连续分数；
12. 相信 judge 自报 total 而不重算；
13. Context overflow 后静默截断；
14. 失败 turn 从分母删除；
15. Evolving 使用额外预算却没有 Matched-Search；
16. 把 600 turns 当作独立统计样本；
17. Oracle 提前注入未来事实；
18. 同题 retry 被计作跨 turn 学习。

## 22. 全量运行前检查清单

- [ ] 已固定 benchmark manifest hash；
- [ ] 已固定 Task Agent、learner 和 judge 版本；
- [ ] Agent/evaluator 文件系统隔离生效；
- [ ] 每个 condition 使用独立 workspace 副本；
- [ ] 10×60 turns 顺序校验通过；
- [ ] 所有 tools 已实现并有状态 diff；
- [ ] User simulator policy 已实现；
- [ ] 600 个 judge prompt 占位符可完整渲染；
- [ ] 600 个 GT 和 behavioral rubrics 可读取；
- [ ] Source-rubric turns 的 rubric index 完整；
- [ ] Judge JSON schema 校验和单次 repair 已实现；
- [ ] Turn score 使用非 NA 权重重算；
- [ ] Feedback projection 不泄漏 gold；
- [ ] Reset/Frozen/Raw-Replay/Evolving/Matched-Search/Oracle 定义已写入 run config；
- [ ] Token/tool/time/state-size 成本均被记录；
- [ ] Stream-level bootstrap 已实现；
- [ ] Smoke test 全部通过；
- [ ] 结果目录足以离线重算主表。

完成以上要求后，评测框架才能将不同系统之间的差异解释为持久学习策略的差异，而不是数据可见性、环境重置、预算或 judge 实现不一致造成的偏差。

