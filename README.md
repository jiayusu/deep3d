# deep3d

面向工业精密 CAD 的开源 Agent 建模系统，已实现首版本地 Rust 工程内核和确定性 CAD 执行模块。

用户已明确的方向：**工业级目标，B-Rep + OCCT，STEP 交付，支持云端、异步并发和长程 Agent。**

当前可执行：参数化法兰 → OCCT B-Rep → 独立进程测量 → AP214/mm STEP → 独立进程重新导入测量。记录非破坏布尔的诊断和操作内拓扑历史，并保留参数、精度配置、报告与内容哈希。

现在还可以把参数、17 个真实 Feature、版本历史及 BREP/STEP/证据保存为自包含 `.deep3d` 工程，重开后继续编辑并重算，保留旧版本。已接入首个受控 Agent 工具循环：读工程 → 提出参数修改 → 独立候选求值/验证 → 版本检查 → 接纳并交付 STEP。详见 [Agent 使用与边界](docs/agent-loop.md)。

当前是有范围限制的本地工程内核和可恢复 Agent 原型；真实模型联调证据与脚本模型测试分开记录。云端运行时、正式发布协调器尚待实现，尚未通过工业资格或客户 CAD 互操作验收。

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

1. [产品原则](docs/product-principles.md)：持续受托、极简入口、后台工作与按需工程工作台。
2. [开发基线](docs/development-baseline.md)：产品边界、模块、CAD 数据、IR、版本与关键接口。
3. [原方案审查](docs/review-findings.md)：原文问题、影响及修订决策。
4. [运行时协议](docs/runtime-protocol.md)：任务、租约、审批、发布、预算、事件与恢复。
5. [工业资格与验收](docs/industrial-qualification.md)：精度、拓扑、STEP 互操作、恢复与工业试用门槛。
6. [实施工作包](docs/implementation-plan.md)：依赖顺序、可领取任务、产物和退出条件。

文档状态为 **开发基线候选 v0.1，2026-10-26**。其中的设计决策是本次审查后的建议；数值目标须按资格试验冻结，不代表已经达到。原文 RFC 的 P0/P1 编号不再作为排期依据，统一使用工作包中的 M0～M6。

[原始方案](docs/reference/original-proposal.txt)保留不改，供审查追溯；新基线负责定义首版开发范围。原文提及的 RFC-005 ZIP、SQL 和 Python 工程未随本次材料提供，不能视为已有代码。
