# Tri-EvoBench

## 源码 ZIP 说明（2026-10-30）

本包来自当前主开发目录，包含本项目运行时、全部 baseline 接入代码与 manifest、
工具、测试、Dockerfile、配置示例及通用文档。它是用于手动上传的源码快照。

**包中不含评测数据、实验结果、trace/checkpoint、凭证、本机专用启动脚本、
虚拟环境和第三方 baseline 的完整仓库。** 下文原项目说明中涉及数据包已附带、
Git submodule 和完整矩阵的操作，需先补齐相应依赖；不能仅凭此 ZIP 复现历史实验。
依赖位置、manifest 声明版本和上传方法见 [源码包使用说明](SOURCE_PACKAGE.md)。
手动上传普通文件不会创建 Git submodule；`.gitmodules` 仅保留上游地址映射。


Tri-EvoBench 是一个面向自演化 Agent 的评测运行时，提供统一的 Flow、Turn、Interaction 执行协议，以及 L1/L2 demo 适配、Sandbox、Verifier、LLM Judge 和 Learner 接口。

当前版本为研究原型，运行配置使用 `schema_version = 0.1`。

## Baseline 框架切换

Runner 现在通过统一 `BaselineRuntime 1.0` 接入完整 Agent 框架。内置 Hermes 接入支持 Flow 级 Memory/Skills、Turn 级新会话、严格 Turn 末单次进化、状态快照和增量 Token 计量。

SkillOpt 也已注册为 `skillopt` baseline，可仅通过 baseline 超参数切换：

```bash
pip install -r baseline/SkillOpt/requirements.txt
trievo-bench --spec <benchmark.json> --baseline skillopt \
  --baseline-config examples/skillopt-baseline.override.json \
  --output runs/skillopt
```

不指定 override 时，manifest 使用 `baseline/SkillOpt` 的 SearchQA 初始 Skill 和默认配置。

Trace2Skill 也已注册为 `trace2skill` baseline，默认使用预加载 XLSX Skill，并在每个 Turn 结束时执行一次公开轨迹演化：

```bash
pip install openai tqdm openpyxl requests diskcache
trievo-bench --spec <benchmark.json> --baseline trace2skill \
  --baseline-config examples/trace2skill-baseline.override.json \
  --output runs/trace2skill
```

也可以直接在 benchmark spec 中设置 `baseline.type` 为 `trace2skill`。通过 override 的 `options.agent` 可切换 `cli_skill_preloaded`/`cli_only`，并可调整 `model`、`max_turns`、`temperature`、`skill`、`skill_name`、`evolution_batch_size`、`max_workers` 等参数。

Prime Agent、Adaptive Auto-Harness 与 OpenCode 也已通过同一 Runtime 注册，可只改 baseline 超参数切换。OpenCode 按“一个 stream 一个 session”运行，同一 Turn 的重试及后续 Turn 都续接这个 session：

```bash
docker build -t trievo-prime-agent:0.1 -f baseline/adapters/prime_agent/Dockerfile .
trievo-bench --spec <benchmark.json> --baseline prime-agent \
  --baseline-config examples/prime-agent-baseline.override.json --output runs/prime-agent

docker build -t trievo-adaptive-auto-harness:0.1 \
  -f baseline/adapters/adaptive_auto_harness/Dockerfile .
trievo-bench --spec <benchmark.json> --baseline adaptive-auto-harness \
  --baseline-config examples/adaptive-auto-harness.override.json \
  --output runs/adaptive-auto-harness

docker build -t trievo-opencode:0.1 -f baseline/adapters/opencode/Dockerfile .
trievo-bench --spec <benchmark.json> --baseline opencode \
  --baseline-config examples/opencode-baseline.override.json \
  --output runs/opencode
```

也可在 spec 中设置 `baseline.type` 为 `prime-agent`、`adaptive-auto-harness` 或 `opencode`。前两者使用
Turn 级新会话和 Flow 级学习状态；OpenCode 则保留整个 Flow 的原生 session，并把 Turn 映射为 session
内的一次或多次交流。三者都会回传增量 Token 计量。

```bash
docker build -t trievo-hermes:0.1 -f baseline/adapters/hermes/Dockerfile .
# 先将 override 中的 seed_from 改成干净 Hermes 配置目录的绝对路径
trievo-bench --spec examples/smoke.json --baseline hermes --baseline-config examples/hermes-baseline.override.json
```

Hermes 部署、协议字段和新增其他 baseline 的完整说明见 [Baseline Runtime 接入说明](docs/baseline-runtime.md)。

Codex 已注册为 `codex` baseline，使用仓库固定版本的 `baseline/codex` 源码构建：

```bash
git submodule update --init baseline/codex
docker build -t trievo-codex:0.1 -f baseline/adapters/codex/Dockerfile .
export OPENAI_API_KEY=<your-key>
trievo-bench --spec <benchmark.json> --baseline codex \
  --baseline-config examples/codex-baseline.override.json --output runs/codex
```

也可在 spec 中设置 `baseline.type=codex`。`options.model`、`reasoning_effort`、`base_url`、
`api_key_env` 和 `request_timeout_seconds` 控制模型与执行参数。自定义模型服务必须支持
Responses API。默认关闭跨 Turn 学习；使用 `examples/codex-evolving.override.json` 可启用
仅基于公开轨迹、在 Turn 末执行的记忆更新。每个 Turn 新建会话，同一 Turn 内的 Interaction
继续该会话。完整 L1 Arena / L2 命令与状态边界见 [Codex 接入说明](docs/codex-baseline.md)。

DeepSeek Harness 与 ZCode 已注册为 `deepseek-harness`、`zcode`，可通过
`--baseline` 或 spec 的 `baseline.type` 切换。构建命令、超参数、原生会话和状态边界见
[接入说明](docs/deepseek-harness-zcode-baselines.md)，示例配置位于
`examples/deepseek-harness-baseline.override.json` 和 `examples/zcode-baseline.override.json`。

## Features

- Flow 内共享 Sandbox，支持本地和 Ubuntu Docker 后端
- 通过 manifest 和独立 Adapter 参数化切换 Agent baseline
- 支持程序 Verifier、Python Verifier 和 LLM Judge
- L2 TaskStream 支持匹配的 reset、frozen、evolving reference conditions
- 旧 spec runner 继续支持 frozen、raw-replay、evolving、oracle-update learner
- 支持 L1 session/turn 数据和 L2 Stream/Episode 数据
- 保存完整 Interaction trace、Sandbox snapshot、状态和 token usage

## Installation

```bash
git clone git@github.com:BUAA-Self-Evolving/tri-evobench.git
cd tri-evobench
git submodule update --init baseline/hermes-agent baseline/prime-agent baseline/adaptive-auto-harness baseline/opencode baseline/codex baseline/deepseek-harness baseline/ZCode
uv sync --frozen --group dev
```

```powershell
# Windows PowerShell
uv sync --frozen --group dev
uv run --frozen python -m pytest tests -q
```

```bash
# Linux / macOS；命令均在隔离的项目 .venv 中执行
uv run --frozen python -m pytest tests -q
```

Python 版本要求：3.11+。Docker 在 Docker Sandbox、Hermes 等容器 baseline 和 L2 运行时需要。

## Run with a model

```powershell
$env:TRIEVO_API_KEY = '<your-api-key>'
$env:TRIEVO_BASE_URL = 'https://api.deepseek.com'
$env:TRIEVO_MODEL = '<model-id>'

uv run --frozen trievo-bench --spec examples/deepseek_smoke.json --output runs/model-smoke --max-total-tokens 3000000
```

## Ubuntu Sandbox

```bash
docker build -t trievo-ubuntu:22.04 sandbox/ubuntu
```

```powershell
trievo-bench --spec examples/smoke.json --output runs/docker-smoke --backend docker --image trievo-ubuntu:22.04
```

## L1

L1 adapter 读取 `session-N/turnN/task.json` 格式的数据包。

```powershell
$env:TRIEVO_API_KEY = '<your-api-key>'
$env:TRIEVO_BASE_URL = 'https://api.deepseek.com'
$env:TRIEVO_MODEL = '<model-id>'

python -m trievo_bench.l1_compat `
  --session '<path-to-session-1>' `
  --turns 'turn1,turn2,turn3,turn4,turn5,turn6,turn7' `
  --backend docker `
  --image trievo-ubuntu:22.04 `
  --learner evolving `
  --output runs/l1
```

### L1-Life v2

完整 L1-Life v2 数据包存放在 `benchmarks/l1_life_v2/`。适配器只将每回合的
Agent-visible messages、可用工具 schema 和当前 stream 工作区传给 baseline；rubrics、GT 和 judge
模板始终留在评测端。先检查数据包，再运行一条 stream 的指定回合：

```powershell
python -m trievo_bench.l1_life_compat --package benchmarks/l1_life_v2 --inspect

python -m trievo_bench.l1_life_compat `
  --package benchmarks/l1_life_v2 `
  --stream stream-1 `
  --turns 'turn1,turn2,turn3' `
  --backend docker `
  --image trievo-ubuntu:22.04 `
  --baseline hermes `
  --baseline-config tmp_hermes_l1_merged.override.json `
  --output runs/l1-life-smoke
```

不指定 `--turns` 时只运行 `turn1`，避免误触发整条 60-turn stream。

## L2

最新的 L1 Arena / L2 TaskStream（包括外部 V3 package）及六种框架 baseline
完整矩阵，请使用 [当前版本运行指南](docs/current-matrix.md)。
指南包含从源码构建 Docker、可移植配置、完整日志与检查点恢复；下方命令仍适用于旧版 episode 数据包。
PackyCode 与企业 Friday 渠道的无密钥双模式配置见 [模型渠道模式](docs/provider-modes.md)。

L2 adapter 用于检查和运行带私有 evaluator 边界的 Stream/Episode 数据包。

```bash
docker build -t trievo-l2:0.1 -f sandbox/l2/Dockerfile .
```

```powershell
$env:TRIEVO_API_KEY = '<your-api-key>'
$env:TRIEVO_BASE_URL = 'https://api.deepseek.com/v1'
$env:TRIEVO_MODEL = '<model-id>'

uv run --frozen python -m trievo_bench.l2_compat `
  --package '<path-to-l2_demo>' `
  --inspect

uv run --frozen python -m trievo_bench.l2_compat `
  --package '<path-to-l2_demo>' `
  --stream 'tau2_telecom_mobile_data_roaming_001' `
  --baseline hermes `
  --baseline-config '<path-to-generated-override.json>' `
  --condition evolving `
  --image trievo-l2:0.1 `
  --allow-generated-code `
  --output runs/l2
```

正式 evolution gain 需要在同一冻结配置下分别运行 `reset`、`frozen`、`evolving`。
Evaluator/control-plane 错误会写成 `evaluation_status=evaluation_error`、`turn_score=null`，
默认停止并保留可恢复 checkpoint，不会记作 Agent 0 分。

Browser-smoke Stream 使用：

```bash
docker build -t trievo-l2-browser:0.1 -f sandbox/l2/Dockerfile.browser .
```

对旧版 materialized `l2_demo`，先执行一次 browser oracle 兼容修复，再运行 browser Stream：

```bash
python tools/repair_l2_browser_oracles.py <path-to-l2_demo>
python tools/repair_l2_browser_oracles.py <path-to-l2_demo> --check
```

## Project layout

```text
trievo_bench/       core runtime and L1/L2 adapters
baseline/adapters/   external baseline container adapters
schemas/             benchmark schema
examples/            example specifications
sandbox/             Dockerfiles
tests/               regression tests
docs/spec.md         detailed specification
```

## References

- [Baseline evaluation method](baseline评测方法.pdf)
- [Self-evolution benchmark construction proposal](self_evolution_benchmark_construction_proposal.pdf)
- [Detailed specification](docs/spec.md)
