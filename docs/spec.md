# Benchmark specification 0.1

本规范把“内容”与“执行协议”分开。数据同学可以先使用 JSON；运行器也接受 YAML（安装 PyYAML 后），但版本字段必须保持为 0.1，未来破坏性变化通过 0.2 迁移。

## 生命周期

1. 创建一个 Flow sandbox。
2. 对每个 Turn 执行 setup，但不清空 Flow sandbox。
3. 创建该 Turn 的 User Simulator。
4. 发送 request，解析 Agent 的 JSON response 和 actions。
5. 应用 actions，运行 verifier/judge，记录 snapshot 和 feedback。
6. 未通过且未达到 max_interactions 时，User Simulator 从 followups 取下一请求。
7. Turn 结束后，learner 读取轨迹，更新持久 state。

## Agent 输出

~~~json
{
  "response": "human-readable answer",
  "actions": [
    {"op": "write_file", "path": "/workspace/result.txt", "content": "..."}
  ],
  "state_update": {}
}
~~~

state_update 只有在 --learner evolving 时才会由 runner 在 Turn 结束后合并进持久 state；评测侧不得因为 Agent 自报成功而加分，分数来自 verifier。

## 数据隔离

普通 Agent 只收到 request、历史、持久 state 和 sandbox snapshot。oracle_update 只由 oracle learner 读取；生产数据建议将其拆到受保护的 oracle 文件中，而不是和公开任务放在同一文件。

## 推荐扩展点

- user_sim.py：替换为 grounded、可修正的 user simulator。
- sandbox.py：增加工具服务或容器快照，同时保持 write/read/remove/snapshot 接口。
- verifier.py：接入任务自己的确定性 evaluator；Judge 只用于补充语言质量或诊断。
- learner.py：实现 memory、skill、harness 或 parameter state 的更新，不改变 Flow/Turn/Interaction 协议。
- agent.py：接入不同模型、rollout 或 tool protocol，不改任务数据。

## L1 demo 兼容格式

对于 session-1/turnN/task.json 格式，可用 trievo_bench.l1_compat 导入。它保留 user_message、max_iterations、iteration_policy、evaluate.type、workspace_files 和 available_tools，并将：

- rule_verifier 映射为 task-owned verify.py；
- llm_as_judge 映射为 rubric Judge；
- agent_workspace 映射为一个 Flow 共享的 /workspace；
- iter_score_0_threshold 映射为 Turn 的 success_threshold。

示例：

~~~powershell
$env:TRIEVO_API_KEY = '<your-key>'
$env:TRIEVO_MODEL = 'deepseek-v4-flash'
python -m trievo_bench.l1_compat --session <path-to-session-1> --turns turn4 --backend docker
~~~

L1 兼容入口默认只把输入 workspace 文件注入 Agent；abstract_polished.txt、abstract_zh.md 和旧的 iterations 日志不会作为初始产物注入，避免把历史输出泄漏给新一轮评测。

## L2 demo 兼容格式

L2 不是 L1 Turn 的扩展：一个 Stream 拥有持久 workspace 和有序 Episode，而 `evaluation.json` / `evaluation_assets/` 必须保持 benchmark-private。`trievo_bench.l2_compat` 会在运行前验证这一可见性边界、workspace 生命周期和 `latest_interaction` 计分策略；不会把私有 evaluator 内容送进 Agent prompt。

API Stream 由 L2 原生 runtime 在一个受限、一次性 Ubuntu container 内运行，源包只读挂载，`--output` 是唯一的宿主机写入点。生成代码仍须显式 opt-in：

~~~powershell
docker build -t trievo-l2:0.1 -f sandbox/l2/Dockerfile .
$env:L2_API_KEY = '<your-key>'
$env:L2_API_BASE = 'https://api.deepseek.com/v1'
python -m trievo_bench.l2_compat --package <path-to-l2_demo> --inspect
python -m trievo_bench.l2_compat --package <path-to-l2_demo> --stream sra_bigcodebench_pandas_dataframe_viz_001 --allow-generated-code --output runs/l2
~~~

Browser-smoke Stream 使用独立 image，避免将 Chromium 强加给普通代码评测：

~~~powershell
docker build -t trievo-l2-browser:0.1 -f sandbox/l2/Dockerfile.browser .
python -m trievo_bench.l2_compat --package <path-to-l2_demo> --stream skilllearn_stock_data_visualization_001 --image trievo-l2-browser:0.1 --allow-generated-code
~~~
