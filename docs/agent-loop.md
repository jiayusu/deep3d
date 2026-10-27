# 第一个 CAD Agent 闭环

2026-10-27：已实现本地、可恢复的受控工具循环。本文只描述该早期入口；后续主线已转到 [持久任务与并发运行时](durable-runtime.md)，由 PostgreSQL/Rust 服务驱动，不依赖模型服务。线上 LLM 联调状态单独记录，脚本模型测试不代表真实模型通过。

## 使用

先构建 Rust CLI 和 Python 包。模型服务使用 Anthropic Messages 兼容协议；必须提供模型名和有效凭据。默认服务为 `https://api.anthropic.com`，可通过 `ANTHROPIC_BASE_URL` 配置 HTTPS 服务。凭据从 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` 读取，不写进工程、Run 或版本库。不接受远程 HTTP 服务或重定向。

```powershell
uv sync --frozen
.\scripts\cargo.ps1 build --workspace --locked
# 在本机环境中配置模型服务凭据，再运行：
.venv\Scripts\python.exe scripts/demo_agent.py --model YOUR_MODEL --out artifacts/agent-live-001
```

演示新建原生法兰工程，让模型完成厚度 12→16 mm、孔分布圆 70→80 mm 的修改，保留其余工程数据，验证并接纳候选，导出 STEP，再验证完成状态重复恢复不会添加版本。模型费用由配置的提供方收取。

离线复现使用显式 `--scripted`，仍运行真实 Rust、OCCT 与独立几何验证器：

```powershell
.venv\Scripts\python.exe scripts/demo_agent.py --scripted --out artifacts/agent-offline-001
```

操作已有工程：

```powershell
uv run --frozen deep3d-agent run --binary target/debug/deep3d.exe --project artifacts/flange.deep3d --request "把厚度改为16mm，孔分布圆改为80mm，其余保持" --goal fixtures/agent/thicker-flange.json --model YOUR_MODEL --out artifacts/run-001
uv run --frozen deep3d-agent resume --binary target/debug/deep3d.exe --out artifacts/run-001 --model YOUR_MODEL
```

`run` 要求新的目录；`resume` 沿用目标、输入版本、模型名、已消耗预算和事务身份。Linux 的 CLI 名称是 `target/debug/deep3d`。结果为 JSON；成功返回码 0，拒绝/错误返回码 2。任务文件包含用户请求和模型对话，应按工程数据保管。

## 模型的权限与验收

工具仅有 `inspect_project`、`evaluate_parameters`、`accept_candidate`。模型先读取固定工程输入，再提出白名单中的长度参数修改，查看真实验证结果后接纳候选。模型不能运行代码、任意文件操作、SQL、外部通信、修改精度或修改验证器。

自然语言请求提供上下文；独立的 [Goal 合同](../specs/agent-goal.schema.json)才是本阶段可执行授权和验收尺寸。五个可修改参数为外径、厚度、中心孔径、螺栓孔径、孔分布圆直径。必须提交全部且仅有目标中的修改，所有未提及参数、Feature、Node、稳定 ID 和精度配置均与基线相同。候选逐字段比较通过后，还必须通过真实 B-Rep 和 STEP 验证；模型自述成功不生效。

这意味着首版不是任意自然语言到通用 CAD：自然语言自动提炼 Goal 的提案/确认流程尚未实现。不能用模型生成的验收目标自行证明模型结果正确。工具循环采用提供方的 [`tool_use` / `tool_result` 协议](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)，运行时独立检查工具名、类型、范围、调用身份和预算。

## 候选、并发与交付

1. Rust `candidate` 读取工程和固定 head token，建立只多一个事务版本的独立 `.deep3d` 包。源工程字节保持不变。
2. OCCT 对候选的真实 Feature DAG 求值。独立进程重新加载 BREP/STEP，对照编译后的目标检查尺寸、孔位、实体、拓扑与精度；完整证据写入候选包。
3. Rust `adopt` 重新验证包、历史前缀、事务、选定物化与版本条件，合并不可变内容，原子推进原工程。版本冲突直接失败，源工程新增的历史和证据保留；不静默重基。
4. STEP 导出绑定具体已接纳版本和物化哈希，经摘要核对后原子暴露为 `delivery.step`。后续编辑不改变这份交付的身份。

几何/LLM 期间不持有源工程写锁。源工程只在候选接纳时变化；几何失败、模型越权和旧版本冲突不能造成部分参数提交。候选副本本身不赋予云端发布权；这里的接纳是本地单分支版本提交，尚无正式 PublicationRecord、租户权限或签名证据。

## 恢复与有界执行

`run.json` 原子刷盘保存固定目标、基线版本、模型消息、工具请求/结果、事务身份、候选、事件序列和预算。OS 文件锁禁止同一 Run 两个写入者。调用前记录预算，工具副作用前保存调用；恢复时先完成未结束的工具。已验证候选可复用；已成功接纳但尚未落 Run 完成状态时，Rust 按原候选幂等返回。源版本后来前进，也不会把旧候选再次推进 head。

默认最多 8 次模型请求、3 次候选/几何作业；硬上限 12 次请求和 3 次作业，恢复不重置计数。模型调用默认超时 60 秒，几何调用 120 秒，CLI 外层 150 秒。模型网络失败为 `waiting_provider`，可显式恢复；目标/预算/版本冲突等终止错误保留原因。进程中断后的模型调用或几何计算可能重复、产生额外费用；没有提供方 exactly-once 或费用账本承诺。

这个入口仍是本地同步进程加持久检查点，停止后需要运行 `resume`。常驻 Worker、数据库租约、控制与恢复由新的持久运行时提供；该入口没有自动转化为数据库任务。完整费用账本、Temporal 与 24 小时可用性保障仍须后续实现及部署验收。

## 验证范围

`tests/test_agent_runtime.py` 用脚本模型隔离编排，使用真实 Rust CLI 与 OCCT，覆盖越权/错误目标拒绝、模型纠错、真实错误几何拒绝、验证后源版本冲突、网络失败恢复、计算/接纳的进程中断窗口与完成后重复恢复。`tests/test_agent_provider.py` 使用本地 HTTP 服务检查协议、认证头、超时、重定向拒绝、严格解析、错误脱敏和大小限制。

这些测试证明限定法兰流程的开发行为；工业资格、任意 CAD 泛化和真实模型成功率仍需独立验收。

本次 Windows 全量回归为 33 项 Rust、199 项 Python 测试通过，Ruff、Rustfmt 和 Clippy 严格检查通过。Linux 另有 31 项 domain/storage 测试、77 项 Agent 传输/集成测试和真实几何演示通过。源码摘要、日志摘要、候选/STEP 绑定和平台结果保存在 [本地验证记录](../qualification/agent-local/summary.json)；原始产物在忽略的 `artifacts/agent-local/` 中，克隆后可重新生成。

真实模型测试使用本机配置的服务尝试 HTTPS 请求，返回认证/访问拒绝，Run 停留在 `waiting_provider`，没有执行模型工具或修改原工程。记录明确标为 `live_llm: blocked`，不把脚本模型的成功结果当作线上模型成功。服务恢复后，可用上面的 `resume` 命令继续同一 Run。
