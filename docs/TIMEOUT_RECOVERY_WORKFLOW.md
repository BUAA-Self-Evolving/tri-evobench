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

### Friday 多容量 admission（2026-10-17）

后续新批次可用 `--friday-capacities` 指定每槽 worker 数；默认一路保留旧调度。
每个 `level + flow_id + baseline` 的 condition jobs 固定到同一账号、同一串行通道，多个通道可并行。
driver 同时检查全局 worker 上限、每账号容量和通道互斥；接管存活 worker 时核对原槽位与通道身份。
容量变化必须用新输出根，不能通过修改旧状态迁移已付费任务。此优化不改变请求 retry、checkpoint、
一次性授权和已执行工具副作用的恢复边界；也不提供跨 driver/原生 Agent 的请求级 RPM 限流。
配置与只读预演见 [Friday 多 App ID 调度器](FRIDAY_MULTI_APP_SCHEDULER.md)。

### 新批次共享请求网关（2026-10-17）

可选的 [Friday 快速配置](FRIDAY_FAST_PROFILE.md) 为六槽各 10 RPM、各最多 2 个在途请求、各 2 worker，
本机全局 8 worker。宿主机 evaluator 与容器原生模型端点都指向共享网关；driver 在启动 worker 前
校验每个凭证和 policy，快速启动器还校验 Docker 可达性。原配置未启用该 profile 时行为不变；旧 root 不能直接切换。

网关通过同机共享 SQLite 对实际账号统一做 6 秒发起间隔、在途租约与 429 后至少 60 秒冷却。
它不自动重试，不改变成功回复/SSE、Turn 预算、工具执行、评分或 checkpoint。HTTP 200 内的业务 429
转成经过脱敏的 HTTP 429，让调用方按已有同次请求 retry 边界处理；每次 retry 也重新占预算。
客户端在排队时断开则不发上游请求；排队超时明确返回 admission 错误。已 dispatch 的失败不重发。
中断或租约过期不产生新的恢复授权；不得为了加速重放已付费 Interaction、删除共享预算或修改旧 checkpoint。

### 已运行批次的请求限流收紧（2026-10-27）

用户明确要求降低上游请求负载时，可用`tools/throttle_friday_requests.py`对共享SQLite做一次
有审计的、仅允许降低的请求RPM/在途上限调整。该操作不迁移worker容量、不更改冻结配置、
不停止已dispatch请求、不删除租约、不重放Interaction；已有请求排空后才满足新的在途上限。
修改在同一事务内校验所有目标账号的旧值，并保留原cooldown、提高next_start以满足新间隔。
独立目录的intent先持久化，commit后写applied；若后者失败须读取实际状态核对，不盲目重试。

网关已加载进程每次admission读取SQLite，所以收紧即时作用于后续请求。旧`/health`及矩阵中的
policy仍是启动声明，实际限流须以SQLite和独立applied审计为准，不篡改历史声明。共享这些账号
的同机网关均受影响；操作前核验其他调用方。旧启动policy与数据库不一致时register会拒绝重启，
不得删除数据库或擅自恢复高额度；后续重启须单独准备一致的配置/运行版本。无法从本机历史拒绝数
推断平台精确RPM/TPM或共享范围；保守降速后仍须观察429与排队/超时，不能直接宣称额度已验证。

用户另行明确要求真实容量测试及提高并发时，可用`tools/calibrate_friday_accounts.py`在独立审计根
执行有上界的账号校准：5/10RPM、1/2/4在途，正式与探测共用同一SQLite pacing/leases。
该授权不来自收紧工具本身，必须来自用户本轮任务。每次升降均CAS核对有效旧值、先落盘意图再
提交并记录回执；保留已dispatch请求、租约和cooldown。首次429中止该账号该RPM后续档位并回5/1，
不循环重试或自动提高；未实际达到的并发/速率不能记成通过。探测只保存数值诊断/用量，不写认证、
提示词或原响应。先让原生排队请求获取配额，测试请求使用剩余容量；正式的工具/学习/判分限额不变。
选择配置后另做原生自然请求观察；较高档实测通过也留RPM余量，失败回保守档。此工具不恢复旧错误
单元、不迁移worker lane，不改变冻结批次或分数。相应manifest、逐请求/预算事件与summary须独立保留。

若5RPM通过而10RPM在单在途即出现RPM拒绝，可在前一控制器完成后用
`tools/refine_friday_rates.py`顺序测6/7/8/9RPM，最多2在途；一个账号首次拒绝后不再测试其更高速率。
每档仍60秒消退和120秒测量，选择至少达到目标90%成功吞吐的档位，再留10%RPM余量做5分钟
原生观察。不同时运行两个限流控制器；启动时核对前一summary和实际SQLite一致，保留原声明与
独立有效policy回执。未达到的速率/并发、无自然流量的账号、短窗口及长请求晚到均须明确披露。

### V5 新批次绑定与 τ² 模拟器路由（2026-10-18）

V5 矩阵将 package manifest、pool manifest、runtime profiles 和 panel 的哈希固定到
job、worker 命令及报告；旧版本 job 不增加 V5 参数。V5 聚合要求三条件的 pool/package/profile、
逐 Turn source/profile/生效预算身份一致，保留连续分数并禁止跨 pool/panel 聚合。

独立运行配置可通过 `options.tau2_live_config` 指定 `user_llm_base_url`、`user_llm` 和
`user_llm_api_key_env`；launcher 在私有临时配置中合并这些公开路由字段，不改包内任务资产，
只按环境变量名继承该槽凭证。模拟器与 NL evaluator 使用同一配置，路由记录进入 session evidence。
未设置该选项的历史调用保留包默认行为。路由注入测试不等于真实模拟器端到端验证。

新V5批次可显式设置 `options.l2_task_terminal_transport=true`。任务容器与Agent共用独立临时
Unix socket卷，公开终端命令在声明镜像、禁网、工作区UID下执行；模型凭证及私有控制socket
不进入任务执行域。该临时卷不进入workspace/learner checkpoint，每Turn清空；每Interaction由
host私有控制设置时间预算并收尾存活命令。原生adapter必须在start_turn确认此传输，否则在付费
interact前拒绝运行。原生public tool/model计数与内部shell bootstrap命令计数分别记录，不能互相
冒充。回复先写reply_received，再收尾终端和tau2 session；收尾异常不授权重放已付费Interaction。
镜像可用、代码专项通过及原生工具端到端验证是三个独立门槛。
新V5长任务允许terminal ceiling最多3600秒并按episode减一/逐Turn profile收紧；旧模式范围保持600。
Hermes同时需要bridge元数据校验及其原生TERMINAL_MAX_FOREGROUND_TIMEOUT配置允许该值，
后者必须显式传入Agent容器；仅在90秒mock通过不能推断899/3599秒真实metadata可运行。
独立长预算原生工具mock应使用最终长预算与镜像/环境。start_turn拒绝且0调用/无Interaction边界
可采证后新root重新准备；不得由此推广为已开始付费或有工具副作用Interaction的重放授权。


### 大workspace Judge清单与保存回复恢复（新批次显式启用）

`options.l2_judge_inventory_encoding=complete_paths_v1`只在清单超过128000字节时改变workspace元数据表示。
全部路径以可精确还原的前缀树提供，增删用集合差、修改用精确路径集或交集补集；小清单保持原payload。
逐文件SHA256完整before/after/added/removed/modified清单保存于输出root的私有`_judge_inventory/`，
用总SHA256绑定。Agent回复、工具及结果、历史、原生verdict和rubric文本不截断，不改评分尺度；
产物记录表示版本/完整manifest位置与摘要。估算tokens不等同模型实际分词器数量。

Judge请求错误时，原score:null/日志/report/checkpoint保留。`phase=adjudicate`且有完整保存回复、
workspace/state归档时可用新root从该回复续评估；不能整条新跑、不能重放已执行Agent Interaction。
新续评估如发生在协议还有剩余Interaction时，只允许新编号，不能重复旧编号。原失败Judge尝试如果
发生在immutable checkpoint之后，须在续跑预算或独立账本准确补记一次，不能静默丢弃。专用guard
在dispatch前拒绝旧Interaction，独占锁和一次性claim防止监督重复启动；既有恢复凭证不因此续签。

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

### V5 tau2工作区权限（2026-10-18）

新Task传输模式的session服务显式按workspace UID/GID运行，保持cap-drop ALL和workspace0755。私有控制socket、日志和record保存在session容器私有/tmp/trievo-tau2-private；Task只共享/workspace/.tau2公开接口。旧未启用模式保留原路径。权限失败发生在session.thread.start和Agent.interact前时，须核验0calls/0tokens、无保存回复、进程退出和锁释放，再以新root补原预声明smoke；interact_started检查点名称本身不证明已经dispatch。已完成其他smoke保留，不重跑。

### Prime abort退出与episode收尾（2026-10-18修复）

L2 overlay曾覆盖L1已有abort ACK后的idle门禁；旧实现在没有agent_end时等待至deadline，零API反例已复现。修复保留abort请求ID，ACK后查询isStreaming/isCompacting，忙则继续有界等待，确认idle后才能完成原prompt。仅显式collect_episode_limit_reply新配置允许episode到期先abort，再最多30秒收取同一原请求的回复/原生usage；没有重新prompt，未确认idle仍为基础设施错误。V5 Prime host transport另给60秒收尾窗口，不扩大模型/工具工作预算。新内容无关probe记录命令ID及event type，不记录请求/凭证；native usage.api_calls为上游原生计数语义，不能无审计当作HTTP精确次数。

V5恢复前缀须以checkpoint元数据构造run_configuration进行包/pool/panel/条件和原Turn前缀验证，不可用row目标值填充以绕过身份检查。失败交互无保存回复且已有工具副作用时仍需显式单次授权；授权消费和runner claim均不可重复创建。

### Prime refine完成判定（2026-10-18巡检修复）

RPC的agent_start/agent_end与get_state idle门禁仅用于prompt。refine命令由原生实现等待
学习结果持久化后返回成功response；其间的后台agent生命周期事件不能把该response转为
额外的prompt idle等待，否则原生refine_complete后仍可能虚假超时。refine仍须收到匹配
请求ID的成功response，单独的refine_complete事件不替代response，错误和绝对超时继续失败。
已发生学习状态变更但缺少post-Turn检查点时保留现场；不得从上一Turn检查点重放任务或
重复refine。保存回复续评估也不能自动恢复已经执行过的学习屏障。

### Adaptive学习回复持久化（2026-10-19）

新批次可显式设置`options.preserve_evolution_completions=true`，在JSON解析和状态应用前将
每次已返回的学习完成包保存于声明状态`evolution/history/completions/`。内容仅包括学习
文本、可见推理、finish_reason、usage与Turn标识，不含请求、环境或认证字段；文件独占创建
不覆盖，位于子目录以避免改变顶层学习次数计数。该开关默认关闭，旧批次行为不变。
保存成功不表示JSON合法、学习成功或预算额度恢复；解析仍严格失败，没有自动补全、重发或
任务重放。失败报告可能尚未累计该学习usage，应按保存完成包单独记账一次，不把缺失当零。

### 结束时导出daemon卷状态（2026-10-19巡检修复）

end_flow在销毁adapter和状态卷前，读取最新声明状态的完整字节并物化到宿主镜像，再用同一次
采集的snapshot导出baseline_state.zip及目录。仅取metadata会在interact异常后导出旧host镜像，
甚至出现snapshot有文件而ZIP为空；旧失败现场不改写，不以修复后的新导出冒充旧证据。
只采集原声明paths/guard_paths并遵守ignore_paths，不扩大Agent可见性或改变恢复授权。
OpenCode首交互未返回session时，report累计0calls不证明0dispatch；缺少保存回复和合法
post-Turn的原交互继续隔离，不据此重放。新未运行队列使用新root与冻结清单。

### Adaptive非法学习提案的有界失败（2026-10-19）

新显式options.recoverable_evolution_proposal_errors=true仅将已返回完成包的JSON解析失败转为
completed:false/recoverable_failure:true，准确返回原usage；默认关闭。不得修补JSON、伪造学习成功、
应用非法提案或重发请求。宿主沿用有界学习失败合同：记账一次、回滚学习状态、保留task score、
Turn passed=false/final_category=evolution_limit，继续下一Turn。此失败处理策略变化须随结果披露。

宿主在evolve异常或completed:false时，先将声明状态字节导出到私有failed_evolution/唯一目录，
然后执行稳定状态回滚。归档明确标recoverable_checkpoint:false，不是恢复检查点或Agent可见状态。
失败采证不可用时继续报错，不能因此丢失证据并冒称可恢复。旧失败Turn缺原文/usage不追补为零，
不从上一post-Turn重放已执行任务/学习；此功能不授权恢复旧学习屏障。

### 2026-10-20 用户授权的单元错误延后完整替代

快速启动器continue模式现在覆盖单worker；单元异常不阻断后续pending，保留原paused_error及
日志/检查点。队列排空后顶层completed_with_errors不是paused_error，failed-job-ids.json列待处理项。
进程返回1仍表示有错误，不伪造通过。全局存储健康门禁继续按配置执行。

本次用户明确要求“某个单元出问题直接先跳过这个单元继续跑，然后等到后面统一把这些报错的再重跑”。
该授权用于新root中的一次完整错误单元替代，不是旧原地Interaction恢复或旧凭证续签。
完成46单元排除；原7及新错误只在97pending排空后各新跑一次，原部分结果不拼入新尝试，
原始证据与用量保留且不能把历史未知usage当0。替代不以分数选取，合法低分任务不触发重跑。

新Adaptive显式bounded_model_requests修复旧配置120秒未传SDK的兼容问题：设置请求timeout、
交互episode剩余时间限制、SDK max_retries=0。旧配置默认路径不改变；网络/工具错误仍会将
该单元标错后继续其他单元。SDK读超时不是连续响应体的全局绝对deadline，不声称消除所有超时。

### 2026-10-26 框架修复与新授权修复轮

Prime诊断probe不再因stderr的EAGAIN或关闭中断已经发送的RPC，tool_execution_update每秒最多
记录一条，下一条成功诊断报告丢弃数量；不重发prompt，不截断实际模型/工具结果。
新配置显式`options.l2_per_interaction_limits=true`才按包的每Interaction模型/工具上限重置计数；
Turn原有absolute deadline保留，到期直接返回无新prompt的execution-limit结果。未启用的旧调用
保留原累计Turn计数。新报告的reply.raw.limit_scope记录interaction，须披露这项执行修正。

新`options.l2_verifier_input_copy=true`仅用于workspace输入binding：复制全部工作区文件和符号链接，
校验文件内容摘要相同，通过独立root-owned daemon卷只读提供给cap-drop verifier。原工作区和
归档的字节、属主/权限不改，私有包资产仍只在verifier中可见；不添加Linux能力、不跳过scratch
文件、不放松native测试。inline/Tau2 live-session binding保留原路径，不复制活跃socket。

Hermes新`options.bounded_evolution_barrier=true`把usage flush、review启动、等待、取消及收尾均置于
Turn声明的学习时限内观察。到期最多10秒请求取消/收尾后fail closed，报告usage_before、
prepare_review、review_wait、cancel_review或usage_after等阶段；超时runtime永久禁止进入下一Turn，
不把尚未提交的学习状态称完成，也不在同一runtime重复学习。外层transport可留150秒收尾窗口，
声明学习工作限额仍120秒。失败state采证与recoverable_checkpoint:false合同继续适用。

用户在2026-10-26明确要求修复并重跑可运行错误单元，这是独立新授权，不能续签10月20日已消费的
单次凭证。143个完整单元不重跑。每个新尝试/保存回复续评估使用新root、独占锁及独占创建claim；
运行前重新核对原进程出生、两driver和监督锁、保存回复、workspace/state完整性及凭证可用性。
Hermes s08可从原p0002-01保存回复续评估，保留原两Turn合法前缀；原错误t0003不计覆盖。另六个
旧失败无法直接原地重放/重复学习的单元，可在本次授权范围各做一次独立完整替代，旧证据保留。
不因新尝试分数更高或更低择优；新尝试再失败则保留错误和阶段信息，不循环重试。若确认baseline
原生故障，按用户规则在独立评分视图把对应baseline+condition+stream整体置0，真实覆盖另记。

### 2026-10-27 本机新批次性能档

`docs/L2_LOCAL_FAST.md` 记录本机资源校准与新 root 入口。可选 resource policy 只控制 admission，
不降低 Task/verifier 配额，不停止已开始的交互，也不把资源暂停变成重试授权。多 panel 共用一个
driver 的全局容量；fast launcher 的本机共享 admission 锁避免多个快速 driver 重复占满资源。

显式 `TRIEVO_FAST_CHECKPOINTS=1` 使用私有缓存、完整标准 ZIP、相同快照硬链接和 APFS 共同块克隆。
摘要缓存必须同时匹配 inode/设备/大小/mtime/ctime/模式；文件变化期间不得提交快照。
所有 ZIP 均可独立恢复，旧名字不会被原地改写，删除缓存名字不破坏已提交的硬链接。
此机制不跳过 interact_started、reply_received、adjudicate、evolution 或 post-Turn 边界，
不改变保存回复恢复、claim、用量记账及一次性授权。旧配置默认行为保持不变。

新阶段/请求计时只保存公开阶段名、槽名、状态、字节与耗时，不记录请求/回复/认证内容。
任务依赖预装和线程配置须随新配置及镜像摘要披露，不回填历史报告。


### 2026-10-29 显式新root dispatcher接管与高并发档

`--continue-state <immutable-handoff.json>`只用于用户明确授权的新root调度接管；源dispatcher必须
退出，输出root不得已有matrix状态，且必须启用Friday/storage guard。完整scope、manifest/pool/panel、
凭证slot/lane及网关policy必须与源state一致。源state需有`continuation_authorization.user_authorized=true`。
已运行worker通过原PID/出生/命令/output核验接管，不发新Agent请求、不移动其工作区或已付费输出。
worker在切换期间退出且有完整合法report则记完成；缺完整report则blocked_recovery，不自动回退。
原completed必须有匹配报告、paused_error/blocked/needs_attention保留；只有真正pending且原输出无执行
证据的行使用新root路径和配置。原driver的旧matrix从此只是冻结的调度快照；原活跃worker的report、
trace及checkpoint仍会在其原output内继续增长。新状态的continuation记录源SHA与旧resource policy，
不改写已消费retry claim，也不授权旧错误或Interaction重放。

资源policy变化仍使用新root与独立冻结配置；不能原地覆盖旧policy以绕过一致性检查。10/29本机
混合负载测试支持试运行10worker上限、heavy最多4/τ²最多6的admission档；降低的是调度预留，Task和
verifier原4CPU及各模型/工具/学习限额保持。实时CPU/可用内存/宿主换页/存储门禁继续限制新admission，
不会杀停已经付费的worker。仅停止dispatcher并不能减少已开始的并发；若需回退应接管全部存活worker
并让它们排空，不能用停止worker或回退checkpoint假装无损降并发。

该结论仅覆盖当时混合探测和短窗口原生流量，非全任务峰值、长期OOM保证或整批提速证明。12路短测
虽完成，但CPU/换页尖峰及计算延迟更大，未实际部署。实测、保留限制和当前唯一运行入口见工作区
progress.md及`runs/glm53-concurrency-tuning-20261029/REPORT.md`。普通fast launcher的旧默认配置不因
本批次试运行自动提升，不扩大六账号实测5RPM策略。
