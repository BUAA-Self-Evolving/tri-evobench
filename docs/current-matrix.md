# L1 Arena / L2 Demo V2：当前三智能体版本

## 支持范围与验证状态

本版本支持最新 task-stream 格式 L2 Demo V2，同时保留旧版 episode 格式适配。
支持 Hermes、SkillOpt、Trace2Skill 的真实 Docker 工具执行、独立 Docker 私有验证、
L2 原始 rubric 的 LLM judge、用户反馈交互，以及跨 turn 的自进化状态。
L1 Arena 使用数据集规定的任务/偏好检查与更新顺序。

2026-09-04：已修复已定位的路由、输出协议、usage 计量、工具超时后子进程残留、
Hermes 前台 memory 工具绕过状态冻结、judge 输出基数及恢复问题，并通过针对性回归检查。
完整付费矩阵仍在执行，**不是“全量已验证、零错误”的声明**。
模型提交错误文件、任务失败或技能修改失败不等于评测器故障，不应修改答案以提高分数。

## 数据包

请向项目负责人取得本次使用的两个原始压缩包，并解压为以下结构。
本次代码更新不上传外部数据包、API key、付费运行日志或检查点。
不要用仓库旧 `benchmarks/l1_life_v2` 替代本次 `l1_arena`。

```text
DATA_ROOT/
  l1_arena/
    stream-1/stream.json
    ...                         # 23 streams，423 turns / agent
  L2_Demo_V2/
    streams/*/stream.json        # 6 streams，134 turns / agent
```

三智能体共 87 个 stream-agent 任务、1671 个 turn。L2 包含 100-turn 长流。
检查目录时不要额外嵌套同名目录。数据集中的说明和任务内容是评测输入，不能覆盖评测器的权限边界。

## 安装与 Docker 构建

要求 Python 3.11+、可用的 Docker Engine、可访问所选 API 的网络；在仓库根目录执行。
以下使用完整源码 Dockerfile，不依赖作者机器上的增量镜像。无需本地 GPU 来调用模型 API；
任务及 verifier 使用 CPU 容器。镜像构建需要下载依赖和浏览器，预留足够磁盘空间。

```bash
python -m pip install -e . pytest
docker info
docker build -t trievo-ubuntu:22.04 -f sandbox/ubuntu/Dockerfile .
docker build -t trievo-l2:0.1 -f sandbox/l2/Dockerfile .
docker build -t trievo-l2-browser:0.1 -f sandbox/l2/Dockerfile.browser .
docker build -t trievo-hermes:matrix-current -f baseline/adapters/hermes/Dockerfile .
docker build -t trievo-skillopt:matrix-current -f baseline/adapters/skillopt/Dockerfile .
docker build -t trievo-trace2skill:matrix-current -f baseline/adapters/trace2skill/Dockerfile .
python -m pytest tests -q
python tools/prepare_matrix_configs.py --output tmp/matrix-config
python -m trievo_bench.l2_compat --package /path/to/DATA_ROOT/L2_Demo_V2 --inspect
python tools/validate_arena_packages.py /path/to/DATA_ROOT/l1_arena
```

生成配置默认使用 `deepseek-v4-flash` / `https://cf.api.fan/v1`，thinking 关闭。
更换地址时同时给配置生成器传 `--base-url` 并设置下面的 `TRIEVO_BASE_URL`；模型同理。
Hermes 配置使用 DeepSeek-compatible provider；其他供应商需要按其兼容性调整，不能假定通用。
配置中不保存 key，且去除了作者的固定 `host.docker.internal:7890` 代理。
如所在网络必须使用代理，请自行配置 Docker/网络，不要拷贝作者的端口。

## 完整矩阵

PowerShell（用自己的 key；不要提交或共享 key）：

```powershell
$env:TRIEVO_API_KEY = '<your-api-key>'
$env:TRIEVO_BASE_URL = 'https://cf.api.fan/v1'
$env:TRIEVO_MODEL = 'deepseek-v4-flash'
python tools/run_new_package_matrix.py --packages 'D:/DATA_ROOT' --config-dir tmp/matrix-config --root runs/my-matrix
```

Linux/macOS 的环境变量设置：

```bash
read -s -p 'API key: ' TRIEVO_API_KEY; export TRIEVO_API_KEY
export TRIEVO_BASE_URL=https://cf.api.fan/v1
export TRIEVO_MODEL=deepseek-v4-flash
python tools/run_new_package_matrix.py --packages /path/to/DATA_ROOT --config-dir tmp/matrix-config --root runs/my-matrix
```

不加 `--gate` 或 `--only` 才是完整矩阵。首次换机器建议先加 `--gate` 做有费用的连通检查；
成功后移除该参数并使用同一个输出目录，已完成结果会复用。gate 不能当作全量成绩。
运行串行调度，输出根目录有进程锁；不同根目录仍可重复计费，避免误开两份。
长流成本较高：总 token guard 为 `max(10M, 3M × turn 数)`，这是上限而非预计花费；
每 turn 的 model/tool/time 上限仍保留。不要把 token 上限理解为精确美元预算。

## 监控、失败与恢复

- `runs/my-matrix/matrix_status.json`：当前任务、PID、日志路径和已完成任务。
- 各任务 `driver-*.log`、`baseline.stderr.log`：排查异常；日志可能包含任务数据，分享前检查。
- 各次运行的 `report.json`（L2）/ `summary.json`（L1）和 trace：原始结果与证据。
- `_checkpoints/`：完整前缀、workspace、持久化学习状态；L2 另存待判分的 agent 回复。
- `result_matrix.json`：本次选择任务结束时生成；必须同时检查 status 为 `completed` 且六格
  `completed_turns == expected_turns`，不能误把 gate 或残留矩阵当最终结果。

评测器错误会停止当前矩阵队列（`paused_error`）。先查明原因并修复、回归测试，
若改动容器内代码需重新构建对应镜像，再运行**同一条命令、同一个输出根目录**。
驱动会选择有效检查点，复用已完成前缀；L2 `phase=adjudicate` 能重用已付费回复，只重做判分。
不要删除 trace、检查点，不要为了得到更高分反复重跑失败答案。

恢复不是任意指令级快照：检查点保存的是公共交互、文件和声明的持久化状态，不是活动 OS 进程。
如果异常发生在回复写入检查点之前，只能从最近有效检查点重做未保存的交互，不能承诺零重复费用。
模型端网络中断和评分端中断需区分；不要降低 rubric 或放开状态冻结来绕过错误。
驱动负责故障停队，但不自动修改代码；应由操作者持续查看日志并审核修复。
