# G5 恢复场景执行记录

本目录保存 [注册草案](../../acceptance/v0.1/registry.draft.json) 中 R01～R05 的实际执行结果。**注册仍是草案，场景驱动与观测由同一会话作者编写，没有独立审阅和冻结清单；R05 需要两个真实模型身份，本环境没有模型服务凭据。因此 G5 只能报告“证据不足”，不是正式通过。**

## 怎样执行

[scripts/g5_stack.py](../../scripts/g5_stack.py) 为每个场景新建 PostgreSQL 数据库，启动 Rust 协调服务（`deep3d-runtime serve`，租约 4 秒）和真实 CAD Worker 进程（与 Compose 部署一样共享对象区和 Attempt 工作区）。[scripts/g5_recovery.py](../../scripts/g5_recovery.py) 先让 C01 种子产生第一个经协调服务重新验证的公开提交，再注入故障：

- 对 Worker 进程组发送真实 `SIGKILL` / `SIGSTOP` / `SIGCONT`，记录进程号；
- 杀掉并重启协调服务进程；
- 经 HTTP 代理扣住、丢弃或延迟 Worker 的完成回报。扣住时候选字节已经存在、可信验收尚未开始，正是注册的注入点。

观测只读取公开接口与数据库：公开 Head、代数、公开原生/STEP 摘要、Attempt 与执行代数、`verified_commits` 唯一性，以及下载公开原生工程后无 Agent 重建。

## 2026-10-28 Linux 云容器记录（源提交 1a4e049）

[2026-10-28-linux-cloud/g5-run.json](2026-10-28-linux-cloud/g5-run.json)：

| 场景 | 结果 | 实际观察 |
| --- | --- | --- |
| R01 验证前 Worker 崩溃 | 通过 | 候选已生成时 `SIGKILL` 整个 Worker 进程组；公开 Head 与资产摘要不变；旧 Attempt 因租约过期记为 abandoned；新 Worker 以执行代数 2 完成，唯一公开提交属于新 Attempt；公开源无 Agent 重建通过 |
| R02 提交成功但回执丢失 | 通过 | 第一次完成回报提交后被代理断开；独立读取已见公开代数 2；同一请求摘要在租约过期后重送，返回原结果；公开代数只前进一次，只有一次几何求值 |
| R03 重复/乱序与旧 Attempt 回写 | 通过 | 旧 Attempt 被冻结、其回报被扣住；新 Attempt 发布后放行旧回报，得到 `409 STALE_EXECUTION_LEASE`；重复送达同一完成为只读回执；内容不同的完成为 `IDEMPOTENCY_CONFLICT`；同一领取 UUID 两次返回同一决定 |
| R04 执行中需求修改与并发变更 | 通过 | 旧目标候选的回报在途时目标改为新版本，旧 Run 被取消（`GOAL_CHANGED`），迟到回报被拒绝；同一公开基线上的两个新目标 Run 一个发布，另一个 `PUBLIC_HEAD_CONFLICT`；公开证据的验收合同就是新目标 |
| R05 模型切换、服务重启与超时 | 证据不足 | 已执行：回报在途时杀掉协调服务，超过租约后重启；旧 Attempt abandoned，新 Attempt 发布一次；按同一冻结目标检查，无 Agent 重建通过。未执行：真实模型切换 |

复查（需要可建库的 PostgreSQL 角色）：

```bash
cargo build --workspace --locked
.venv/bin/python scripts/g5_recovery.py --out NEW-DIRECTORY --admin-url postgres://USER:PASSWORD@localhost/postgres
```

`tests/test_g5_recovery.py` 在设置 `DEEP3D_TEST_ADMIN_DATABASE_URL` 后运行 R02 和 R03 作为回归。
