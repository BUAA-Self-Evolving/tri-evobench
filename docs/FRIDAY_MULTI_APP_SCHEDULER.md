# Friday 多 App ID stream 调度器

## 目标与约束

Friday 渠道支持按 App ID 配置不同的活跃 worker 容量，未配置时每账号仍为一路。调度单位是
`level + flow_id + baseline`。同一 stream 的不同 baseline 可以并行使用不同 App ID；同一 baseline
的 reset/frozen/evolving 固定使用同一个 App ID，从而避免把六个不同 Agent 框架错误串行到一个槽，
同时保留真正有用的 provider 侧前缀 cache 局部性。

调度器同时保证：

- 一个 App ID 的活跃 worker 数不超过该槽位显式配置的容量；
- 一个 6-baseline stream 在有 6 个 App ID 时可立即形成 6 条并行流水线；
- 同一 `stream + baseline` 的 condition 不拆分；
- worker 环境只含分配给自己的 App ID，不含完整凭证池；
- 状态文件和日志只记录槽位名，不写入 App ID 值。

容量是 **worker 数**，不是每分钟请求数（RPM），也不是所有原生 baseline 共用的请求信号量。
一个 worker 的 Agent、evolver、judge 或 simulator 可能发出多次甚至并行请求；应按实际调用路径
预留余量，不应直接把服务端并发请求上限无条件照抄成 worker 数。仅使用 worker 容量配置不会实施请求级
RPM 限流；可选的 [快速配置](FRIDAY_FAST_PROFILE.md) 提供共享请求网关。`ProviderProfile.default_rpm`
仍仅为兼容元数据，不会自动每分钟限制一次请求。

## 调度算法

将每个 `stream + baseline` affinity group 视为不可拆分的调度任务。预计工作量为：

```text
W(stream, baseline) = sum(max(1, turn_count(condition job)))
```

求和范围是该 stream、该 baseline 在本次选择中的 condition job。100-turn stream 的三条件组权重
为 300；如果只选择 evolving，权重就是 100。不同 baseline 形成不同 group，可以并行。

所有 affinity group 按 `W` 降序排列，权重相同时按 group key 排序保证可复现。每个 App ID 按
容量划分独立通道；最小堆将下一个 group 放入累计负载最小的通道。相同负载时先按通道号、再按
槽位号排序，使空闲账号先各用一路，再填第二路。容量大的账号拥有更多通道，因此可承担更多 group。
一个 group 的所有 condition 固定在同一通道，按 reset、frozen、evolving 顺序执行。

LPT 只负责静态 group 分配。运行时 admission control 同时检查总并发、每槽容量、通道互斥；同一
通道不同时运行两个 job。机器总并发低于账号池总容量时，队列优先分散到不同账号。通道保留静态亲和，
不在运行中迁移 group；实际 Turn 耗时偏离估计时，尾部仍可能有空闲容量。

## Reset 与 probe

调度器不会把 reset 缩减为 probe-only。Reset 的含义是每个 Turn 前重建 workspace 和 SUT state，
不是跳过 learning/application Turn；三条件正式聚合要求 reset、frozen、evolving 的 turn id、turn index
和 sequence role 完全匹配。probe 约占 1/10 只说明可单独生成一种诊断指标，不能替代正式
`Evolving - Reset` 的 matched denominator。若未来需要低成本预检，应作为显式的 diagnostic panel 或
新实验 profile 输出，不能写入正式 reset report，也不能与完整三条件结果聚合。

## 配置

推荐使用稳定编号的环境变量：

```powershell
$env:FRIDAY_APP_ID_1 = Read-Host 'Friday App ID slot 1'
$env:FRIDAY_APP_ID_2 = Read-Host 'Friday App ID slot 2'
$env:FRIDAY_APP_ID_3 = Read-Host 'Friday App ID slot 3'
```

编号可以不连续，解析时按数值排序。也兼容：

- 单凭证：`FRIDAY_APP_ID`；
- JSON 数组：`FRIDAY_APP_IDS='["id-1", "id-2"]'`；
- 换行列表：`FRIDAY_APP_IDS="id-1`n`id-2"`。

若同时设置多种来源，优先使用 `FRIDAY_APP_ID_<n>`。空值和重复 App ID 会在启动前失败。为避免恢复
时改变 group 身份，复用输出根时必须保持槽位名称、顺序及其实际 App ID 对应关系不变；改变凭证池
或任务选择时使用新的 `--root`。

不同账号的 worker 容量放入独立公开 JSON 文件，以槽位名为键。例如
`examples/friday-capacities.example.json`：

```json
{
  "FRIDAY_APP_ID_1": 10,
  "FRIDAY_APP_ID_2": 20
}
```

这只是格式示例，不代表现有账号额度。缺省槽位为 1；只接受正整数，未知槽位会报错。
容量按槽位名称绑定，因此 `--friday-slot-order` 重排后仍匹配同一账号。列表式凭证的键为
`FRIDAY_APP_IDS[1]`，单凭证键为 `FRIDAY_APP_ID`。文件中不应填写真实 App ID 或密钥。

## 运行

Friday 默认使用 `min(各槽 worker 容量之和, 本次选择的 stream-baseline group 数)`。
未配置容量时与旧行为一致。如果机器资源不足，应通过 `--concurrency` 设置较小的全局上限；
更大的值会被账号池容量和 group 数安全截断。只有一个 group 时，即便账号允许十路仍只启动一个 worker。

```powershell
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode friday `
  --packages benchmarks/current `
  --config-dir tmp/matrix-config-friday `
  --root runs/friday-matrix
```

后续新批次可先做只读计划检查；下面的 `--concurrency 12` 是机器资源上限示例：

```powershell
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode friday `
  --packages benchmarks/current `
  --config-dir tmp/matrix-config-friday `
  --friday-capacities examples/friday-capacities.example.json `
  --concurrency 12 `
  --root runs/friday-capacity-new `
  --dry-run
```

正式执行时移除 `--dry-run`。预演仍要求进程环境有凭证并通过 profile/任务清单校验，但不会调用模型、
创建运行目录、加锁或写入状态。现有 root 的调度不匹配时预演也会拒绝。

启动后可检查 `runs/friday-matrix/matrix_status.json` 中的 `credential_schedule`。其中包含策略版本、
槽位名、每槽预计 work units、affinity group 数和 group 映射，但不应出现任何 App ID 值。
多路配置还包含 `slot_capacities`、`slot_lane_work_units` 和各 group 的 `credential_lane`；
`credential_slot_usage` 记录每槽容量和活跃 worker 数。

默认一路保留原 v2 strategy 与映射，以兼容已有计划。任何多路配置使用 v3 strategy；改变容量或从
v2 切换到 v3 必须使用新的输出根，不能给冻结的旧实验静默扩容。恢复与 worker adoption 均校验通道身份。
容量约束仅在一个 driver 内有效；同时启动多个共享账号池的 root，必须人工分摊 worker/RPM 预算。

## 验证

不调用模型的调度专项测试：

```powershell
python -m pytest -q `
  tests/test_provider_profiles.py `
  tests/test_friday_stream_scheduler.py `
  tests/test_matrix_parallel.py `
  tests/test_matrix_storage_recovery.py `
  tests/test_matrix_levels.py
```

这些测试覆盖数值槽位排序、凭证隔离、异构容量和通道互斥、condition 顺序、stream-baseline 分配、
turn-weighted LPT、恢复计划校验、只读预演、并发上限和 PackyCode 兼容性。
真实 Friday 连通性仍按项目的单 job 预检流程执行，不要直接跑全量矩阵。

## 有界 RPM 探测

`tools/probe_friday_capacity.py` 可对进程环境中的池逐槽测试，或通过 `--app-id-file` 在内存中读取
私有换行凭证文件。每槽最多一个在途请求，以目标 RPM 均匀发起；响应慢时不补发突发流量。
每次最多 8 个输出 token，关闭 thinking，首次拒绝或网络错误即停止该槽，绝不自动重试。
默认每槽仅 1 条，需显式提高 `--requests-per-slot` 才做速率测试；真实请求会产生少量模型用量。

```powershell
uv run --frozen python tools/probe_friday_capacity.py `
  --output runs/friday-rpm-new/observations.json `
  --target-rpm 20 --requests-per-slot 22
```

报告只保存槽位名、HTTP/业务状态、数值型白名单限流头、token usage、请求时间和数值 RPM 提示；
不保存 prompt 回复、服务端错误正文或凭证。输出文件必须是新路径，中断遗留的空文件也不能覆盖。
服务未返回官方额度时，成功测试只能说明该次请求形态与时间窗的观测下界；目标 RPM 不等于实际发起
速率，需核对时间戳与 `max_accepted_starts_in_60_seconds`。429 也不能独自区分每账号、共享项目、IP
或模型级计数窗口；长上下文 TPM、其他进程用量、原生 Agent 并行请求需另测。

2026-10-17 的独立六槽实测、验证边界与后续建议见 [RPM 审计](FRIDAY_RPM_AUDIT_20261017.md)。
