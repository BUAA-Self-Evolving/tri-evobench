# DeepSeek Harness 与 ZCode baseline

注册 ID 为 `deepseek-harness` 和 `zcode`。两者使用下载到仓库的原生源码，固定为 Git submodule；Runner、预算、验证器、公共轨迹、Turn barrier 和断点恢复沿用 baseline runtime。真实测评必须使用 Docker。

## 构建与切换

在仓库根目录执行：

```bash
git submodule update --init baseline/deepseek-harness baseline/ZCode
docker build -t trievo-deepseek-harness:0.1 -f baseline/adapters/deepseek_harness/Dockerfile .
docker build -t trievo-zcode:0.1 -f baseline/adapters/zcode/Dockerfile .
export TRIEVO_API_KEY='<provider key>'
trievo-bench --spec <benchmark.json> --baseline deepseek-harness \
  --baseline-config examples/deepseek-harness-baseline.override.json --output runs/deepseek-harness
trievo-bench --spec <benchmark.json> --baseline zcode \
  --baseline-config examples/zcode-baseline.override.json --output runs/zcode
```

也可直接通过 spec 的 baseline 超参数选择：

```json
{
  "baseline": {
    "type": "zcode",
    "options": {
      "model": "glm-5",
      "api_type": "openai-chat-completions",
      "context_window": 128000
    },
    "evolution": {"policy": "disabled"}
  }
}
```

这只是 spec 的 baseline 片段；其余 run、sandbox、flows 字段沿用测评包。
L1/L2 兼容入口使用相同 `--baseline`、`--baseline-config` 参数；先执行
`python -m trievo_bench.l2_compat --package <package> --inspect`，再将对应 baseline 参数加入原有测评命令。保存每个条件的 summaries、traces、checkpoints 和 audit 元数据到不同的 `runs/` 目录。

## 原生接口与超参数

| Baseline | 源码版本 | 执行接口 |
| --- | --- | --- |
| `deepseek-harness` | `d743267388641bc76f17c45ce8b4c231aed1d32c` | 官方同步 Python SDK，`dsh --profile sdk-minimal` |
| `zcode` | `aac4755666d09fdcd70272fcf063c077a639015f` | 原生 CLI，`--output-format stream-json` |

DeepSeek Harness 使用官方 `sdk-minimal` profile 的 persistent shell 和本地执行能力。该组合的 provider 为 `deepseek-official`，可配置 `model`、`base_url`、`reasoning_effort`、`max_output_tokens`、`request_timeout_seconds`。固定源码中的 DeepSeek provider 使用 Messages 协议，默认根 URL 为 `https://api.deepseek.com/anthropic`；自定义 endpoint 必须兼容 Messages API，不能使用仅支持 Chat Completions 的 proxy。它不等同于交互 CLI 的全功能 profile。SDK 子进程由适配器管理，并在 barrier 或 Flow 结束时关闭。Docker 安装源码 SDK 并显式传入构建的 `dsh_bin`，不需要另下载 SDK runtime wheel。

ZCode 使用 `yolo` headless 模式、禁用 workflow 和浏览器，配置中关闭 Project Memory、subagent、MCP、plugins 和 hooks；原生仓库文件与指令读取仍保留。可配置 `model`、`base_url`、`api_type`、`context_window`、`reasoning_effort`、`model_config` 和 `request_timeout_seconds`。`model_config` 是原生 sparse model config overlay，可设置模型能力与 `optionSpecs`；推理档位必须与模型配置的 `reasoningLevel.values/map` 匹配。默认使用源码自带 provider 规则，不主动下载或刷新 provider 配置。项目配置按原生优先级可覆盖用户配置，测评包需要审计这类项目配置。

`base_url` 的备用来源为 `TRIEVO_BASE_URL`；模型为空时读取 `TRIEVO_MODEL`。DeepSeek API key 备用来源为 `DEEPSEEK_API_KEY`，ZCode 为 `ZAI_API_KEY`。默认先读取 `TRIEVO_API_KEY`。自定义 `api_key_env` 时，须同时在 manifest override 的 `environment.inherit` 和 Docker command 中加入对应变量。不要把 key 写入 spec 或 override。

ZCode 原生 provider schema 要求显式 key，因此运行时从环境变量写入权限为 `0600` 的临时配置，执行结束（包括超时与错误）删除。会话目录不进入 baseline state checkpoint。不要把 `/agent-state/sessions` 作为发布的产物；运行 trace 中保留会话 ID 和原生公开事件。

## 生命周期、预算与恢复

每个 Flow 的状态由 runtime 隔离；每个 Tri-EvoBench Turn 新建隔离 HOME 和原生 session，同一 Turn 的多次 Interaction 才使用同一 session。Turn 末恰好一次 `evolve` barrier；这两个适配器仅支持 `evolution.policy=disabled`，barrier 返回 skipped，不额外调用模型或写入学习状态。设置其它学习策略会明确报错。`reset/frozen/evolving` 条件仍可由测评框架安排，但这两个 baseline 没有新增跨 Turn 学习算法。

Checkpoint 只导出 manifest 声明的 `learning/**/*`（默认为空）和 runtime 工作区；原生 session、provider key 和配置不导出。恢复从下一 Turn 开始时新建 session，不重放已经执行的工具。运行中断、provider failure、缺失 usage、混合 session 或 malformed stream 都会报告协议错误，不当作成功回答。

工具已由原生框架执行，适配器标记 `tool_calls_are_executed`，避免 Runner 重复执行。ZCode 多个 tool lifecycle 事件合并为一次工具调用。DeepSeek 的未缓存输入与 cache read/write 是分离计数，适配器汇总为总输入，并保留 cache 明细；reasoning 属于输出明细。ZCode 使用本次 `result.usage` 的 provider summary，保持原生包含 cache 的输入口径，不使用 session 累计 `projection.totalTokenCount`。两者使用本次实际模型请求的 token 总数，成功交互不会重复计费。

## 验证

```bash
python -m pytest tests/test_native_baselines.py tests/test_baseline_runtime.py -q
python -m pytest -q
```

单测使用 fake SDK / CLI，覆盖参数选择、会话隔离、工具结果、缓存与失败尝试计费、错误与超时、冻结 barrier、工作区导出和 checkpoint resume。`test_built_native_agent_with_local_mock_provider` 还用 localhost 模拟 provider 验证已构建的原生 SDK/CLI、同 Turn session resume 和 usage；源码未构建时跳过，不调用外部 API。单测不能替代 Docker 构建或完整真实测评矩阵；发布测评结果前须在有 Docker 的主机上构建并运行所需矩阵。
