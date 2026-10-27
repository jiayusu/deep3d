# 持久任务与并发运行时

2026-10-27：实现 PostgreSQL 业务状态、Rust HTTP 协调服务和独立 CAD Worker。运行时不依赖模型提供方；一个已提交的 Run 可以在提交进程退出后继续运行，多个 Worker 可以同时计算互不依赖的任务。

## 运行

```powershell
uv sync --frozen
.\scripts\cargo.ps1 build --workspace --locked
uv build --wheel
Copy-Item deploy/runtime/runtime.env.example .env.runtime
# 在 .env.runtime 中设置三个独立的随机凭据，再启动：
docker compose --env-file .env.runtime -f deploy/runtime/compose.yaml up --build -d --scale worker=2
```

Compose 启动数据库、协调服务与两个 Worker 容器，每个 Worker 默认两个并发计算槽。数据库、不可变产物和 Attempt 工作区分别使用持久卷；进程重启不清空已提交任务。服务端口只绑定本机 `127.0.0.1:8088`。Worker 共享服务的网络命名空间，通过 loopback 调用 API；服务对象卷在 Worker 中只读，计算工作区单独可写。远程入口需要配置 HTTPS。

CLI 的凭据来自本机 `DEEP3D_OPERATOR_TOKEN`。PowerShell 可从本地环境文件加载，过程不打印值：

```powershell
Get-Content .env.runtime | ForEach-Object {
  if ($_ -match '^([A-Z_]+)=(.+)$') {
    [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
  }
}
.venv\Scripts\deep3d-run.exe submit --project artifacts/flange.deep3d --plan fixtures/runtime/parallel-flanges.json --out artifacts/run-request.json
```

`--project` 指已有的本地 `.deep3d` 工程。客户端先上传、检查摘要，再保存含稳定 Run UUID 的提交请求，然后异步提交；不会等待 CAD 计算结束。`--out` 必须是新的文件。提交回执丢失时重发同一请求：

```powershell
.venv\Scripts\deep3d-run.exe submit --request artifacts/run-request.json
.venv\Scripts\deep3d-run.exe status RUN_UUID
.venv\Scripts\deep3d-run.exe events RUN_UUID --after 0
.venv\Scripts\deep3d-run.exe download RUN_UUID --task flange-thick-and-wide --kind step --out artifacts/final.step
```

`download --kind native` 下载可继续编辑的原生候选包；STEP 和原生包下载都校验服务返回的摘要及版本。输出文件拒绝覆盖。

暂停、恢复和取消显式绑定控制代数与操作 UUID；网络重试沿用同一操作 UUID 和相同请求：

```powershell
.venv\Scripts\deep3d-run.exe control RUN_UUID --action pause --expected-epoch 0 --operation-id OPERATION_UUID
.venv\Scripts\deep3d-run.exe control RUN_UUID --action resume --expected-epoch 1 --operation-id NEW_OPERATION_UUID
```

控制前通过 `status` 读取当前 `control_epoch`。这里的暂停采用立即撤权：不再领取任务，活动 Attempt 失效；恢复后创建新的 Attempt。已完成的输出保留。取消是终态，不能恢复原 Run。暂停已经消耗的尝试次数不退回，耗尽后会明确失败。

## 工程边界

| 模块 | 责任 |
| --- | --- |
| `core-runtime` | 不可变任务图、事务领取、执行租约、去重、控制、恢复、事件游标 |
| `runtime-service` | 角色鉴权、冻结上传输入、HTTP API、候选复核、不可变对象登记、周期恢复 |
| `deep3d-worker` | 多槽常驻执行、独立续租、真实 CAD 子进程、进程树回收、完成回执重送 |
| `deep3d-run` | 异步提交、观察、控制、交付下载；不承担后台运行权威 |

PostgreSQL 保存 Run、Task、Attempt、领取/完成/控制去重记录及事件。服务可以重启或增加副本；每个副本运行同一恢复扫描。数据库事务和 Run 行锁串行化一次业务决定，几何计算在事务外并行执行。

Artifact 先完整刷盘进入内容寻址存储，再提交数据库引用。数据库提交失败时可能留下未引用对象，不产生合法的完成结果；首版关闭自动物理删除。对象存储或数据库永久丢失仍需要部署方备份恢复，本实现不宣称跨区域零丢失。

## 任务与输入

固定 DAG 最多 256 个 Task。每个任务包括 `key`、`depends_on`、可选 `source_task`、`changes_mm`、尝试上限和每次计算的绝对超时。

- `depends_on` 表示先后条件，依赖全部成功后才可领取。
- `source_task` 必须在依赖中，表示使用该任务已登记的不可变原生输出作为输入；为空则使用 Run 最初冻结的工程。
- 多个依赖不会自动融合成一个几何体。当前每个 Task 只有一个几何源，支持五个法兰长度参数修改。
- 原工程上传后固定完整字节、版本和摘要。排队期间的人工编辑不会改变已提交 Run 的输入。

示例计划先并行产生两个参数变体，再等待它们完成，基于厚度变体继续修改孔距。所有 Task 保留各自输入、Attempt 和输出；不会把两个同 Base 的 B-Rep 自动合并。

## 并发和恢复约束

领取采用数据库短事务：选择可执行 Run 并 `FOR UPDATE SKIP LOCKED`，按最近调度时间轮转，检查依赖与每 Run 最多 8 个有效执行槽，再分配新的 Attempt 与执行代数。相同领取 UUID 重发返回原决定；空领取也有稳定回执，下一轮轮询使用新 UUID。

所有续租、失败和完成均校验 Run/Task/Attempt/Worker、执行代数、控制代数和锁后数据库实际时间。过期租约不能续活。默认租约 30 秒，Worker 约每 10 秒续租；恢复扫描每 2 秒检查一次。失效 Attempt 保留历史，按有界退避建立下一次尝试；单任务永久失败或耗尽尝试使整个必需任务图失败并撤销其他未完成任务。

完成时协调服务重新打开候选包，校验冻结输入历史、精确参数变更、事务身份、物化清单及 BREP/STEP 验证绑定，把确切包写入不可变对象区，最后重新检查执行权后提交。Worker 自报成功不直接改变 Task。已提交的同一完成请求在租约过期、Run 完成或服务重启后仍可返回原结果。

Worker 为每次 Attempt 建立独立工作区。它把只读对象复制为摘要一致的工作输入，再调用 Rust 内核；不会为输入对象创建写锁或修改对象区。续租与几何计算使用不同线程。撤权、截止或关闭时尽力结束整棵计算进程树；数据库先撤权，因此进程退出延迟不恢复旧写入权。Windows 子进程加入 Job Object 前存在很短的创建窗口，不据此宣称物理资源回收无遗漏。

完成请求先保存到 Worker 本地持久 Outbox；回执不确定时重发同一请求，重启后继续补送。计算可能重复，业务接纳按身份去重。暂时的网络/数据库故障不会被当作计算已经失败或已经成功。

状态快照与事件游标来自同一数据库快照；事件用于查询变化，状态仍由 API 给出。心跳不逐次写业务事件。当前提供游标分页查询，尚未实现 SSE 或外部通知分发。

## 当前交付范围

这是固定 CAD DAG 的长程执行与并发调度内核。长期 Run 由数据库保存，关闭客户端不影响执行；服务/Worker 重启后可继续领取、续租或恢复。

任务完成表示私有候选及交付包已通过当前验收并持久登记。它不会推进用户原工程 Head，也没有把 PostgreSQL 事务与 `.deep3d` 文件替换假装成同一原子提交。正式分支发布、多租户权限、动态重规划、资源/费用账本及 Temporal 编排属于后续范围。

实现进行过真实 PostgreSQL 的并发领取、过期回写、控制撤权和幂等检查，也跑通了双 Worker、上游产物接续与 STEP 下载。它们用于工程回归，不能代替负载、耐久性或工业资格声明。

本次以本机 PostgreSQL 容器、Windows Rust 服务和独立 Worker 进程完成端到端运行；Compose 配置已检查，完整容器部署仍需在目标环境构建和验收。
