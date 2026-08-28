# L1-Life LLM-as-Judge 评测规范

L1-Life 的语义评分统一采用 LLM-as-judge。Judge 负责对明确的单项检查做证据判断，最终权重计算由评测实现方确定性完成，不直接相信 judge 自报的总分。

每个 turn 都在 `evaluation.judge_config.judge_prompt` 中内嵌完整英文 prompt，不依赖外部 prompt 文件。Prompt 显式读取 `evaluation.rubrics.source_rubrics`、`evaluation.rubrics.behavioral_rubrics` 和 `evaluation.gt`。

## 1. Reference judge 配置

Reference track 使用冻结的 judge prompt、temperature 0 和精确的 judge model/version。当前规范中的 reference model identifier 为 `gpt-5.1`。使用其他 judge 时必须作为独立 track 报告，不能与 reference track 混合排名。

评测报告必须记录：

- provider；
- 精确 model/version；
- context limit；
- max output tokens；
- temperature；
- timeout；
- 重试次数；
- JSON 解析失败次数；
- context overflow 次数。

## 2. Judge 可见证据

Judge 可以获得：

- turn 的 sealed `evaluation.checks`；
- Agent 最终回复；
- 完整用户–Agent 交互轨迹；
- tool calls 与工具返回；
- state before / after；
- state diff；
- expected state；
- expected tool call；
- expected workspace effect；
- 需要时的 sealed historical messages；
- 内嵌 source rubrics。

Judge 必须区分“Agent 声称已经执行”和“确实执行”。如果回复说已经保存记忆、创建文件或调用工具，但 tool calls、workspace/tool state 或 state diff 中没有相符证据，相应执行项必须记 0。

GT 定义需要验证的目标，不是 Agent 已执行的证据。Fact QA 和 correction 有 canonical response；开放式任务不编造唯一参考文案，而由 source/behavioral rubrics、state GT、tool GT 和 workspace GT 共同定义正确性。

## 3. 单项分数

每个 behavioral check 只能使用以下分数：

| 分数 | 含义 |
|---:|---|
| `1` | 完全满足，且证据充分 |
| `0.5` | 有实质性部分完成，但仍缺少关键要求 |
| `0` | 未完成、明确违反或与证据矛盾 |
| `null` | 逻辑上确实不适用；必须说明原因 |

不得因为某项很难、Agent 没做或证据不足就标记 `null`。这些情况应记 0。`null` 只能用于真正不适用或源 rubrics 明确互斥的情况。

## 4. Source rubrics

对于 `scoring=mean_source_rubrics` 的 check，judge 必须逐条判断 `evaluation.rubrics.source_rubrics`：

```json
{
  "rubric_index": 1,
  "score": 1,
  "reason": "回答保留了最终修正后的日期，并给出了相应证据。"
}
```

如果两个 rubrics 明确要求互斥行为，仅将这两个冲突项标记为 `null`，不能丢弃整个 turn，也不能把普通未满足项标为 `NA`。

Source answer check 的得分为：

```text
source_answer_score = mean(所有非 NA 的 source rubric scores)
```

## 5. Turn 总分

每个 turn 的 `evaluation.checks` 权重之和为 1。评测实现方根据非 NA checks 重新归一化：

```text
turn_score = Σ(weight_i × score_i) / Σ(applicable_weight_i)
```

Judge 不应生成或决定最终总分。如果 judge 输出的单项分数与其自报总分不一致，以重新计算的结果为准。

## 6. 不同 turn 的评分重点

### `fact_disclosure`

- 原始任务回答质量；
- scoped fact 是否正确写入；
- interactive arm 是否先澄清再使用隐藏事实；
- 是否错误持久化无关上下文。

### `fact_correction`

- 新值是否成为唯一 active value；
- 旧值是否保留为 superseded history；
- 是否建立 `supersedes` 关系；
- 回复是否仍把旧值当成 current。

### `preference_application`

- 原始任务是否正确；
- 是否激活最新且兼容的持久偏好；
- 当前 turn 的明确要求是否正确覆盖默认偏好；
- 是否错误使用 stale preference。

### `fact_qa`

- 是否回答最新 active value；
- 是否确实从持久状态激活，而不是生成一个新事实；
- 是否避免旧值和竞争值；
- 是否在没有历史上下文时仍正确回答。

### `fact_dependent_tool`

- 原始任务回答质量；
- 工具名称、调用次数和参数；
- 参数是否来自 active stored fact；
- 文件或日历终态是否符合预期；
- 是否保留既有内容和无关文件；
- 是否发生未授权发送或额外副作用。

### `distractor`

- 原始任务是否完成；
- 一次性格式/语气要求是否应用；
- state diff 是否保持长期记忆不变；
- 是否发生 intrusion。

### `long_delay_replay`

- 是否恢复早期 source task 的关键答案；
- 是否使用经过修正的最新事实；
- 是否出现 stale value；
- replay 是否造成重复事实或其他 state corruption。

## 7. Feedback 边界

完成一个 turn 后，learner 最多只能获得：

- 未通过的 dimension 名称；
- 一句描述可观察不足的局部反馈。

不得返回：

- expected value；
- source rubrics；
- check weights；
- gold fact；
- gold tool arguments；
- state patch；
- judge 的完整内部推理。

Hidden judge 结果只用于报告，不能写入 learner 的 persistent state。

## 8. Context-window 规则

Reference track 必须提供完整上下文，不允许截断、摘要、重新排序或选择性删除 messages。无法容纳完整 turn 的系统或 judge 必须记录 `context_overflow`；正式 reference track 中该 turn 记 0 且保留在分母中。

允许单独报告 truncated-context ablation，但不得与 full-context leaderboard 结果混合。

## 9. 聚合指标

正式报告至少包含：

- 600 turns overall mean；
- 10-stream macro mean；
- 每条 stream 的单独得分；
- 每类 turn 的均分；
- 每 10 turns 的 learning curve；
- long-delay replay retention；
- stale error rate；
- intrusion error rate；
- judge failure rate；
- context overflow count；
- token、tool call、wall-clock 和 state size 成本。

Self-evolution 主增益为：

```text
Δ_evo = mean(Evolving) - mean(Reset)
```

只报告 pooled gain 不足以支持自进化结论，还应报告每流增益、最坏流、bootstrap confidence interval、replay regression 和副作用。

## 10. Judge 质量审计

- 固定分层抽取至少 10% turns 做双重 judge；
- 覆盖所有 stream 和 turn type；
- 人工审核至少 10% turns 以及全部 rubric-conflict `NA`；
- 报告 ordinal exact agreement、turn-score MAE、NA rate 和 parse failure rate；
- 建议数据冻结门槛为 ordinal agreement ≥90%，或 turn-score MAE ≤0.05。

## 11. Judge 输出字段规范

建议每个 turn 的 judge 输出使用以下结构：

```json
{
  "stream_id": "l1_life_stream_1",
  "turn_id": "turn5",
  "check_scores": [
    {
      "check_id": "tool_execution",
      "score": 1,
      "reason": "工具名、调用次数和路径参数均与期望一致。"
    }
  ],
  "source_rubric_scores": [
    {
      "rubric_index": 1,
      "score": 0.5,
      "reason": "覆盖了主要事实，但缺少一项要求的直接证据。"
    }
  ],
  "rationale": "任务主体完成，但 source answer 存在部分遗漏。"
}
```

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `stream_id` | string | 是 | 必须与 task 一致 |
| `turn_id` | string | 是 | 必须与 task 一致 |
| `check_scores` | object[] | 是 | 必须覆盖 task 中的所有 ordinal checks |
| `source_rubric_scores` | object[] | 是 | 无 source rubrics 时为空数组；否则逐条覆盖 |
| `rationale` | string | 是 | 简短总体诊断，不替代单项 reason |

`check_scores[]`：

| 字段 | 类型 | 规则 |
|---|---|---|
| `check_id` | string | 必须与 `evaluation.checks[].check_id` 完全一致 |
| `score` | `1/0.5/0/null` | 不允许其他连续值 |
| `reason` | string | 必须引用可观察证据，不得只写“符合/不符合” |

`source_rubric_scores[]`：

| 字段 | 类型 | 规则 |
|---|---|---|
| `rubric_index` | integer | 1-based，必须完整覆盖 rubrics |
| `score` | `1/0.5/0/null` | 与 behavioral check 同一标尺 |
| `reason` | string | 说明回答如何满足、遗漏或冲突 |

Judge 输出不得包含 learner 可直接写入的 gold state patch、完整改写答案或未来 turn 信息。评测方应校验 ID 完整性、重复项、非法分数和缺失 rubric 后再计算总分。

## 12. 按维度报告

除 turn 总分外，应从 checks 聚合以下维度：

| Dimension | 含义 |
|---|---|
| `task_success` | 当前任务、工具和产物是否完成 |
| `update_precision` | 是否写入正确且必要的持久更新 |
| `revision_quality` | 是否正确 supersede 旧版本 |
| `activation` | 是否在需要时使用 active fact |
| `stale_control` | 是否避免使用 superseded value |
| `intrusion_control` | 是否避免噪音、临时指令和无关事实污染 |
| `interaction_quality` | 是否正确澄清并利用 simulator 回复 |
| `replay_retention` | 长延迟内容和事实是否保留 |
| `response_quality` | 格式、直接性和表达是否符合要求 |

同一个 check 只能指定一个主 dimension。需要多维诊断时，应拆成多个互不重复计分的 checks，避免同一错误被多次扣分。
