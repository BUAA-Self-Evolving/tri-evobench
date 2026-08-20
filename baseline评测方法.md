## 基本结构

- 每个 **Flow** 对应一个独立的 **Sandbox**，整个 Flow 执行期间共享该 Sandbox。
- 每个 Flow 由多个 **Turn** 组成，每个 Turn 使用一份独立的配置。
- 每个 Turn 内可以包含多次 **Interaction**，直到任务成功或达到最大交互次数。

### 组件生命周期

- **待测模型**：同一 Flow 内存活。

- **User Simulator**：每个 Turn 初始化一个新的实例，在该 Turn 内复用。
- **LLM Judge**：每次 Interaction 独立初始化，用于评估当前模型响应及 Sandbox 状态。

## 执行流程

### 1. Turn 初始化

每个 Turn 开始时，根据该 Turn 的配置文件对 Workspace / Sandbox 进行相应更新，为当前任务构造初始环境。

### 2. 第一次 Interaction

从当前 Turn 的配置中读取初始问题（request），并发送给待测模型。

待测模型完成响应后，将：

- 模型的 response；
- 当前 Sandbox / Workspace 状态；

一并发送给 **LLM Judge** 进行评估，并获得本次 Interaction 的分数及错误原因等反馈。

### 3. 判断是否结束当前 Turn

- 若得分达到或超过预设阈值，则认为当前 Turn 成功，本次 Interaction 的得分作为该 Turn 的最终得分，结束当前 Turn。
- 若得分低于阈值，且尚未达到最大 Interaction 次数，则进入下一次 Interaction。
- 若达到最大 Interaction 次数仍未通过，则结束当前 Turn，并按照预设规则记录该 Turn 的最终结果。

### 4. 后续 Interaction

对于第二次及之后的 Interaction，调用当前 Turn 的 **User Simulator**。

User Simulator 根据：

- 当前 Turn 已有的完整交互上下文；
- LLM Judge 对上一轮结果给出的错误原因或反馈；

生成新的 request，并发送给待测模型。

随后再次执行：

**模型响应 → Sandbox 状态更新 → LLM Judge 评估 → 判断是否结束**

这一循环，直到得分达到阈值或达到最大 Interaction 次数。