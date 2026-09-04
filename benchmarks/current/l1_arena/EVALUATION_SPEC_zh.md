# L1-Arena 评测规范

每个 turn 的官方源任务分为确定性二值分。渲染 `{evaluation_dir}` 与 `{workspace}` 后执行 `evaluation.primary_evaluation.source_exec_check.command`，保留上游完整复合命令的 pass/fail 语义。

`evaluation.exec_check` 是去除上游内嵌 preference 子命令后的 task-only verifier。若存在 `preference_check`，必须先确认声明的 target 存在，再运行 checker；target 缺失或 checker 失败都记 0。L1 统一分采用任务执行 0.8、偏好遵循 0.2；无偏好 checker 时任务执行权重为 1.0。

所有声明的 check ID 都是 mandatory success component。因此 task 通过但 preference 失败时，即使加权分恰好为 0.8，Interaction 仍然失败，并在有预算时触发 User Simulator；不能只看数值阈值。

顺序条件只应用本轮 `environment_updates`；Reset 恢复 seed 后按序应用 `reset_environment_updates`。session `new` 必须同步 upsert `sessions.json`，append 到尚未登记的 session 时也要注册。缺失、重复、越界、顺序错误或哈希不符均属于 runtime failure。

内嵌 judge prompt 仅作诊断或回退。对二值 verifier 对应的 check，judge 必须原样采用 pass=1/fail=0，不能推翻确定性结果。聚合分由评测器从 check 分量重算。

Judge repair 后仍失败属于 `evaluation_error`，Turn 分数为 `null`，不得记成 Agent 0 分，也不得触发 User Simulator；deterministic source result 只保留为审计证据。

Derived distractor 不运行 executable checker，也不开放工具。`response_accuracy` 权重 0.7，对去除首尾空白后的最终回复做大小写敏感精确匹配；`state_hygiene` 权重 0.3，要求无工具调用、workspace/session/memory/fact/skill/standing-preference 写入。仅本轮有效的格式指令不得影响后续源 turn。

每个 turn 都使用 bounded L1 post-failure User Simulator。合法 Agent failure、分数低于 0.8 且仍有预算时，每次生成一条新的 user message，初始 Interaction 后最多两次 retry。源 turn 还要求 primary source verifier 通过；distractor 要求 exact response 与 state hygiene。Verifier/Judge/Simulator 控制面错误记 `null` 且不触发 simulator。Simulator 只获得公开输入与 bounded feedback，正式评分固定使用最后一次 Interaction。

Bounded feedback 必须由 `runtime/l1_bounded_feedback.py` 只根据 dimension/score 生成，并通过 `l1_bounded_feedback/1.0`；raw checker/Judge reason 不能作为 projector 输入。Retry 使用 `continue_with_patch`，不重复应用 update，state hygiene 始终相对 Turn-start。

报告应分别给出 377 个源任务的 completion 与 46 个 distractor 的答案准确率、状态卫生和噪音后恢复率，并按双语 domain/subtag 分解；不得在未标注的情况下把噪音分混入源任务完成率分母。
