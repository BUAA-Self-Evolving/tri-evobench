# Timeout、工作区传输与恢复工作流

本文描述 2026-09-21 起的最终运行合同。它适用于 Prime Agent、OpenAI-compatible evaluator transport、
L1 daemon-owned workspace 和矩阵 fail-fast 恢复，不记录排障试错过程。

## 1. 故障边界

`paused_error` 是一个 job 状态。job 的身份是 `level / condition / baseline / stream`，内部可以包含多个
Turn。矩阵 driver 在 fail-fast 模式下只把实际失败的 job 标为 `paused_error`，停止继续 admission，并让
已经启动的其他 worker 排空；尚未启动的 job 保持 pending。矩阵顶层随后进入 `paused_error`，但这不代表
所有 pending job 都运行失败。

恢复前必须分别判断：

- 模型答案或任务结果失败：保留为正式任务结果，不自动重放；
- provider 429/5xx 或连接中断：只允许同一次模型请求内部的有界 retry；
- adapter/RPC deadline、verifier timeout、容器或卷传输失败：基础设施错误，不能伪造分数；
- 有合法 post-Turn checkpoint：从下一 Turn 继续；
- 中断发生在已执行工具副作用的 Interaction 内：除非用户显式授权，不重放该 Interaction。

状态文件中的旧 `running` 只是一条陈旧记录，不能证明进程仍存在。恢复工具必须同时核对 PID、进程启动
标识、driver lock、report、checkpoint 和日志。

## 2. 完整执行路径

```text
matrix driver
  -> 为 stream 选择 credential slot 和 job
  -> 启动单独 CLI worker
  -> Runner/L2AgentStreamRunner 创建 workspace
  -> ProcessBaselineRuntime 启动 adapter 容器
  -> PrimeAgentBridge 启动每 Turn 新 RPC session
  -> Prime Agent provider stream + native tools
  -> bridge 返回 text/tool trace/usage
  -> verifier 在同一 workspace 上检查
  -> Turn 末 evolution barrier
  -> workspace + baseline state checkpoint
  -> report -> matrix_status.json
```

每层只负责自己的 retry。matrix driver 不重放一次已经开始的付费 Interaction；bridge 不把 provider retry
伪装成新的 benchmark Interaction；verifier 失败不改写 Agent 原始答案。

## 3. Prime RPC 生命周期

每个 Turn 都创建新的 Prime RPC 进程，Flow 级 continual harness 保留，session transcript 和 Python
kernel snapshot 不跨 Turn 泄漏。

Bridge 不再用阻塞 `readline()` 猜测结束状态。stdout 由独立线程持续解码到队列；每个 RPC 请求共享本
Turn 的 monotonic absolute deadline。`prompt` 完成必须同时满足：

1. 收到本次 prompt response；
2. 至少观察到新的 `agent_start`；
3. 其后的 `agent_end` 只是 idle candidate；
4. bridge 立即发 `get_state`，只有 `isStreaming=false` 才确认结束；
5. 若确认期间又收到 `agent_start`，旧 `agent_end` 作废，等待新一轮结束。

这覆盖 provider 自动 retry 在旧 `agent_end` 之后启动新 agent run 的竞态。deadline 到达时 bridge 立即发
RPC `abort`，抛出基础设施错误，后续 prompt 不会继续。

Bridge 从 Turn metadata 读取 `episode_timeout_seconds`、`max_model_calls` 和 `max_tool_calls`。assistant
`message_end` 计入模型调用，`tool_execution_start` 计入工具调用；达到预算后不再接受下一 Interaction，
越界或可能进入下一模型调用时发 abort。结果中的 `raw.limit_exhausted` 和 `limit_reasons` 供上层审计。

## 4. Provider 请求硬上限

Tri-EvoBench 自己的 OpenAI-compatible client 使用两层限制：

- `urlopen` 的连接/单次 socket timeout；
- 从发出请求开始计算的 absolute deadline。

response body 以 64 KiB 分块读取，每次读取前把 socket timeout 收紧到剩余绝对预算。因此持续发送小
chunk 不能无限续命。默认 body 上限为 32 MiB，超过后立即失败且不重放，避免异常响应消耗数百 MiB。
deadline 错误只包含事件类型、耗时、chunk 数和 byte 数；不包含 URL、Authorization、headers、prompt
或响应正文。

Prime Agent 的 `settings.json` 由 bridge 在临时 state 中生成：provider timeout 与当前 profile 一致，
`maxRetries<=5`，指数退避基数 2 秒，`Retry-After` 与指数退避取较大值，server wait 上限 60 秒。普通
非 retryable 4xx 不重试；正常返回但答案为空不会重放整个任务。

Prime 子模块负责收到 abort 后取消 provider、终止工具、关闭 kernel，并按其固定 revision 已有的进程组
清理逻辑回收 `rlm.bash` 子树。每次修改 bridge 或 Prime submodule 后必须重新构建
`trievo-prime-agent:matrix-current`；旧 image 不会自动包含 host checkout 的修改。

## 5. L1 daemon-owned workspace

当 `TRIEVO_DOCKER_VOLUME_TRANSPORT=required` 时，L1 `DockerSandbox` 创建 daemon-owned named
volume。sandbox 容器、baseline Agent 容器和 verifier 看到同一个 `/workspace`，不依赖 Docker daemon
解析客户端 checkout 或临时目录的 bind mount。

同步顺序为：

1. Turn 开始前 volume -> host mirror；
2. host 应用 fixture、session/environment updates；
3. host mirror -> volume；
4. Agent 和 verifier 直接在共享 volume 上工作；
5. checkpoint 前 volume -> host mirror，再制作 `workspace.zip`；
6. flow 结束时最后同步并删除 carrier container 和 named volume。

每次 host-to-volume 同步先清空 volume，防止删除过的文件复活；volume-to-host 使用 staged directory 和
原子目录交换，失败时恢复旧 host tree。Agent 与 verifier 不再通过两个不可见的 host bind 各自读取目录。

## 6. 构建与验证

```powershell
python -m pytest -q `
  tests/test_model_reply.py `
  tests/test_model_transport.py `
  tests/test_prime_agent_baseline.py `
  tests/test_docker_volume.py `
  tests/test_baseline_runtime.py `
  tests/test_arena_runtime.py `
  tests/test_runtime.py `
  tests/test_matrix_portability.py

docker build -t trievo-prime-agent:matrix-current `
  -f baseline/adapters/prime_agent/Dockerfile .
```

单元测试不调用真实模型。Docker daemon 可用后，先用合成 workspace 验证 named volume 双向同步，再按
provider 文档运行一个精确 `--only` job；不得用 smoke/gate 代替正式矩阵。
