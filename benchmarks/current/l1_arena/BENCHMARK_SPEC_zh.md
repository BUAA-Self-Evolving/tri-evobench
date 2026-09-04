# L1-Arena 数据规范

## 运行单元

每个 `clawarena-real` 场景映射为一条隔离 stream；每个原始 round 映射为一个 source `turnK/task.json`。每条 stream 在源第 5、10 轮后各插入一个确定性 derived distractor，因此总计 377 个源 turn 和 46 个衍生 turn，发布 stream 长度为 16--20。

## Task 字段

每个 task 都包含 `schema_version`、`stream_id`、`turn_id`、`turn_index`、`turn_type`、`turn_origin`、`sequence_role`、`interaction_arm`、`tag`、`source_basis`、`agent_input`、`user_simulator` 和 `evaluation`。

- 源 `turn_type` 为 `workspace_exec` 或 `dynamic_update_exec`，噪音为 `distractor`；
- 源 `turn_origin` 为 `source_task`，噪音为 `derived_evolution_turn`，source task ID 为 null，并记录相邻 parent source IDs；
- `source_basis` 内嵌来源 ID、描述、commit 和哈希，不包含绝对路径；
- `agent_input.messages` 将原始 system context 投影为 L1 兼容的 user message，保留 assistant seed context，并以当前 round user 问题结束；只允许 `user|assistant` 和 `role/content`；
- `agent_input.agent_workspace` 固定为 `agent_workspace/workspace`。

## 持久化

同一 stream 的 `agent_workspace/workspace` 与 `agent_workspace/session_store` 跨 turn 持续存在，不同 stream 永不共享。全新运行从发布的 seed 内容恢复这两个目录，并由 seed state 初始化 current state。

## 动态更新

更新载荷密封在 `evaluation/updates`。`environment_updates` 只声明本轮新增 update；`visible_update_ids` 与 `reset_environment_updates` 明确 Reset 恢复 seed 后应重放的累计外生更新。新 session 文件还必须登记到 `sessions.json`。动作语义保持 `new/modify/add/append/delete`。

Session append 必须从 `sessions.json` 保留目标 session 的既有 channel，不能写成通用 `history`；new session 才使用源显式 channel，确实没有索引时才允许按文件名确定性推断。

## 可见性

Agent 可见：`agent_input`、精确 Native 工具 schema、只挂载的 `/workspace` 和 SessionHistory 返回。Agent 进程不得挂载 stream 父目录。必须密封：`evaluation`、`facts.json`、checker、feedback、expected effect、update 载荷路径、GT 和 judge 输出。

## 自包含

每条 stream 内含完整初始 workspace、session store、update 载荷和 verifier；运行时无需读取下载源目录。

## 噪音 turn

每条 stream 在源第 5 轮后插入 `unrelated_exact_qa`，在源第 10 轮后插入 `one_turn_format_override`。噪音不开放工具，使用精确答案 GT，并要求 workspace、session、learner state 均无修改。其累计 update 可见性与周围 stream 一致，但本身不引入 update；临时格式指令不得成为长期 preference。

## User Simulator

全部 423 个 turn 的 `user_simulator` 都必须是非 `null`：`type=llm_post_failure`、失败且有预算时触发、内嵌 `l1_user_simulator/1.1`、四项公开输入、temperature 0、输出 `l1_user_simulator_output/1.0`，初始 Interaction 后最多两条 retry message。Arena 的 grounded clarification 全部显式禁用。成功立即停止；evaluator error 记 `null` 且不触发；正式评分采用 `latest_only`。Retry 文本进入公开 transcript，但默认不是 durable state；distractor 的一次性指令不得持久化。

## 双语领域标签

每个 task 必须包含 `tag`，字段为 taxonomy version、中英文 domain、中英文 subtag 和 `is_noise`。Source subtag 格式为 `{scenario_topic}.{turn_intent}`；噪音使用 `unrelated_noise.exact_arithmetic` 或 `unrelated_noise.one_turn_format_override` 且 `is_noise=true`。英文 ID 为稳定 snake_case，中文为人类可读标签。详见 `TAG_TAXONOMY_zh.md`。

Source intent 从冻结的 377-entry `taxonomy/tag_intent_gold.json` 读取，不再由发布时正则推断。Manifest 保存 gold SHA-256，validator 要求全部 source task ID 精确覆盖。

## Interaction workspace transition

每个 task 声明 `workspace_transition.mode=continue_with_patch`：environment update 只在 Interaction 1 前应用一次；retry 继承最新 workspace、session store 与公开 transcript；state hygiene 相对 Turn-start snapshot；终局 success/Agent failure 采用 latest valid Interaction workspace；evaluator error 丢弃该 Turn copy。
