# deep3d

以 [目标状态契约 v1.0](docs/target-contract-v1.md) 为验收依据；[整套系统设计](docs/system-design-v1.md)定义目标架构，[当前状态](docs/current-state.md)列出真实实现与缺口。**V0.1 尚未达标，未启动或证明两小时/72 小时自主验收任务。**

面向工业精密 CAD 的开源 Agent 建模系统，已实现 Rust 原生工程内核、确定性 CAD 执行与持久并发任务运行时。

当前主线是 [持久任务与并发运行时](docs/durable-runtime.md)：PostgreSQL 保存任务与租约，Rust 服务协调多个 CAD Worker，支持异步提交、依赖执行、暂停/恢复/取消、故障接续和交付下载。模型调度与此运行内核解耦。

[系统数学模型 v0.2](docs/mathematical-model.md)定义工程、求值、验收、任务、执行权和恢复之间的关系，并标明当前实现与后续扩展的边界。

用户已明确的方向：**工业级目标，B-Rep + OCCT，STEP 交付，支持云端、异步并发和长程 Agent。**

当前可执行：参数化法兰 → OCCT B-Rep → 独立进程测量 → AP214/mm STEP → 独立进程重新导入测量。记录非破坏布尔的诊断和操作内拓扑历史，并保留参数、精度配置、报告与内容哈希。

0.3.0 新增 [参数化机械 CAD 操作集](docs/cad-capabilities.md)：约束草图、矩形/多边形轮廓、旋转、布尔、圆角/倒角、孔和阵列、刚体放置、命名 STEP 装配与 BOM。工程源可保存、重开并修改；这些能力回归不作为预注册自然语言验收成绩。

参数、Feature DAG、组件结构、版本历史及 BREP/STEP/证据保存为自包含 `.deep3d` 工程，重开后继续编辑并重算，保留旧版本。已接入首个受控 Agent 工具循环：读工程 → 提出参数修改 → 独立候选求值/验证 → 版本检查 → 接纳并交付 STEP。详见 [Agent 使用与边界](docs/agent-loop.md)。

当前工程操作集有明确范围；正式发布协调器、动态计划和 Temporal 编排尚待实现，尚未通过工业资格或客户 CAD 互操作验收。早期本地 Agent 原型保留为独立入口，模型服务不阻塞当前运行时建设。

## Rust 原生工程

```powershell
uv sync --frozen
.\scripts\cargo.ps1 build --workspace --locked
.venv\Scripts\python.exe scripts/demo_project.py --out artifacts/project-demo-001
```

演示每一步都使用新进程，保存工程、修改厚度与孔距、重开并重算、读取历史，最后从搬移后的工程包导出两版 STEP。详细命令和格式见 [Rust 工程内核](docs/rust-engineering-kernel.md)。Linux 使用 `cargo build --workspace --locked`。

## 运行

需要 Python 3.12 和 uv；依赖已在 `uv.lock` 锁定。首次运行会下载对应平台的 OCP wheel。

```powershell
uv sync --frozen
uv run --frozen deep3d-cad build --spec fixtures/flange/standard.json --out artifacts/flange-001
uv run --frozen deep3d-cad qualify --out artifacts/qualification-001
uv run --frozen pytest -q
```

输出目录必须是新的，已有产物不会被覆盖。`build` 默认以输入参数为目标；使用 `--goal another-goal.json` 可以独立指定验收条件。`qualify` 包含两项应通过与四项应拒绝的真实几何用例。开发者可编辑参数 JSON 后再次构建，目前尚不是通用 CAD 工程编辑器。

输出包含 `flange.brep`、`flange.step`、候选/目标/精度 JSON、各阶段报告、内核日志和总报告。返回码 0 表示预期通过；2 表示拒绝、验证未通过或执行错误，具体原因见报告。

Linux 隔离运行、实测结果和当前限制见 [CAD 执行模块说明](docs/cad-foundation.md)。

## 设计规格

建议按以下顺序阅读：

1. [目标状态契约](docs/target-contract-v1.md)、[系统设计](docs/system-design-v1.md)与[验收协议](docs/acceptance-protocol-v0.1.md)：目标、权威边界和可执行验收准备。
2. [当前状态](docs/current-state.md)与[CAD 能力范围](docs/cad-capabilities.md)：实际已有能力、证据和缺口。

早期设计背景：

1. [产品原则](docs/product-principles.md)：持续受托、极简入口、后台工作与按需工程工作台。
2. [开发基线](docs/development-baseline.md)：产品边界、模块、CAD 数据、IR、版本与关键接口。
3. [原方案审查](docs/review-findings.md)：原文问题、影响及修订决策。
4. [运行时协议](docs/runtime-protocol.md)：任务、租约、审批、发布、预算、事件与恢复。
5. [工业资格与验收](docs/industrial-qualification.md)：精度、拓扑、STEP 互操作、恢复与工业试用门槛。
6. [实施工作包](docs/implementation-plan.md)：依赖顺序、可领取任务、产物和退出条件。

早期文档状态为 **开发基线候选 v0.1，2026-10-26**；其中的决策与 M0～M6 工作包保留为背景。当前以目标状态契约和系统设计 v1.0 为准；数值目标与设计文档均不代表已经达到。

[原始方案](docs/reference/original-proposal.txt)保留不改，供审查追溯；新基线负责定义首版开发范围。原文提及的 RFC-005 ZIP、SQL 和 Python 工程未随本次材料提供，不能视为已有代码。
