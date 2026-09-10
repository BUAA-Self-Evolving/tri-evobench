# Tri-EvoBench Baseline Runtime 接入说明

本文说明 Tri-EvoBench 如何接入 Hermes Agent，以及后续如何在不修改 Runner 的情况下接入 OpenHands、AutoGen 或其他自进化 Agent 框架。

## 1. 设计目标

本次改造解决三个问题：

1. Benchmark、Judge 与 baseline 框架解耦。
2. baseline 可以拥有自己的模型循环、终端、文件工具、Memory、Skills 和学习流程，而不是被当作单次 LLM API。
3. 新增 baseline 后，只通过 `--baseline <id>` 或 benchmark spec 中的 `baseline.type` 切换，不再修改 Runner。

旧的 `scripted` 和 `openai_compatible` 行为仍然保留，它们通过 `LegacyActionRuntime` 运行。新的完整 Agent 框架通过独立进程协议运行。

## 2. 总体架构

```text
Benchmark Spec
      │
      ▼
Tri-EvoBench Runner
      ├── User Simulator
      ├── Public Judge
      ├── Hidden Verifier（不进入 baseline 协议）
      ├── Flow / Turn / Interaction 生命周期
      ├── Token Budget
      └── BaselineRuntime
              │ NDJSON request/response
              ▼
         Baseline Adapter
              ├── Hermes bridge
              ├── OpenHands bridge
              └── 其他框架 bridge
                      │
                      ▼
               逻辑 /workspace
          （bind mount 或 named volume）
```

主要代码位置：

| 路径 | 职责 |
|---|---|
| `trievo_bench/baselines/base.py` | Runtime 生命周期、Reply、Usage、Snapshot 数据模型 |
| `trievo_bench/baselines/legacy.py` | 兼容原有 scripted/OpenAI action 模式 |
| `trievo_bench/baselines/process.py` | 独立进程、NDJSON、状态冻结、Token 计量、状态导出 |
| `trievo_bench/baselines/manifest.py` | baseline manifest 加载、覆盖和查找 |
| `trievo_bench/baselines/registry.py` | 根据 baseline id 构建 Runtime |
| `trievo_bench/baselines/hermes_bridge.py` | Hermes SessionManager 和自进化流程桥接 |
| `trievo_bench/baselines/manifests/hermes.json` | Hermes 默认运行声明 |
| `baseline/adapters/hermes/Dockerfile` | Hermes 隔离运行镜像 |

## 3. 统一生命周期

所有 baseline 都必须映射以下生命周期。

### 3.1 `start_flow`

- 一个 Flow 只启动一次。
- 创建 Flow 独占的 baseline 状态目录。
- 创建共享 workspace。
- 不同 Flow 的 Memory、Skills、SessionDB 必须隔离。

### 3.2 `start_turn`

- 每个 Turn 创建全新会话。
- Turn 之间不继承聊天 history。
- Flow 级学习状态继续保留。
- 上一个 Turn 必须已经完成 `evolve` barrier。

### 3.3 `interact`

- 一次 `interact` 是 benchmark 看到的一次 Interaction，不等于一次模型调用。
- baseline 内部可以进行任意次模型请求和工具调用。
- 返回最终文本、工具轨迹、事件和本次增量 usage。
- 完整框架直接操作 workspace；旧 action baseline 仍可让 Runner 执行动作。

### 3.4 `evolve`

- 每个 Turn 恰好跨过一次 evolution barrier。
- 输入仅包含公开 trajectory 和公开 Judge feedback。
- `EvolutionContext.update_enabled=false` 时 Adapter 必须只完成 barrier，不得写入 Memory、Skill、history 或其他 SUT 持久状态；Reset/Frozen 和 evaluator error 使用这一通用路径。
- `verifier_details`、`oracle_update`、隐藏评测文件不会进入外部 baseline 协议。
- Flow/Turn 的任意 `metadata` 默认视为私有；只有 `metadata.public` 子对象会传给 baseline。
- `evolve` 返回前，所有持久状态更新和后台任务必须完成。

### 3.5 `snapshot_state`

- 对 manifest 中声明的学习状态文件计算 SHA-256。
- Runner 记录 evolution 前后 diff。
- `interact` 阶段如果检测到非法持久写入，会恢复 Interaction 开始前的文件并将该 Interaction 标记为失败。
- `start_flow` 返回后立即建立稳定快照，因此第一个及后续所有 `start_turn` 都不能夹带学习状态更新。

### 3.6 `end_flow`

- 关闭框架会话和进程。
- 将白名单内的学习状态导出到运行目录。
- 删除包含临时配置和 SessionDB 的 Flow 临时目录。

## 4. Hermes 接入细节

### 4.1 ACP SessionManager

Hermes 已经通过 `acp_adapter.session.SessionManager` 提供新建 Session、固定工作目录、AIAgent 初始化、SessionDB 和完整工具集。Tri-EvoBench bridge 直接复用该 SessionManager，但使用 Tri-EvoBench 自己的 NDJSON 生命周期作为外层协议。这样不需要修改 Hermes 上游源码，也不需要让 Runner 理解 ACP。

当前 Hermes 源码固定为：

```text
fce30d818e2a1833e99bb12160ccec648e8b2661
```

运行前 Runtime 会校验 `baseline/hermes-agent` 的 Git revision，并拒绝 tracked/untracked 变更，避免相同 revision 实际运行不同源码。升级 Hermes 时必须同时更新 manifest revision 并重新执行契约测试；`options.allow_revision_mismatch=true` 只用于本地调试，不应出现在正式结果中。

### 4.2 Flow 和 Turn 映射

| Tri-EvoBench | Hermes |
|---|---|
| Flow | 独立临时 `HERMES_HOME` 和一个 bridge 进程 |
| Turn | 一个全新的 Hermes ACP Session |
| Interaction | 同一个 Turn Session 上的一次 `run_conversation` |
| Evolution | 显式 `_spawn_background_review` |
| Barrier | 等待 `_background_review_run.request_done` |
| Flow State | `memories/MEMORY.md`、`memories/USER.md`、`skills/` |

Turn 结束后旧 Session 会被关闭和移除。下一 Turn 只能通过 Memory/Skills 获得进化结果，不能通过恢复聊天记录绕过学习边界。

### 4.3 严格进化模式

默认 policy 为 `turn_boundary`：

1. Hermes 前台自动 background review 被关闭。
2. Memory nudge 和 Skill nudge 在前台会话中被关闭。
3. 前台工具白名单移除 `memory`、`skill_manage`、`session_search`。
4. Memory 和 Skills 仍会进入 Hermes 系统上下文，`skills_list`、`skill_view` 等只读能力仍可使用。
5. Turn 结束后 bridge 将公开 trajectory 和 feedback 作为 review focus，显式执行一次 Memory + Skill review。
6. bridge 等待 review provider/tool 阶段完成后才返回。
7. Runtime 再次扫描状态文件，记录实际变化。

还支持两个调试 policy：

- `native`：保留 Hermes 原生 nudge 和异步 review 行为，只在 Turn 末等待已有任务结束。
- `disabled`：不执行自进化。

正式 leaderboard 应只比较同为 `turn_boundary` 的结果。`native` 结果应单独分组。

### 4.4 Token 计量

Hermes 前台的 Token 和成本字段是 Session 累计值，bridge 会保存上一次值并转换为每个 Interaction 的增量。`api_calls` 是当前 `run_conversation` 的局部调用次数，因此直接作为本次 Interaction 的值，不做累计差分：

- `input_tokens`
- `output_tokens`
- `cached_read_tokens`
- `cached_write_tokens`
- `reasoning_tokens`
- `total_tokens`
- `api_calls`
- `estimated_cost_usd`

后台 review usage 从 Hermes `session_model_usage` 中读取，并以 `task='background_review'` 单独计入 evolution 阶段；`total_tokens` 同时包含普通输入、输出、cache read 和 cache write。Runner 将 interaction 和 evolution usage 汇总到同一个 Flow Token Budget。

## 5. 构建和运行 Hermes

### 5.1 构建镜像

在仓库根目录运行：

```bash
docker build \
  -t trievo-hermes:0.1 \
  -f baseline/adapters/hermes/Dockerfile \
  .
```

默认 manifest 使用 `trievo-hermes:0.1`。如需其他名称，通过 baseline override 修改 `options.image`。

### 5.2 准备干净配置

建议准备一个只包含模型配置、不包含历史 Memory/Skills 的 Hermes seed 目录：

```text
/absolute/path/hermes-eval-seed/
└── config.yaml
```

创建 override 文件：

```json
{
  "state": {
    "seed_from": "/absolute/path/hermes-eval-seed",
    "seed_files": ["config.yaml"]
  },
  "options": {
    "image": "trievo-hermes:0.1",
    "approval_policy": "session"
  }
}
```

不要将真实用户的 `memories/` 或 `skills/` 放进 seed，否则不同实验不是从相同初始学习状态开始。

模型密钥通过环境变量传入，不写入 benchmark spec 或 run artifacts。默认 manifest 允许以下变量进入 Hermes 容器：

```text
OPENAI_API_KEY
OPENROUTER_API_KEY
ANTHROPIC_API_KEY
DEEPSEEK_API_KEY
GEMINI_API_KEY
GOOGLE_API_KEY
XAI_API_KEY
NOUS_API_KEY
```

### 5.3 执行评测

```bash
trievo-bench \
  --spec examples/smoke.json \
  --baseline hermes \
  --baseline-config /absolute/path/hermes.override.json \
  --output runs/hermes
```

也可以写入 benchmark spec：

```json
{
  "baseline": {
    "type": "hermes",
    "state": {
      "seed_from": "/absolute/path/hermes-eval-seed"
    }
  }
}
```

CLI `--baseline` 优先级高于 spec。旧的 `--agent scripted` 和 `--agent openai_compatible` 仍作为兼容别名存在。

`--learner` 只控制旧的 legacy baseline；外部框架的 learner 由其 Adapter 和 `evolution.policy` 控制。

### 5.4 运行产物

每个 Flow 目录新增：

```text
<output>/<flow-id>/
├── summary.json
├── trace.jsonl
├── baseline.stderr.log
├── baseline_state_snapshot.json
└── baseline_state/
    ├── memories/
    └── skills/
```

`summary.json` 中新增 `baseline`、`baseline_runtime`、manifest hash、源码 revision、`state_snapshot`、每个 Interaction 的 `usage` 和每个 Turn 的 `evolution`。

临时 `HERMES_HOME` 中的 `.env`、config、auth 和 SessionDB 不会整体复制到输出目录；只导出 manifest 中声明的学习状态路径。

## 6. Baseline Manifest

Runner 按以下顺序查找 manifest：

1. `--baseline-manifest` 指定路径。
2. `<repo>/baseline/manifests/<baseline-id>.json|yaml`。
3. `trievo_bench/baselines/manifests/<baseline-id>.json|yaml` 内置 manifest。

最小 manifest：

```json
{
  "schema_version": "1.0",
  "id": "my-agent",
  "adapter": {
    "type": "process",
    "command": ["docker", "run", "--rm", "-i", "my-agent:latest"],
    "timeout_seconds": 900
  },
  "workspace": {
    "mode": "shared_volume",
    "adapter_path": "/workspace"
  },
  "state": {
    "adapter_path": "/agent-state",
    "paths": ["memory/**/*", "skills/**/*"],
    "enforce_turn_freeze": true
  },
  "evolution": {
    "policy": "turn_boundary",
    "timeout_seconds": 600
  }
}
```

常用字段：

| 字段 | 说明 |
|---|---|
| `adapter.command` | 独立 Adapter 启动命令；支持模板变量 |
| `adapter.cwd` | 启动目录 |
| `adapter.timeout_seconds` | 默认请求超时 |
| `source.path` | baseline 源码目录 |
| `source.revision` | 必须匹配的 Git commit；默认同时要求源码树干净 |
| `environment.inherit` | 允许传给 Adapter 的宿主环境变量白名单 |
| `environment.set` | Adapter 固定环境变量，可使用模板 |
| `workspace.adapter_path` | Adapter 看到的 workspace 路径 |
| `state.adapter_path` | Adapter 看到的状态目录 |
| `state.paths` | 学习状态快照和导出白名单 glob |
| `state.guard_paths` | 参与冻结检查和 hash、但不导出的配置/框架文件 glob |
| `state.ignore_paths` | 从快照、冻结检查和导出中排除的易变遥测文件 glob；仅限不影响学习决策的运行计数或锁文件 |
| `state.seed_from` | 初始配置目录 |
| `state.seed_files` | 从 seed 复制的相对路径 |
| `state.enforce_turn_freeze` | 检测并恢复 Interaction 期间的非法学习状态写入 |
| `evolution.policy` | `turn_boundary`、`native` 或 `disabled` |
| `capabilities` | 记录到运行元数据的能力声明 |
| `options` | Adapter 自定义参数和命令模板值 |

可用模板变量：

```text
{repo_root}   仓库根目录
{workspace}   Flow 的宿主 workspace
{state_dir}   Flow 的临时 baseline 状态目录
{flow_id}     原始 Flow id
{flow_name}   可用于进程/容器名称的安全 Flow id
{instance_id} 本次 Adapter 随机实例 id
{uid}/{gid}   宿主用户 id
```

`options` 中的标量字段也会成为模板变量，例如 `{image}`。

## 7. Adapter NDJSON 协议

Adapter 的 stdin/stdout 每行一个 JSON 对象。stdout 只能输出协议，日志必须写 stderr。

```json
{"id": 1, "method": "initialize", "params": {}}
{"id": 1, "result": {}}
{"id": 1, "error": {"message": "reason"}}
```

### 7.1 `initialize`

输入包含 protocol、baseline id、options 和 evolution 配置。Adapter 必须返回相同的 `protocol_version`：

```json
{
  "protocol_version": "1.0",
  "baseline_id": "my-agent",
  "options": {},
  "evolution": {}
}
```

### 7.2 `start_flow`

```json
{
  "flow_id": "flow-001",
  "workspace": "/workspace",
  "state_dir": "/agent-state",
  "metadata": {}
}
```

### 7.3 `start_turn`

```json
{
  "turn_id": "turn-001",
  "request": "task request",
  "max_interactions": 3,
  "metadata": {}
}
```

### 7.4 `interact`

输入包含 request、公开 conversation history、sandbox 文件摘要和剩余 Token Budget。推荐响应：

```json
{
  "text": "final response",
  "actions": [],
  "tool_calls": [
    {"tool": "terminal", "arguments": {"command": "..."}, "result": "..."}
  ],
  "events": [
    {"type": "tool.completed", "tool": "terminal"}
  ],
  "usage": {
    "input_tokens": 100,
    "output_tokens": 20,
    "total_tokens": 120,
    "api_calls": 2
  },
  "actions_are_executed": true,
  "tool_calls_are_executed": true
}
```

Usage 必须是本次 Interaction 增量，不能返回 Session 累计值。完整 Agent 框架通常直接修改 workspace，因此两个 `*_are_executed` 应为 `true`。如果 Adapter 只是生成旧式 Runner action，应显式返回 `false`。

### 7.5 `evolve`

```json
{
  "turn_id": "turn-001",
  "trajectory": [],
  "feedback": []
}
```

响应必须在学习状态写入完成后返回：

```json
{
  "completed": true,
  "events": [{"type": "evolution.completed"}],
  "usage": {"total_tokens": 80, "api_calls": 1}
}
```

`completed` 必须为 `true`。返回 `false` 会使当前 Flow 失败，Runtime 会恢复 evolution 前的已声明学习状态，避免半完成更新进入下一 Turn。

### 7.6 `end_flow`

Adapter 应关闭会话、后台任务和数据库连接，返回响应后退出进程。

## 8. 新增其他 baseline

以下流程不需要修改 `runner.py` 或 `registry.py`。

### 第一步：确定框架状态边界

明确哪些文件或数据库代表可学习状态、框架是否会在前台自动写状态、是否存在异步 learner，以及如何确认后台更新真正结束。如果框架不能提供稳定的 evolution barrier，不应直接用于严格 leaderboard。

### 第二步：实现 bridge

```text
baseline/adapters/<baseline-id>/
├── bridge.py
└── Dockerfile
```

bridge 实现本文 NDJSON 方法，并把统一生命周期映射到框架原生 API。优先使用框架正式的 server/session API，不要解析 CLI 彩色文本，也不要把完整框架 import 到 Tri-EvoBench 主进程。

### 第三步：实现 evolution gate

至少保证：

1. Turn 内学习状态不可改变。
2. Turn 末只更新一次。
3. 只使用公开轨迹和反馈。
4. 下一 Turn 开始前后台更新完成。
5. 新 Turn 不恢复旧聊天 history。

如果框架原生语义不同，可同时提供 `native` 模式，但必须与严格结果分开。

### 第四步：添加 manifest

创建 `baseline/manifests/<baseline-id>.json`，声明命令、镜像、revision、环境白名单、workspace、状态路径和 evolution policy。完成后直接运行：

```bash
trievo-bench --spec <benchmark.json> --baseline <baseline-id>
```

### 第五步：添加契约测试

至少覆盖：

- Flow 间状态隔离。
- Turn 使用新 Session。
- Interaction 内状态 hash 不变。
- 每 Turn 恰好一次 evolution。
- evolution 超时会失败，而不是带着后台任务进入下一 Turn。
- Token usage 为增量且 interaction/evolution 都计费。
- `verifier_details`、oracle 和隐藏路径没有进入 Adapter。
- Adapter 异常退出后能清理容器和状态目录。
- 固定源码 revision 不匹配时拒绝运行。

仓库中的 `tests/fixtures/fake_baseline_adapter.py` 和 `tests/test_baseline_runtime.py` 是最小协议参考。

## 9. 当前边界与后续演进

当前版本已经实现参数化 baseline 选择、独立进程/容器隔离、Flow 级共享 workspace、Hermes 严格 Turn evolution gate、学习状态冻结检测和恢复、隐藏 verifier 数据隔离、Token 计量和状态快照。

baseline、任务 sandbox 和 verifier 都使用同一个逻辑 `/workspace`。普通主机通过宿主 bind mount
传输；daemon 看不到 checkout 路径的嵌套/rootless 环境，可由 TaskStream Runtime 使用
daemon-owned named volume 和 `docker cp` 传输。两种方式不改变 `BaselineRuntime 1.0` 生命周期。
Hermes 的 shell 命令运行在 Hermes 容器，而不是任务 sandbox 容器。因此：

- 文件生成、编辑和由 verifier 在任务容器中执行测试的 benchmark 可以直接使用。
- 如果 Agent 自己必须使用任务镜像中的专用编译器、服务或系统依赖，需要让 Hermes Adapter 镜像基于对应任务镜像构建，或继续实现 `SandboxToolGateway`，将 terminal/process 调用转发到任务容器。

建议下一阶段把 `SandboxToolGateway` 定义为独立本地 RPC 服务，并在各 Adapter 中仅替换终端/文件 backend。该扩展不会改变 `BaselineRuntime 1.0`，也不会影响 `--baseline` 切换方式。

Tri-EvoBench 当前继续使用自己的轻量 Runtime，不依赖 Harbor。长期边界是让 Tri-EvoBench 只负责
Benchmark contract、Flow/Turn/Interaction、Reset/Frozen/Evolving、Judge/verifier 编排和结果记录；
容器创建、Workspace transport、网络策略与 teardown 由可替换的 environment backend 负责。
当前 Docker CLI 是第一个 backend，未来可以增加 Harbor backend，把相同生命周期映射到 Harbor
task/environment/verifier，而不改 L1/L2 数据合同或 baseline NDJSON 协议。此处只是迁移边界，
不代表仓库已经实现或验证 Harbor。

## 10. SkillOpt 接入

仓库中的 `baseline/SkillOpt` 通过 `baseline/manifests/skillopt.json` 注册为 `skillopt` baseline。它使用独立的 NDJSON bridge：Interaction 调用 SkillOpt target backend，Turn 结束时把公开 trajectory 和 feedback 交给 SkillOpt 的 Reflect → Aggregate → Clip → Update 流程。学习结果保存在 Flow 隔离的 `skill.md` 和 `history/` 中，Interaction 期间的写入会被 Runtime 冻结检查拒绝。

默认选择 SkillOpt：

```bash
trievo-bench --spec <benchmark.json> --baseline skillopt --output runs/skillopt
```

也可以在 benchmark spec 中写入 `baseline.type`，或用 `--baseline-config` 覆盖模型、Skill、配置和 SkillOpt 超参数：

```bash
trievo-bench \
  --spec <benchmark.json> \
  --baseline skillopt \
  --baseline-config examples/skillopt-baseline.override.json \
  --output runs/skillopt
```

`options.config` 默认是 `baseline/SkillOpt/configs/searchqa/default.yaml`；`options.skill` 指定初始 Skill。`target_backend`、`target_model`、`optimizer_backend`、`optimizer_model`、`config_options`、`edit_budget`、`minibatch_size`、`skill_update_mode` 和 `evolution.policy` 都可以通过 baseline override 调整。SkillOpt 的运行依赖请按 `baseline/SkillOpt/requirements.txt` 安装；密钥只从 manifest 声明的环境变量传入，不写入 spec 或运行产物。

需要容器隔离时，可构建 `baseline/adapters/skillopt/Dockerfile`，再用 `examples/skillopt-docker.override.json` 覆盖 Adapter 命令：

```bash
docker build -t trievo-skillopt:0.1 -f baseline/adapters/skillopt/Dockerfile .
trievo-bench --spec <benchmark.json> --baseline skillopt \
  --baseline-config examples/skillopt-docker.override.json \
  --output runs/skillopt
```

## 11. Trace2Skill 接入

仓库中的 `baseline/Trace2Skill` 通过 `baseline/manifests/trace2skill.json` 注册为 `trace2skill` baseline。它使用独立的 NDJSON bridge：Interaction 在共享 workspace 中直接运行 Trace2Skill 的 `CLIOnlyAgent` 或 `CLISkillPreloadedAgent`，Turn 结束时把 Runner 提供的公开 trajectory 和 Judge feedback 转换为 Trace2Skill 的并行混合记录，并执行 MAP → REDUCE → APPLY → VERIFY 演化流程。Bridge 不接收 `verifier_details`、`oracle_update` 或私有 metadata。

默认选择 Trace2Skill：

```bash
trievo-bench \
  --spec <benchmark.json> \
  --baseline trace2skill \
  --baseline-config examples/trace2skill-baseline.override.json \
  --output runs/trace2skill
```

`options.agent` 默认是 `cli_skill_preloaded`，也可以设置为 `cli_only`。默认 Skill 根目录是 `baseline/Trace2Skill/spreadsheet_agent/skills`；`options.skill` 可指定一个 `SKILL.md` 或 Skill 目录，`options.skill_name` 指定演化的 Skill 子目录。模型参数使用 `options.model`、`options.base_url`、`options.llm_client`、`options.generation_config` 和 `options.max_turns`；未设置模型时读取 `TRIEVO_MODEL`/`OPENAI_API_KEY`/`OPENAI_BASE_URL`。

Trace2Skill 的学习状态保存在 Flow 独占的 `skills/` 与 `history/`，Interaction 阶段受到 Runtime 冻结检查保护。可通过 `evolution.policy=disabled` 做不演化的控制实验。运行依赖按 `baseline/Trace2Skill/README.md` 安装：

```bash
python -m pip install openai tqdm openpyxl requests diskcache
```

需要容器隔离时，构建 `baseline/adapters/trace2skill/Dockerfile`，并使用 `examples/trace2skill-docker.override.json`：

```bash
docker build -t trievo-trace2skill:0.1 -f baseline/adapters/trace2skill/Dockerfile .
trievo-bench --spec <benchmark.json> --baseline trace2skill \
  --baseline-config examples/trace2skill-docker.override.json \
  --output runs/trace2skill
```

## 12. Prime Agent 接入

`baseline/prime-agent` 固定为 manifest 中记录的上游 commit，并通过 Prime Agent 原生 JSONL RPC
接入。每个 Turn 启动一个新 RPC Session，Interaction 通过 `prompt` 执行，工具直接操作共享
`/workspace`；Turn 结束时由 Runtime 单独调用 `refine`，把公开 trajectory 和 Judge feedback 写入
Flow 级 continual harness。前台提示明确禁止 `refine.run` 和 harness 写操作，Runtime 还会通过状态
hash 冻结检查阻止绕过 Turn barrier。

```bash
git submodule update --init baseline/prime-agent
docker build -t trievo-prime-agent:0.1 -f baseline/adapters/prime_agent/Dockerfile .
trievo-bench --spec <benchmark.json> --baseline prime-agent \
  --baseline-config examples/prime-agent-baseline.override.json \
  --output runs/prime-agent
```

常用超参数位于 `options`：`provider`、`model`、`thinking`、`max_output_tokens`。使用兼容
OpenAI 的自定义服务时设置 `base_url`、`api=openai-completions` 和 `api_key_env`；示例见
`examples/prime-agent-openai-compatible.override.json`。学习状态只导出
`prime-home/harness/`，Session、Python REPL 状态和运行配置不会作为学习状态带到下一 Flow。

## 13. Adaptive Auto-Harness 接入

`baseline/adaptive-auto-harness` 固定为 A-Evolve 上游 commit。Bridge 使用其 `BaseAgent` /
`AgentWorkspace` 文件系统契约加载 prompt、skills、memory，并用通用终端 agent 直接完成任务；
Turn 末通过上游 `AEvolveEngine.evolve` 执行一次演化，禁用额外 trial 和隐藏 evaluator 输入。
Runner 只传公开 trajectory 与公开 feedback，提案只能更新 manifest 声明的 prompt、skill、memory
和可选工具 registry 文件。

```bash
git submodule update --init baseline/adaptive-auto-harness
docker build -t trievo-adaptive-auto-harness:0.1 \
  -f baseline/adapters/adaptive_auto_harness/Dockerfile .
trievo-bench --spec <benchmark.json> --baseline adaptive-auto-harness \
  --baseline-config examples/adaptive-auto-harness.override.json \
  --output runs/adaptive-auto-harness
```

常用超参数为 `model`、`base_url`、`max_steps`、`tool_timeout`、`evolver_model`、
`evolver_max_tokens`，以及 `evolve_prompts`、`evolve_skills`、`evolve_memory`、`evolve_tools`。
设置 `evolution.policy=disabled` 或运行 frozen/reset condition 时只跨过 barrier，不更新学习状态。
