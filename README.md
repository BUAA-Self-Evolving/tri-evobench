# Tri-EvoBench

Tri-EvoBench 是一个面向自演化 Agent 的评测运行时，提供统一的 Flow、Turn、Interaction 执行协议，以及 L1/L2 demo 适配、Sandbox、Verifier、LLM Judge 和 Learner 接口。

当前版本为研究原型，运行配置使用 `schema_version = 0.1`。

## Features

- Flow 内共享 Sandbox，支持本地和 Ubuntu Docker 后端
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

Python 版本要求：3.11+。Docker 仅在 Docker Sandbox 和 L2 运行时需要。

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
