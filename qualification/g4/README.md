# G4 失效传播性能测量

本目录保存 G4 协议（[注册草案](../../acceptance/v0.1/registry.draft.json) 中的 `G4-HYBRID-1000`）的实际测量记录。**注册仍是草案，oracle 由同一会话作者另行编写，不是独立审阅者准备；冻结清单尚未建立。因此这里的结果只能作为“证据不足”的观测，不是正式 G4 通过。**

## 被测对象

生产入口 `core_domain::invalidation_plan` 在每次编辑时重建反向依赖索引，再调用 `DependencyIndex::invalidate` 求出确切失效集合与原因边（每个失效 Feature 的全部已失效直接输入）。`dirty_features` 也走同一条路径，没有测试专用算法。

计时主序列包含每次请求的索引重建（`from_edges` + `invalidate`），次序列只计已建索引上的遍历；冷启动建索引单独报告。不含 CAD 求值、文件加载、传输和报告序列化。

## 夹具与 oracle

[scripts/g4_benchmark.py](../../scripts/g4_benchmark.py) 按注册文本生成 1,000 节点混合图（链、共享上游、扇出与汇合）和 1,000 个固定顺序查询。oracle 用另一种算法（按节点序前向动态规划，Python 实现）得到每个查询的期望集合和原因边，不调用被测 Rust 代码。完整 oracle 约 24 MB，不入库；仓库只保存 [摘要清单](../../acceptance/v0.1/g4-fixture.json)，运行时重新生成并校验摘要一致才继续。

## 复查

```bash
.venv/bin/python scripts/g4_benchmark.py run --out qualification/g4/NEW-DIRECTORY
```

输出 `machine.json`（机器清单）、`measurement.json`（全部原始时长、逐项不一致）和 `observation.json`（观测片段）。

## 2026-10-28 Linux 云容器记录

[2026-10-28-linux-cloud](2026-10-28-linux-cloud/)：1,000 个测量请求全部与 oracle 逐项一致（失效集合和原因边均无多报/漏报）；含索引重建的 nearest-rank P95 约 0.49 ms，已建索引遍历 P95 约 0.20 ms，低于 100 ms 门槛。机器为共享云容器，不是冻结的约定测试机。
