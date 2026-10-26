# 实施工作包与验收顺序

日期：2026-10-26。状态：实施中，M0/M1 的解析法兰子集已启动，见 [CAD 基础模块](cad-foundation.md)。其他工作包尚待实施；部分测试通过不代表整个里程碑已完成。此顺序替代原 RFC 各自定义的 P0/P1，避免同一能力在不同 RFC 中有不同优先级。

2026-10-27 更新：[Rust 工程内核](rust-engineering-kernel.md)已覆盖 M1-01/02 的首批类型与事务，以及 M1-06 的本地原生包保存/重开编辑。物化与历史已有自包含文件实现；M1-04 的 PostgreSQL、增量 I/O、M1-07 的真实增量执行及完整操作集尚待完成。

先读[开发基线](development-baseline.md)、[运行时协议](runtime-protocol.md)与[工业验收](industrial-qualification.md)。M0～M2 证明 CAD 与状态基础，M3～M4 建立工程工作流，M5～M6 才进入限定范围的工业资格。里程碑以证据通过为准，不以代码行数、工具次数或运行时长代替完成。

## 1. 关键路径及并行边界

```text
M0 产品样件/契约 + OCCT 资格试验
  ↓
M1 确定性 CAD + 原生可编辑工程 + STEP
  ↓
M2 PostgreSQL 事务/租约/候选/Evidence/原子发布
  ↓
M3 Temporal 恢复 + Agent + 目标变更 + 审批
  ↓
M4 工程编辑/有限草图约束/圆角/刚性装配/BOM
  ↓
M5 工业语料与目标 CAD 互操作
  ↓
M6 耐久性、升级、连续运行与限定范围工业试用
```

在 M0 冻结公共契约后，CAD Adapter、Rust Core、Verifier 和编辑器可以并行推进；它们使用同一批固定输入输出夹具。运行时团队可提前验证数据库协议，但 M2 的退出条件必须接真实 OCCT，不能只用 mock 宣布闭环完成。M4 的求解器调查与 M3 可并行，未资格化能力不向 Agent 暴露。

## 2. M0：冻结开发输入，优先消除几何风险

| 工作包 | 主责 | 依赖 | 必交产物 | 验收 |
| --- | --- | --- | --- | --- |
| M0-01 使用场景与样件 | 产品 + CAD 工程师 | 无 | 支持零件/操作/尺寸包络；法兰、盖板、隔套、支架 fixture；硬尺寸和预期拒绝样例 | 每个目标有测量法和阈值；未知需求列表明确；不能只交截图 |
| M0-02 OCCT/OCP 资格试验 | CAD 内核 | M0-01 | Linux 干净环境、依赖锁/镜像、BREP/STEP、布尔诊断、操作历史支持矩阵 | 拉伸、旋转、切孔、失败布尔、序列化、单位、子形状历史实测；失败样例保留 |
| M0-03 契约仓库 | Core + Runtime + CAD | M0-01 | JSON Schema 2020-12、OpenAPI 3.1、操作注册表、版本/状态/错误清单、跨语言 golden vectors | Schema 单一来源，禁止含糊字段；Rust/Python/TS 通过同一组合法/非法向量 |
| M0-04 精度与交换 profile | CAD + QA | M0-02 | 每夹具 precision profile；STEP AP214 profile；目标客户 CAD 名称/版本计划 | 单位/坐标/四类容差分离；导出单位和 reader/writer 配置显式 |
| M0-05 部署与依赖边界 | Core + 运维 | M0-02 | 本地 Compose 设计、凭据角色、资源限制、依赖清单和第三方许可归档 | 几何 Worker 无业务库写入权；版本可重建；不使用浮动 latest |

**M0 停止条件：** 基础操作不满足数值或历史合同、绑定无法提供所需诊断、STEP 接收 profile 不可用时，先修订适配方案。不能用放宽验收或增加 Agent 推理绕过。草图求解器不在这个阶段暗中自研。

## 3. M1：不用 LLM 也能完成的真实 CAD 闭环

| 工作包 | 主责 | 依赖 | 必交产物 | 验收 |
| --- | --- | --- | --- | --- |
| M1-01 Scene/Feature 内核 | Rust Core | M0-03 | 稳定 UUID、树、静态 DAG、类型端口、datum/frame、版本化参数 | 重命名/移动身份不变；引用缺失/环/单位不合法拒绝；候选隔离 |
| M1-02 Transaction IR | Rust Core | M1-01 | 顺序命令应用、前置条件、原子候选、错误映射 | 创建后引用支持；任意命令失败不部分生效；删除/重父级语义符合基线 |
| M1-03 几何适配 | CAD | M0-02/03 | profile/extrude/revolve/fuse/cut/rigid transform/import/tessellate/export | 每操作成功、空结果、非法输入、数值失败都有结构化结果 |
| M1-04 内容与版本存储 | Core | M1-01/02 | PostgreSQL revision/branch 与本地开发 CAS（同对象协议）、immutable manifest、generation CAS | 历史可重载；head ABA 拒绝；上传失败不出现悬空版本；正式 Task 发布在 M2 接入 |
| M1-05 Verifier 与交换 | CAD + QA | M1-03/04 | 独立加载 BREP/STEP 的验证进程；Evidence 数据格式 | 法兰逐孔/厚度/孔距/实体数量通过；错误孔位、缺孔、单位错等反例全部拒绝 |
| M1-06 原生项目与查看器 | Core + 前端 | M1-04/05 | `.deep3d` 读写；基础参数表/特征树/预览/报告 | 关闭重开后修改厚度和孔距；重算及 STEP 往返；引用闭包完整 |
| M1-07 增量与缓存 | Core + QA | M1-03/04 | 静态 dirty closure、环境绑定 action cache、全量基准 | 局部/全量输出在资格公差内一致；无关 Feature 不重复执行；缓存冲突可见 |

M1 交付一个可复现命令流程：读取固定设计合同 → 创建法兰 Feature → OCCT 求值 → 精确验收 → 保存原生工程 → 导出 STEP → 独立进程重导入 → 重开工程改参数。预览面板可简化，真实几何、失败反例和编辑历史不可简化。

## 4. M2：持久化执行与原子发布

| 工作包 | 主责 | 依赖 | 必交产物 | 验收 |
| --- | --- | --- | --- | --- |
| M2-01 领域迁移 | Core + Runtime | M0-03、M1-04 | Run/Goal/Plan/tasks/plan_tasks/Attempts/FinalizeLease/Evidence/Publication/DeliveryRecord/ledger/outbox 的迁移与约束 | 跨 project/run 伪造引用失败；计划冻结；回滚/升级策略明确 |
| M2-02 Claim 与 Fencing | Core | M2-01 | 一致锁序、数据库时钟、claim/renew/recover；每步请求去重 | RT 租约/并发/撤权测试；100 个并发 Claim 无双重合法执行权 |
| M2-03 Candidate 与 Publish | Core + CAD | M2-01/02、M1-05 | immutable candidate、Evidence 绑定、短 finalize lease、原子 publication | RT 并发 CAS/ABA、同键同请求、换键重复、断连重试、每个事务写点故障注入 |
| M2-04 成本/事件/存储 pin | Runtime + Core | M2-01 | 独立成本结算、未知账单保留、每 Run 事件序号、Outbox、Artifact 保留 | 失败/取消也记真实费用；SSE 补读不丢；上传/候选不被 GC 删除 |
| M2-05 双 Worker 集成 | QA + 全栈 | M2-02/03/04 | 真实 CAD 两候选并发、显式冲突与重基、数据库/Artifact 对账报告 | 一个先发布；另一个重基并重验后继续；重复投递只有一个正式验收事实 |

M2 可用有脚本计划的测试执行器验证；此时不以 LLM 输出质量干扰一致性验证。完成 M2 才可把“运行时基础正确”列为已验证能力，仍不能声称工业级产品。

## 5. M3：持久化 Agent 与人类接管

| 工作包 | 主责 | 依赖 | 必交产物 | 验收 |
| --- | --- | --- | --- | --- |
| M3-01 Temporal 编排 | Runtime | M2 | 稳定 Workflow ID、Start/Signal Outbox、Activity 幂等、心跳、Continue-As-New、升级回放 fixture | Worker 被终止后继续；重复 Start、过期通知、关闭 Workflow 重投不新建业务执行 |
| M3-02 有界 Agent | Agent + CAD | M1 Registry、M3-01 | Goal 结构化、有限 Task DAG、版本绑定上下文、IR 静态检查与有界修复 | 模型只能用受支持操作；不能自己改尺寸要求/预算/验证结论；保留全部尝试 |
| M3-03 控制与重规划 | Runtime + Core | M3-01/02 | pause/resume/cancel、目标版本、plan membership、旧执行撤权 | 目标改孔距时旧结果不能发布；原 accepted 历史保留；新授权重验可复用产物 |
| M3-04 人工审批 | Core + 前端 | M2-03、M3-03 | 候选内容绑定审批、过期/撤销、短发布租约与再次检查 | 等待数小时无计算租约占用；候选/目标/head 变化后旧审批失效 |
| M3-05 最小工程工作台 | 前端 | M1-06、M3-03/04 | 当前/候选版本、精确测量、特征/失败树、审批差异、任务/证据/交付界面 | 工程师能定位错误、改参数、拒绝候选、接管任务；过期预览不冒充已发布 |

Agent 验收同时用固定脚本基准与模型实际调用。LLM 结果另行记录成功率、成本和人工接管率，不把单次成功变成一致性证明。运行中更换模型只影响新调用；不得隐式修改已经提交的计划与证据。

## 6. M4：工程编辑与装配能力

| 工作包 | 主责 | 必交产物 | 验收 |
| --- | --- | --- | --- |
| M4-01 语义子拓扑/圆角 | CAD + Core | 每操作历史能力、ExactlyOne 选择器、解析报告；Fillet/Chamfer | 1.5 mm 圆角及超大半径/边分裂/删除反例；歧义不静默重绑；原工程不污染 |
| M4-02 有限草图约束 | CAD + 交互 | 求解器选择记录、许可与集成、明确支持约束/自由度、冲突解释 | 支持域内 DOF、过约束、退化、解分支、重复编辑可测；未支持自由曲线明确拒绝 |
| M4-03 工程特征 | CAD + Agent | 语义孔/阵列及引用身份、尺寸编辑工作流 | 改孔数、节圆直径后尺寸与实例身份可解释；增量与全量一致 |
| M4-04 刚性装配与 BOM | CAD + Core | Part 与 Instance 分离、冻结定位、部件版本、数量/BOM、间隙/干涉检查 | 法兰+盖板+隔套组合、双 Agent 独立部件、集成验证；不宣称动力学/通用装配求解 |
| M4-05 格式/内核迁移 | Core + CAD | 原包保留、只读兼容、显式迁移、影子重算与差异报告 | 旧工程可读取；不支持操作不丢；升级不后台改历史或证据 |

M3 先以锚定轮廓完成 Agent 编辑；M4 负责工业试用所需更完整的编辑能力。PMI/GD&T、工程图、钣金或复杂装配若成为首个设计伙伴的必需项，应增加具名工作包及验收，不隐含归入“接入 OCCT”。

## 7. M5：工业语料与交换资格

- 冻结支持能力矩阵，建立建议不少于 30 个夹具、500 组有界参数组合，分别统计合法成功与非法拒绝。
- 加入真实企业样件；每个样件明确受测操作、尺寸范围、硬条件、独立参考结果、允许误差及来源授权。
- 用至少一种设计伙伴实际 CAD 与指定版本接收 STEP；记录尺寸、拓扑、单位、结构/名称等声明范围。AP242/PMI 另验，不能以同 OCCT 自写自读代替互操作。
- 校准 precision profile；所有失败保留且显式归类。数值目标调整是受审的资格版本变化，不能改期望值掩盖回归。
- 对用户/Agent/Worker 权限、跨项目读写、候选内容篡改、包导入、资源耗尽做集成资格；同时测量耗时/资源的 p50/p95/p99、超时率与数据集规模。

退出产物：完整回归报告、受支持范围、已知缺陷、目标 CAD 交换报告、可复查原生工程及 STEP。缺少企业样件或目标 CAD 时保持“工程试用候选”，不把门槛删掉。

## 8. M6：生产耐久性与限定范围工业试用

- 固定数据库 WAL、主备/故障切换、对象持久化、备份/保留与恢复拓扑，实测所声明故障域的 RPO/RTO。
- 备份恢复校验 spec、BREP、STEP、Evidence、预算和权限的完整引用闭包；对象缺失即失败。
- 连续运行 72 小时并注入 Worker/API/Verifier 中断、DB 连接丢失、重复/延迟消息、目标变更和人工编辑。记录真实操作量与恢复，避免空转凑时间。
- 以真实历史执行 Workflow 回放与升级；旧镜像可读取历史工件，新内核升级必须有迁移报告和回滚路径。
- 完成监控告警、trace 关联、秘密轮换、审计导出、值班恢复手册、发布/回退演练。运行报告应区分基础设施恢复和 Agent 重新规划。

退出条件：M5 及相应 RT 测试全部通过，具名部署的恢复指标达标，CAD 工程师确认用途范围。此后才能发布“在该版本、操作集、尺寸范围和目标 CAD 上通过工业试用”的声明。

## 9. 建议代码边界

以下是待实施的目录，不表示已经存在实现：

```text
specs/                       JSON Schema / OpenAPI / precision与交换profiles
crates/core-domain/          ID、Scene、Feature、IR、静态检查、依赖
crates/core-service/         HTTP、授权、任务、候选、发布与恢复
crates/core-storage/         PostgreSQL repositories、CAS、manifest
workers/cad/                 OCP adapter、隔离作业、tessellation/STEP
workers/verifier/            可信测量、拓扑、原生/STEP 验证
runtime/                    Temporal、Agent、Context/Plan、Outbox消费者
apps/editor/                参数、特征、视口、任务、证据、版本
migrations/                 单一 PostgreSQL schema owner
fixtures/                   解析零件、反例、资格配置、期望
tests/contracts/            跨语言 schema/hash/单位向量
tests/integration/          真 PostgreSQL + 对象存储 + OCCT
tests/faults/               租约/发布/恢复/消息竞争
qualification/              版本绑定报告、支持矩阵与迁移记录
deploy/                     开发环境与具名生产拓扑
```

不在每个 crate 内重复领域模型，不让 Python 与 Rust 各自维护一套 SQL 状态机。生成文件标明来源；契约变更提交同版本样例、迁移和拒绝旧输入策略。

## 10. 团队、估算与完成标准

建议最小持续配置：1 名熟悉 OCCT/机械 CAD 的内核工程师、1 名 Rust/数据库一致性工程师、1 名 Python/Agent 工作流工程师、1 名编辑器工程师；CAD 测试与运维需要明确责任人，可由团队兼职但不能省略。团队角色是估算假设，并非已分配人员。

暂不为完整工业级产品承诺日历工期。M0 完成后，依据 CAD 边界失败率、约束求解器接入成本、真实交换样件与人员情况给工作包估算；完整草图、PMI 或复杂装配会实质改变工期。所有开发任务均至少包含：实现、失败反例、集成验证、版本化文档与可复查产物。

最先可以领取：M0-01 与 M0-02 的环境/API 探测并行；M0-02 正式几何资格测试等待 M0-01 夹具及契约冻结，随后完成 M0-03/04。下一次编码的具体首交付应是**法兰零件的确定性 CAD 闭环及其契约夹具**，并行验证运行时事务协议；不先建设完整多 Agent 产品界面，也不把数月的工程功能藏在“生成一个城堡”演示中。
