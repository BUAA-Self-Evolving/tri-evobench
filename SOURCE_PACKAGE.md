# 源码包使用说明

## 手动上传

1. 解压 ZIP，进入 `tri-evobench` 文件夹。
2. 将文件夹内的源码和 README 上传到目标仓库根目录；保留目录结构。
3. 需要隐藏配置文件时，在访达按 Command+Shift+. 显示点号开头的文件。
4. 将 ZIP 作为单个文件上传只会保存压缩包；展示源码时应上传解压后的内容。

## 外部依赖

完整上游仓库和评测数据未收入本包；使用相应 baseline 时需另行准备。
下表版本来自本地 manifest 声明，不代表本次在线核验或上游最新版本。
本次没有连接 GitHub，也没有创建提交、分支或上传。

| 目标目录 | 上游仓库 | manifest 声明版本 |
|---|---|---|
| `baseline/hermes-agent` | https://github.com/NousResearch/hermes-agent.git | `未声明，需另核验` |
| `baseline/prime-agent` | https://github.com/PrimeIntellect-ai/prime-agent.git | `163dd5798cd40e86b540aa9ecb68201920a83a1b` |
| `baseline/adaptive-auto-harness` | https://github.com/A-EVO-Lab/a-evolve.git | `17bc9ebb7d4d142af1b109b43ef160031967cc9a` |
| `baseline/opencode` | https://github.com/anomalyco/opencode.git | `df23b7f9488a38e6f8064a0739d4f8cde86d7cfb` |
| `baseline/codex` | https://github.com/openai/codex.git | `cc7ba33601286a751477832b01de7b6f8de0dd9c` |
| `baseline/deepseek-harness` | https://github.com/deepseek-ai/deepseek-harness.git | `d743267388641bc76f17c45ce8b4c231aed1d32c` |
| `baseline/ZCode` | https://github.com/zai-org/ZCode.git | `aac4755666d09fdcd70272fcf063c077a639015f` |

另外 `baseline/SkillOpt` 和 `baseline/Trace2Skill` 的完整上游源码也未打包；
其运行参数及目录要求见对应 `baseline/manifests/*.json` 和接入文档。

手动上传后 `.gitmodules` 本身不能创建 gitlink。需要上游源码时，可在有网络的环境
将上游仓库克隆到上表目录，并检出要求的版本；原生镜像构建与 strict source 校验
还需要正确的上游 Git 元数据，不能通过关闭检查来冒充通过。

评测数据目录可放在仓库外，再按运行指南显式传入。通用文档保留了历史数据路径和
实验恢复示例，它们不表示相应数据、历史运行或一次性恢复授权随包提供。

## 验证边界

本轮只验证打包清单、源码字节一致性、Python 语法、JSON 格式、ZIP 完整性和凭证扫描；
没有运行生产全量测试、构建 Docker、调用模型或重跑实验。
完整测试依赖额外数据、第三方源码及 Git 元数据，包内 CI 原样保留，未承诺上传后全绿。
主项目未见顶层 LICENSE，本次未新增或替项目指定许可；第三方代码及数据仍遵从各自许可。

排除了本机专用 `tools/start_dsh_friday.zsh` 和历史迁移脚本 `tools/relocate_graph_run.py`，
以及本机交接、历史实验报告和含本机绝对路径的审计盘点。原始开发目录未修改。
