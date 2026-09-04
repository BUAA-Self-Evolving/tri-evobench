# L1-Arena 双语 Domain Tag 规范

每个 `turnK/task.json` 顶层必须包含：

```json
{
  "tag": {
    "taxonomy_version": "l1_arena_domain_tags/1.1",
    "domain": "security",
    "domain_zh": "安全",
    "subtag": "requests_cve_remediation.root_cause_analysis",
    "subtag_zh": "Requests CVE 修复·根因分析",
    "is_noise": false
  }
}
```

英文字段使用稳定 snake_case，`subtag` 格式为 `{scenario_topic}.{turn_intent}`。中文字段是对应的人类可读标签，不是机器 ID。

## Domain

| domain | domain_zh | 场景 |
|---|---|---|
| `security` | 安全 | eng1、sec1、sec2 |
| `engineering` | 工程 | eng2、eng3、eng4、eng5 |
| `product_compliance` | 产品合规 | prd1a、prd1b、prd2 |
| `business_operations` | 业务运营 | prd3、prd4、prd5 |
| `research_integrity` | 科研诚信 | sci1a、sci1b |
| `healthcare` | 医疗 | sci2、sci3 |
| `legal_and_hr` | 法律与人力 | sci4、sci5 |
| `financial_risk` | 金融风控 | sec3a、sec3b、sec5 |
| `privacy` | 隐私 | sec4 |
| `unrelated_noise` | 无关噪音 | 所有 derived distractor |

## Scenario topic

Source turn 的 subtag 前半部分固定表达场景主题，例如：

- `requests_cve_remediation` / Requests CVE 修复
- `postgresql_query_optimization` / PostgreSQL 查询优化
- `medical_device_incident` / 医疗器械事故
- `gdpr_compliance` / GDPR 合规
- `payment_fraud_investigation` / 支付反欺诈调查

## Turn intent

后半部分根据当前 turn 的具体请求标记，包含：

- `final_signoff` / 最终签核
- `test_execution` / 测试执行
- `test_creation` / 测试编写
- `code_remediation` / 代码与配置整改
- `root_cause_analysis` / 根因分析
- `source_verification` / 来源核验
- `state_update` / 状态更新与更正
- `workspace_initialization` / 工作区初始化
- `timeline_reconstruction` / 时间线重建
- `data_pipeline` / 数据管线处理
- `data_quality_analysis` / 数据质量分析
- `statistical_analysis` / 统计分析
- `inventory_audit` / 盘点审计
- `metadata_extraction` / 元数据提取
- `metric_calculation` / 指标计算
- `compliance_assessment` / 合规评估
- `risk_assessment` / 风险评估
- `impact_assessment` / 影响评估
- `policy_analysis` / 政策分析
- `evidence_synthesis` / 证据综合
- `remediation_planning` / 整改规划
- `report_generation` / 报告与文档生成
- `comparison` / 比较分析
- `classification` / 分类判定

## Noise

噪音不继承所在 stream 的业务 domain：

- `unrelated_noise.exact_arithmetic` / 无关噪音·精确算术
- `unrelated_noise.one_turn_format_override` / 无关噪音·单轮格式覆盖

Noise 必须设置 `is_noise=true`；所有 source turn 必须设置 `is_noise=false`。

## 汇总

根 `manifest.json` 的 `tag_taxonomy` 保存 taxonomy version、domain 列表、domain turn counts、subtag turn counts，以及冻结 gold 的路径和 SHA-256；每个 `stream.json` 保存该 stream 的 bilingual source domain 与实际 domain/subtag counts。

Source intent 不再由发布时正则自证，而是读取 `taxonomy/tag_intent_gold.json` 中人工审校并冻结的 377-entry `source_task_id → intent` 映射。Validator 检查全部 source task ID 与 gold 一一对应、文件 hash 固定、task/stream/manifest 标签一致。新增或修改源 round 时必须更新并审校 gold，不能自动回退到通用标签。
