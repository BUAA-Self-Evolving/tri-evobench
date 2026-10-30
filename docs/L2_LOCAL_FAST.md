# 本机 L2 快速运行档

本档用于新输出根。2026-10-27 已在本机完成代码、标准 ZIP 恢复、资源调度、Docker 数值负载、原生 Prime 假模型验证；没有重跑已完成的正式矩阵，也没有实测整批模型工期。

## 已部署配置

- 宿主：8 个性能核、4 个能效核，16 GiB 内存。
- Colima：10 CPU / 12 GiB。保留约 2 个逻辑核和 4 GiB 内存给宿主；原 26 个空闲载体容器和 65 个卷均保留，镜像和挂载身份核验一致。
- 当前配置：`tmp/l2-local-fast-current`，指向 `tmp/l2-local-fast-20261027-ready`。
- 默认五 baseline：Hermes、Prime、Adaptive、OpenCode、DSH；V5 r2 intra10、三个条件、两个 panel，由一个 driver 共用本机容量。分 panel 评分合同保留。
- 全局最多 4 worker，CPU 重任务最多 2，τ² 最多 2。按任务类型预留 CPU / 内存，同时检查 VM 实际 CPU、可用内存、宿主内存压力、Docker 数据盘剩余空间。
- 每个 Task / verifier 保留 4 CPU 和原工作预算；BLAS / OpenMP 线程上限按 CPU 配额设置。SRA 和 τ² 的 admission 权重是本机调度预留，不改变其容器 CPU 上限。
- 先让 τ² 和重任务交错运行，再填充 SRA；保留同 stream / baseline 的条件顺序及账号串行通道。
- 宿主存储门禁 20 / 25 GiB；Docker 数据盘至少保留 5 GiB。资源压力只暂停新任务进入，现有付费交互排空。
- 六槽共享请求网关继续每槽 10 RPM / 2 在途，使用原共享 SQLite。没有扩大额度、删除预算或添加整请求重放。

## 运行入口

默认快速入口会优先使用 `tmp/l2-local-fast-current`。仅做计划预演：

```bash
python tools/run_friday_fast_matrix.py \
  --app-id-file ../new_appids.txt \
  --root runs/l2-next-new-batch \
  --dry-run
```

实际新批次使用相同命令去掉 `--dry-run`。本次优化没有执行该实际命令。使用具有 pytest 等项目依赖的 Python；本机验证使用 `/opt/anaconda3/bin/python`，系统 Python 3.9 不满足项目要求。

不得把新档应用到历史暂停根。已有付费交互、副作用、checkpoint 和一次性恢复授权仍遵守 `TIMEOUT_RECOVERY_WORKFLOW.md`。本机快速 launcher 使用共享 admission 锁；两个 panel 应使用同一个 `--l2-panel all` driver。

## 检查点与计时

新档显式启用 `TRIEVO_FAST_CHECKPOINTS=1`，私有存储位于新 root 的 `_snapshot_store/`：

1. 文件摘要缓存同时检查设备、inode、大小、mtime、ctime、模式，不能只凭 mtime 复用。
2. 相同快照硬链接复用；变化快照使用压缩级别 1，并在 APFS 上克隆前一 ZIP 的共同块。
3. 新 ZIP 原子发布，生成期间工作区变化会拒绝提交；旧检查点不原地修改。
4. 每份 `workspace.zip` 仍是完整标准 ZIP，删除私有缓存的名字不影响已有硬链接和独立恢复。没有外部 blob 依赖，也没有删除恢复阶段。

`performance.jsonl` 记录 Agent、native verifier、Judge、学习和卷同步阶段。`_snapshot_store/timings.jsonl` 记录快照耗时与复用。共享网关在预算 SQLite 旁记录 `.timings.jsonl`，区分排队、上游 headers、首个 body byte 和总耗时。首个 body byte 不是模型首 token；日志不含 prompt、reply、工具正文或凭证。

## 原生环境与框架准备

Prime 使用独立镜像扩展最终修复后的 adapter。公开科学库按依赖闭包从既有 ARM64 Task 镜像离线复制，核验 Python / CPU ABI，并清理旧 distribution 元数据；默认 `python` 和 kernel 都使用 `/opt/prime-kernel`。没有复制任务、私有 verifier 或答案资产，也没有改原生算法源码。

所有 baseline 在新档统一开启已有的 `l2_verifier_input_copy`，为 verifier 提供内容校验一致的独立只读输入，避免权限错误引发无效重复交互。保留现有 Prime 每 Interaction 计数修复、Adaptive 有界请求和 Hermes 最终 HTTP 学习预算修复。旧报告和已验收分数不改写。

## 实测与限制

持续数值负载中，原 8 核 VM 双路吞吐约为单路 1.89 倍；三、四路反而较低。12 核满配也没有提升该负载的吞吐。10 核档允许两路重任务与两路 τ² 同时满足预留，给宿主留出 CPU；重任务上限仍选两路。

本机 APFS 的九快照合成测试（含每份 ZIP 的 CRC 和恢复内容校验）约 4.68 → 1.50 秒，约 3.13 倍；九份输出只需三个独立 ZIP inode，变化 ZIP 的共同块可克隆。这不是完整数据集的压缩率或物理空间测量。

按历史选中单元耗时、账号槽和资源约束的反事实排程，原资源档约 50.2 小时，新档约 39.9 小时；给新档所有重任务额外加 6% 耗时仍约 42.2 小时。它排除了故障、停等，假设模型延迟不变，不能当真实矩阵提速或承诺工期。新负载或宿主应用变化后应重新校准。

证据：`runs/l2-local-fast-optimization-20261027/`。其中保存了吞吐探测、原生假模型验证、完整测试日志、配置来源、虚拟机参数调整及保留容器/卷的核验。初次 VM 重启发现本机 Lima 缺虚拟化 entitlement，已备份工具并修复最小签名；VM 和原空闲容器均已恢复，后续恢复不得继续依赖旧进程出生时间。
