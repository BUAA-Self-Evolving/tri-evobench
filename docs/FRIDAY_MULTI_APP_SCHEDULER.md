# Friday 多 App ID stream 调度器

## 目标与约束

Friday 渠道按每个 App ID 同时最多一个活跃 job 使用。调度单位是
`level + flow_id + baseline`。同一 stream 的不同 baseline 可以并行使用不同 App ID；同一 baseline
的 reset/frozen/evolving 固定使用同一个 App ID，从而避免把六个不同 Agent 框架错误串行到一个槽，
同时保留真正有用的 provider 侧前缀 cache 局部性。

调度器同时保证：

- 一个 App ID 任意时刻最多启动一个 worker；
- 一个 6-baseline stream 在有 6 个 App ID 时可立即形成 6 条并行流水线；
- 同一 `stream + baseline` 的 condition 不拆分；
- worker 环境只含分配给自己的 App ID，不含完整凭证池；
- 状态文件和日志只记录槽位名，不写入 App ID 值。

## 调度算法

将每个 `stream + baseline` affinity group 视为不可拆分的调度任务。预计工作量为：

```text
W(stream, baseline) = sum(max(1, turn_count(condition job)))
```

求和范围是该 stream、该 baseline 在本次选择中的 condition job。100-turn stream 的三条件组权重
为 300；如果只选择 evolving，权重就是 100。不同 baseline 形成不同 group，可以并行。

所有 affinity group 按 `W` 降序排列，权重相同时按 group key 排序保证可复现；随后用最小堆把
下一个 group 放入当前累计负载最小的 App ID 槽位，负载相同时优先编号较小的槽位。这是带
`stream + baseline` 亲和约束的 Longest Processing Time first（LPT）调度。一个 group 内固定按
reset、frozen、evolving 顺序执行。

LPT 只负责静态 group 分配。运行时 admission control 会再次检查活跃槽位；即使总并发设置过高，
也不会在同一 App ID 上启动第二个 job。

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

## 运行

通常不要显式传 `--concurrency`：Friday 默认使用
`min(App ID 数, 本次选择的 stream-baseline group 数)`。如果机器资源不足，可以传更小值；更大的值会被安全截断。

```powershell
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode friday `
  --packages benchmarks/current `
  --config-dir tmp/matrix-config-friday `
  --root runs/friday-matrix
```

启动后可检查 `runs/friday-matrix/matrix_status.json` 中的 `credential_schedule`。其中包含策略版本、
槽位名、每槽预计 work units、affinity group 数和 group 映射，但不应出现任何 App ID 值。

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

这些测试覆盖数值槽位排序、凭证隔离、每槽互斥、stream-baseline 分配、turn-weighted LPT、恢复计划校验、
并发上限和 PackyCode 兼容性。真实 Friday 连通性仍按项目的单 job 预检流程执行，不要直接跑全量矩阵。
