# L1 Arena / L2 TaskStream：六种 baseline Runtime 与 matched conditions

## 支持范围与验证状态

本版本支持 `l2_taskstream/1.0`，数据目录名不参与协议判断；仓库内当前 L2 包为
Demo V4，带 `panels.json`，正式矩阵必须显式选择一个 panel。旧版 episode 格式适配仍保留。
支持 Hermes、SkillOpt、Trace2Skill、Prime Agent、Adaptive Auto-Harness、OpenCode 的真实 Docker 工具执行、独立 Docker 私有验证、
L2 原始 rubric 的 LLM judge、用户反馈交互，以及跨 turn 的自进化状态。
L1 Arena 使用数据集规定的任务/偏好检查与更新顺序。

L2 明确支持三个运行条件：`reset` 每 Turn 重建 Workspace 与 SUT state；`frozen` 保留
Workspace、禁止 SUT update；`evolving` 同时保留 Workspace 与允许的 SUT update。
Evaluator error 使用 `turn_score=null`，不会被记成 Agent 0 分或触发 User Simulator。

2026-09-04：已修复已定位的路由、输出协议、usage 计量、工具超时后子进程残留、
Hermes 前台 memory 工具绕过状态冻结、judge 输出基数及恢复问题，并通过本地回归检查。
Docker image/runtime smoke 与真实模型矩阵是另外两层证据；只有对应服务器检查和付费运行实际完成后，
才能把它们写成已验证结果。
模型提交错误文件、任务失败或技能修改失败不等于评测器故障，不应修改答案以提高分数。

## 数据包

仓库内当前附带 L1 Arena 与 L2 Demo V4。Demo V4 的主统计面板是
`short_stream_main_panel`；`parent_stream_diagnostic` 只能单独跑诊断，不能和主面板混算。
外部包应先解压到 Git checkout 之外，再通过 `--l2-package` 显式传入；Runtime 不需要也不直接读取 ZIP。
全部数据版本见 [数据包目录](../benchmarks/README.md)，文件完整性可用
`python tools/dataset_inventory.py --check` 检查。API key、付费运行日志和检查点不上传。
不要用仓库旧 `benchmarks/l1_life_v2` 替代本次 `l1_arena`。

```text
DATA_ROOT/
  l1_arena/
    stream-1/stream.json
    ...                         # 23 streams，423 turns / agent
  L2_Demo_V4/
    panels.json                  # short_stream_main_panel / parent_stream_diagnostic
    streams/*/stream.json        # 30 streams，580 turn placements / agent
```

检查目录时不要额外嵌套同名目录。数据集中的说明和任务内容是评测输入，不能覆盖评测器的权限边界。

## 安装与 Docker 构建

要求 Python 3.11+、可用的 Docker Engine、可访问所选 API 的网络；在仓库根目录执行。
以下使用完整源码 Dockerfile，不依赖作者机器上的增量镜像。无需本地 GPU 来调用模型 API；
任务及 verifier 使用 CPU 容器。镜像构建需要下载依赖和浏览器，预留足够磁盘空间。

```bash
git submodule update --init baseline/hermes-agent baseline/prime-agent baseline/adaptive-auto-harness baseline/opencode
uv sync --frozen --group dev
docker info
docker build -t trievo-l2:0.1 -f sandbox/l2/Dockerfile .
docker build -t trievo-l2-browser:0.1 -f sandbox/l2/Dockerfile.browser .
docker build -t trievo-hermes:matrix-current -f baseline/adapters/hermes/Dockerfile .
docker build -t trievo-skillopt:matrix-current -f baseline/adapters/skillopt/Dockerfile .
docker build -t trievo-trace2skill:matrix-current -f baseline/adapters/trace2skill/Dockerfile .
docker build -t trievo-prime-agent:matrix-current -f baseline/adapters/prime_agent/Dockerfile .
docker build -t trievo-adaptive-auto-harness:matrix-current -f baseline/adapters/adaptive_auto_harness/Dockerfile .
docker build -t trievo-opencode:matrix-current -f baseline/adapters/opencode/Dockerfile .
uv run --frozen python -m pytest tests -q
uv run --frozen python tools/prepare_matrix_configs.py --output tmp/matrix-config
uv run --frozen python -m trievo_bench.l2_compat --package benchmarks/current/L2_Demo_V4 --inspect
uv run --frozen python tools/validate_arena_packages.py benchmarks/current/l1_arena
```

以上构建六种 baseline image 和两个 verifier image。只有运行 L1 Docker sandbox 时才额外构建：

```bash
docker build -t trievo-ubuntu:22.04 -f sandbox/ubuntu/Dockerfile .
```

这些 Dockerfile 默认使用官方 Debian/PyPI 源。受限网络可以给基础 verifier 和 baseline
传公开的 `APT_ORIGIN`，并给包含 pip 步骤的 image 传 `PIP_INDEX_URL`；browser image 会继承
基础 verifier 的 APT 源。不要把含凭据的私有源写进仓库。
baseline Dockerfile 还支持 `BASE_IMAGE`；可按各镜像依赖选择兼容的基础镜像，复用相同的
Python/科学计算依赖，同时仍会安装并校验各 baseline 自己的依赖。
`PLAYWRIGHT_DOWNLOAD_HOST` 可让 browser build 使用操作者信任的 Playwright archive cache；
默认仍使用 Playwright 自己的下载地址和 fallback。browser 的 Python 包、系统库和浏览器下载分层，
网络重试时可以复用已经完成的 layer。当前 verifier 全部以 headless 模式启动 Chromium，因此 image
只安装 Playwright 的 headless shell，不额外下载完整 GUI Chrome。

生成配置默认使用 `deepseek-v4-flash` / `https://cf.api.fan/v1`，thinking 关闭。
更换地址时同时给配置生成器传 `--base-url` 并设置下面的 `TRIEVO_BASE_URL`；模型同理。
配置生成器把中转站模型写入 Hermes 的 named custom provider，避免 Hermes 的 first-party
DeepSeek provider 把未知模型 ID 归一化为其他 DeepSeek 模型；直连官方 provider 时仍应按其原生配置处理。
配置中不保存 key，且去除了作者的固定 `host.docker.internal:7890` 代理。

L2 的默认单次模型输出上限为 `12000`，与当前 V4 `execution_requirements` 一致；Hermes L1
仍保留 `1200`，SkillOpt L1 仍保留 `4096`。配置生成器可用 `--l2-max-output-tokens` 生成较低的
参数试验，但大于数据包声明值的配置仍会被 Runtime 截到数据包上限。`32000`/`40000` 只适合
验证 provider 是否接受大请求，不能在未修改数据包版本的情况下冒充正式 V4 条件。

中转站运行默认给 benchmark 自有 Judge/User Simulator client 配置 `150s` 单请求 timeout 和
最多 `4` 次额外 transport retry；只重试连接中断、残缺响应、`408/409/429` 与 `5xx` 等瞬时故障，
不重试普通 `4xx`、空答案或 schema/任务失败。Hermes 的生成配置写入相同 timeout/retry 数值；
SkillOpt、Trace2Skill 保留各自 SDK/adapter 的 call 内重试。transport retry 不增加 benchmark
Interaction，也不能在工具副作用已经发生后重跑整个 Turn。

### 受限网络与嵌套 rootless Docker

普通 Linux 主机默认继续使用 bind mount，代理桥默认关闭。若宿主代理只监听 loopback，而 baseline
容器无法访问该端口，可使用 SETA 同类的短生命周期 TCP bridge：

```bash
export TRIEVO_DOCKER_PROXY_BRIDGE=required
export TRIEVO_DOCKER_PROXY_RANGE='<observed-container-source-cidr>'
```

需要安装 `socat`。Runtime 自动选择默认路由地址和动态端口，只把重写后的 `HTTP_PROXY`、
`HTTPS_PROXY`、`NO_PROXY` 大小写变量传给 Docker baseline，并在 Flow 结束或初始化失败时关闭 bridge。
`TRIEVO_DOCKER_PROXY_RANGE` 必须来自一次容器侧连通预检，不能用作者机器的地址；带用户名或密码的
proxy URL 会被拒绝。必要时可用 `TRIEVO_DOCKER_PROXY_HOST` 覆盖监听地址，这仍是机器本地配置，
不得写进仓库。

`TRIEVO_DOCKER_PROXY_BRIDGE=off` 只关闭 bridge，不会删除宿主环境中原有的 proxy 变量。如果宿主
使用容器无法访问的 loopback proxy，而目标 API 又能从 baseline 容器直连，应在启动 Runtime 前
显式 `unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy`，并先做一次容器内连通
检查；不要把某台机器观察到的 proxy 地址或 CIDR 固化进配置。

若 Docker daemon 看不到 checkout 所在的宿主路径，bind mount 会静默得到空目录。此时给 Docker CLI
配置该机器实际可用的 `DOCKER_HOST`，并启用：

```bash
export TRIEVO_DOCKER_VOLUME_TRANSPORT=required
```

Runtime 会通过 `docker cp` 把 package、Workspace 和 baseline state 送入 daemon-owned named volume，
Agent 和 verifier 继续看到相同的 `/workspace`。回写先进入同文件系统 staging，再做可回滚目录交换；
Flow 结束后删除 carrier container 和 volume。该模式当前只接入 `l2_taskstream/1.0`；L1 和旧 L2
继续使用原有 bind mount，但共享同一套 `BaselineRuntime`、proxy bridge 和 mount 模板，后续扩展不需要
改 L1/L2 数据合同。数据包复制会增加每条 Stream 的启动时间，不能把它当作零成本 mount。

## 完整矩阵

PowerShell（用自己的 key；不要提交或共享 key）：

```powershell
$env:TRIEVO_API_KEY = '<your-api-key>'
$env:TRIEVO_BASE_URL = 'https://cf.api.fan/v1'
$env:TRIEVO_MODEL = 'deepseek-v4-flash'
uv run --frozen python tools/run_new_package_matrix.py `
  --l1-package benchmarks/current/l1_arena `
  --l2-package benchmarks/current/L2_Demo_V4 `
  --l2-panel short_stream_main_panel `
  --l2-condition reset --l2-condition frozen --l2-condition evolving `
  --config-dir tmp/matrix-config --root runs/my-matrix
```

Linux/macOS 的环境变量设置：

```bash
read -s -p 'API key: ' TRIEVO_API_KEY; export TRIEVO_API_KEY
export TRIEVO_BASE_URL=https://cf.api.fan/v1
export TRIEVO_MODEL=deepseek-v4-flash
export TRIEVO_THINKING=disabled
export TRIEVO_API_TIMEOUT_SECONDS=150
export TRIEVO_API_MAX_RETRIES=4
uv run --frozen python tools/run_new_package_matrix.py \
  --l1-package benchmarks/current/l1_arena \
  --l2-package benchmarks/current/L2_Demo_V4 \
  --l2-panel short_stream_main_panel \
  --l2-condition reset --l2-condition frozen --l2-condition evolving \
  --config-dir tmp/matrix-config --root runs/my-matrix
```

未传 `--l2-condition` 时只跑 `evolving`，保持旧命令行为；正式 L2 gain 必须显式传入三种 condition。
当前 V4 包声明了 panels；L2 正式矩阵必须传 `--l2-panel short_stream_main_panel`，诊断父流单独用
`--l2-panel parent_stream_diagnostic` 跑，不能混合汇总。
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
- `result_matrix.json`：本次选择任务结束时生成；必须同时检查 status 为 `completed` 且所有所选 cell
  都满足 `completed_turns == expected_turns`，不能误把 gate 或残留矩阵当最终结果。
- `l2_evolution_aggregations.json`：只有同一 baseline 的三条件、全部匹配 Turn 都完成且非 null 时生成；
  包含 10-Turn bins、per-role mean、Evolving−Reset 和 Evolving−Frozen。

评测器错误默认会停止当前矩阵队列（`paused_error`）。单独调用 L2 CLI 时可冻结
`--evaluation-error-policy continue` 以保留 null Turn 后继续，但这类 Run 不能进入正式 matched aggregation。
先查明原因并修复、回归测试，
若改动容器内代码需重新构建对应镜像，再运行**同一条命令、同一个输出根目录**。
驱动会选择有效检查点，复用已完成前缀；L2 `phase=adjudicate` 能重用已付费回复，只重做判分。
不要删除 trace、检查点，不要为了得到更高分反复重跑失败答案。

恢复不是任意指令级快照：检查点保存的是公共交互、文件和声明的持久化状态，不是活动 OS 进程。
每次调用 Interaction 前会持久化 `phase=interact_started` 标记。若进程在回复保存前退出，
默认恢复会停住，不能因存在更早的完成前缀就默默重放已有 Workspace 副作用。

操作者明确批准某个 reset Turn 的首次 Interaction 中断后，可以对单个 `--only` job 使用
`--retry-interrupted-run <失败 run ID 或目录> --retry-authorization <外部授权 JSON>`。
授权必须绑定 job、失败 run、Turn 和一次额外尝试，并在启动前消耗；子进程只重试目标 Turn，
在该 Turn 完成后停止。该操作可能重复中断尝试已经支付的调用费用，不能承诺零重复费用。
已保存的判分证据继续使用 `phase=adjudicate` 恢复，不需要重跑 Interaction；合法低分不能由此重试。
持续状态条件及后续 Interaction 的中断不能套用这一恢复办法，也不会因超时而自动制造一个合法分数。

若 reset 的后续 Turn 已有真实完整结果，可显式调用 `tools/merge_l2_reset_retry.py`，传入
`--retry-report`、`--suffix-report`、`--stream-manifest`、`--target-turn`、`--output-root`，
仅替换标记为 `runtime_unavailable` 的超时补记项，将目标重试结果与原样保留的后续结果
合并到新 report/checkpoint；不会替换已有真实得分答案。工具检查 reset 身份、完整顺序、
前缀、trace、限额和非重复预算增量；拒绝未完成、状态不可信或由 timeout 人工补出的结果。
它不调用 baseline、verifier 或模型，也不覆盖输入文件。应在该 job 的运行器结束后操作。
模型端网络中断和评分端中断需区分；不要降低 rubric 或放开状态冻结来绕过错误。
驱动负责故障停队，但不自动修改代码；应由操作者持续查看日志并审核修复。

## 当前隔离边界

Native verifier 容器明确使用 `--network none`，且 package 只读、Workspace 单独挂载。六种 baseline
容器仍需联网调用远端模型 API；当前版本尚未把模型网络与 task tool 网络拆成独立 Tool Gateway，
因此不能宣称已经在进程层完全强制 `execution_requirements.network=disabled`。该限制不影响本次
V3 的数据读取、Workspace transition、评分和 matched condition smoke，但正式公开运行前仍需补齐。
Verifier command 同时把常见 proxy 环境变量显式置空；proxy bridge 只服务 baseline，不改变私有评分
容器的网络边界。
