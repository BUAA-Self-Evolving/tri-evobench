# Tri-EvoBench 完整 Agent 交接手册

更新时间：2026-09-13（Asia/Shanghai）

本文面向在新设备上接手本项目的开发 Agent。目标是让接手者只依赖当前 Git 仓库和本文，便能理解研究问题、运行时架构、数据边界、五种 baseline、模型渠道、测试、正式评测和安全恢复方式，并在不破坏已有结果的前提下继续工作。

本文描述当前最终实现和必须保持的工程合同，不记录开发试错过程。历史 handoff、旧运行报告和旧问题清单只用于溯源；若与本文、`AGENTS.md`、当前代码或最新提交冲突，以这四者为准。

---

## 1. 项目身份与唯一正式仓库

项目名称：**Tri-EvoBench**。

GitHub 主仓库：

```text
https://github.com/BUAA-Self-Evolving/tri-evobench.git
```

主分支：`main`。

双模型渠道功能的基线提交为：

```text
51249b2 feat(provider): add Friday and PackyCode modes
```

接手时不要假设上述提交仍是远端最新提交，必须先获取最新引用并确认身份：

```bash
git fetch origin --prune
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
git log --oneline --decorate -10
git submodule status
```

正式工作目录就是 clone 得到的 `tri-evobench/`。旧名称中的 `migration-*`、`hermes-runtime/`、`tmp/all-baselines-merged` 和旧 worktree 不是新的权威入口。不要把旧目录中的运行输出、临时配置或代码覆盖到当前仓库。

在存在本地修改时不要直接 reset、checkout 覆盖或删除工作树。先检查 diff，保留用户和其他 Agent 的未提交工作，再决定 rebase、merge 或分提交整理。

---

## 2. 项目解决的研究问题

Tri-EvoBench 是一个**评测可自我进化 Agent 的统一运行时**。它不只比较一次问答质量，而是测量 Agent 在一条有序任务流中能否：

1. 完成当前任务并产生可验证的文本、工具调用或文件结果；
2. 从公开交互轨迹和公开反馈中更新自己的 Memory、Skills、Prompt 或 Harness；
3. 在后续 Turn 中利用这些更新；
4. 避免把错误事实、一次性指令、隐藏 gold 或评测器信息写入长期状态；
5. 在 Reset、Frozen、Evolving 三个匹配条件下产生可比较的学习增益；
6. 在模型失败、容器失败、存储故障和进程中断后保留付费回复与已执行副作用的边界，不伪造分数、不盲目重放。

项目的职责边界是：

```text
Benchmark package
  -> Flow / Turn / Interaction 编排
  -> Agent baseline Runtime
  -> Workspace / tool execution
  -> User Simulator
  -> private verifier 或 LLM Judge
  -> Turn 末 evolution barrier
  -> checkpoint / trace / report
  -> matched aggregation
```

Tri-EvoBench 负责评测协议和证据，不负责替 Agent 修答案，也不能把“程序成功退出”当成“任务通过”。最终得分只能来自 verifier/Judge 对实际可见回复、工具轨迹、状态变化和 workspace 产物的评估。

---

## 3. 核心概念与生命周期

### 3.1 Flow

Flow 是一条完整、有序且状态隔离的任务流。每个 Flow 拥有自己的共享 workspace、baseline 持久学习状态、token budget、trace、checkpoint 和报告，以及与其他 Flow 隔离的 Session、Memory、Skills 和临时配置。

Flow 之间不得共享可学习状态，除非数据协议未来显式定义另一种实验条件。

### 3.2 Turn

Turn 是 Flow 中一个有序任务。每个 Turn 必须使用新会话，不能通过延续聊天 history 偷渡学习；允许跨 Turn 继承的只有当前 condition 允许保留的 workspace，以及 manifest 声明的 Flow 级学习状态。

### 3.3 Interaction

Interaction 是评测器可见的一轮“用户请求 → Agent 回复 → 工具/动作 → 评分 → 反馈”。一次 Interaction 不等于一次模型 API 请求。完整 baseline 可以在一个 Interaction 内部执行多次模型调用和多轮工具调用，但必须返回该 Interaction 的增量 usage。

如果当前 Interaction 未达到成功阈值且未超过上限，User Simulator 可以提供下一轮公开反馈。分数不因下降或持平而提前终止；达到最大 Interaction 数时使用最后一次合法评分。

### 3.4 Evolution barrier

每个 Turn 结束后必须恰好跨过一次 evolution barrier：

- `update_enabled=true`：baseline 可以用公开 trajectory 和公开 feedback 更新声明的学习状态；
- `update_enabled=false`：只确认 barrier 完成，不得写入学习状态；Reset、Frozen 和 evaluator error 会使用该路径；
- barrier 返回前，后台 review、写文件和学习线程必须全部结束；
- evolution 失败时，Runtime 恢复 evolution 前的声明状态，不能让半完成更新进入下一 Turn。

### 3.5 State freeze

manifest 的 `state.paths` 和 `state.guard_paths` 定义可学习状态及守卫文件。Runtime 在 Interaction 前后计算 hash：

- Interaction 阶段不得修改长期学习状态；
- 非法写入会被恢复并导致 Interaction 失败；
- 只有 evolution 阶段允许更新；
- `ignore_paths` 只能用于不影响学习决策的计数器、锁或遥测，不能用于规避冻结检查。

---

## 4. Reset、Frozen、Evolving 三种条件

L2 正式自进化结论必须来自同一 baseline、同一数据版本、同一模型/参数、同一 timing profile 下的三条件匹配结果。

| Condition | Workspace | SUT 学习状态 | 用途 |
|---|---|---|---|
| `reset` | 每 Turn 重建 | 每 Turn 重建，禁止 update | 无跨 Turn 积累的基线 |
| `frozen` | 跨 Turn 保留 | 跨 Turn 保留但禁止 update | 分离 workspace 积累与 Agent 学习 |
| `evolving` | 跨 Turn 保留 | 跨 Turn 保留并允许 Turn 末 update | 完整自进化条件 |

主增益为 `Evolving - Reset`，诊断增益为 `Evolving - Frozen`。

目录标签不是条件正确执行的证据。实现变更必须验证 workspace 生命周期、状态冻结、`update_enabled` 和报告中的 condition 身份。

L2 CLI 为兼容旧调用，在未传条件时默认 `evolving`；这不代表单独的 evolving 运行可以支持正式 self-evolution claim。正式匹配矩阵必须显式传入三个条件。

---

## 5. 当前数据包与矩阵规模

权威数据入口位于 `benchmarks/current/`：

| 路径 | 内容 | 当前规模 |
|---|---|---:|
| `benchmarks/current/l1_arena` | 当前 L1 Arena | 23 streams，423 turns |
| `benchmarks/current/L2_Demo_V2` | 当前 L2 TaskStream Demo V2 | 6 streams，134 turns |

五 baseline 下：

- 默认 L1 + L2 evolving：145 个 stream-agent jobs，2,785 个 agent-turn；
- L1 加 L2 三条件完整矩阵：205 个 jobs，4,125 个 agent-turn；
- L1 共 `423 turns × 5 baselines = 2,115 agent-turn`；
- L2 三条件共 `134 turns × 5 baselines × 3 conditions = 2,010 agent-turn`。

当前 L2 streams：

| Stream | Turns | 主要任务类型 |
|---|---:|---|
| `airs_sick_textual_entailment_accuracy_001` | 2 | 文本蕴含分类 |
| `mlgym_titanic_tabular_classification_001` | 2 | 表格机器学习 |
| `skilllearn_stock_data_visualization_001` | 10 | 股票数据处理和可视化 |
| `skillsbench_data_to_d3_001` | 10 | 数据到 D3/Web 产物 |
| `sra_bigcodebench_pandas_dataframe_viz_001` | 100 | 长程 Pandas/可视化代码任务 |
| `tau2_telecom_mobile_data_roaming_001` | 10 | 电信策略和状态任务 |

L2 Turn 带有 `learning`、`application`、`probe` 等 `sequence_role`。聚合必须保持三条件的 stream、turn、顺序和 role 完全匹配。

其他数据目录：

- `benchmarks/l1_demo`：旧七轮 L1 demo；
- `benchmarks/l2_demo`：旧 episode 格式 L2 工作副本；
- `benchmarks/l1_life_v2`：独立的 10 streams × 60 turns 自包含 FactStream；
- `benchmarks/archive/`：历史数据版本，仅用于复现，不得与 current 混算。

`l1_life_v2` 是独立研究包，不可替代本次矩阵的 `l1_arena`。它评测事实披露、纠正、偏好应用、延迟问答、事实依赖工具、干扰项和长延迟 replay；Agent 只能看到 `agent_input`、工具 schema、grounded simulator 回复和可写 workspace，不能看到 `evaluation`、`facts.json`、rubrics、expected state/tool/workspace 或 judge 输出。

外部 L2 V3 应解压到 Git checkout 外，通过 `--l2-package` 显式传入。协议选择基于 schema/content，不基于目录叫 V2 还是 V3。

### 5.1 数据完整性现状

当前 runtime-repaired L2 包的只读合同检查应使用：

```bash
python tools/repair_l2_runtime_contracts.py benchmarks/current/L2_Demo_V2 --check
```

在本文更新时，该命令返回 `changes: []`，说明七类 Runtime 合同修复已物化。

`python tools/dataset_inventory.py --check` 当前会报告部分 `runtime-repair.json`、`verifier_cli.py` 和 `native_semantics.py` 与旧 SHA inventory 不一致。这表示数据 inventory 尚未与后续已提交的 verifier/runtime 修订重新冻结，不表示这些 tracked 文件是本地脏改动。发布新数据版本或声称逐文件 manifest 完整前，必须审计并重新生成对应 inventory；不得静默忽略，也不得为了让检查变绿而回退已修复 verifier。

---

## 6. 仓库结构与关键代码

```text
tri-evobench/
├─ trievo_bench/                 # 核心 Python Runtime
│  ├─ runner.py                  # 通用 spec Runner / CLI
│  ├─ spec.py                    # schema_version 0.1 解析
│  ├─ arena_runtime.py           # Arena 公共执行逻辑
│  ├─ l1_compat.py               # 旧 L1 session 兼容
│  ├─ l1_arena_compat.py         # 当前 L1 Arena 入口
│  ├─ l1_life_compat.py          # L1-Life v2 入口
│  ├─ l2_compat.py               # L2 package 检查与 CLI 路由
│  ├─ l2_agent.py                # L2 Stream 执行、判分、checkpoint
│  ├─ run_conditions.py          # Reset/Frozen/Evolving 语义
│  ├─ evolution_aggregation.py   # 三条件匹配聚合
│  ├─ model.py                   # OpenAI-compatible transport
│  ├─ provider_profiles.py       # PackyCode/Friday profile
│  ├─ sandbox.py                 # 本地/Docker sandbox
│  ├─ verifier.py                # deterministic/Python/LLM Judge
│  ├─ user_sim.py                # bounded feedback
│  ├─ docker_proxy.py            # 容器代理 bridge
│  ├─ docker_volume.py           # daemon-owned volume transport
│  ├─ storage_recovery.py        # 共享存储健康 gate
│  └─ baselines/                 # BaselineRuntime 与 bridges
├─ baseline/
│  ├─ manifests/                 # baseline manifests
│  ├─ adapters/<baseline>/       # NDJSON bridge + Dockerfile
│  ├─ hermes-agent/              # Git submodule
│  ├─ SkillOpt/                  # vendored source
│  ├─ Trace2Skill/               # vendored source
│  ├─ prime-agent/               # Git submodule
│  └─ adaptive-auto-harness/     # Git submodule (A-Evolve)
├─ benchmarks/                   # current、demo、archive 数据包
├─ examples/                     # benchmark/baseline override 示例
├─ schemas/                      # JSON schema
├─ sandbox/                      # verifier/task images
├─ tools/                        # 矩阵、审计、恢复、数据工具
├─ tests/                        # 主 pytest suite
├─ docs/                         # 架构、运行、恢复、审阅文档
├─ AGENTS.md                     # 仓库开发规范
└─ AGENT.md                      # 本交接手册
```

重点入口：

| 任务 | 入口 |
|---|---|
| 生成五 baseline 可移植配置 | `tools/prepare_matrix_configs.py` |
| 当前完整矩阵调度 | `tools/run_new_package_matrix.py` |
| 旧三 baseline L2 control driver | `tools/run_l2_condition_matrix.py` |
| 单独检查/运行 L2 | `python -m trievo_bench.l2_compat` |
| 单独运行当前 L1 Arena | `python -m trievo_bench.l1_arena_compat` |
| 通用 spec 运行 | `trievo-bench` / `python -m trievo_bench.runner` |
| 慢服务配置 | `tools/configure_slow_provider.py` |
| 存储恢复 | `tools/run_new_package_matrix.py --storage-guard ...` |
| 合并受控 reset retry | `tools/merge_l2_reset_retry.py` |
| 最终矩阵审计 | `tools/audit_final_matrix.py`、`tools/verify_final_audit.py` |

---

## 7. BaselineRuntime 1.0 与 NDJSON 协议

新 baseline 通过独立进程或 Docker 容器接入，不把完整第三方框架 import 到主评测进程。stdin/stdout 每行一个 JSON 对象；stdout 只能输出协议，日志写 stderr。

统一方法顺序：

```text
initialize
start_flow
  start_turn
    interact (一次或多次)
    evolve (恰好一次 barrier)
  start_turn ...
snapshot_state
end_flow
```

核心实现：

- `trievo_bench/baselines/base.py`：Reply、Usage、Snapshot、抽象 Runtime；
- `trievo_bench/baselines/process.py`：进程、NDJSON、超时、revision 校验、状态冻结和导出；
- `trievo_bench/baselines/legacy.py`：兼容 scripted/OpenAI action baseline；
- `trievo_bench/baselines/manifest.py`：manifest 查找、加载和 override；
- `trievo_bench/baselines/registry.py`：按 baseline id 创建 Runtime。

manifest 查找顺序：

1. `--baseline-manifest` 显式路径；
2. `baseline/manifests/<id>.json|yaml`；
3. `trievo_bench/baselines/manifests/<id>.json|yaml`。

完整 Agent 通常直接操作 `/workspace`，因此返回 `actions_are_executed=true`、`tool_calls_are_executed=true`。只生成旧式 Runner actions 的 adapter 才返回 false。usage 必须是当前 Interaction 或 evolution 的增量，不能返回整个 Session 的累计值。

Adapter 永远不能收到 `verifier_details`、`oracle_update`、private evaluation assets 或任意未显式列入 `metadata.public` 的 metadata。

---

## 8. 五种 baseline 的最终接入

### 8.1 Hermes

- id：`hermes`
- source：`baseline/hermes-agent` Git submodule
- pinned revision：`fce30d818e2a1833e99bb12160ccec648e8b2661`
- bridge：`trievo_bench/baselines/hermes_bridge.py`
- manifest：`trievo_bench/baselines/manifests/hermes.json`
- Dockerfile：`baseline/adapters/hermes/Dockerfile`
- 持久状态：`memories/MEMORY.md`、`memories/USER.md`、`skills/**/*`

Hermes 的 Flow 映射到独立 `HERMES_HOME` 和 bridge 进程；每个 Turn 创建全新 ACP Session；Interaction 调用 `run_conversation`；Turn 末显式执行 background review。严格 `turn_boundary` 模式关闭前台 memory/skill nudge 和写工具，只允许在 evolution barrier 内更新。

Hermes 支持 provider profile 传入的 `thinking=disabled|enabled`，并把控制同步到 `reasoning_config` 和 request `extra_body`。Session 累计 token 会在 bridge 中转换为 Interaction 增量；background review usage 单独记入 evolution。

### 8.2 SkillOpt

- id：`skillopt`
- source：`baseline/SkillOpt`，作为父仓库 tracked source
- bridge：`baseline/adapters/skillopt/bridge.py`
- manifest：`baseline/manifests/skillopt.json`
- Dockerfile：`baseline/adapters/skillopt/Dockerfile`
- 持久状态：`skill.md`、`history/**/*`

Interaction 使用 target backend；Turn 末使用公开 trajectory/feedback 执行 Reflect → Aggregate → Clip → Update。支持 target/optimizer 分离配置、原生工具循环和 OpenAI-compatible backend。Friday profile 会在 OpenAI-compatible 请求中发送 `thinking.type=enabled`，PackyCode 发送 disabled。

### 8.3 Trace2Skill

- id：`trace2skill`
- source：`baseline/Trace2Skill`，作为父仓库 tracked source
- bridge：`baseline/adapters/trace2skill/bridge.py`
- manifest：`baseline/manifests/trace2skill.json`
- Dockerfile：`baseline/adapters/trace2skill/Dockerfile`
- 持久状态：`skills/**/*`、`history/**/*`

Interaction 使用 `CLIOnlyAgent` 或 `CLISkillPreloadedAgent`；Turn 末将公开记录送入 MAP → REDUCE → APPLY → VERIFY 演化流程。生成配置通过 `generation_config.extra_body` 传 thinking。现有接入和冻结边界完整，但当前质量证据不应被表述为“Trace2Skill 的演化稳定提升”；任务通过和学习增益必须以新的 matched results 为准。

### 8.4 Prime Agent

- id：`prime-agent`
- source：`baseline/prime-agent` Git submodule
- pinned revision：`163dd5798cd40e86b540aa9ecb68201920a83a1b`
- bridge：`baseline/adapters/prime_agent/bridge.py`
- manifest：`baseline/manifests/prime-agent.json`
- Dockerfile：`baseline/adapters/prime_agent/Dockerfile`
- 持久状态：`prime-home/harness/**/*`

每个 Turn 创建新 JSONL RPC Session，Interaction 用 `prompt`，Turn 末单独调用 `refine` 更新 continual harness。前台禁止调用 refine/harness 写接口，Runtime 另外执行 hash freeze。自定义 OpenAI-compatible 服务通过 `api=openai-completions`、`base_url` 和 `api_key_env` 配置。

### 8.5 Adaptive Auto-Harness

- id：`adaptive-auto-harness`
- source：`baseline/adaptive-auto-harness` Git submodule
- pinned revision：`17bc9ebb7d4d142af1b109b43ef160031967cc9a`
- bridge：`baseline/adapters/adaptive_auto_harness/bridge.py`
- manifest：`baseline/manifests/adaptive-auto-harness.json`
- Dockerfile：`baseline/adapters/adaptive_auto_harness/Dockerfile`
- 持久状态：manifest、prompts、skills、memory、tools、evolution history

Bridge 使用 A-Evolve 的 `BaseAgent` / `AgentWorkspace` 文件合同，通过通用终端 Agent 完成任务，Turn 末调用 `AEvolveEngine.evolve`。提案只能更新启用的 prompt/skill/memory/tool 层。模型 interaction 和 evolver 都支持 PackyCode/Friday thinking profile。

---

## 9. 模型渠道：PackyCode 与 Friday

渠道定义集中在 `trievo_bench/provider_profiles.py`。生成配置只写公开端点、模型、thinking、超时、重试和凭证**环境变量名称**；不写凭证值。

| 模式 | 凭证变量 | Base URL | 模型 | Thinking | timeout | extra retries | proxy |
|---|---|---|---|---|---:|---:|---|
| `packycode` | `PACKYCODE_API_KEY`；兼容旧 `TRIEVO_API_KEY` | `https://cf.api.fan/v1` | `deepseek-v4-flash` | disabled | 150s | 4 | 继承宿主配置 |
| `friday` | `FRIDAY_APP_ID` | `https://aigc.sankuai.com/v1/openai/native` | `deepseek-v4-flash` | enabled | 300s | 5 | 默认直连并清理 proxy 变量 |

Friday 控制台当前默认 RPM 为 20，运行器在未显式指定并发时强制选择并发 1。只有渠道管理员已经调整配额并完成小规模验证后，才可显式提高 `--concurrency`。

运行器在内存中把选定凭证映射到各 adapter 需要的 `TRIEVO_API_KEY`、`OPENAI_API_KEY`、`OPENAI_COMPATIBLE_API_KEY`、`TARGET_OPENAI_COMPATIBLE_API_KEY` 和 `DEEPSEEK_API_KEY`。这些兼容变量不能被落盘到配置、日志或报告。

Friday 可能在 HTTP 200 body 中返回非 200 的业务 `status`。主模型 transport 会保留 provider `status/message`；429 和 5xx 只在同一次模型请求内重试，普通 4xx、鉴权、余额不足、空答案和 schema 错误不重试。transport retry 不能增加 benchmark Interaction，也不能重放已经执行过工具副作用的整个 Turn。

绝对禁止：

- 把 API key/App ID 写进代码、`.env.example`、Markdown、JSON override、命令参数、commit message 或 issue；
- 在命令回显、截图、trace 或聊天中暴露真实凭证；
- 将本地中转站 key 复制到企业电脑，或将企业 App ID 带出企业设备；
- 用完整矩阵测试连通性。

---

## 10. 新设备安装与环境准备

要求：Python 3.11+、Git、`uv`、可用 Docker Engine、足够的 Docker 磁盘空间，以及能访问选定模型端点的网络。

初始化：

```bash
git clone https://github.com/BUAA-Self-Evolving/tri-evobench.git
cd tri-evobench
git submodule update --init baseline/hermes-agent baseline/prime-agent baseline/adaptive-auto-harness
uv sync --frozen --group dev
uv run --frozen python -m pytest tests -q
docker info
```

必须检查 submodule revision。空 submodule 目录中的 Git 命令可能向上找到父仓库，从而产生误导性的 revision；所以不要只执行目录内 `git rev-parse`，优先使用：

```bash
git submodule status
git -C baseline/hermes-agent status --short
git -C baseline/prime-agent status --short
git -C baseline/adaptive-auto-harness status --short
```

正式 baseline Runtime 默认拒绝 pinned source revision 不匹配或 source tree 有 tracked/untracked 改动。`allow_revision_mismatch` 只用于本地开发，不能进入正式结果。

---

## 11. Docker 镜像

在仓库根目录构建：

```bash
docker build -t trievo-l2:0.1 -f sandbox/l2/Dockerfile .
docker build -t trievo-l2-browser:0.1 -f sandbox/l2/Dockerfile.browser .
docker build -t trievo-ubuntu:22.04 -f sandbox/ubuntu/Dockerfile .
docker build -t trievo-hermes:matrix-current -f baseline/adapters/hermes/Dockerfile .
docker build -t trievo-skillopt:matrix-current -f baseline/adapters/skillopt/Dockerfile .
docker build -t trievo-trace2skill:matrix-current -f baseline/adapters/trace2skill/Dockerfile .
docker build -t trievo-prime-agent:matrix-current -f baseline/adapters/prime_agent/Dockerfile .
docker build -t trievo-adaptive-auto-harness:matrix-current -f baseline/adapters/adaptive_auto_harness/Dockerfile .
```

两个 verifier image 和五个 baseline image 是不同职责：

- baseline 容器联网调用模型并操作逻辑 `/workspace`；
- verifier 容器读取私有 evaluator 资产并执行评分；
- browser verifier 单独包含 Playwright headless shell；
- native verifier 使用 `--network none` 并清空常见 proxy 环境变量。

修改 `baseline/adapters/**`、vendored baseline transport 或 submodule 后，必须重建对应镜像。代码测试通过不等于旧 Docker tag 已包含新代码；正式报告必须记录实际 image ID/revision。

---

## 12. 生成无凭证矩阵配置

PackyCode：

```bash
uv run --frozen python tools/prepare_matrix_configs.py \
  --provider-mode packycode \
  --output tmp/matrix-config-packycode
```

Friday：

```bash
uv run --frozen python tools/prepare_matrix_configs.py \
  --provider-mode friday \
  --output tmp/matrix-config-friday
```

生成目录包含八个 `*.override.json`、Hermes L1/L2 seed config 和 `provider-profile.json`。其中没有凭证。Friday Runtime 会要求 profile 文件存在且 mode 匹配，避免把企业凭证错误发送到 PackyCode 地址或反向混用。

默认 L2 `max_output_tokens=12000`。数据包声明的上限仍由 Runtime 约束；提高生成配置数值不能绕过数据合同，也不能把 qualification 参数冒充正式结果。

配置目录位于 `tmp/`，属于机器本地生成物，不提交 Git。换设备后重新生成，不要把旧机器的绝对路径或代理地址复制过去。

---

## 13. 最小检查、gate 与完整矩阵

### 13.1 免费的只读检查

```bash
uv run --frozen python -m pytest tests -q
uv run --frozen python -m compileall -q trievo_bench baseline/adapters tools tests
uv run --frozen python -m trievo_bench.l2_compat \
  --package benchmarks/current/L2_Demo_V2 --inspect
uv run --frozen python tools/validate_arena_packages.py benchmarks/current/l1_arena
uv run --frozen python tools/repair_l2_runtime_contracts.py \
  benchmarks/current/L2_Demo_V2 --check
```

### 13.2 PackyCode 运行

```powershell
$env:PACKYCODE_API_KEY = '<只在当前进程设置>'
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode packycode `
  --l1-package benchmarks/current/l1_arena `
  --l2-package benchmarks/current/L2_Demo_V2 `
  --l2-condition reset --l2-condition frozen --l2-condition evolving `
  --config-dir tmp/matrix-config-packycode `
  --root runs/packycode-matrix `
  --concurrency 1
```

旧环境只设置 `TRIEVO_API_KEY` 仍与 `packycode` 兼容。

### 13.3 Friday 企业设备运行

```powershell
$env:FRIDAY_APP_ID = '<只在企业设备当前进程设置>'
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode friday `
  --l1-package benchmarks/current/l1_arena `
  --l2-package benchmarks/current/L2_Demo_V2 `
  --l2-condition reset --l2-condition frozen --l2-condition evolving `
  --config-dir tmp/matrix-config-friday `
  --root runs/friday-matrix `
  --concurrency 1
```

### 13.4 有费用的最小预检

首次换设备、换网络、换镜像或换 provider 时，不运行完整矩阵。使用一个精确 job：

```powershell
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode friday `
  --level l2 `
  --l2-package benchmarks/current/L2_Demo_V2 `
  --config-dir tmp/matrix-config-friday `
  --root runs/friday-preflight `
  --only l2/evolving/hermes/s01 `
  --concurrency 1
```

job id 形式：

```text
l1/<baseline>/sNN
l2/<condition>/<baseline>/sNN
```

`--gate` 只运行调度器选出的最短 gate cells，用于受控连通和镜像检查；gate 不是完整成绩。gate 通过后移除参数、使用同一个 output root，合法完成结果会被复用。

默认未传 `--l2-condition` 时只运行 evolving。不要因为命令退出 0 就误称运行了完整三条件矩阵。

---

## 14. 资源预算与慢服务

`L2AgentWorkLimits` 的无 override 回退值为：

```text
max_model_calls = 10
max_tool_calls = 8
terminal_timeout_seconds = 90
episode_timeout_seconds = 480
max_output_tokens = 12000
evolution_timeout_seconds = 120
evolution_max_model_calls = 4
```

当前 `prepare_matrix_configs.py` 生成的正式矩阵 override 会显式使用更适合完整 Agent 的 L2 限制：

```text
max_model_calls = 24
max_tool_calls = 20
episode_timeout_seconds = 900
max_output_tokens = 12000
```

Hermes、SkillOpt 和 Trace2Skill 的 repaired SkillsBench L2 路径使用 `max_tool_calls=40`；Prime Agent 和 Adaptive Auto-Harness 仍使用常规 20。terminal/evolution 等未覆盖字段继续受 Runtime 和 adapter 的硬限制。数据包也可在受控范围内声明 stream-specific 限制；正式结果必须保存实际 applied limits 和 budget profile。

矩阵 driver 的 stream token guard 为：

```text
max(10,000,000, 10,000,000 × turn_count)
```

这是防止 runaway 的流级保险丝，不是预计花费，也不是科学上的每 Turn 等预算。真正可比的是每 Turn 的 model/tool/time/output 合同。

慢服务可通过 `tools/configure_slow_provider.py` 从冻结配置生成单独 overlay。默认每 API attempt 600 秒、20 次额外 retry、Interaction 21,600 秒、evolution 14,400 秒。慢服务结果必须使用一致 timing profile；旧 900 秒结果与 relaxed profile 混合时，Runtime 不生成正式 matched aggregation。

不要自动重试整个失败 job 来掩盖 provider 不稳定。`--job-error-policy continue` 只允许其他独立 job 继续，失败 job 仍为 `paused_error`，最终退出非零。

---

## 15. 结果、trace 与“完成”的判定

主要产物：

```text
runs/<root>/matrix_status.json
runs/<root>/result_matrix.json
runs/<root>/l2_evolution_aggregations.json
runs/<root>/<level>/.../driver-*.log
.../summary.json                 # L1
.../report.json                  # L2
.../trace.jsonl
.../baseline.stderr.log
.../baseline_state_snapshot.json
.../baseline_state/
.../_checkpoints/
```

一个矩阵只有同时满足以下条件才可称为完成：

1. `matrix_status.json.status == "completed"`；
2. 所有选中 cell 的 `completed_turns == expected_turns`；
3. report 无 `error`、`cleanup_error`；
4. 每个 Turn 没有 `final_category=error`；
5. `evaluation_status` 不是 evaluation error；
6. `passed` 和 terminal score 均非 null；
7. L2 正式聚合使用 `budget_profile=formal`；
8. 三条件结果的 identity、role、timing profile 完全匹配。

`result_matrix.json` 是选择范围的汇总，可能只是 `--only` 或 gate，不能脱离 status/phase 宣称完整结果。

`l2_evolution_aggregations.json` 只在同一 baseline 的 reset/frozen/evolving 全部合法完成时生成，包含 turn micro mean、stream macro mean、sequence-role mean、10-Turn bins、Evolving−Reset 和 Evolving−Frozen。

Evaluator/runtime error 必须是 null/unscored 并阻断正式聚合，不能转换成 Agent 0 分。合法低分不能借恢复机制重跑取最好结果。

分享 trace 前检查是否包含任务敏感数据。凭证不应出现；若出现，先视为安全事件处理，不能只做展示层遮挡后继续提交原文件。

---

## 16. Checkpoint 与安全恢复

Checkpoint 保存公共交互、workspace、声明的 baseline state、Turn 前缀和判分阶段，不保存活动 OS 进程。

关键阶段：

- `interact_started`：模型调用/工具执行可能已经开始，但尚无安全 reply；
- `reply_received`：Agent reply 已保存，但 workspace 同步可能尚未完成；
- `adjudicate`：Agent reply 和所需证据已保存，可只重做评分；
- 完整 Turn checkpoint：可继续下一 Turn。

规则：

- 有已保存 reply 的 adjudication failure 不应再次调用付费 Agent；
- 只有较早完成前缀、但当前 Interaction 已有 `interact_started` 时，不能静默回退并重放副作用；
- reset/evolving 的特定中断 Interaction 只有在用户明确授权后，才能通过 `--retry-interrupted-run` 和一次性 `--retry-authorization` 重试；
- 授权必须绑定 job、run、Turn、Interaction 和一次额外尝试，启动前消费；
- Frozen 不使用该重试入口；
- 后续 Interaction 的恢复必须保留此前已完成 Interaction 的判分 checkpoint；
- 不删除旧 trace/checkpoint，不覆盖输入 report。

`tools/merge_l2_reset_retry.py` 只用于把一个受控 reset retry 结果与已有真实后续结果合并到**新** report/checkpoint。它拒绝替换真实得分、拒绝不可信前缀，不调用模型/工具/verifier，也不覆盖输入。

---

## 17. 并发、共享存储与存活进程接管

输出根目录有独占 driver lock；不同根目录仍可能重复计费，所以启动前必须检查是否已有同矩阵在运行。

共享 Docker 存储紧张时启用：

```bash
export TRIEVO_STORAGE_GUARD=1
export TRIEVO_STORAGE_STATE_ROOT=/absolute/matrix/_storage
export TRIEVO_STORAGE_PATH=/actual/docker/root
export TRIEVO_STORAGE_MIN_FREE_GIB=20
export TRIEVO_STORAGE_RESUME_FREE_GIB=25
export TRIEVO_STORAGE_RECOVERY_SECONDS=30
export TRIEVO_STORAGE_INTERVAL=2
```

并传入：

```text
--storage-guard --recover-storage-errors --storage-retry-limit 5
```

Storage gate 在空间、inode 或 overlay fault 不健康时停止 admission，等待安全边界，不在任意工具调用中间杀 worker。恢复要求更高 free-space threshold、稳定窗口和真实 Docker create/cp/read-back probe。

旧 storage error 只有在日志、job identity 和 checkpoint history 证明安全时才进入 deferred recovery；鉴权、Judge 空答案和协议错误不能伪装成存储错误。

Linux 并发 dispatcher 重启时可按 PID、`/proc` start ticks、命令行和 output path 接管仍存活 worker。只凭 PID 不足以认领进程；身份不一致必须拒绝，绝不能杀死或接管无关进程。

---

## 18. Workspace、Docker 与网络隔离边界

baseline、task sandbox 和 verifier 使用同一个逻辑 `/workspace`，但运行于不同边界：

- 普通主机使用 bind mount；
- Docker daemon 看不到 checkout 时，L2 可设置 `TRIEVO_DOCKER_VOLUME_TRANSPORT=required`，通过 daemon-owned named volume 和 `docker cp` 传输；
- volume pull 使用同文件系统 staging 和可回滚目录交换；
- verifier package 只读、workspace 单独挂载；
- native verifier `--network none`；
- baseline 容器需要联网调用模型。

当前尚未把模型网络与 task tool 网络拆成独立 Tool Gateway，因此不能宣称进程层已经完全强制所有任务的 `execution_requirements.network=disabled`。这是公开结果必须披露的隔离限制。

Hermes terminal 在 Hermes 容器内运行，不自动拥有 task verifier image 的全部专用依赖。需要专用编译器/服务时，应基于任务 image 构建 adapter image，或未来实现 SandboxToolGateway；不能假设 verifier 容器就是 Agent 工具容器。

受限网络下可使用 `TRIEVO_DOCKER_PROXY_BRIDGE=required`，但必须提供该机器实际观测的容器源 CIDR。不得固化作者机器的 proxy host、端口或 CIDR。含用户名/密码的 proxy URL 会被拒绝。

Friday 企业内网 profile 默认移除 HTTP(S)/ALL proxy；不要把本地中转代理传入企业设备。

---

## 19. 从本地开发设备单向迁移到企业设备

企业设备“只能进不能出”时采用以下流程：

1. 本地开发、离线测试、commit 并 push 到 GitHub；
2. 生成不含 `.git` 历史也可以运行的源码压缩包，或在允许的入口机 clone 指定 commit；
3. 不打包 `.env`、`runs/`、`tmp/`、Docker logs、credentials、个人配置和 API key；
4. 把源码与所需公开数据单向传入企业设备；
5. 在企业设备初始化 submodules/依赖并构建镜像；
6. 在企业设备本地设置 `FRIDAY_APP_ID`；
7. 在企业设备重新生成 `tmp/matrix-config-friday`；
8. 先跑一个 `--only` job，再启动正式矩阵；
9. 企业设备产生的结果若不能导出，就在设备内保留 commit SHA、image ID、profile、status、report 和 trace 的完整审计链。

不要把 Friday App ID 写入传输包来“省一步配置”。凭证只在企业设备运行环境中注入。

---

## 20. 开发、测试与提交规范

Python 要求 3.11+。代码遵循 PEP 8、四空格、显式 imports、公共接口 type hints、清晰错误消息。JSON 使用 UTF-8 和两空格缩进。协议/manifest 字段必须保持向后兼容；破坏性变化应升级 schema/protocol，而不是静默改变旧行为。

改动流程：

```bash
git status --short --branch
git diff
uv run --frozen python -m compileall -q trievo_bench baseline/adapters tools tests
uv run --frozen python -m pytest tests -q
git diff --check
```

截至本文更新，合并最新 `origin/main` 后主测试基线为：

```text
263 passed, 6 skipped, 3 subtests passed
```

SkillOpt OpenAI-compatible transport 专项为 `8 passed`。

测试数量会随新提交增长；接手者应以“全部通过且无意外减少”为准，不要硬编码旧数量作为唯一正确性标准。

变更应增加行为测试，至少覆盖：

- success 和 malformed protocol；
- Flow 隔离、Turn 新 Session、Interaction freeze、一次 evolution；
- private evaluator/oracle 不泄漏；
- usage 增量和 token guard；
- condition-aware report/checkpoint；
- evaluator error 与 Agent failure 分离；
- Docker export/cleanup；
- provider thinking、错误分类和无凭证配置；
- resume 不重放已执行副作用。

单元测试不得调用真实 API。模型连通性只做用户授权的最小真实 smoke，不把 smoke 当全矩阵验证。

提交前审计：

```bash
git status --short
git diff --cached --check
git diff --cached
git grep -n -E 'sk-[A-Za-z0-9]{20,}' HEAD -- .
```

仓库中 benchmark 安全测试可能故意包含合成 key-shaped 字符串；必须结合路径和上下文判断，但任何真实凭证都不得提交。

不要提交：

- `runs/`、`tmp/`、`.env*`、credentials、logs；
- 机器绝对路径、个人代理地址；
- Docker 临时状态；
- 外部 V3 解压包，除非用户明确要求发布且许可/manifest 已审计；
- 为提高分数而修改过的任务、gold、rubric 或历史结果。

---

## 21. 新增或升级 baseline 的合同

新增 baseline 不应修改 Runner 的核心选择逻辑。正确流程：

1. 明确框架的持久学习状态和临时状态；
2. 实现 `baseline/adapters/<id>/bridge.py` 和 Dockerfile；
3. 映射 `initialize/start_flow/start_turn/interact/evolve/end_flow`；
4. 确保 Turn 内 freeze、Turn 末一次 update、公开反馈边界和新 Session；
5. 新建 manifest，声明 command、source revision、workspace、state paths、environment allowlist 和 capabilities；
6. 增加契约测试；
7. 构建新 image，记录 image ID；
8. 先离线 fake adapter，再真实单 job，再 matched matrix。

升级 Git submodule 时必须同时更新 manifest pinned revision、适配代码、镜像和测试。仅移动 gitlink 不算完成。

---

## 22. 结果声明与独立审阅原则

必须区分四层证据：

1. source/code review；
2. package/schema validation；
3. Docker image/runtime smoke；
4. 真实模型完整结果。

前一层通过不能替代后一层。不得用绿单测证明模型质量，不得用 image build 证明三条件执行，不得用少量 gate 宣称完整矩阵，不得用旧 image 的结果证明新代码。

正式 review 使用 `docs/reviews/REVIEWER_BRIEF.md` 的维度：L1 回归、三条件状态、error taxonomy、私有数据、V2/V3 路由、baseline lifecycle、matched aggregation 和证据层级。Findings 按 P0–P3 给出文件/行和失败场景，最终 verdict 为 `PASS` 或 `CHANGES_REQUIRED`。

历史报告中已有某 baseline 的高分或低分都不是未来版本的预设结论。新运行应保留完整配置、commit、submodule revision、image ID、数据 package identity、timing profile 和原始 trace，再进行分析。

---

## 23. 已完成并归档的研究成果

仓库已经归档一轮 Hermes、SkillOpt、Trace2Skill 的统一 Docker 真实模型矩阵。该结果覆盖 29 streams、87/87 stream-agent jobs、1,671/1,671 agent-turns；它是三 baseline 阶段的完整结果，不包含后来接入的 Prime Agent、Adaptive Auto-Harness，也不能自动代表未来 commit/image/provider 的表现。

归档聚合：

| 层级 | Baseline | Turns | 严格通过 | 通过率 | Mean score | Tokens |
|---|---|---:|---:|---:|---:|---:|
| L1 | Hermes | 423 | 265 | 62.65% | 0.6875 | 153.52M |
| L1 | SkillOpt | 423 | 266 | 62.88% | 0.7478 | 76.56M |
| L1 | Trace2Skill | 423 | 70 | 16.55% | 0.2260 | 102.56M |
| L2 | Hermes | 134 | 49 | 36.57% | 0.4746 | 82.82M |
| L2 | SkillOpt | 134 | 42 | 31.34% | 0.4142 | 50.25M |
| L2 | Trace2Skill | 134 | 9 | 6.72% | 0.2007 | 58.45M |

这轮结果支持的有限结论是：SkillOpt 在当时的 L1 分布上质量/Token 综合表现最好；Hermes 在当时的 L2 原生工具执行上领先；Trace2Skill 已完成真实接入和演化链，但调用放大明显，学习结果尚未稳定转化为任务收益。L1 与 L2 的绝对分数不能混合排名。

另有一个 L2 五 stream、每条件 34 matched turns 的三条件探针。归档 turn-micro mean：

| Baseline | Reset | Frozen | Evolving | Evolving−Reset | Evolving−Frozen |
|---|---:|---:|---:|---:|---:|
| Hermes | 0.0581 | 0.0375 | 0.2022 | +0.1441 | +0.1647 |
| SkillOpt | 0.1096 | 0.0588 | 0.1765 | +0.0669 | +0.1176 |
| Trace2Skill | 0.2125 | 0.2287 | 0.1941 | −0.0184 | −0.0346 |

该探针支持 Hermes/SkillOpt 在当时配置上的正向迁移信号，不支持 Trace2Skill 的当前演化链稳定提升。它仍是单轮/单配置证据，下一步科学验证应做多随机种子重复和 Trace2Skill skill 检索、MAP/REDUCE、APPLY/VERIFY 消融。

权威归档说明：

- `docs/final_matrix_analysis_20260911.tex`：87 jobs / 1,671 turns 的完整矩阵与短流 trace；
- `docs/weekly_progress_20260912_matrix_probe.tex`：三条件探针、role/stream 分析和成本；
- `docs/report_assets/final_matrix_analysis_20260911/`：可读 trace 素材。

这些结果不能替代新五 baseline、Friday profile 或后续修订版数据的重新验证。引用时必须同时写明其 baseline 范围、数据版本、模型渠道和提交/image 身份。

---

## 24. 当前明确边界与后续优先方向

当前已经具备：

- 统一 BaselineRuntime 1.0；
- 五 baseline 参数化切换；
- L1 Arena 和 L2 TaskStream；
- Reset/Frozen/Evolving；
- private verifier/Judge 边界；
- Flow workspace 和学习状态；
- Turn 级 evolution barrier；
- 状态冻结、回滚和导出；
- 增量 usage 和 token guard；
- condition-aware checkpoint；
- reply-preserving adjudication resume；
- 受控 interrupted Interaction retry；
- 并发、storage gate 和存活 worker 身份接管；
- bind mount / named-volume transport；
- matched L2 aggregation；
- PackyCode/Friday 双模型渠道。

继续工作时优先保持或推进：

1. 将 L2 runtime-repaired files 与发布用 SHA inventory 重新冻结并审计；
2. 完成模型网络与 task tool 网络的独立 Tool Gateway；
3. 对 Trace2Skill Action/TASK_COMPLETE 资格问题做独立修复和验证；
4. 在同 commit、同 image、同 package、同 timing profile 上完成五 baseline 三条件真实矩阵；
5. 对 L1-Life v2 做 reference judge 校准和完整 Evolving/Reset 对照；
6. 保持恢复逻辑保守，不用自动重跑替代证据审计；
7. 若迁移 Harbor，只替换 environment backend，不改变 Flow/Turn/Interaction、判分和 evolution 合同。

Harbor 目前是长期架构方向，不是已经完成的实现。Tri-EvoBench 仍使用自己的轻量 Runtime 和 Docker CLI backend。

---

## 25. `ssh graph` 的持久安全规则

以下规则只针对共享服务器 `graph`，无论从哪台设备接手都持续有效：

```text
host: graph
user/host: myuan@172.17.62.99
project target: /mnt/myuan/sunqh/tri-evobench-20260904
```

1. 操作前同时阅读 `runs/mx24/MONITOR_HANDOFF.md`；如果该文件不在当前 checkout，先确认运行归档位置，不得猜测现场状态。
2. 能不用 `sudo` 就不用。
3. 每一条确实需要的 `sudo` 命令都必须先向用户列出完整命令、目标、用途、影响和回滚办法，并获得明确审批。
4. 免密 sudo、sudo 组、旧授权或“迁移 Docker”的总目标都不授权新命令；只读 sudo 也要审批。
5. 不得用 root shell、sudo 脚本、Docker root 等效能力绕过审批。
6. 服务器是共享环境，不得未经审批停止/重启 Docker、影响他人容器、修改系统权限、迁移/删除系统数据或操作其他用户的 rootless Docker。
7. Docker 数据迁移必须先检查容器和占用，保留 NVIDIA runtime 等配置；先复制、校验并保留源数据。删除旧副本需要单独审批，不自动 prune。
8. 未完成迁移验证前，不重启旧评测或 heartbeat，不创建第二份可能重复计费的矩阵。

不要把本文的服务器标识理解为当前仍有运行任务；实际状态必须通过只读检查和最新 handoff 证据确认。

---

## 26. 接手阅读顺序

新 Agent 建议按以下顺序阅读，不需要先读历史聊天：

1. `AGENTS.md`：仓库级开发和安全规范；
2. `AGENT.md`：本文；
3. `README.md`：用户入口；
4. `docs/current-matrix.md`：当前矩阵、命令、输出和隔离；
5. `docs/provider-modes.md`：PackyCode/Friday；
6. `docs/baseline-runtime.md`：BaselineRuntime 和五框架接入；
7. `docs/spec.md`：Flow/Turn/Interaction 基础协议；
8. `benchmarks/README.md`：数据版本和可见性；
9. `docs/storage-recovery.md`、`docs/slow-provider-runtime.md`：生产运行恢复；
10. `docs/reviews/REVIEWER_BRIEF.md`：独立审阅标准；
11. 当前任务涉及的具体代码和测试。

`docs/handoffs/TRIEVOBENCH_HANDOFF.md` 是 2026-08-28 的历史交接，包含旧 worktree、三 baseline 和早期未完成状态；只用于理解历史，不是当前运行指令。

---

## 27. 接手后的标准起点

在任何修改或付费运行前完成：

```bash
git fetch origin --prune
git status --short --branch
git log --oneline --decorate -10
git submodule status
uv sync --frozen --group dev
uv run --frozen python -m pytest tests -q
uv run --frozen python -m compileall -q trievo_bench baseline/adapters tools tests
python tools/repair_l2_runtime_contracts.py benchmarks/current/L2_Demo_V2 --check
python -m trievo_bench.l2_compat --package benchmarks/current/L2_Demo_V2 --inspect
python tools/validate_arena_packages.py benchmarks/current/l1_arena
docker info
```

随后根据任务选择 focused tests、重建受影响镜像、生成对应 provider profile，并从一个精确 `--only` job 开始真实验证。任何完整矩阵、恢复授权、共享服务器提权、数据版本变更或结果发布都必须保留可审计证据。
