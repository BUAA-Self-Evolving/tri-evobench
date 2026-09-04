# L2 TaskStream 评测合同 V1

## 1. 目标

L2 使用一个固定评测序列，把源 Benchmark 的客观 verifier 与 LLM Judge 的语义判断统一为一个可复核 Turn score：

```text
冻结 Interaction 证据
  → 强制运行 native verifier
  → 按 evaluation profile 选择 Judge Prompt 与 Rubric route
  → 渲染通用 Judge Prompt，并在固定位置注入 Domain/Benchmark Rubric
  → LLM Judge 把 native result 作为权威证据并输出全部分项结果
  → 校验器校验 Judge 输出
  → 按 checks 权重确定性重算
```

Judge 不能决定是否运行 verifier，不能用主观判断覆盖 verifier 的客观结果，也不能决定最终加权总分。Native result 是 Judge 证据的一部分，不是与 Judge 平行加权的独立 component。

## 2. `evaluation` 字段

```text
type
protocol
evaluation_profile
metric_profile
checks
rubrics
gt
expected_state
expected_tool_call
expected_workspace_effect
reference_context
submission_contract
native_verifier
judge_config
success_policy
feedback_policy
feedback_projection
```

### 固定值

- `type = "llm_as_judge"`
- `protocol = "l2_native_evidence_llm_judge_v1"`

未声明 `evaluation_profile` 的既有 Task 继续按 legacy General Profile 与 `l2_judge/1.0` 解释。新 Task 应显式声明 Profile；`auto_research` 必须使用 Judge Prompt V2。

## 2.1 Evaluation Profile 与 Rubric route

Judge Prompt V2 不为每个 Benchmark 复制完整模板，而是固定按以下顺序组合：

```text
通用 Judge Prompt
  → Domain Rubric
  → 可选 Benchmark-specific Rubric
  → Task metric/check/GT/evidence
```

AutoResearch Task 至少声明：

```json
{
  "evaluation_profile": "auto_research",
  "metric_profile": {
    "profile_id": "mlgym_titanic_accuracy_v1",
    "metric_name": "accuracy",
    "raw_direction": "maximize",
    "raw_metric_required": true,
    "judge_value_source": "native_verifier_result.score",
    "judge_value_semantics": "normalized_higher_is_better_0_1",
    "performance_bands": {
      "partial_min_inclusive": 0.65,
      "full_min_inclusive": 0.80
    }
  }
}
```

`judge_config` 必须使用：

```json
{
  "prompt_version": "l2_judge/2.0",
  "rubric_route": {
    "composition": "common_then_domain_then_benchmark",
    "domain_rubric": {
      "rubric_id": "auto_research",
      "version": "1.0",
      "path": "prompts/rubrics/auto_research_v1.md"
    },
    "benchmark_rubric": null
  }
}
```

Runtime 必须读取 route 指向的版本化 Rubric，并把内容分别填入 Judge Prompt V2 的 `{domain_rubric}` 与 `{benchmark_rubric}` 固定位置。Task 不能通过 `reference_context` 或普通 rubric 偷换 route。

Benchmark-specific Rubric 只在原生 artifact、metric 或可复现性语义确有差异时增加，例如 AIRS 与 MLGym 对 submission 的要求不同。它只能细化 Domain Rubric，不能改变 `0/0.5/1` 输出集合、覆盖 native metric、放宽通用证据规则或披露 sealed data。

### AutoResearch 离散总分

AutoResearch 只声明一个 Judge-owned check：

```json
{
  "check_id": "auto_research_overall",
  "dimension": "task_success",
  "weight": 1.0,
  "scoring": "ordinal",
  "instruction": "Apply the routed AutoResearch rubric to all declared evidence."
}
```

因此 framework 仍按既有确定性聚合实现，但最终 Turn score 必然是 `0`、`0.5` 或 `1`。连续 native metric 只是该 ordinal 判断的强制证据，不与 Judge 分数做算术加权。

## 2.2 Submission contract

Runtime 不得猜测 response、Workspace 产物或 tool trajectory 应如何转换成 verifier 输入。每个 Task 必须声明 `submission_contract`：

```json
{
  "mode": "final_response",
  "payload_kind": "python_source",
  "source_binding": "submission",
  "adapter": {
    "owner": "native_verifier",
    "type": "source_specific",
    "entrypoint": "evaluation_assets/native_semantics.py",
    "version": "sra_submission_adapter/1.0"
  },
  "audit": {
    "retain_raw_submission": true,
    "retain_adapted_submission": true,
    "hash_algorithm": "sha256"
  },
  "failure_semantics": {
    "empty_or_invalid_submission": "task_failure",
    "adapter_error": "verifier_error"
  }
}
```

- `mode` 只能是 `final_response`、`workspace_artifact` 或 `tool_trajectory`；
- `source_binding` 必须精确引用当前 `native_verifier.input_bindings[]` 中同名 binding；
- Runtime 只负责按 binding 冻结并转交原始 submission，不实现 source-specific parser；
- `adapter.owner=native_verifier`。代码提取、轨迹解析、格式适配等逻辑随 Task 的私有 verifier 资产交付并版本化；
- `identity` adapter 用于无需转换的 Workspace artifact，entrypoint 必须为 `null`；
- 原始 submission 必须审计留存。若声明保留 adapted submission，Runtime/verifier integration 必须保留适配后载荷或其可复核哈希；
- 空、语法非法或不满足提交格式的 Agent 输出属于普通 `task_failure`；adapter 自身 crash、超时或合同违规属于 `verifier_error`。

## 3. Native verifier 合同

每个 Reference Task 必须提供：

```json
{
  "required": true,
  "verifier_id": "example_native_verifier",
  "entrypoint": "evaluation_assets/verifier.py",
  "input_bindings": [
    {
      "name": "submission",
      "source": "workspace",
      "path": "agent_workspace/submission.csv"
    },
    {
      "name": "ground_truth",
      "source": "evaluation_assets",
      "path": "evaluation_assets/ground_truth.csv"
    }
  ],
  "request_schema": "l2_native_verifier_request/1.0",
  "result_schema": "l2_native_verifier_result/1.0",
  "timeout_seconds": 300,
  "network": "disabled",
  "authority": "objective_evidence"
}
```

规则：

- verifier 在 evaluator 隔离域执行；
- Task Agent、learner 和 User Simulator 不得读取 entrypoint、private inputs 或 diagnostics；
- verifier 输入只来自冻结的当前 Interaction 结果、当前 Workspace snapshot 和声明的 evaluation assets；
- verifier 不得访问网络；
- 输出必须通过 `l2-native-verifier-result.schema.json`；
- Runtime 按 `input_bindings` 解析输入后，使用 `l2-native-verifier-request.schema.json` envelope 传给 entrypoint；`bindings` 的键必须与当前 Task 声明精确一致。Interaction/state 中的 JSON 值使用 `{"kind":"inline","value":...}`，Workspace 或 evaluation asset 使用 `{"kind":"path","path":"<package-relative path>"}`；
- timeout、crash 或非法输出记录为 verifier error，不得伪装成 Agent 的普通失败；Reference score 按 `success_policy` 处理；
- 同一 Task、相同 snapshot 与 verifier version 必须得到相同结果。

## 4. 标准 native verifier 输出

```json
{
  "schema_version": "l2_native_verifier_result/1.0",
  "verifier_id": "example_native_verifier",
  "status": "pass",
  "score": 0.86,
  "checks": [
    {
      "check_id": "submission_schema",
      "status": "pass",
      "score": 1.0,
      "evidence": ["submission.csv contains required columns"]
    }
  ],
  "artifacts": [
    {
      "path": "agent_workspace/submission.csv",
      "exists": true,
      "sha256": "<64 lowercase hex>"
    }
  ],
  "public_summary": {
    "result_category": "pass",
    "observable_deficiencies": []
  },
  "private_diagnostics": {},
  "error": null
}
```

`score` 必须是源 Benchmark 分数经声明方法归一化后的 `[0,1]` 连续值。若原生结果为 binary，则使用 `0` 或 `1`。

AutoResearch 的合法 verifier result 还必须保留原始 metric：

```json
"metric": {
  "name": "accuracy",
  "raw_value": 0.742,
  "raw_direction": "maximize",
  "normalized_value": 0.605
}
```

`metric.normalized_value` 必须与顶层 `score` 一致。`raw_value` 用于审计和未来重新校准 performance bands；Judge 使用 `metric_profile.judge_value_source` 指向的顶层归一化 `score` 进行分档，不得自行重算归一化。General Profile 的 verifier result 可以省略 `metric`。

`public_summary` 可用于 feedback projection；`private_diagnostics` 只能进入 evaluator/Judge audit，不得直接进入 User Simulator。

## 5. Checks 与通用 Judge 评分

每个 `evaluation.checks[]` 包含：

```text
check_id
dimension
weight
scoring
instruction
```

所有 checks 都由 Judge 评分。推荐用一组跨任务通用的粗粒度 checks；Task-specific 正确性由 native result、source rubrics、GT 和 expected effects 提供，不通过另写一份 Judge Prompt 表达。

### Task completion

```json
{
  "check_id": "task_completion",
  "dimension": "task_success",
  "weight": 0.6,
  "scoring": "ordinal",
  "instruction": "Use the native verifier result as authoritative evidence for objective completion and combine it with the declared GT and observable end state."
}
```

`0.6` 只是示例配置，不是 L2 通用权重。Native failure 若证明客观目标未完成，`task_completion` 必须记 `0`；只有 native evidence 明确支持实质性部分完成且 rubric 允许 partial credit 时，才可记 `0.5`。

### 其他通用 ordinal checks

```json
{
  "check_id": "side_effect_control",
  "dimension": "safety",
  "weight": 0.15,
  "scoring": "ordinal",
  "instruction": "No unrelated files, state, or external side effects were modified."
}
```

Judge 只能输出 `1/0.5/0/null`。

### Source-rubric check

```json
{
  "check_id": "source_answer",
  "dimension": "task_success",
  "weight": 0.2,
  "scoring": "mean_source_rubrics",
  "instruction": "Score every source rubric and use their non-null mean."
}
```

Judge 逐条输出 `source_rubric_scores`，框架计算非 null 均值。

所有 checks 权重之和必须为 1。

AutoResearch 是例外 Profile，而不是另一套 native/Judge 加权公式：它使用一个权重为 `1` 的 `auto_research_overall` check，将提交有效性、可复现性、约束遵循和 native performance band 统一映射到 `0/0.5/1`。细分判断保存在 reason、evidence refs、source rubric scores 和审计记录中，不产生第二个平行总分。

## 6. Rubrics 与 GT

沿用 L1：

```json
"rubrics": {
  "source_rubrics": [],
  "behavioral_rubrics": []
}
```

每个 behavioral rubric 必须与同 ID check 一致。每个 Turn 必须提供结构化 `gt`：

```text
gt_version
status
answer_gt
rubric_gt
state_gt
tool_gt
workspace_gt
negative_constraints
notes
```

开放式任务允许 `canonical_response=null`，但必须由 rubrics 和结构化 expected effects 完整定义正确性。

## 7. Judge 输入

Judge 接收：

```text
stream_id
turn_id
turn_type
turn_origin
interaction_index
agent_input
final_response
interaction_transcript
tool_calls
tool_results
state_before
state_after
state_diff
workspace_before
workspace_after
workspace_diff
workspace_transition_record
native_verifier_result
execution_error
resource_usage
reference_context
rubrics
checks
expected_state
expected_tool_call
expected_workspace_effect
gt
```

Judge Prompt V2 另外接收：

```text
evaluation_profile
metric_profile
domain_rubric
benchmark_rubric
```

Prompt 使用 literal placeholder replacement；所有值先 JSON 序列化，不使用通用 `str.format()` 解析 Prompt 中的示例花括号。

`judge_reference.py` 是可执行参考：它实现模板渲染、Judge 输出 Schema/identity/精确覆盖校验，以及 weighted non-null aggregation。生产 Runtime 可以使用不同语言重写，但行为必须与参考测试向量一致。

## 8. Judge 职责

Judge：

- 对所有 checks 逐项判断；
- 对 source rubrics 逐条判断；
- 结合 native verifier 结果解释任务完成情况；
- 检查 Agent 声称与可观察执行证据是否一致；
- 检查无关副作用、路径越界、未授权工具和任务要求覆盖；
- 输出简短、可复核 evidence refs。
- 在 AutoResearch Profile 中，按声明的 performance bands 解释 native score，并对唯一 `auto_research_overall` check 给出离散分；

Judge 不得：

- 把 native score 当作独立加权 component 或自行修改 verifier result；
- 估算、重算或忽略 AutoResearch 的 native metric；
- 输出可信 weighted total；
- 运行 verifier 或任意程序；
- 用自然语言合理性覆盖客观 verifier failure；
- 把 GT 当作已经执行的证据；
- 生成可直接反馈给 Agent 的 gold-bearing 修复步骤。

## 9. Judge 输出

```json
{
  "stream_id": "example_stream",
  "turn_id": "turn1",
  "interaction_index": 1,
  "check_scores": [
    {
      "check_id": "side_effect_control",
      "score": 1,
      "reason": "Only the requested submission file changed.",
      "evidence_refs": ["workspace_diff.changed_files[0]"]
    }
  ],
  "source_rubric_scores": [],
  "rationale": "The artifact passed native evaluation and no unrelated side effects were observed."
}
```

`check_scores` 必须恰好覆盖所有 declared checks。

## 10. Judge 输出校验与 repair

校验：

- identity 与 Interaction index 一致；
- LLM checks 无缺失、重复或未知 ID；
- source rubric indices 完整且无重复；
- score 只能为 `1/0.5/0/null`；
- reason 与 evidence refs 非空；
- 无未知顶层字段；
- 无 weighted total。

首次非法 JSON：携具体 parse/schema error 只重试一次。第二次失败记录 `judge_failure` 并归类为 `evaluation_error`；当前 Turn 分数为 `null`，不得把它记为 Agent failure，也不得据此调用 User Simulator。Native result 仍保留为审计证据，但不单独形成最终分数。

Judge repair 是 API/runtime retry，不增加 Benchmark Interaction。

Judge 输出必须通过 `l2-judge-output.schema.json`；除了 Schema 校验，Runtime 还必须按当前 Task 检查 LLM-owned check ID 与 source rubric index 的精确覆盖关系。

## 11. 确定性聚合

对每个 check：

- `ordinal`：使用 Judge 的 `1/0.5/0/null`；
- `mean_source_rubrics`：使用非 null source rubric mean。

```text
turn_score
  = Σ(weight_i × component_i)
    / Σ(non_null_component_weights)
```

全部 component 为 null 时，评测无效。不得使用 Judge 自报总分；不得把 earlier Interaction 的较高分替代 latest Interaction。

General Profile 的聚合结果可能是连续小数，因为它由多个 Judge-owned checks 加权平均得到；这与 L1 Demo 的“离散分项 + 加权总分”语义一致。若某个 Profile 需要最终 Turn score 也严格落在 `0/0.5/1`，应像 AutoResearch 一样只声明一个权重为 `1` 的 ordinal check，或在后续合同版本中显式增加总分量化规则。当前 V1 不在 General Profile 里额外量化总分，避免丢失 side-effect / evidence-compliance 等分项信息。

## 12. Success policy

```json
{
  "native_verifier_requirement": "valid_result_before_judge",
  "turn_score_threshold": 0.8,
  "judge_failure_blocks_success": true,
  "interaction_selection": "latest_only"
}
```

Turn success 必须同时满足：

1. native verifier 已成功执行并产生合法 result；`status=error` 属于 evaluation error，不进入普通 Judge 评分；
2. Judge 未发生最终失败；
3. `turn_score >= threshold`。

`0.8` 仅是示例/Pilot 配置，不是所有 L2 Stream 的统一阈值。Native `status=fail` 可以作为合法 Judge 输入，但它对客观 task-completion check 具有约束力，不能被文案质量覆盖。

成功后不调用 User Simulator、不生成下一 Interaction。

### Evaluator error 的最小控制语义

以下情况属于 evaluator/control-plane error，而不是 Agent failure：

- native verifier crash、timeout 或输出不符合 result schema；
- Judge 在一次 repair 后仍无法产生合法输出；
- User Simulator 在 Runtime 允许的 retry/repair 后仍无法产生合法输出。

遇到上述错误时：

1. 不生成或伪造 `turn_score`，终态分数为 `null`；
2. 不调用 User Simulator，也不增加 Benchmark Interaction；
3. 必须保留已经产生的 raw submission、native evidence、模型原始输出、错误阶段和重试记录；
4. 当前 Turn 标记为 `evaluation_error`/invalid；
5. 是否停止整个 Run 或跳过该 Turn，由 Runtime run configuration 决定并记录。本数据合同不替 Runtime 固定调度策略。

### AutoResearch continuation policy

AutoResearch 把“是否继续 Interaction”与“最终分数是否达到 success threshold”分开。必须声明：

```json
{
  "interaction_continuation": "invalid_or_unverifiable_submission_only",
  "low_metric_is_retryable": false
}
```

对应的 `user_simulator.trigger` 必须改为：

```json
"submission_invalid_or_unverifiable_and_budget_remaining"
```

- submission 缺失、格式非法、Agent 执行失败或结果不可验证时，仍有 Interaction 预算才可调用 User Simulator；
- submission 合法且 verifier 成功时，无论 native metric 落入 `full`、`partial` 还是 `insufficient` band，当前 Turn 都结束并记录 Judge 给出的离散终态分；
- verifier 自身 crash/timeout 属于 evaluation error，不能伪装成 Agent failure，也不能用 User Simulator 向 Agent泄露 evaluator 故障；
- `turn_score_threshold` 仍可用于报告 success/failure 标签，但不能作为 AutoResearch retry 条件。

## 13. Bounded feedback

完整 Judge output、GT、weights 和 private diagnostics 只供 evaluator/report。

`feedback_projection`：

```json
{
  "include": [
    "failed_dimensions",
    "observable_deficiency",
    "public_result_category"
  ],
  "exclude": [
    "gt",
    "private_diagnostics",
    "evaluation_assets",
    "future_turns",
    "exact_fix_steps"
  ]
}
```

示例：

```json
{
  "schema_version": "l2_bounded_feedback/1.0",
  "failed_dimensions": ["output_schema"],
  "observable_deficiency": "The produced artifact has an invalid externally visible schema.",
  "public_result_category": "fail"
}
```

只有这份 bounded object 可以进入 User Simulator prompt。

## 14. LLM User Simulator

调用条件：

```text
Interaction evaluation failure
AND interaction_index < max_interactions
```

输入：公开 Task、完整公开 Interaction transcript、最新 response 和 bounded feedback。输出必须通过 `l2-user-simulator-output.schema.json`。

Simulator 的职责是生成自然、简短、与失败现象一致的下一条用户输入，促使 Task Agent再次尝试；它不评分、不泄漏答案、不改变任务。

Simulator 输出通过后，`next_user_message` 立即作为下一 Interaction 的 user message。每次 Simulator 调用只产生一条消息；Task 中的 `max_generated_messages=2` 是整个 Turn 的累计上限，对应最多两次 retry Interaction。Simulator parse repair 和模型 API retry 不增加 Interaction；若允许的 repair/retry 耗尽，则按 evaluator error 处理，而不是继续 Agent Interaction。

## 15. Terminal Interaction

- 成功发生在哪个 Interaction，就以该 Interaction score 结束；
- 第三个 Interaction 失败时，不再调用 Simulator；
- 最终失败分数取第三个 Interaction 的 score；
- earlier Interaction 的分数只用于 Interaction-level diagnostic trace，不进入正式 Stream learning curve，也不得替代终态；
- Framework/verifier crash 与 Agent failure 分开记录。

## 16. 评测复现记录

Runtime 至少保存：

```text
run configuration
model/version/temperature
prompt versions
task/manifest hashes
workspace transition record
每个 Interaction 的完整公开输入与 Agent 输出
tool calls/results
state/workspace before/after/diff
raw + parsed verifier result
raw + parsed Judge output
bounded feedback
raw + parsed User Simulator output
每个 component 与最终聚合分
errors/retries/costs
```

这些记录必须足以在不重新调用模型的情况下重算所有 Turn score。

## 17. 全局 learning curve 与 evolution gain

所有 `l2_taskstream/1.0` Release 使用根 manifest 声明的统一 profile：

```text
l2_evolution_aggregation/1.0
```

本 Profile 不在 Stream 中重复保存 `analysis_plan`，也不改变单 Turn 的 verifier/Judge 评分。Runtime 必须使用相同模型、Prompt、工具权限、Turn 顺序、资源预算和 evaluator 配置，分别执行 `reset`、`frozen`、`evolving` 三个匹配 condition；三者的 Workspace 与 SUT-state 语义见 `L2_BENCHMARK_SPEC_zh.md`。

### 17.1 输入与完整性

聚合只使用每个 Turn 的 terminal `turn_score`，不得使用 earlier Interaction 的最好成绩。每条记录至少保留：

```text
condition
stream_id
turn_id
turn_index
sequence_role
turn_score
```

三个 condition 必须包含完全相同的 Stream、Turn identity 和顺序。`evaluation_error` 的 `turn_score=null` 不得静默删除、改成 0 或当作 Agent failure；在 evaluator 修复或重跑完成前，该组匹配结果标记为 incomplete，不发布正式 gain。

### 17.2 Learning curve

对每个 condition、每条 Stream，按固定顺序每 10 Turns 形成一个完整 bin：

```text
bin 1 = turns 1..10
bin 2 = turns 11..20
...
```

每个 bin 的值为该区间全部 terminal Turn scores 的算术平均；`noise` 是合法测试角色，不得从主曲线或 overall mean 中静默删除，同时另报 per-role mean。少于 10 Turns 的尾部不进入正式 bin，但仍进入 Stream overall mean；Runtime 必须保留原始 per-Turn trajectory。只有至少包含两个完整 bin 的 Stream 才能报告 learning-curve 趋势，因此当前 100-Turn SRA Stream 可报告 10 个点，10-Turn Streams 只有一个描述性 bin，2-Turn AutoResearch Pilots 不声明 learning-curve 结论。

### 17.3 Evolution gain

与 L1 对齐的主增益为：

```text
Delta_evo_reset = mean(Evolving) - mean(Reset)
```

L2 同时报告用于隔离 learner update 的诊断增益：

```text
Delta_evo_frozen = mean(Evolving) - mean(Frozen)
```

三个 condition 至少同时报告：

- 全部 Turns 的 micro mean；
- 各 Stream mean 的 macro mean；
- 每条 Stream 的 mean；
- 每条 Stream 的 10-Turn bins；
- Evolving 相对 Reset 的 per-stream gain；
- Evolving 相对 Frozen 的 per-stream gain。

当前六条 Stream 长度不同，micro mean 会由长 Stream 主导，因此不能只报告 pooled micro gain。`evolution_reference.py` 是上述匹配检查和聚合语义的参考实现；生产 Runtime 可以使用等价实现，但输出必须能由冻结日志重新计算。

### 17.4 当前指标边界

`sequence_role` 可用于额外报告 per-role mean，但 V1 不定义 forgetting、retention 或 transfer 的正式聚合公式。`probe` 仍按自身 native verifier 和 Judge checks 正常产生 Turn score；缺少未来配对指标不会使 Probe 无法评分。
