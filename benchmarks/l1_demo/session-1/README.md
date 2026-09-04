# L1 Agent Benchmark — 7-Turn 多任务流

基于 `l1_project_manager_30_episodes(1).json`（Aurora CRM / 林倩）场景扩展的多任务 benchmark。
每个 turn 一个 `task.json`，包含：用户问题（多需求）、`max_iterations=3`、评估配置、
统一 `iter_score_0_threshold = 0.8`、0–1 连续打分。

## 统一迭代策略（所有 turn 相同）

| 参数 | 值 |
|---|---|
| max_iterations | 3 |
| 分数范围 | [0, 1] 连续值 |
| iter_score_0_threshold | 0.8（第 0 轮即达标 → 直接通过） |
| 停止规则 | 达标即停 / 无改进即停（取历史最高分）/ 3 轮封顶取最高分 |
| 反馈 | 每轮把未通过项反馈给 agent，不泄露 gold/权重 |

## Turn 一览

| Turn | 任务类型 | 素材 | 评估方式 | 考点 |
|---|---|---|---|---|
| turn1 | 事实记忆 + 工具 | E01/E02/E04 | rule_verifier（regex/if/code/word_count） | 偏好抽取入库、澄清后建日历事件 |
| turn2 | 偏好应用 + 工具事实 | E05/E10/E11 | llm_as_judge（+规则 fallback） | 命名偏好落文件、从工具返回学事实、禁发消息 |
| turn3 | 修正 + 干扰项 | E17/E19/E21/E24 | hybrid（verifier 0.6 + judge 0.4） | supersede 语义、一次性西语不落库、英文确认 |
| turn4 | 数据分析 + 交互澄清 | `agent_workspace/data/sprint_metrics.csv` | rule_verifier（数值 code 校验为主） | 先澄清口径再分析、sprint 汇总正确、防幻觉 |
| turn5 | 学术润色 | `agent_workspace/docs/abstract.txt` | hybrid（verifier 0.35 + judge 0.65） | 主张零漂移、LaTeX 保留、长度区间、不覆盖原文件 |
| turn6 | 中英翻译 | turn5 产物 `abstract_polished.txt` | hybrid（verifier 0.4 + judge 0.6） | 术语表、专名保留、句级完整性 |
| turn7 | 通俗化改写 | turn6 产物 `abstract_zh.md` | hybrid（verifier 0.4 + judge 0.6） | 要点结构、不过度承诺、结尾原句、字数区间 |

## 依赖链

```
turn1 ─┬─> turn2 ─> turn3        （事实记忆线，状态跨 turn 累积）
turn4   （独立：数据分析）
turn5 ─> turn6 ─> turn7          （文档流水线：润色 → 翻译 → 改写，文件跨 turn 累积）
```

turn1–3 的 persistent state（facts / fact_history）需跨 turn 传递；
turn5–7 的产物文件在 `agent_workspace/docs/` 中跨 turn 传递。

## 评估方式：脚本 verifier vs LLM-as-Judge

调研主流 benchmark 后的选型准则：

- **SWE-bench**：跑仓库测试 → 程序化判分（测试通过 = 正确）
- **τ-bench**：对比终态数据库/环境状态，确定性检查，不匹配参考轨迹
- **GAIA**：脚本化答案匹配（标准化后与参考答案比对）

**准则：gold 可确定性导出（数字、状态、副作用、正则可验）→ 脚本 verifier；质量主观（忠实度、流畅度、结构）→ llm_as_judge**

turn4（CSV 统计分析）属于前者：sprint 汇总、最差模块、P0 合计都能从 CSV 确定性重算，LLM judge 反而可能在算术比对上产生噪声，故用脚本；turn5/6/7（润色/翻译/改写）本质是主观质量评估，用 judge。

## 评估方式说明

两种方式共存，每个 turn 只用一种，单一真相源在 task.json：

- **rule_verifier**（turn4）：`verify.py`，接口 `verify(response, workspace, trajectory) -> {"score","checks","feedback"}`，可直接 import 或 CLI 调用；gold 从 workspace 数据实算，不依赖 LLM
- **llm_as_judge**（turn1/2/3/5/6/7）：judge prompt 在 `evaluate.judge_config.judge_prompt` 字段；填入 `{final_response}`/`{tool_calls}`/`{final_state}`/`{workspace_files}` 后调任意 LLM，解析 JSON 得 0-1 连续分

统一约束：分数 0-1 连续；`max_iterations=3`；`iter_score_0_threshold=0.8`；停止规则见 `iteration_policy`

## 各 turn 文件分布

| Turn | 任务类型 | 评估 | 文件 |
|---|---|---|---|
| turn1 | 事实记忆+工具 | llm_as_judge（规则清单并入 prompt） | task.json |
| turn2 | 偏好应用+工具事实 | llm_as_judge | task.json |
| turn3 | 修正+干扰项 | llm_as_judge（supersede 状态核对入 prompt） | task.json |
| turn4 | 数据分析+交互澄清 | **rule_verifier**（CSV 实算 gold） | task.json + verify.py |
| turn5 | 学术润色 | llm_as_judge | task.json |
| turn6 | 中英翻译 | llm_as_judge | task.json |
| turn7 | 通俗化改写 | llm_as_judge | task.json |

`verify.py` CLI：`python verify.py --response-text "..." --workspace ../agent_workspace [--trajectory traj.json]`

## agent_workspace 布局

```
agent_workspace/
├── state/        # persistent state（facts、fact_history），turn1-3 跨 turn
├── artifacts/    # 产物登记
├── iterations/   # 各 turn 各迭代的轨迹与得分日志
├── data/
│   └── sprint_metrics.csv   # turn4 分析数据（S24-S27 × 3 模块质量指标）
└── docs/
    ├── abstract.txt          # turn5 原始摘要（IntentAnony 投稿）
    ├── abstract_polished.txt # turn5 产出 → turn6 输入
    └── abstract_zh.md        # turn6 产出 → turn7 输入
```
