# 模型渠道模式

矩阵工具提供两个显式模式。模式只保存端点、模型与传输策略；凭证始终从进程环境读取，
不会写入生成配置、运行状态或仓库。

| 模式 | 凭证环境变量 | Base URL | 模型 | Thinking | 网络 |
| --- | --- | --- | --- | --- | --- |
| `packycode` | `PACKYCODE_API_KEY`，兼容旧变量 `TRIEVO_API_KEY` | `https://cf.api.fan/v1` | `deepseek-v4-flash` | disabled | 继承代理 |
| `friday` | `FRIDAY_APP_ID` | `https://aigc.sankuai.com/v1/openai/native` | `deepseek-v4-flash` | enabled | 默认直连 |

Friday 控制台当前显示默认 RPM 为 20。运行器默认并发为 1；只有渠道配额已调整时才建议显式提高
`--concurrency`。服务若在 HTTP 200 响应体内返回非 200 的业务 `status`，客户端会保留真实
`status` 和 `message`，429/5xx 仍只在同一次模型调用内重试。

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
$env:FRIDAY_APP_ID = '<在企业电脑上设置，不写入文件>'
uv run --frozen python tools/prepare_matrix_configs.py `
  --provider-mode friday --output tmp/matrix-config-friday
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode friday `
  --packages benchmarks/current `
  --config-dir tmp/matrix-config-friday `
  --root runs/friday `
  --concurrency 1
```

Friday 模式会在进程内把 `FRIDAY_APP_ID` 映射给旧 baseline 所需的兼容变量，并清除子进程的
HTTP(S)/ALL proxy 变量。磁盘上的 `provider-profile.json` 只记录环境变量名称，不记录值。

## 只做最小连通性检查

不要用完整矩阵验证凭证。先生成 Friday 配置，再选择一个最短 job，给运行器传入它的精确
`--only` job id；检查成功后再决定是否启动正式评测。密钥不应放在命令行参数、Git commit、
聊天内容或传输包中。
