# 后续 baseline 快速配置

2026-10-17 根据六槽 RPM 微请求实测建立。用于**新批次**；没有改写旧批次的配置、报告、checkpoint 或恢复凭证。

## 当前已生成的参数

本机配置目录：`tmp/friday-fast-20261017/`。其中 `execution-preset.json` 保存运行策略，
`friday-capacities.json` 保存 worker 容量，`provider-profile.json` 保存共享网关路由。

| 参数 | 当前配置 |
| --- | --- |
| 账号池 | `FRIDAY_APP_ID_1` 至 `FRIDAY_APP_ID_6`，真实值只在环境/内存读取 |
| 每账号请求发起速率 | 10 RPM，至少间隔 6 秒，不积攒突发配额 |
| 每账号最大在途请求 | 2（Agent、evolver、judge/simulator、重试共同占用） |
| 每账号最大 worker | 2 |
| 本机全局 worker | **8**；当前 Docker 为 8 CPU、约 10 GiB 内存 |
| 429 | 该账号新请求至少冷却 60 秒；数值 Retry-After 更长时按较长值，最多 600 秒 |
| 网关自动重试 | **0**；调用方原有同一次请求内的有界 retry 保留并重新占预算 |
| job 故障策略 | continue：记录错误单元并继续；可显式 --job-error-policy stop |
| 存储保护 | 启用；低于 60 GiB 暂停接收，恢复阈值 75 GiB |

原全局上限为 6；新配置允许同账号运行两个独立 stream，同时把模型请求合并限速。
逻辑账号池最多有 12 条 worker 通道，本机因资源限制采用 8；资源更充足的机器可新生成 12 路配置。
这不代表端到端速度提升 8/6 或其他固定倍数；实际速度取决于模型时延、工具、内存、TPM 和判分用量。

选择 10 RPM 的依据是六槽都通过了约两分钟连续测试；20 RPM 只通过短测，40 RPM 触发限流。
精确官方额度和长期 TPM 尚未知，原始证据见 [RPM 审计](FRIDAY_RPM_AUDIT_20261017.md)。

## 使用已生成配置

在项目根目录执行。下面只做计划预演，不创建运行根、不启动网关或 worker、不调用模型：

```bash
python tools/run_friday_fast_matrix.py \
  --app-id-file ../new_appids.txt \
  --root runs/friday-fast-new \
  --baseline deepseek-harness \
  --l1-package benchmarks/current/l1_arena \
  --l2-package ../tri-evobench-offline-opencode-dcbcc00/benchmarks/current/L2_Demo_V2 \
  --l1-condition reset --l1-condition frozen --l1-condition evolving \
  --l2-condition reset --l2-condition frozen --l2-condition evolving \
  --dry-run
```

本机验证该清单为 87 jobs / 29 affinity groups / 1671 Turns，全局并发 8。
预演不检查真实镜像、源码 revision 或服务连通性，不能视为真实矩阵已通过。

以后实际执行时使用新的输出根，先按原项目流程选择一个精确 `--only` job 做独立真实预检；
去掉 `--dry-run` 才会启动真实工作。单 job 预检与正式全矩阵分别使用不同 root。
启动器自动执行以下检查后才放行 worker：

1. 对所选 baseline 核验实际 Docker 命令使用的镜像及 manifest 声明的源码 revision。
2. 启动或复用本地共享网关，对池中每个凭证执行 `/health` 身份和策略校验；不调用模型。
3. 从已有 Docker sandbox 镜像检查容器到网关的 `/health` 连通性和认证；不调用模型。
4. driver 再次核验配置、root 计划、网关和存储状态。

如不传 `--baseline`，仍按通用 driver 默认选择六种 baseline；当前其中一些源码元数据缺失，
会在付费请求前被预检阻止。启动器禁止通过额外参数改写 provider、并发或注入一次性恢复授权。
这是新批次入口；旧暂停实验继续使用其原恢复流程。

## 重新生成与其他 Docker 平台

以下命令是本机生成方式；输出目录必须不存在。不要覆盖已经用于实验的配置：

```bash
python tools/prepare_friday_fast_config.py \
  --output tmp/friday-fast-another \
  --global-workers 8 \
  --dsh-source-config runs/dsh-deepseek-v4-flash-opencode-matched-20261016/config/tmp_deepseek-harness_docker.override.json
```

本机 Colima 使用 `host.lima.internal`，已验证可从容器连接宿主机 loopback。
Docker Desktop 可显式设置 `--container-host host.docker.internal`；其他平台须使用经过验证的宿主机可达地址。
宿主机 evaluator 使用 `127.0.0.1`，容器使用上述别名。跨机器部署必须重新验证，不能照搬 Colima 的地址。

六种旧 baseline 的配置基于原生成器，仅改变 provider 路由并补齐路由环境变量。
DSH 从指定配置复制 execution profile、会话、状态、预算、thinking 与 image 设置，仅替换端点；
`dsh-source.json` 保存原配置 SHA256，可核对原文件未变。未指定 DSH 来源时不生成 DSH 配置。
Codex/ZCode 也生成路由 override，但其镜像、源码与 Friday 原生协议兼容性仍须单独验证。

## 网关与恢复边界

`trievo_bench/friday_gateway.py` 只监听本机 `127.0.0.1`；通过现有 App ID 鉴权，固定转发到 Friday，
不接受任意上游地址，不记录 prompt、reply 或凭证。成功的 JSON/SSE 不改变协议或 usage；
上游 HTTP/业务 429 会转成不含认证内容的 HTTP 429，并对共享预算设置冷却。

默认共享状态位于 `.tmp/friday-shared-budget.sqlite`，含账号值的不可逆摘要、节流时间与有过期时间的在途租约，
不含真实凭证。文件权限为 0600，`.tmp/` 已忽略。不同 root/网关进程使用同一数据库时，
同一账号仍共享请求间隔、最大在途数和冷却；换槽位名称不会绕过对实际账号的预算。
此协调仅限同一宿主机和同一状态文件，其他机器/其他调用程序必须另行分摊账号额度。

网关没有额外 provider retry：每次放行最多发起一次上游请求。排队客户端断开后不再 dispatch；
请求在排队截止前未获准时返回 admission 错误，不伪装成模型成功。读取回复有绝对 deadline 和 32 MiB 上限，
部分流失败不重发。失联在途租约到期只能释放容量，不能证明某个 Turn 可安全重放。
不能通过删除状态文件、换端口、重启 worker 或修改 checkpoint 绕过付费请求/工具副作用恢复合同。

真实启动会自动创建独立网关进程，日志仅有公开启动信息，位于 `.tmp/friday-gateway.log`。
网关独立于某一个矩阵 driver 存活，以便后续新批次共享预算；driver 结束不会删除预算或关闭其他批次正在用的服务。
也可在终端前台运行 `python tools/run_friday_gateway.py --app-id-file ../new_appids.txt`。
停止/更换服务前须先排空使用它的 worker，不要在运行中清空 SQLite 状态。

## 本机验证与限制

- 专项：109 passed / 4 skipped（Linux `/proc` 项在 macOS 跳过）。
- 完整回归（Docker 加入 PATH）：498 passed / 6 failed / 9 skipped；6 项均因根目录、Codex 或 ZCode 源码缺失 Git 元数据，不能通过 revision 校验。未伪造元数据或禁用校验。
- Docker mock：宿主 evaluator + 容器内真实 DSH SDK 的两次模型调用共享一个账号预算；实际 shell 工具执行、回复和 token usage 正确，调用间隔约 6 秒；**真实 API 调用为 0**。
- 已做真实账号池的只读预演；没有启动正式 baseline、恢复旧暂停单元或续签授权。
- 当前 baseline 预检：SkillOpt、Trace2Skill、DSH 通过源码/镜像检查；Hermes、Prime、Adaptive、OpenCode 缺少可核验源码 revision；Codex、ZCode 镜像不可用。此状态记录在 `runs/friday-fast-validation-20261017/readiness.json`，不能把配置已生成当作九种 baseline 全部可运行。
- 完整回归日志、最终 Docker mock 与预演结果位于 `runs/friday-fast-validation-20261017/`；完整回归的 Git 元数据失败单独保留。
- 未做新配置的真实长上下文矩阵或端到端加速比测试；TPM、其他机器流量、OOM 和底层服务故障不能仅靠 RPM 配置保证。

## 2026-10-17 精选L1获批批次补充

新生成器支持 `--model deepseek-v4-flash-meituan`；默认仍为普通版，旧配置不改。新profile同时绑定worker模型环境、网关POST身份和health身份，并保留所选账号key别名的继承。旧生成器在DSH只有options/adapter覆盖时曾遗漏API key继承，已修复并有专项回归。源码预检和worker共同支持预先绑定archive_sha256的离线源码；摘要不匹配会拒绝运行，未关闭revision检查。

已运行的五baseline精选十流批次见 `../runs/l1-selected10-five-baselines-20261017/`，正式配置为 `config-v2/`、状态 `continuation-state.json` 与 `new/matrix_status.json`。此批次已经启动，不要再次执行supervise.py/continue_launch.py；第一版失败smoke及config原件保留。99项专项通过，完整502 passed / 6既有源码元数据失败 / 9 skipped；详见批次VALIDATION.md。

### 2026-10-20 单元错误跳过与延后替代

fast launcher默认continue，并允许显式stop；continue在全矩阵单worker时也使用统一队列调度。
失败单元保留paused_error，后续pending继续；队列排空但有错误时顶层为completed_with_errors、
queue_drained=true，failed-job-ids.json仅列错误单元。返回码仍为1以表明结果不完整，不能当无错成功。

本次L2按用户明确授权分两阶段：先97从未运行单元，再将原7与新增错误各完整替代一次。
独立formal-pending-v13和formal-errors-v13，已完整46单元不重跑；原始部分/错误尝试保留，
失败单元的部分Turns可在独立完整替代中再次执行，不能混合成无缝续跑或按分数择优。
依据deferred-retry-v13.json/同名单元列表固定替代来源，低分/合法任务失败不纳入重跑。
二次错误汇总unresolved-job-ids.json，不无限重试。全球1worker、20/25GiB存储门槛与两panel边界不变。

## 2026-10-27 本机 L2 资源自适应档

本机新 L2 批次已增加 [L2_LOCAL_FAST.md](L2_LOCAL_FAST.md) 的独立性能档。快速入口优先使用存在的 `tmp/l2-local-fast-current`；历史显式 `--config-dir` 与冻结配置不改。新档采用实测选择的10 CPU/12 GiB Colima、最多4 worker（重任务2、τ²2）、实时资源门禁、标准ZIP复用/克隆及Prime离线预装镜像。每槽10 RPM/2在途与恢复授权边界不变；不启动或改写已完成矩阵。此前8 worker/原资源建议继续作为旧通用档说明。
