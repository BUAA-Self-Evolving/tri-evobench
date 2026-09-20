# Friday 多 App ID stream 调度器

## 目标与约束

Friday 渠道按每个 App ID 同时最多一个活跃 job 使用。调度单位不是单个 baseline job，而是逻辑
stream：`level + flow_id`。同一 stream 下的全部 baseline 和 condition 固定使用同一个 App ID，避免在
stream 内切换身份，从而保留 provider 侧前缀 cache 的局部性。

调度器同时保证：

- 一个 App ID 任意时刻最多启动一个 worker；
- App ID 数不少于 stream 数时，每个 stream 使用独立 App ID；
- App ID 不足时，同一 stream 仍不拆分；
- worker 环境只含分配给自己的 App ID，不含完整凭证池；
- 状态文件和日志只记录槽位名，不写入 App ID 值。

## 调度算法

将每个 stream 视为不可拆分的调度任务。预计工作量为：

```text
W(stream) = sum(max(1, turn_count(job)))
```

求和范围是该 stream 在本次选择中的所有 baseline/condition job。因此同样有 8 个 turns 的 stream，
若需要运行 4 个组合，其权重就是 32，而不是 8 或 4。

所有 stream 按 `W` 降序排列，权重相同时按 stream key 排序保证可复现；随后用最小堆把下一个 stream
放入当前累计负载最小的 App ID 槽位，负载相同时优先编号较小的槽位。这是带 stream 亲和约束的
Longest Processing Time first（LPT）调度，时间复杂度为 `O(S log S + S log K)`，其中 `S` 是
stream 数，`K` 是 App ID 数。与轮询或按 stream 数均分相比，它会把长 stream 优先错开，减少最后
一个长 stream 拖延整个矩阵的概率。

LPT 只负责静态 stream 分配。运行时 admission control 会再次检查活跃槽位；即使总并发设置过高，
也不会在同一 App ID 上启动第二个 job。

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
时改变 stream 身份，复用输出根时必须保持槽位名称、顺序及其实际 App ID 对应关系不变；改变凭证池
或任务选择时使用新的 `--root`。

## 运行

通常不要显式传 `--concurrency`：Friday 默认使用
`min(App ID 数, 本次选择的 stream 数)`。如果机器资源不足，可以传更小值；更大的值会被安全截断。

```powershell
uv run --frozen python tools/run_new_package_matrix.py `
  --provider-mode friday `
  --packages benchmarks/current `
  --config-dir tmp/matrix-config-friday `
  --root runs/friday-matrix
```

启动后可检查 `runs/friday-matrix/matrix_status.json` 中的 `credential_schedule`。其中包含策略版本、
槽位名、每槽预计 work units、stream 数和 stream 映射，但不应出现任何 App ID 值。

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

这些测试覆盖数值槽位排序、凭证隔离、每槽互斥、唯一 stream 分配、turn-weighted LPT、恢复计划校验、
并发上限和 PackyCode 兼容性。真实 Friday 连通性仍按项目的单 job 预检流程执行，不要直接跑全量矩阵。
