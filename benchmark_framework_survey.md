# Tri-EvoBench 提案相关开源 benchmark 评测框架调研

调研日期：2026-08-15

来源：`self_evolution_benchmark_construction_proposal(1).pdf` 的 Appendix A/B。范围只覆盖评测框架、runner、verifier、环境适配器和协议；不下载原始数据集、任务包或模型权重，不拉取完整 sandbox 镜像。

## 1. 本地 code-only 检出

本地目录：`G:\Research\projects\evobench\frameworks`。大多数仓库采用 Git partial clone + sparse checkout；仓库中出现 `datasets`、`tasks`、`skills` 字样的路径，均为 loader/verifier/接口代码，不代表已下载对应数据。

| 项目 | 本地目录 | 评测框架要点 | sandbox / 操作系统 |
|---|---|---|---|
| CL-bench / CL-bench Life | `cl-bench` | `infer.py` 生成模型输出，`eval.py` 用 rubric 逐条判断；适合 context-learning，不需要 agent 环境。 | 不使用 sandbox；宿主 Python/LLM API。 |
| AppWorld | `appworld` | 9 个模拟应用、API/数据库状态和 state-based evaluator；允许多种正确 action sequence，重点检查终态与 collateral damage。 | 可不使用 Docker；若 `appworld serve --docker`，是 Linux Docker 容器。官方 runner 未固定具体发行版。 |
| τ-bench / τ²-bench | `tau2-bench` | LLM user simulator + tool agent + symbolic DB/policy；终态 goal-state、policy 和 `pass^k`。 | Retail/airline 等核心环境不需要 sandbox；knowledge/search 路径可调用 Anthropic `sandbox-runtime`。它是宿主 OS 原生隔离：Linux 用 `bwrap`/`socat`/`rg`，macOS 用 `rg`；不是固定 guest OS，Windows 路径未提供。 |
| BFCL V3/V4 Multi-Turn | `bfcl` | multi-turn/multi-step 的 response-based + state-based checks，覆盖 missing parameter/function、long context 和工具状态。 | 基础 evaluator 不规定统一 sandbox；执行函数通常在本地 runner/工具环境中完成，OS 由运行机器决定。 |
| SkillLearnBench | `skilllearnbench` | 生成 skill 与固定 solver 解耦；`evaluate_skills.py` 对同族实例运行 deterministic verifier，并汇总 success、skill usage、token 等。 | 每次 trial 必须 Docker；`tasks/<task>/environment/Dockerfile` 决定基础镜像，统一是 Linux 容器，但发行版/依赖按 task 变化。 |
| SkillsBench + BenchFlow | `skillsbench`, `benchflow` | task.md + oracle + verifier；BenchFlow 提供 task/scene/agent/environment/score runtime，SkillsBench 做 curated/no-skill/self-generated skill 对照。 | 本地 Docker、云端 Daytona 或 Modal；容器/远端环境均为 Linux 风格运行时，但具体 base image 由 task Dockerfile 或云 provider 决定。未检出 SkillsBench 的 `tasks/`。 |
| StreamBench | `streambench` | 在线输入-反馈序列；Agent 接口有 `__call__` 与 `update`，可比较 memory/prompt/retrieval/parameter 更新。 | 主协议不规定 sandbox；DS-1000 代码执行明确不是安全 sandbox，使用宿主 subprocess，平台分支包含 Darwin。 |
| LifelongAgentBench | `lifelongagentbench` | lifelong stream、skill overlap、迁移/负迁移/遗忘；包含 DB 与 OS interaction 环境。 | README 要求 `mysql` 和 `ubuntu` Docker 镜像，并构建 `local-os/default`；OS-interaction 明确是 Ubuntu/Linux。因 Windows 超长路径只保留了根 README/依赖和仓库元数据，未取任务数据。 |
| ClawArena | `clawarena` | `infer → score → report/compare`，支持多框架、多 session、round hook 与插件 adapter。 | README/runner 未发现统一 sandbox 依赖；默认是本地框架/工作目录，OS 由被测 agent 决定。 |
| SRA-Bench / SR-Agents | `sra-bench` | 将 skill retrieval、load/incorporation 和 end-task execution 分开；有 Recall@K/nDCG、hard-negative 与代码/规则 verifier。 | BigCodeBench execution 文件明确声明不是安全 sandbox；为宿主 subprocess，不能作为安全执行隔离。 |
| Evo-Memory | `evo-memory` | 统一 memory `(F,U,R,C)`，按 stream 运行 ExpRAG/ReMem 等 memory agents，报告 accuracy/success/progress/step efficiency。 | 框架本身未规定统一 sandbox；AlfWorld/BabyAI/PDDL/ScienceWorld 等 backend 各自决定运行环境。 |
| EvoMemBench | `evomembench` | 四格协议：in/cross-episode × knowledge/execution；覆盖 memory 使用、transfer、retention/revision 与统一评测。 | 核心 suite 不固定 sandbox；Web-search 子路径可用 E2B，某些附属 memory-system 测试使用 Docker，OS 随 provider/image 变化。 |
| BenchTrace | `benchtrace` | Reflection Evaluation 测 detection/localization/diagnosis，Evolution Evaluation 测 failure avoidance rate (FAR)；支持 signal/noise/test。 | 没有统一 sandbox；Jericho、ALFWorld、BabyAI、ScienceWorld 等环境按各自 backend 运行。Jericho 还需另行取得游戏文件。 |
| TaskBench | `taskbench` | ToolGraph + Back-Instruct；TaskEval 评 task decomposition、node/edge F1、parameter/value F1 和 graph exact match。原始协议主要预测计划，不实际执行外部服务。 | 不使用 sandbox。 |
| CFQ | 仅调研（`cfq` partial clone 因 Windows 路径名未 checkout） | MCD1/2/3 控制 atom 覆盖相近、compound 分布不同；SPARQL exact match，适合迁移到 operator/DAG 组合切分。 | 不使用 sandbox。 |
| AgentSkillOS | `agentskillos`（README/架构/入口；大源码 sparse fetch 中断） | capability tree 检索 + 多 skill DAG 编排；artifact 客观检查与 pairwise/Bradley–Terry 聚合。 | 论文/README 未规定统一 sandbox；执行环境随任务/agent runner。 |
| Frontier-Eng | `frontier-eng` | 固定预算的 propose→execute→evaluate→revise；冻结 verifier 返回连续工程 reward，重点是 improvement trajectory。 | 主要是本地 Python/任务 runtime，未发现统一 Docker guest OS 要求；OS 由 task runtime/宿主决定。 |
| RSIBench-Data | `rsibench-data` | 固定 post-training stack 上评数据策略的递归自改；训练/服务、Harbor/E2B 评测、固定预算。 | 评测路径使用 Harbor + E2B managed sandbox；仓库未 pin 一个统一发行版，通常是 provider 的 Linux 环境。 |
| Rethinking Harness Evolution | `rethinking-harness` | 对比 parallel sampling、sequential refinement、harness evolution/scaling；匹配 feedback/inference budget，并做 held-out evaluation。 | Terminal-Bench 2.1 通过 E2B；E2B template 决定镜像，代码未固定 distro。 |

## 2. 只调研到协议/论文，未找到可下载的官方评测代码

| 项目 | 结论 |
|---|---|
| RealUserSim | 主要是 grounded user simulator/profile 与 PT3 fidelity 评测；本身不是 sandbox，接入 τ-bench 后继承 τ-bench 的环境。未找到提案所指向的明确官方 code-only repo。 |
| SEA-Eval | sequential correlated/orthogonal/implicit-intent streams，报告 success/token trajectory；检索到论文与摘要，未找到明确公开 evaluator repo。 |
| AgentStream | 将 AppWorld、BFCL、BrowseComp+、HLE、SWE-bench、τ²-bench 组织成 isolated/sequential/interleaved；未找到明确官方 repo，sandbox 继承底层 benchmark。 |
| Harness Updating Is Not Harness Benefit | 研究 HUC 与 HBC 的解耦，重点是 Update@Anchor/Benefit@Oracle 类协议；论文页面提到的代码仓库未能稳定定位，不能确认有公开可复用 runner。 |
| HarnessBank | 关注 harness candidate validity、activation、significance gate 与 sealed test；提案未给出明确 code repo，暂不下载。 |
| SEAGym | Harbor-compatible 的 train/update-validation/held-out ID/OOD/replay/snapshot 协议；检索到论文，但未确认公开 repo。sandbox 继承 Harbor benchmark。 |

## 3. sandbox 操作系统结论

| sandbox 类型 | 确认到的 OS/运行方式 | 备注 |
|---|---|---|
| Docker task sandbox（SkillLearnBench、SkillsBench/BenchFlow） | Linux 容器；具体发行版由每个 `environment/Dockerfile` 决定 | 不是一个统一 Ubuntu 版本；不要仅凭 Docker Desktop 的宿主 Windows 推断 guest distro。 |
| LifelongAgentBench OS interaction | Ubuntu/Linux Docker | README 明确 `docker pull ubuntu` 并构建 `local-os/default`。 |
| AppWorld Docker mode | Linux Docker 容器 | Docker 可选；不使用 Docker 时直接运行本机 server。官方命令使用 GHCR image，但 runner 未在 code-only 检出中固定 distro。 |
| τ²-bench knowledge sandbox | 宿主 OS 原生：Linux `bubblewrap`，macOS 走 macOS 分支 | `sandbox-runtime` 不是 Docker/VM；Linux 需要 `rg`、`bwrap`、`socat`，macOS 至少需要 `rg`。 |
| E2B/Harbor（Rethinking Harness、RSIBench-Data、EvoMemBench 某些路径） | provider-managed，通常 Linux；具体 image/template 未由这些 repo 统一 pin | 只核对了调用方式，没有下载 E2B/Harbor sandbox。 |
| 本地 subprocess（StreamBench、SRA-Bench、部分 BFCL） | 宿主 OS | 相关代码明确写了“不是真正的 security sandbox”，不能直接用于不可信代码。 |

## 4. 对 Tri-EvoBench 的使用建议

1. **L1 FactStream**：优先借 AppWorld 的 state-based verifier、τ-bench 的 user/tool/database loop、BFCL 的 turn-level + final-state checks；不要直接继承完整任务数据。
2. **L2 Skill/Procedure**：优先复用 SkillLearnBench 的“skill generator 与 fixed solver 分离”、SkillsBench/BenchFlow 的 task/oracle/verifier 结构、SRA-Bench 的 retrieval/incorporation/execution 三段诊断。
3. **L3 Structure**：借 CFQ 的 MCD 切分思想、TaskBench 的 ToolGraph/DAG 表达、AgentSkillOS 的 capability tree/DAG 编排；跨域 source/target 的真值仍建议由同一 latent generator 产生。
4. **Online evolution protocol**：StreamBench、Evo-Memory、LifelongAgentBench 提供 stream/update/replay/forgetting 基线；BenchTrace 和 SEAGym 提供 failure diagnosis、held-out、replay、snapshot/cost 记录方式。
5. **安全边界**：不要把 StreamBench/SRA-Bench 的 subprocess execution 当作 sandbox；真正运行模型生成代码时，应选 Docker/VM/E2B/Daytona，并单独审计网络、挂载目录、权限和 verifier 是否可被读取。

## 5. 官方入口

- [CL-bench](https://github.com/Tencent-Hunyuan/CL-bench)
- [AppWorld](https://github.com/StonyBrookNLP/appworld)
- [τ²-bench](https://github.com/sierra-research/tau2-bench)
- [BFCL/Gorilla](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard)
- [SkillLearnBench](https://github.com/cxcscmu/SkillLearnBench)
- [SkillsBench](https://github.com/benchflow-ai/skillsbench) / [BenchFlow](https://github.com/benchflow-ai/benchflow)
- [StreamBench](https://github.com/stream-bench/stream-bench)
- [LifelongAgentBench](https://github.com/caixd-220529/LifelongAgentBench)
- [ClawArena](https://github.com/aiming-lab/ClawArena)
- [SRA-Bench / SR-Agents](https://github.com/oneal2000/SR-Agents)
- [Evo-Memory](https://github.com/zhaosnw/evo_mem) / [EvoMemBench](https://github.com/DSAIL-Memory/EvoMemBench)
- [BenchTrace](https://github.com/Alab-NII/BenchTrace)
- [TaskBench](https://github.com/microsoft/JARVIS/tree/main/taskbench)
- [CFQ](https://github.com/google-research/google-research/tree/master/cfq)
- [AgentSkillOS](https://github.com/ynulihao/AgentSkillOS)
- [Frontier-Engineering](https://github.com/EinsiaLab/Frontier-Engineering)
- [RSIBench-Data](https://github.com/evolvent-ai/RSIBench-Data)
- [Rethinking Harness Evolution](https://github.com/rethinking-harness-evolution/code)
