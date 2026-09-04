# L1-Arena：自包含的持久工作区任务流

L1-Arena 以 `l1_life_v2` 的 stream/turn 自包含封装为标准，把 `clawarena-real` 转换为持续型 Agent 数据：**一个原始场景对应一条独立 stream，一个原始 round 对应一个有序 `turnK/task.json`**。

当前版本包含 **23 条 stream、423 个 turn**：377 个一对一源 `exec_check` turn，加上 46 个确定性 derived distractor。每条 stream 在原始第 5 轮后插入一个无关精确问答，在第 10 轮后插入一个明确仅本轮有效的格式覆盖，用于测量上下文切换、恢复能力和持久状态污染。原始任务顺序、verifier、反馈、偏好、动态 update、workspace、session history 和来源描述均被保留。

## 目录结构

```text
l1_arena/
├── README.md / README_zh.md
├── BENCHMARK_SPEC.md / BENCHMARK_SPEC_zh.md
├── EVALUATION_SPEC.md / EVALUATION_SPEC_zh.md
├── BASELINE_EVALUATION_GUIDE_zh.md
├── L1_USER_SIMULATOR_SPEC_zh.md
├── TAG_TAXONOMY_zh.md
├── CALIBRATION_PLAN.md
├── LICENSE.md
├── manifest.json
├── tools.json
├── prompts/l1_user_simulator_prompt.md
├── schemas/l1-user-simulator-output.schema.json
├── schemas/l1-bounded-feedback.schema.json
├── runtime/l1_bounded_feedback.py
├── taxonomy/tag_intent_gold.json
└── stream-N/
    ├── stream.json
    ├── facts.json
    ├── agent_workspace/
    │   ├── workspace/          # 持久任务工作区
    │   ├── session_store/      # 多渠道历史会话
    │   ├── state/、tool_state/
    │   ├── artifacts/
    │   └── iterations/
    ├── evaluation/
    │   ├── scripts/            # 原始确定性 verifier
    │   └── updates/            # 密封动态更新载荷
    ├── turn1/task.json
    └── ...
```

Agent 进程只能挂载 `agent_workspace/workspace`，不能只靠 cwd 约定隔离；历史会话只能通过 `SessionHistory` 访问。带 update 的 turn 开始前，由 evaluator/runtime 根据密封的 `evaluation.environment_updates` 注入文件或追加会话；`new` session 还必须登记到 `sessions.json`。checker、feedback、GT、update 载荷和 `facts.json` 不挂载给 Agent。

全部 423 个 turn 都内嵌非 `null` 的 L1 bounded User Simulator，参考 L2 post-failure Interaction：合法 Agent failure 且有预算时，每次生成一条简短 retry user message，初始 Interaction 后最多两条。Arena 不启用隐藏事实澄清；simulator 只能看到公开 task/轨迹/最新回复和 bounded feedback，禁止访问 checker、GT、精确 expected value、未来 turn 和 sealed state。

Bounded feedback 由包内 `l1_bounded_feedback/1.0` dimension-and-score-only projector 生成；preference adherence 是 mandatory success component；Judge/控制面失败保持 `null`。Retry 使用 `continue_with_patch`，不重复应用 environment update。

官方任务分运行 `evaluation.primary_evaluation.source_exec_check`，pass=1、fail=0。task-only 与 preference-only verifier 形成不重复的 L1 ordinal checks；偏好目标缺失必须记 0。公共 evaluation/message/GT 字段兼容 L1 v2，Arena 确定性字段作为显式扩展。

噪音 turn 使用 `turn_type=distractor`、`turn_origin=derived_evolution_turn`、`sequence_role=noise`，不提供工具；评分由精确短答案和 state hygiene 两部分组成，禁止修改 workspace/session 或把临时指令存成长期事实、skill、memory、preference。它们不消耗 ClawArena 源任务，也不改变累计 update 可见性。

每个 turn 顶层还包含双语 `tag`：`domain`、`domain_zh`、`subtag`、`subtag_zh`、`is_noise`。Source subtag 由场景主题和 `taxonomy/tag_intent_gold.json` 中冻结、审校的 per-round intent 组成；distractor 固定使用 `unrelated_noise / 无关噪音`，不继承 stream 的业务领域。根 manifest 与 stream manifest 提供确定性 tag 计数和 gold SHA-256。

构建：`python scripts/build_l1_arena.py`；校验：`python scripts/build_l1_arena.py --check`。
