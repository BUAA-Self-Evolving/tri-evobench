# 数据包目录

全部数据以可直接读取的目录提交，不需要 Git LFS 或另行解压。
当前矩阵使用 `current/` 下的两个数据包；其他目录用于保留历史版本，不能混算成绩。

| 路径 | 版本与用途 | 规模 |
|---|---|---|
| `current/l1_arena` | 2026-09-03 导入的最新 L1 Arena | 23 streams，423 turns |
| `current/L2_Demo_V2` | 2026-09-03 导入、当前正在评测的 L2 Demo V2 | 6 streams，134 turns |
| `l1_demo` | 原始 L1 七轮 Demo | 1 session，7 turns |
| `l2_demo` | 旧 episode 格式 L2 Demo 的本地评测输入版本 | 6 streams，17 episodes |
| `l1_life_v2` | 已有的 L1 Life V2 数据包 | 以包内 manifest 为准 |
| `archive/L2_Demo_V2_previous` | 此前导入的 L2 Demo V2，保留以便复现旧实验 | 6 streams，134 turns |

版本名称保留原称，不将 Demo 擅自称为正式版。经逐文件 SHA-256 核对，两个导入批次的
L2 Demo V2 在排除缓存后内容完全一致；保留 archive 路径只是标注此前的本地输入来源，
不代表另一个不同的数据集。Git 对相同文件内容共享存储。
本次发布基于本地实际评测输入的逐文件拷贝；只排除 `.DS_Store`、`__MACOSX`、
Python 缓存等非评测文件，未改写任务、gold、rubric 或 verifier。
`l2_demo` 为已用于评测的工作副本，不声称是最初压缩包逐字节重建。

## 完整性与使用

```bash
python tools/dataset_inventory.py --check
python -m trievo_bench.l2_compat --package benchmarks/current/L2_Demo_V2 --inspect
```

执行最新三智能体矩阵请看 [运行指南](../docs/current-matrix.md)，传入
`--packages benchmarks/current`。`SHA256_MANIFEST.json` 记录各版本每个文件的大小和摘要。

保留各数据包自带的来源和许可说明；仓库代码许可不覆盖上游数据、素材的独立使用限制。
包内含私有评测 gold 与 verifier，不能把整个包挂载给被测 agent，必须通过评测器划分可见边界。
L1 Arena stream-19 的密钥泄露响应场景包含合成的 key-shaped 测试字符串，它们是任务素材，
不是本项目调用模型的凭据。没有上传运行 API key、个人配置或付费运行 trace。
