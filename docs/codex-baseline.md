# Codex baseline

`baseline.type=codex` 或 CLI `--baseline codex` 通过现有 manifest registry 选择 Codex，
无需修改 Runner。源码是 `baseline/codex` Git submodule，固定 revision 为
`cc7ba33601286a751477832b01de7b6f8de0dd9c`。Runtime 在启动前检查 revision 和源码是否干净。

## 构建与选择

```bash
git submodule update --init baseline/codex
docker build -t trievo-codex:0.1 -f baseline/adapters/codex/Dockerfile .
export OPENAI_API_KEY=<your-key>
trievo-bench --spec <benchmark.json> --baseline codex \
  --baseline-config examples/codex-baseline.override.json --output runs/codex
```

镜像使用 Rust 1.95 从固定源码构建 Codex CLI，包含 Python、Node.js 22、Git、Bash、ripgrep。
如任务要求其他编译器或 Python 库，请扩展该镜像；Agent 工具在 baseline 容器执行，
verifier 在任务 sandbox 执行。两个容器共享逻辑 workspace。

spec 的等效配置：

```json
{
  "baseline": {
    "type": "codex",
    "options": {"model": "gpt-5.5", "reasoning_effort": "high"},
    "evolution": {"policy": "disabled"}
  }
}
```

L1 Arena：

```bash
python -m trievo_bench.l1_arena_compat \
  --package benchmarks/current/l1_arena --stream stream-2 \
  --backend docker --image trievo-ubuntu:22.04 \
  --baseline codex --baseline-config examples/codex-baseline.override.json \
  --output runs/codex/l1
```

L2 先 inspect，再运行指定 stream；支持 `reset`、`frozen`、`evolving`：

```bash
python -m trievo_bench.l2_compat --package benchmarks/current/L2_Demo_V5 --inspect
python -m trievo_bench.l2_compat \
  --package benchmarks/current/L2_Demo_V5 --stream <stream-id> \
  --image trievo-l2:0.1 --allow-generated-code --condition evolving \
  --baseline codex --baseline-config examples/codex-evolving.override.json \
  --output runs/codex/l2
```

正式结论需要同配置的三条件完整匹配矩阵。离线契约测试不代表真实模型质量或矩阵完成。

## 超参数与模型渠道

| 参数 | 作用 |
| --- | --- |
| `options.image` | 已构建的 baseline 镜像，默认 `trievo-codex:0.1` |
| `options.model` | 模型 ID；为空时读取 `TRIEVO_MODEL`，两者都为空则拒绝运行 |
| `options.base_url` | Responses API 服务根地址；回退到 `TRIEVO_BASE_URL`、`OPENAI_BASE_URL`、OpenAI 官方地址 |
| `options.api_key_env` | 密钥环境变量名，默认 `TRIEVO_API_KEY`；回退到 `CODEX_API_KEY`、`OPENAI_API_KEY` |
| `options.reasoning_effort` | 原生 Codex reasoning effort，默认 `medium` |
| `options.web_search` | 原生 Codex 搜索模式，默认 `disabled` |
| `options.model_context_window` | 可选的模型上下文窗口覆盖 |
| `options.model_auto_compact_token_limit` | 可选的原生压缩阈值 |
| `options.request_timeout_seconds` | 单次 CLI 请求超时，默认 1700 秒，需小于 Adapter 超时 |
| `options.request_max_retries` | Codex transport 重试上限，默认 2 |
| `options.memory_max_chars` | 可选记忆更新的长度上限，默认 12000 字符 |
| `evolution.policy` | `disabled` 为原生无学习对照；`turn_boundary` 为 Tri-EvoBench 记忆扩展 |
| `evolution.timeout_seconds` | 独立记忆更新请求的超时，默认 900 秒 |

自定义服务必须支持 `/responses`；只有 Chat Completions 的服务不能作为 Codex 渠道。
密钥仅通过环境传入。默认 manifest 只透传表内约定的密钥变量；自定义 `api_key_env` 时，
同时在 `environment.inherit` 与 Docker 命令 `-e` 列表中加入对应变量。
Adapter 生成独立的 `CODEX_HOME`，不会加载个人 Codex 配置、认证文件或历史会话。

官方接口参考：[OpenAI Docs：Non-interactive mode](https://developers.openai.com/codex/noninteractive/)。
bridge 解析 JSONL 事件，不解析终端彩色文本。

## 生命周期、状态与计量

- 每 Flow 使用独立 workspace 和 baseline state。每 Turn 清空临时会话，Interaction 用明确
  session ID 执行 `codex exec resume`，禁止 `--last` 选取其他会话。
- 默认 `disabled` 不产生跨 Turn 学习；`evolving` condition 本身不会开启学习。
  记忆扩展由 `examples/codex-evolving.override.json` 显式开启，属于 benchmark 扩展，
  不声明为 Codex 原生演化算法。
- `learning/memory.md` 是唯一可学习状态，受 Runtime 的 Turn freeze、回滚、checkpoint 和导出保护。
  临时 Codex sessions、配置与演化临时目录不会导出为跨 Turn 学习状态。
- Turn 末记忆更新使用独立 ephemeral 会话，仅接收公开 trajectory/feedback 和已有记忆。
  shell 与搜索关闭，输出通过 JSON schema 限定；bridge 校验并原子写入记忆。
  `update_enabled=false`、reset、frozen 或 disabled policy 只确认 barrier，不调用模型。
- Native shell、文件变更、MCP、搜索事件作为已经执行的工具轨迹返回，避免 Runner 重放副作用。
- 固定版本的 `turn.completed.usage` 是 session 累计值，bridge 返回相邻 Interaction 的差值。
  cached input 已包含在 input 中，reasoning 已包含在 output 中，不重复加到 total。
  JSONL 不暴露每次内部模型请求数，因此 `api_calls` 记录 CLI 调用次数下界，
  `adapter_metadata.api_calls_semantics` 明确记录该限制。
- 非 JSON、缺失完成事件、provider failure 和超时均失败。超时后不自动重放已执行工具。
  真实评测须通过 Docker，并把 reports、traces、checkpoints 和审计 metadata 保存在 `runs/`。

## 验证

```bash
python -m pytest tests/test_codex_baseline.py tests/test_baseline_runtime.py -q
python -m pytest tests -q
```

Codex 专项测试使用 fake CLI，不调用真实 API；覆盖参数选择、Flow 隔离、Turn 新会话、
Interaction resume、增量 usage、公开反馈过滤、disabled/frozen barrier、非法状态写入回滚、
失败记忆更新、源码 revision 校验、workspace/state 导出与 checkpoint 恢复。
