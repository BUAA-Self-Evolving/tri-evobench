# 模型渠道模式

矩阵工具提供两个显式模式。模式只保存端点、模型与传输策略；凭证始终从进程环境读取，
不会写入生成配置、运行状态或仓库。

| 模式 | 凭证环境变量 | Base URL | 模型 | Thinking | 网络 |
| --- | --- | --- | --- | --- | --- |
| `packycode` | `PACKYCODE_API_KEY`，兼容旧变量 `TRIEVO_API_KEY` | `https://cf.api.fan/v1` | `deepseek-v4-flash` | disabled | 继承代理 |
| `friday` | `FRIDAY_APP_ID`（单个）或 `FRIDAY_APP_ID_<n>` / `FRIDAY_APP_IDS`（池） | `https://aigc.sankuai.com/v1/openai/native` | `deepseek-v4-flash` | enabled | 默认直连 |

Friday 默认每个 App ID 同时承载一个 job，可用 `--friday-capacities <公开 JSON 文件>` 显式配置
每账号不同的 worker 容量。多个 App ID 时，运行器自动采用
stream-baseline-affinity LPT 调度：同一 `level + flow_id + baseline` 的 condition jobs 固定到同一
App ID 的同一通道，不同 baseline/stream 可并行；各 group 按 condition turn 总数从长到短放入
当前预计负载最小的通道，每账号通道数由配置容量决定。同一通道任何时刻最多一个活跃 job。
默认一路保留旧计划，多路配置及详细示例见 [账号池调度说明](FRIDAY_MULTI_APP_SCHEDULER.md)。

服务若在 HTTP 200 响应体内返回非 200 的业务 `status`，客户端会保留真实 `status` 和
`message`，429/5xx 仍只在同一次模型调用内重试。调度器限制的是每个 App ID 的并行 job 数，
不会伪造或绕过服务端的请求速率限制。容量配置本身不是 RPM 或请求级共享限流；
[新批次快速配置](FRIDAY_FAST_PROFILE.md) 则将原生 Agent、judge/simulator 和同机多 driver 的请求
接入共享网关，当前采用各槽 10 RPM / 2 个在途请求 / 2 worker，本机全局 8 worker。
未启用此配置的程序或其他机器流量仍须另行预留。`default_rpm` 元数据不实施限流。

## 本地使用 PackyCode

PowerShell：

```powershell
$env:PACKYCODE_API_KEY = '<仅放在本机环境中>'
uv run --frozen python tools/prepare_matrix_configs.py `
  --provider-mode packycode --output tmp/matrix-config-packycode
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode packycode `
  --packages benchmarks/current `
  --config-dir tmp/matrix-config-packycode `
  --root runs/packycode
```

已有脚本如果只设置 `TRIEVO_API_KEY`，仍可继续使用 `packycode` 模式。

## 企业电脑使用 Friday

把不含 `.env`、运行输出和凭证的代码 checkout/压缩包单向传入企业电脑，在企业电脑上重新生成配置：

```powershell
$env:FRIDAY_APP_ID_1 = Read-Host 'Friday App ID slot 1'
$env:FRIDAY_APP_ID_2 = Read-Host 'Friday App ID slot 2'
$env:FRIDAY_APP_ID_3 = Read-Host 'Friday App ID slot 3'
uv run --frozen python tools/prepare_matrix_configs.py `
  --provider-mode friday --output tmp/matrix-config-friday
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode friday `
  --packages benchmarks/current `
  --config-dir tmp/matrix-config-friday `
  --root runs/friday
```

编号环境变量是推荐方式：编号按数值排序，名称会作为非秘密槽位身份写入状态，恢复时必须保持同名
槽位对应同一 App ID。也可设置单个 `FRIDAY_APP_ID`；或把 JSON 字符串数组/换行列表放入
`FRIDAY_APP_IDS`。三种来源同时存在时优先编号变量。重复或空 App ID 会在启动前被拒绝。

未显式传 `--concurrency` 时，Friday 有效并发为 `min(各槽 worker 容量之和, 选中 stream-baseline group 数)`；显式值只可
进一步降低并发，超过容量的部分会被安全截断。未配置容量时各槽为 1。`--only`、同一 gate stream 和单次授权恢复
仍只使用一个槽位。

Friday 模式会为每个 worker 单独把选中的 App ID 映射给旧 baseline 所需的兼容变量，并从该 worker
环境删除完整凭证池及 HTTP(S)/ALL proxy 变量。`matrix_status.json` 只保存槽位环境变量名、affinity
group 映射和预计工作量，不保存 App ID 值。已有输出根会拒绝槽位顺序或任务选择变化；要改变池或
选择范围请使用新的 `--root`。
已有 root 也拒绝容量变化；使用新 root 和 `--dry-run` 可先查看不含凭证值的容量计划，不启动 worker 或写入状态。

## 只做最小连通性检查

不要用完整矩阵验证凭证。先生成 Friday 配置，再选择一个最短 job，给运行器传入它的精确
`--only` job id；检查成功后再决定是否启动正式评测。密钥不应放在命令行参数、Git commit、
聊天内容或传输包中。
