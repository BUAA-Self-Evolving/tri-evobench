# L2 TaskStream Contract V1

本目录是 L2 数据组交付给统一 Benchmark/Runtime 团队的版本化数据合同。V1 在 L1-Life `l1_life/2.0` 的基础对象模型上扩展 L2 能力任务所需的动态 Workspace、文件资产、失败后 LLM User Simulator、native verifier 与统一 LLM-as-Judge 评分。

## 规范文件

- `L2_BENCHMARK_SPEC_zh.md`：目录、对象、字段、可见性、Workspace 和 Interaction 生命周期。
- `L2_EVALUATION_SPEC_zh.md`：native verifier、Judge、Profile/Rubric 路由、聚合、成功条件和反馈投影。
- `prompts/l2_judge_prompt.md`：兼容既有 General Task 的 Judge Prompt V1。
- `prompts/l2_judge_prompt_v2.md`：在固定位置组合 Domain/Benchmark Rubric 的 Judge Prompt V2。
- `prompts/rubrics/auto_research_v1.md`：AutoResearch 通用 Domain Rubric；Benchmark-specific Rubric 通过同一路由按需追加。
- `prompts/rubrics/airs_sick_v1.md`：AIRS SICK 的 artifact、官方归一化和可复现性细化规则。
- `prompts/rubrics/mlgym_titanic_v1.md`：MLGym Titanic 的 CSV/PassengerId、Accuracy 和可复现性细化规则。
- `judge_reference.py`：Judge literal 模板渲染、输出覆盖校验和确定性聚合参考实现。
- `evolution_reference.py`：Reset/Frozen/Evolving 匹配检查、10-Turn learning curve 与 evolution gain 的参考聚合实现。
- `prompts/l2_user_simulator_prompt.md`：失败后生成下一条用户输入的 User Simulator Prompt 模板。

## 机器合同

- `schemas/l2-manifest.schema.json`
- `schemas/l2-tools.schema.json`
- `schemas/l2-stream.schema.json`
- `schemas/l2-task.schema.json`
- `schemas/l2-judge-output.schema.json`
- `schemas/l2-bounded-feedback.schema.json`
- `schemas/l2-native-verifier-request.schema.json`
- `schemas/l2-native-verifier-result.schema.json`
- `schemas/l2-user-simulator-output.schema.json`

## 合同样例

`examples/` 仅保存独立协议输出样例，不是正式 Demo 数据：

- `native-verifier-result.example.json`
- `user-simulator-output.example.json`
- `judge-output.example.json`
- `bounded-feedback.example.json`

运行 `python3 validate_contract.py` 会在内存中构造 General 与 AutoResearch 代表性对象，嵌入 Judge Prompt V1/V2 和 User Simulator Prompt，并同时检查 Schema、占位符、Rubric route、负例及跨对象不变量。这样不会让示意 Task 被误当作可迁移 Demo。

## V1 freeze candidate 决策

当前 `1.0` 已完成数据侧验证，但尚未被 L1/Runtime 正式消费。除必要缺口或用户批准外暂不修改；一旦跨组正式接入，新增必填字段或不兼容语义必须发布 `1.1`，不能继续原地修改 `1.0`。

本次在跨组接入前增加的是向后兼容的 Judge Prompt V2 路由：既有 Task 未声明 `evaluation_profile` 时继续按 legacy General Profile 与 `l2_judge/1.0` 解释；显式 General Task 也固定使用 `l2_judge/1.0`。新 AutoResearch Task 必须显式声明 `evaluation_profile=auto_research`、`metric_profile` 和 `l2_judge/2.0` Rubric route。Contract V1 不支持 General + Judge V2；未来若需要该组合，必须在后续兼容版本中补齐 route 与验证样例。

只包含一种 Judge Prompt 的 manifest 继续使用 `judge_prompt_version`；同时包含 V1/V2 Task 的发布包改用互斥字段 `judge_prompt_versions`，列出实际使用的全部版本。具体 Task 仍以 `evaluation.judge_config.prompt_version` 为准。

1. Schema version 为 `l2_taskstream/1.0`。
2. 基本单位为 Benchmark → Stream → Turn → Interaction。
3. Benchmark 数据语义下，一个 Stream 只有一个持久 `agent_workspace`；只有进入下一 Stream 才创建新 Workspace。`reset` reference condition 是显式控制实验例外，会在每个 Turn 前从冻结 seed 重建干净副本。
4. 每个 Turn 开始前可执行声明式 `workspace_transition`；V1 仅允许 `copy`、`create`、`apply_patch`。
5. `sequence_role` 只允许 `learning/application/probe/replay/noise`。当前 V3 Demo 使用 `learning/application/probe`；`replay/noise` 为后续 retention/干扰项设计保留。
6. 完整 V2 聚合需要 Runtime 跑匹配的 `reset`、`frozen`、`evolving` 三类 reference condition。L2 的 `reset` 必须在每个 Turn 前清空 SUT persistent state，并把 Workspace 还原到 Stream 初始态后只应用当前 Turn 的 `workspace_transition`；`frozen/evolving` 都延续 Stream Workspace，但只有 `evolving` 允许 SUT 自更新。
7. Learning curve 默认每 10 个 Turn 一个 bin；evolution gain 默认为 `mean(evolving) - mean(reset)`。这些由 Runtime/aggregation 从 Run 结果计算，数据包只固定 turn order、sequence roles 和评分合同。
8. 一个 Turn 最多三个 Interaction。每个 Interaction 都完整执行并评分，不存在真人等待或 `awaiting_user` 状态。
9. Interaction 失败且仍有预算时，LLM User Simulator 根据公开轨迹和 bounded feedback 每次生成一条用户输入，自动开始下一 Interaction；`max_generated_messages=2` 是整个 Turn 的累计上限。
10. 每个 Reference Task 必须声明 native verifier。Evaluator 必须先运行 verifier，再调用 Judge；Judge 不决定是否运行 verifier。
11. native verifier 的归一化连续分数与状态作为 Judge 的权威输入证据，不作为独立加权 component；LLM Judge 对所有 checks 输出 `1/0.5/0/null` 或 source-rubric mean。
12. General Profile 继续按 `evaluation.checks[].weight` 确定性聚合 Judge 分项，聚合后总分可能是连续值。AutoResearch Profile 只声明一个权重为 `1` 的 `auto_research_overall` ordinal check，因此最终 Turn score 严格属于 `0/0.5/1`。
13. Judge Prompt V2 保留一份通用 Prompt，并在固定位置按 `common → domain → benchmark` 路由 Rubric。AutoResearch 必须注入公共 Domain Rubric；AIRS、MLGym 等只有在 artifact 或 metric 语义确有差异时才追加 Benchmark-specific Rubric。
14. AutoResearch 的连续 raw metric 与归一化 `[0,1]` score 必须保留；Judge 依据 Task 声明的 performance bands 评定离散总分，不得重算或覆盖 native metric。
15. AutoResearch 的合法低分 submission 会直接结束 Turn；只有 submission 缺失、非法或不可验证时才允许进入下一 Interaction。低 metric 本身不能触发 User Simulator。
16. 每个 Task 必须声明 `submission_contract`。Runtime 转交原始 submission；source-specific response/code/trajectory 解析属于随 Task 交付的 native-verifier adapter，而不是 Runtime 内置分支。
17. L2 数据组交付数据、prompt、schema、verifier/evidence 合同和聚合语义；Runtime/Baselines 组实现 Harness、Sandbox、工具、模型调用和实验运行。
18. 对外 Task schema 不包含许可证字段；来源 revision 与许可判断仍保留在项目内部 provenance/合规记录中。
19. Verifier/Judge/Simulator 在 Runtime retry/repair 后仍失败时属于 `evaluation_error`：Turn 分数为 `null`，不触发 User Simulator，也不能伪装为 Agent 失败；Run 停止或继续由 Runtime run configuration 决定。
20. 根 manifest 固定声明 `aggregation_profile=l2_evolution_aggregation/1.1`。Runtime 使用匹配的 Reset/Frozen/Evolving Runs 计算每 10 Turns 一个 bin 的 learning curve、Evolving 相对 Reset 的主增益，以及 Evolving 相对 Frozen 的诊断增益。V1 暂不定义 forgetting 指标。

## Reference 数据流

```text
复制 Stream seed agent_workspace
  → 应用当前 Turn 的 workspace_transition
  → Task Agent 收到本 Interaction 的用户输入并完整执行
  → 冻结 response / transcript / tools / state / workspace evidence
  → Evaluator 强制运行 native verifier
  → 按 evaluation profile 渲染通用 Prompt 与可选 Rubric 扩展
  → 调用 L2 Judge
  → 框架确定性聚合所有 check 分数
  ├─ success：Turn 结束，不调用 User Simulator
  └─ failure 且 Interaction < 3：投影 bounded feedback
       → LLM User Simulator 生成下一条用户输入
       → 自动开始下一 Interaction
```

上图的 score-threshold continuation 适用于 General Profile。AutoResearch 在 submission 合法且 verifier 成功后直接结束 Turn；只有 submission 缺失、非法或不可验证时才进入 User Simulator 分支。

## 非目标

本合同不选择或实现：

- 具体 Agent Harness；
- Sandbox/容器产品；
- Task Agent、Evolution learner、Judge 或 User Simulator 的具体模型；
- Baseline runner；
- 生产结果聚合服务；合同只冻结聚合语义并提供纯分析参考实现。

这些值必须由 Reference run configuration 冻结，但不属于 L2 数据组的实现交付。
