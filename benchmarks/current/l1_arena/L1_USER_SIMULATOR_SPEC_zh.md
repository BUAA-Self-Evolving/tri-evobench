# L1 User Simulator 规范

L1-Life 与 L1-Arena 的每个 turn 都必须包含非 `null` 的 `user_simulator`。当前 prompt 为 `l1_user_simulator/1.1`。它参考 L2 Demo 的 post-failure Interaction 机制，但针对 L1 的事实学习、状态卫生、workspace side effect 和长期记忆边界进行了限制。

## 两类行为

1. `clarification_policy`：仅 L1-Life 原有 interactive fact turn 可启用。Agent 在第一次提交前提出与隐藏事实直接相关的澄清问题时，最多返回一次冻结的 `scripted_reply`；其他 turn 固定 `enabled=false`。
2. post-failure retry：一次完整 Interaction 被合法评测为失败且仍有预算时，调用 LLM simulator 生成一条新的 user message，最多生成两条，因此每个 Turn 最多三次 Interaction。

## 触发与停止

- 成功立即停止，不调用 simulator；
- Agent failure 且仍有预算才调用；
- verifier/Judge/simulator 自身 crash、timeout、非法输出属于 evaluator error，不调用 simulator，Turn 分数为 `null`；
- 每次 simulator invocation 只输出一条 `next_user_message`；
- 正式评分采用 `latest_only`，不得把多个 Interaction 的最好分挑出来。

## 可见性

Simulator 只能看到 `agent_input`、公开 interaction transcript、最新 Agent response 和 bounded evaluation feedback。禁止读取或推断 `facts.json`、GT、rubrics、weights、expected state/tool/workspace、checker、private diagnostics 和未来 turn。

Bounded feedback 必须符合 `l1_bounded_feedback/1.0`，并由包内 `runtime/l1_bounded_feedback.py::project_bounded_feedback` 从仅含 `dimension` 和 `score` 的分量结果确定性生成。Projector 不接受 raw reason、checker feedback、GT 或 expected value；`observable_deficiency` 只能使用冻结的通用模板。输出只允许失败维度、可观察缺陷和公开结果类别，不得包含精确 hidden value、gold patch、逐步解法或私有 verifier 输出。

## 持久化

Simulator retry message 属于公开 transcript，但默认不是长期事实。除非原始公开任务明确要求持久化，否则 learner 不得把 retry 措辞、临时格式、错误提示或 simulator metadata 写入长期 memory/skill/preference。L1-Arena distractor 的 one-turn override 必须保持一次性。

## 输出

Simulator 输出必须符合 `schemas/l1-user-simulator-output.schema.json`；bounded feedback 必须符合 `schemas/l1-bounded-feedback.schema.json`。每次 simulator 调用只包含一条自然用户消息和一个有限 rationale category。
