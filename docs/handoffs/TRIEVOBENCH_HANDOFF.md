# Tri-EvoBench 交接文档（给下一位 Agent）

更新时间：2026-08-28（Asia/Shanghai）  
主目标：将 Hermes、SkillOpt、Trace2Skill 三套 baseline 合并到 Tri-EvoBench，并完成真实 Docker + DeepSeek 的 L1/L2 评测、修复问题、提交可复现代码与 trace。

> 安全说明：不要把用户的 API Key 写入仓库、日志、报告或提交信息。运行前由调用环境设置 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`TRIEVO_API_KEY`；模型为 `deepseek-v4-flash`，端点为 `https://api.deepseek.com/v1`。

---

## 1. 仓库与当前工作目录

顶层工作区：

```text
C:\Users\Administrator\Documents\Research\projects\Evobench
```

真正的合并工作树（后续所有命令应在这里执行）：

```text
C:\Users\Administrator\Documents\Research\projects\Evobench\tri-evobench-hermes-runtime\tmp\all-baselines-merged
```

当前分支：

```text
codex/all-baselines-merged
```

Hermes 是 git submodule：

```text
baseline/hermes-agent
```

请先执行以下只读检查，确认没有误把临时评测文件提交：

```powershell
git status --short
git log --oneline -8
git -C baseline/hermes-agent log --oneline -3
```

### 已有关键提交

父仓库已有：

```text
d50b7b2 Merge Hermes, SkillOpt and Trace2Skill runtimes
68a727e Bridge Hermes to L1 simulated tools and facts
306c8a5 Constrain Hermes L1 protocol execution
116faed Bound L1 baseline interactions
538175a Bound Hermes L1 to one decision per interaction
fea49bf Bound Hermes L1 retries and reasoning
```

Hermes submodule 的本地提交：

```text
2d69ea3 Allow bounded callers to disable length continuations
```

此 submodule 提交修改 `agent/conversation_loop.py`：让调用方可用 `agent.max_length_continue_retries = 0` 禁用 Hermes 原本的截断后自动续写（默认行为仍保持 4 次）。它新增了对应的 pytest。

注意：后续 L1 修复中有若干父仓库文件修改尚未提交；应先运行测试并审阅 diff 后统一提交，临时 override 文件不要提交。

---

## 2. 这个项目在做什么

Tri-EvoBench 是一个评测“可自我进化 agent”的基准运行时。

- **L1**：多轮办公型任务，包括持久偏好/事实、日历、消息、文件创建、数据分析、文本润色、翻译和总结。L1 的副作用工具是**模拟工具**：Agent 应输出协议行，由 Runner 在隔离沙箱中执行并计分。
- **L2**：更复杂的真实项目/代码/数据类 episode。Hermes 要在 Docker sandbox 中使用工具，按 turn boundary 做 memory/skill evolution；评测包含 SRA/private verifier 等。
- **Hermes baseline**：通过 `trievo_bench/baselines/hermes_bridge.py` 适配 Hermes 的 SessionManager/ACP 到 Tri-EvoBench runtime protocol。
- **SkillOpt / Trace2Skill baseline**：已合并，可用独立 manifest/run 路径运行。

用户的核心要求不是 smoke test，而是使用真实模型完成完整 L1 和 L2，以及三套 baseline 的真实回归；并保留可读 trace。用户此前特别强调：

1. evolution 不能被禁用；必须对每 turn 设工作上限，并在超限时切断/失败。
2. L1 中模拟工具必须真的体现在 trace 的 tool_calls/state/events 中，不能把 native Hermes 操作伪装成模拟工具。
3. agent judge 要读配置指定的检查文件，而不是把整个 sandbox 无差别传给 judge（此项已有较早实现，改动需谨慎）。
4. 真实 token 预算要求总量不超过约 5M；未完成/被终止运行的实际消耗不一定有完整 summary，因此后续应保守设上限并记录。

---

## 3. 运行环境

Windows 主机 + Docker Desktop，Docker CLI：

```text
C:\Users\Administrator\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe
```

已验证 Docker daemon 正常。历史遗留两个正在运行的 `trievo-ubuntu:22.04` 容器：

```text
trievo-479fc1fcb3d9
trievo-e4a8cd880632
```

不要擅自删除它们；之前删除请求被安全层拒绝，且无法完全确认是否属于用户的其他工作。

DeepSeek 环境变量（不要把实际 key 放进命令回显或文档）：

```powershell
$env:DEEPSEEK_API_KEY = '<provided-secret>'
$env:OPENAI_API_KEY = $env:DEEPSEEK_API_KEY
$env:TRIEVO_API_KEY = $env:DEEPSEEK_API_KEY
$env:TRIEVO_BASE_URL = 'https://api.deepseek.com/v1'
$env:TRIEVO_MODEL = 'deepseek-v4-flash'
```

Hermes seed config（本地评测用、在合并工作树外；不要提交）：

```text
C:\Users\Administrator\Documents\Research\projects\Evobench\tri-evobench-hermes-runtime\tmp\hermes-eval-seed\config.yaml
```

其中已设置：

- provider: `deepseek`
- model: `deepseek-v4-flash`
- base_url: `https://api.deepseek.com/v1`
- `max_tokens: 1200`
- 禁用 session title generation（DeepSeek 与 Hermes 的 JSON-schema title 输出不兼容）

---

## 4. 当前 Hermes L1 设计与关键代码

核心桥接文件：

```text
trievo_bench/baselines/hermes_bridge.py
```

L1 task 转换器：

```text
trievo_bench/l1_compat.py
```

L1 的模拟协议为：

```text
TRIEVO_TOOL_CALL: {"tool":"...","arguments":{...}}
TRIEVO_STATE_UPDATE: {"facts":{...}}
TRIEVO_EVENT: {"event":"asked_for_<topic>"}
```

Bridge 会从 Hermes 的文本输出抽取上述行；这些协议行不会出现在可见回复中，随后由 Runner 执行模拟工具并写入 trace。

### 为 L1 已做的修复

1. 在存在 L1 `available_tools` 时：
   - `agent.max_iterations = 1`，由外层 evaluator 控制 interaction retry；
   - `agent.reasoning_config = {"enabled": False}`，关闭 DeepSeek V4 默认 hidden thinking；
   - `agent.max_length_continue_retries = 0`，避免内部截断续写绕过 L1 互动上限；
   - native Hermes tools 禁用，避免直接修改 workspace 或产生非审计副作用。
2. 协议 prompt 强制：
   - 事件澄清写 `TRIEVO_EVENT`；
   - 日历 ISO 时间带 `+08:00`；
   - 简短中文回复；
   - 不重复已成功的模拟调用。
3. 修复了 L1 原生 tool callback 被 bridge 用作模拟调用的旁路：L1 现在只接受显式 `TRIEVO_TOOL_CALL`，不再 fallback 到 `self._tool_calls`。
4. 将 L1 的 `agent_limits` 放入 `metadata.public`。这是关键：Runner 只向真实 baseline 传递 `metadata.public`，之前把 limits 放到 metadata 外层导致 Hermes review fork 看不到限制。
5. L1 当前工作上限：foreground 3 个模型调用、单回合 180 秒；evolution 1 个模型调用、120 秒。此设置在最近的小型真实回归中已经生效。

### 仍未完成、必须继续的 L1 问题

完整 L1（7 turn）结果并不通过。最可信的完整运行：

```text
runs\merge-hermes-l1-full-limited\l1-session-1\summary.json
```

结果：7 turn 中仅 turn3、turn7 通过，turn success rate `0.285714...`，平均 `0.45`，总 `530,920` token / 46 API calls。

最近的单 turn 验证（比完整运行更晚，说明关键框架修复有效）：

```text
runs\merge-hermes-l1-turn1-protocolfixed\l1-session-1\summary.json
```

结果：turn1 pass，`0.95`，2 interactions，`51,937` token；evolution 已真的降到 1 API call。

#### 完整 L1 各 turn 的主要失败

| Turn | 结果 | 已知原因 |
|---|---:|---|
| turn1 | 0.55 | 旧完整运行发生在最新 protocol 回退修复前；日历澄清记录未被正确保留。应以 `protocolfixed` 单回合结果为准重新跑。 |
| turn2 | 0.55 | 文件创建大致成功，但命名偏好、正确群 ID、持久状态和消息步骤未满足；最终文本还违反中文约束。 |
| turn3 | 0.95 | 可通过；剩余问题是 `en` 与 `en-US` / fact_history 的细节。 |
| turn4 | 0.15 | 分析任务被 native 工具/迭代限制错误中断，最终 response 为 `None`；需要保证可读 workspace 上下文与两步澄清交互。 |
| turn5 | 0.0 | 润色任务没有产生文本，最后为 iteration limit summary。 |
| turn6 | 0.0 | 翻译任务无法读取 `/workspace/docs/abstract_polished.txt`；agent 误以为只有 `file.create_file` 且不能读取文件。 |
| turn7 | 0.95 | 可通过，只有措辞细节扣分。 |

### 下一位 agent 最优先的实现建议

不要重新开放 native 写工具。正确方案是：**L1 副作用仍只能通过 TRIEVO 协议，但将当前 sandbox 的可读文本作为只读上下文附加给 L1 agent。**

推荐在 `HermesBridge.interact()` 中、调用 `agent.run_conversation()` 前，若为 L1：

1. 从 `self._workspace` 递归读取 UTF-8 文本文件；
2. 排除 `.git`、`__pycache__`、`state/`、二进制/过大文件、评测产生的 artifacts；
3. 只包含基准需要的内容（通常 `docs/*.txt`, `docs/*.md`, `data/*.csv`），每文件和总量设置保守截断；
4. 追加到请求：`【只读沙箱上下文】path... content...`，明确“此内容只读；副作用只能走 TRIEVO protocol”；
5. 保留 `agent.valid_tool_names = set()`，防止读上下文功能演变为可变更沙箱的 native terminal/tool access。

这样应解决 turn4/5/6 的“读不到文件”，同时不破坏 L1 模拟工具安全模型。需要先写 unit test，确保 workspace context 不泄露 state 文件或超出大小上限。

另一个重要细节：当前协议 prompt 对 `TRIEVO_EVENT` 的示例为 `asked_for_<topic>`，模型有时会逐字输出尖括号。这本身不是唯一失败原因，但建议将示例写成具体 JSON，如 `{"event":"asked_for_calendar_id"}`，并明确 event 只可用 `[a-z0-9_]`，避免 judge 不认。

---

## 5. Evolution / work-limit 状态

用户明确要求保留 evolution，不能通过禁用它来省 token。

Hermes background review 在：

```text
baseline/hermes-agent/agent/background_review.py
```

桥接向 parent agent 设置：

```python
agent._trievo_evolution_limits = {
  "max_model_calls": ...,
  "timeout_seconds": ...,
}
```

第一次尝试失败的原因不是 Hermes 本身，而是 L1 limits 没经过 Runner 的 public metadata filter；因此 review 使用默认 16 次。修复 public metadata 后，真实验证显示 evolution usage 为 `1` API call、约 `16,503` token，约束已生效。

历史上出现过 `TokenBudgetExceeded` 后 `end_flow` 报 persistent state changed asynchronously；原因是 review 在预算异常路径仍写 memory/skill。尽量不要用刚好临界的总 token cap；应给 evolution 留余量，或在 runner 层改成预留/拒绝启动 evolution 的预算检查。不要因该问题删除 state guard。

---

## 6. Docker 镜像与 override

临时 override（不要提交）：

```text
tmp_hermes_l1_merged.override.json
tmp_hermes_l2_merged.override.json
tmp_skillopt_local.manifest.json
tmp_trace2skill_local.manifest.json
```

L1 override 最新应指向：

```text
trievo-hermes-merged:0.10
```

已构建镜像历史：`.0.5` 到 `.0.10`。最新 `.0.10` 包含“L1 不 fallback native tool callbacks”的修复，但不包含本文建议的只读 workspace-context 修复（上一次准备修改时被用户中断，尚未 apply patch）。每次改 bridge/l1_compat 后需新 tag 并更新 override。

构建命令：

```powershell
& 'C:\Users\Administrator\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe' build `
  -t trievo-hermes-merged:<new-tag> `
  -f baseline/adapters/hermes/Dockerfile .
```

---

## 7. 真实 L1 运行命令

从合并工作树运行，先正确设置 DeepSeek 环境变量。不要把 key 放进 stdout/stderr 或 git。

单 turn 验证（先做）：

```powershell
python -m trievo_bench.l1_compat `
  --session 'C:\Users\Administrator\Documents\Research\projects\Evobench\tri-evobench-hermes-runtime\tmp\full-eval-inputs\l1_demo\session-1' `
  --turns turn1 `
  --backend docker `
  --baseline hermes `
  --baseline-config tmp_hermes_l1_merged.override.json `
  --learner evolving `
  --max-total-tokens 100000 `
  --output runs\<new-l1-turn1-run>
```

完整 L1：

```powershell
python -m trievo_bench.l1_compat `
  --session 'C:\Users\Administrator\Documents\Research\projects\Evobench\tri-evobench-hermes-runtime\tmp\full-eval-inputs\l1_demo\session-1' `
  --turns turn1,turn2,turn3,turn4,turn5,turn6,turn7 `
  --backend docker `
  --baseline hermes `
  --baseline-config tmp_hermes_l1_merged.override.json `
  --learner evolving `
  --max-total-tokens <conservative-limit> `
  --output runs\<new-full-l1-run>
```

先逐项验证 turn4/5/6（每个 80–120k），再考虑完整 run；旧完整 L1 用 530k，但修复后上下文长度可能变化。为满足约 5M 总预算，保守估算并审计每次 summary 的 `token_budget`。

---

## 8. L2 / SkillOpt / Trace2Skill 状态

这些是较早、在最终 L1 修复前得到的真实结果；不能作为最终“全部通过”结论，必须在最终合并代码上回归。

### 既有成功证据

1. Trace2Skill，真实 Docker + DeepSeek：

```text
runs/merge-trace2skill-real-fixed/deepseek-flow-001/summary.json
```

5/5 通过，6 interactions，约 664 token。

2. SkillOpt，真实 Docker + DeepSeek：

```text
runs/merge-skillopt-real/deepseek-flow-001/summary.json
```

5/5 通过，5 interactions，约 4,644 token。

3. Hermes L2，真实评测：

```text
runs/merge-hermes-l2-real/sra_bigcodebench_pandas_dataframe_viz_001/20260827T123456Z-a9f8a1b6/report.json
```

SRA E001/E002 通过（1.0），约 286,814 runtime tokens / 13 calls。

4. `l2_compat --inspect` 曾验证 6 streams 均有效。

### 后续 L2 工作

L2 代码入口：

```text
trievo_bench/l2_agent.py
trievo_bench/l2_compat.py
```

L2 的 `L2WorkLimits` 默认包括：

```text
max_model_calls=12
max_tool_calls=8
terminal_timeout_seconds=90
episode_timeout_seconds=480
evolution_timeout_seconds=120
evolution_max_model_calls=4
```

最终 L1 修复不应影响 L2，但 Hermes submodule 的 length continuation 改动影响公共 conversation loop，故必须重建最终 L2 evolution 镜像并跑真实 L2，不能只引用旧 `.0.1` 镜像结果。

---

## 9. 测试与提交要求

每次重要修改至少运行：

```powershell
python -m unittest discover -s tests -p 'test_*.py'
python -m py_compile trievo_bench\l1_compat.py trievo_bench\baselines\hermes_bridge.py
git diff --check
```

当前常规 unittest 应为：

```text
Ran 39 tests ... OK
```

Hermes submodule 新增 pytest（镜像缺 pytest，因此可在一次性容器中临时安装）：

```powershell
docker run --rm --entrypoint sh -w /opt/hermes trievo-hermes-merged:<tag> `
  -lc "pip install --no-cache-dir pytest && python -m pytest tests/run_agent/test_run_agent.py -k defer_retry"
```

该测试已通过一次。

最终提交前：

```powershell
git status --short
git diff --check
git add baseline/hermes-agent trievo_bench/baselines/hermes_bridge.py trievo_bench/l1_compat.py tests/...
git commit -m '<clear-message>'
```

绝对不要 `git add tmp_*.json`、`runs/`、seed config、API secret 或 Docker logs。

---

## 10. trace / 报告要求

用户之前多次要求 human-readable trace，需包含：

- 每个 L1/L2 interaction 的 user request、agent response；
- TRIEVO protocol tool call、执行结果、state update、event；
- LLM judge / agent judge 的 request 和 response；
- 对重复未变化文件内容可写“同上”；
- LLM judge request 后续重复项可写“略”；
- L1 和 L2 都要有，不要只放最终 summary。

现有 JSON 结果通常在：

```text
runs\<run-name>\...\summary.json
runs\<run-name>\...\trace.jsonl
```

以前生成过 PDF/周报，但本次交接的主要目标是测试通过与保留 trace；只有用户再次要求时再导出报告。

---

## 11. 建议的执行顺序

1. 运行 `git status`，确认当前未提交 diff；阅读 `trievo_bench/baselines/hermes_bridge.py` 与 `trievo_bench/l1_compat.py`。
2. 实现/测试 L1 的只读 workspace context 注入；不要给 L1 native 写入能力。
3. 先跑 `turn4`、`turn5`、`turn6` 真实单回合回归；同时验证 turn1 没被回归破坏。
4. 处理 turn2（群 ID、state、文件/消息流程）与 turn3 的 fact history 小问题。
5. 在合理 token 上限下完整跑 7 turns；失败才局部重跑，不要反复盲目完整运行。
6. L1 达标后，以最终 Docker 镜像跑 Hermes L2；然后复跑 SkillOpt 与 Trace2Skill。
7. 运行所有本地测试、审计 token、整理 trace，并提交代码。
8. 只有在所有上述项目完成后，才能回复用户“已完美完成”。当前绝对不能这么说。

---

## 12. 当前结论（一句话）

合并框架、Docker、DeepSeek 连接、L1 协议首回合、L1 evolution 请求级限制、Hermes L2/SkillOpt/Trace2Skill 的历史真实运行都已验证；但**最终代码尚未跑通完整 L1，且尚未在最终版本完成 L2/三 baseline 回归，因此整体任务尚未完成。**
