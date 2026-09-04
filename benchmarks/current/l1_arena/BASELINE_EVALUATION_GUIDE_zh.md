# L1-Arena baseline / runtime 实现指南

1. 读取根 `manifest.json`，按 `stream-N/stream.json` 的 turn 顺序运行。
2. 每条 stream 开始时复制 seed workspace 与 session store 到独立 run 目录。Agent 进程只能把 workspace 子目录挂载到 `/workspace`；不得挂载 stream 父目录、`evaluation/`、`facts.json` 或 manifest。Bash 及其所有子进程必须被限制在该 mount，拒绝绝对路径、`..` 与 symlink escape。仅设置 cwd 不构成隔离。
3. 初始化 active conversation 时只加载一次 `message_projection.seed_message_count` 条 seed messages。每个 turn 仅追加 terminal user message、真实 Agent 回复与工具结果，不得重复加入 seed messages。
4. 顺序执行条件在 turn 前只应用 `evaluation.environment_updates`。Reset 在恢复 seed 后应用 `evaluation.reset_environment_updates`。每个 payload 先校验 SHA-256，再校验 action 与 target containment；`new/modify/add` 复制覆盖，`append` 字节追加，`delete` 删除。
5. 对 session update，除文件动作外还要维护 `session_store/sessions.json`：按 `session_index` upsert `session_id/session_file/channel/is_active=false`；append 目标若尚未登记也要注册。SessionHistory 只能访问这个索引化 store。
6. 只把标准化后的 `agent_input.messages` 和 `available_tools` schema 发给 Agent。不同 stream、model、condition 和重复 rollout 不共享可写目录。
7. 全部 turn 的 `user_simulator` 都必须非空。一次完整 Interaction 评测后，只有 primary verifier 和所有 `mandatory_checks_must_pass` 都通过且分数达到阈值才算成功；task 通过但 preference 失败仍须 retry。合法 Agent failure 且仍有预算时，向 simulator 只投影公开 task/轨迹/最新回复和 bounded feedback，生成一条 retry user message。初始 Interaction 后最多两条，总计最多三次 Interaction，正式结果使用 `latest_only`。
8. Bounded feedback 必须由 `runtime/l1_bounded_feedback.py` 从只含 dimension/score 的结构生成并通过 schema；禁止把 raw checker/Judge reason、GT、expected values、private diagnostics 或未来 turn 传入。Verifier/Judge/Simulator crash、timeout 或非法输出是 evaluator error：结果为 `null`，不调用 simulator。Simulator retry 默认不是长期事实。
9. `turn_type=distractor` 时只追加该条无关 user message，工具列表必须为空；记录执行前后的 workspace/session/state 树。对 exact response 做首尾空白归一化后大小写敏感匹配，并验证无工具或持久写入。Simulator 也不得把 one-turn override 改成长期 preference。随后恢复正常源任务，但不删除合法的既有 stream state。
10. 源 turn 结束后先运行完整 `primary_evaluation.source_exec_check` 得到官方源主分。L1 分量另行运行 task-only `exec_check`；preference check 先验证 target 存在再执行，缺失 target 直接记 0，且 preference check 是 mandatory success component。Interaction 2/3 继承上一 Interaction 的 workspace/session/public transcript，不重复应用 environment update；state hygiene 相对 Turn-start。保存每个 Interaction 的命令、退出码、stdout/stderr、超时和 workspace 终态证据。
11. 源主分严格使用完整 source verifier 的 0/1；L1 checks 和 judge 只用于统一分析，不得覆盖。不得把 checker feedback、GT、payload 或未来 turn 透露给 Agent/learner。
12. Evolving/Frozen 按顺序保留外生更新和允许的 workspace/session side effects；Reset 只重放累计外生 update，不重放前序 Agent 产物。依赖前序 Agent 产物的任务在 Reset 中自然失败。
13. 分别报告 source task completion、preference pass、distractor response accuracy、state hygiene、post-noise recovery、simulator invocation/success/error、每 stream/类别及双语 domain/subtag 得分、runtime update failure、隔离失败和成本。源 completion 分母固定为 377，不与 46 个 distractor 混合。
