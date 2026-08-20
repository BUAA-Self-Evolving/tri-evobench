# tri-evobench

Specification-driven baseline infrastructure for evaluating self-evolving agents.

这是一个与数据内容解耦的第一版基础实验环境。它落实 baseline评测方法.pdf 的生命周期，并为 proposal 中的在线演进协议预留了状态和 learner 接口：

~~~text
Flow: one shared sandbox
  Turn: setup -> UserSimulator -> Interaction loop -> verifier/judge
    Interaction: agent response -> sandbox actions -> judge -> feedback
  after Turn: learner observes trajectory and may update persistent state
~~~

当前版本的稳定边界是 schema_version = 0.1。任务内容可以继续变，但执行器只依赖 id/request/setup/user/verifier 这些字段；额外信息放在 metadata，隐藏 oracle 信息放在 oracle_update，不会发送给普通 Agent。未通过时严格执行到 `max_interactions`，不会因分数下降提前停止；Turn 失败时取最后一次 Interaction 的分数。

## 快速 smoke test

~~~powershell
C:\\Users\\Lenovo\\anaconda3\\envs\\codex-work\\python.exe -m trievo_bench.runner --spec examples/smoke.json --output runs/smoke --backend local --agent scripted --judge deterministic
~~~

结果写入 runs/smoke/smoke-flow-001/summary.json 和 trace.jsonl。smoke 测试包含一次失败后用户纠正的 Interaction，以及跨 Turn 共享 sandbox 的检查，不消耗任何模型 token。

## Ubuntu sandbox

~~~powershell
docker build -t trievo-ubuntu:22.04 sandbox/ubuntu
C:\\Users\\Lenovo\\anaconda3\\envs\\codex-work\\python.exe -m trievo_bench.runner --spec examples/smoke.json --output runs/docker-smoke --backend docker --image trievo-ubuntu:22.04
~~~

运行器按 Flow 启动一个 Ubuntu container，所有 Turn 共用它；Flow 结束后删除 container。LocalSandbox 只用于无 Docker 的开发和单元测试。

## 模型适配器与 token 预算

模型调用是可选的。不要把 key 写入 spec 或仓库：

~~~powershell
$env:TRIEVO_API_KEY = '<your-key>'
$env:TRIEVO_BASE_URL = 'https://api.deepseek.com'
$env:TRIEVO_MODEL = '<model-id>'
C:\\Users\\Lenovo\\anaconda3\\envs\\codex-work\\python.exe -m trievo_bench.runner --spec examples/smoke.json --agent openai_compatible --judge deterministic --max-total-tokens 3000000 --output runs/model-smoke
~~~

客户端在发请求前进行输入加最大输出 token 预检查，并在响应后记录实际 usage；超过上限会直接停止。L1/L2 回归产生的运行结果位于本地 `runs/`，该目录不会提交到 Git。

## 迁移数据规范

- request 是第一个用户请求；user.followups 是 simulator 在失败后的后续请求，支持 `{feedback}`、`{history}`、`{turn_id}` 占位符，也可替换为 grounded simulator。
- setup.files/remove 只描述 Turn 初始化时对共享 workspace 的变更。
- 严格 Baseline 使用 LLM rubric Judge；离线 smoke 和可复现测试可使用确定性 verifier，支持 response_contains、file_exists、file_contains、json_file_equals、all、any，以及 task-owned Python verifier。
- Agent 只通过结构化 actions 修改 sandbox；这样可以把未来工具 API、AppWorld/τ-bench 风格工具和当前文件操作放在同一执行边界。
- 每个 Interaction、Turn 和 Flow 都落盘；结果包含 state、sandbox snapshot、feedback、token budget 和基础曲线指标。
- --learner frozen|raw-replay|evolving|oracle-update 已接入；其中 oracle-update 仅用于校准上界，不属于普通评测。

更完整的字段说明见 schemas/benchmark.schema.json。

## L2 Stream adapter

L2 demo 的单位是持久 Workspace 上的有序 Episode，且任务可见数据和私有 evaluator 必须分离。L2 adapter 先验证这种边界，再把原生 L2 runtime 放进一个临时受限 Ubuntu container：源包只读挂载，唯一宿主机写入位置是 output 目录。它不把 evaluation.json 或 evaluation_assets 注入模型上下文。

~~~powershell
docker build -t trievo-l2:0.1 -f sandbox/l2/Dockerfile .
C:\Users\Lenovo\anaconda3\envs\codex-work\python.exe -m trievo_bench.l2_compat --package <path-to-l2_demo> --inspect

$env:L2_API_KEY = '<your-key>'
$env:L2_API_BASE = 'https://api.deepseek.com/v1'
C:\Users\Lenovo\anaconda3\envs\codex-work\python.exe -m trievo_bench.l2_compat --package <path-to-l2_demo> --stream sra_bigcodebench_pandas_dataframe_viz_001 --allow-generated-code --output runs/l2
~~~

`--allow-generated-code` 需要明确指定。Docker 限制 host 写入、权限、PID、CPU 和内存，但不等同于对不可信代码的安全证明。

两个 native browser-smoke Stream 另用可选 image：

~~~powershell
docker build -t trievo-l2-browser:0.1 -f sandbox/l2/Dockerfile.browser .
C:\Users\Lenovo\anaconda3\envs\codex-work\python.exe -m trievo_bench.l2_compat --package <path-to-l2_demo> --stream skilllearn_stock_data_visualization_001 --image trievo-l2-browser:0.1 --allow-generated-code
~~~
