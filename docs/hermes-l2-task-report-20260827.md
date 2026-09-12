# Hermes L2 任务执行报告

## 运行概况

本报告对应最后一次完整的 Hermes L2 自进化评测运行：

- 评测任务：AIRS-Bench SICK textual-entailment Accuracy
- Episode 数量：3（E001、E002、E003）
- 总耗时：约 6 分 55 秒
- 总模型调用：27 次
- 总 token：633,087
- 结果：E001 失败，E002 和 E003 通过
- 三个 Episode 均完成了 turn-boundary self-evolution review

## 统一任务内容

每个 Episode 都要求 Agent 完成一个三分类文本蕴含任务：

1. 读取训练集、验证集和测试集。
2. 数据列为 `sentence_A`、`sentence_B` 和 `label`；标签 0、1、2 分别表示 entailment、neutral、contradiction。
3. 编写 `train_and_predict.py`，训练或构造预测模型。
4. 运行脚本生成 `submission.csv`，文件必须只有一列 `label`，并为每条测试数据生成一个预测。
5. 使用与 scikit-learn 1.9.0 兼容的接口。

评测同时设置了工作上限：最多 10 次模型调用、8 次工具调用、单次终端命令 90 秒、单个 Episode 480 秒。达到上限会立即判定当前 Episode 失败。

## E001

### Agent 做了什么

- 先检查训练集、验证集和测试集的格式与数据规模。
- 编写了第一版 `train_and_predict.py`。
- 尝试运行脚本并进行验证。
- 运行中发现实现问题，随后多次读取、修改和修补脚本。
- 在继续调试时达到工具调用上限，Episode 被评测器中止。

### 结果

- 评测结果：失败
- 得分：0
- 失败原因：`execution_limit`，具体为达到 `max_tool_calls`
- 没有形成可被评测器接受的最终提交结果。

### Self-evolution

任务结束后，Hermes 执行了 evolution review，对本次失败轨迹进行了复盘，重点涉及：尽早产出可运行版本、避免重复修补、复用已经 fit 的文本特征转换器，以及在有限工具调用下优先完成有效提交。

## E002

### Agent 做了什么

- 读取 SICK 数据并确认句子列和标签格式。
- 编写 `train_and_predict.py`。
- 分别对 `sentence_A` 和 `sentence_B` 提取词级/字符级 TF-IDF 特征。
- 增加两句话之间的 token overlap 特征。
- 使用 `LogisticRegression` 训练三分类模型，并避免使用已废弃的 `multi_class` 参数。
- 运行脚本完成预测，生成包含 1200 条预测的 `submission.csv`。

### 结果

- 评测结果：通过
- 得分：0.7633
- 主要生成文件：`train_and_predict.py`、`submission.csv`

### Self-evolution

任务结束后完成了 evolution review。该轮复盘保留了本次有效的机器学习提交经验，并将其作为后续同类任务的可复用经验。 

## E003

### Agent 做了什么

- 编写并运行 `train_and_predict.py`。
- 将 `sentence_A` 和 `sentence_B` 拼接成一个文本字段。
- 使用词级 TF-IDF（1-2 gram）和字符级 TF-IDF（`char_wb`，2-4 gram）提取特征。
- 使用 `LogisticRegression(solver="lbfgs")` 训练模型。
- 运行脚本生成 1200 条测试集预测。
- 额外执行了一次验证检查，验证集准确率约为 0.6586，高于约 0.568 的多数类基线。

### 结果

- 评测结果：通过
- 得分：0.6633
- 主要生成文件：`train_and_predict.py`、`submission.csv`

### Self-evolution

任务结束后完成了 evolution review。该轮复盘确认了这种结构更简单、依赖更少的文本分类方案，可作为工具调用预算紧张时的有效起始方案。

## 总结

这次运行中，E001 的主要问题不是模型完全没有方向，而是在调试过程中消耗了过多工具调用，最终没有在上限内交付。E002 使用更完整的句子对特征取得了最高分；E003 使用更简单的拼接文本方案也成功通过，但得分较低。

## 原始运行记录

- [评测结果 report.json](../runs/l2-hermes-selfevolving-clean-2m-20260827/airs_sick_textual_entailment_accuracy_001/20260827T031834Z-5af4e718/report.json)
- [完整交互与 evolution trace.jsonl](../runs/l2-hermes-selfevolving-clean-2m-20260827/airs_sick_textual_entailment_accuracy_001/20260827T031834Z-5af4e718/trace.jsonl)
- [最终沙盒状态 baseline_state.zip](../runs/l2-hermes-selfevolving-clean-2m-20260827/airs_sick_textual_entailment_accuracy_001/20260827T031834Z-5af4e718/baseline_state.zip)
