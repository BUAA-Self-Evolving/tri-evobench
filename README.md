# Tri-EvoBench

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

```bash
docker build -t trievo-hermes:0.1 -f baseline/adapters/hermes/Dockerfile .
# 先将 override 中的 seed_from 改成干净 Hermes 配置目录的绝对路径
trievo-bench --spec examples/smoke.json --baseline hermes --baseline-config examples/hermes-baseline.override.json
```

Hermes 部署、协议字段和新增其他 baseline 的完整说明见 [Baseline Runtime 接入说明](docs/baseline-runtime.md)。

## Features

- Flow 内共享 Sandbox，支持本地和 Ubuntu Docker 后端
- 通过 manifest 和独立 Adapter 参数化切换 Agent baseline
- 支持程序 Verifier、Python Verifier 和 LLM Judge
- 支持 frozen、raw-replay、evolving、oracle-update learner
- 支持 L1 session/turn 数据和 L2 Stream/Episode 数据
- 保存完整 Interaction trace、Sandbox snapshot、状态和 token usage

## Installation

```bash
git clone git@github.com:s7777777h/tri-evobench.git
cd tri-evobench
python -m venv .venv
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

```bash
# Linux / macOS
source .venv/bin/activate
python -m pip install -e .
```

Python 版本要求：3.11+。Docker 在 Docker Sandbox、Hermes 等容器 baseline 和 L2 运行时需要。

## Run with a model

```powershell
$env:TRIEVO_API_KEY = '<your-api-key>'
$env:TRIEVO_BASE_URL = 'https://api.deepseek.com'
$env:TRIEVO_MODEL = '<model-id>'

trievo-bench --spec examples/deepseek_smoke.json --output runs/model-smoke --max-total-tokens 3000000
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

## L2

L2 adapter 用于检查和运行带私有 evaluator 边界的 Stream/Episode 数据包。

```bash
docker build -t trievo-l2:0.1 -f sandbox/l2/Dockerfile .
```

```powershell
$env:L2_API_KEY = '<your-api-key>'

python -m trievo_bench.l2_compat `
  --package '<path-to-l2_demo>' `
  --inspect

python -m trievo_bench.l2_compat `
  --package '<path-to-l2_demo>' `
  --stream 'tau2_telecom_mobile_data_roaming_001' `
  --image trievo-l2:0.1 `
  --model '<model-id>' `
  --api-base 'https://api.deepseek.com/v1' `
  --allow-generated-code `
  --output runs/l2
```

Browser-smoke Stream 使用：

```bash
docker build -t trievo-l2-browser:0.1 -f sandbox/l2/Dockerfile.browser .
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
