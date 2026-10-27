# Runtime Protocol：任务、证据与原子发布

状态：开发基线，待实现及资格验证。日期：2026-10-26。

2026-10-27 实现范围说明：早期 [本地 Agent 闭环](agent-loop.md)使用原生包、文件锁和持久检查点验证候选/证据/CAS/幂等接口，不能将本地版本接纳等同为正式云端发布。

同日后续实现：[持久并发运行时](durable-runtime.md)已落地 PostgreSQL 固定任务 DAG、执行租约、代数隔离、控制和恢复，输出为协调服务验证登记的私有候选。下面的正式 Publication、资源账本、动态计划与 Temporal 仍是目标合同；立即暂停的当前语义与本规范的未来优雅排空分别说明。

关系和不变量的统一定义见 [系统数学模型 v0.2](mathematical-model.md)。尤其区分新接纳与历史回执重放、授权判定时刻与事务可见时刻，以及有效执行凭据与实际进程数量。

本规范修订原 RFC-005 的运行时边界，面向精密 CAD、OCCT B-Rep 与 STEP 交付。文中的“必须”是实现约束，“建议参数”是待基准验证的初值。本文没有声称系统已达到工业可靠性。

## 1. 权威与边界

- PostgreSQL 是 Goal、Plan、Task、Attempt、授权、预算、分支与发布元数据的唯一业务权威。
- Rust API 与 Publication Coordinator 共享一个数据库事务边界，维护工程和任务元数据。
- Python Temporal Worker 只通过受控 Internal API 调用业务服务，不直接写数据库或推进分支。
- Temporal 保存编排历史；Workflow 不直接执行数据库、网络、LLM、几何计算或系统时间读取。
- OCCT Worker 只产生候选及执行记录；可信 Verifier 产生证据；两者均不能直接推进正式发布分支。
- 对象存储保存不可变内容，注册为持久可读 Artifact 后才允许被正式发布引用。
- 内部几何约定为 mm、rad、右手坐标、Z-up。所有导入导出适配必须显式转换并记录原单位。

Temporal 的 Activity 可能因超时或故障重试，不能假定外部副作用只发生一次。本协议保证合法业务提交去重；外部计费和计算次数仍取决于提供方幂等能力。[Temporal Activity Execution](https://docs.temporal.io/activity-execution)

## 2. 三种不可变身份

| 实体 | 内容 | 禁止行为 |
| --- | --- | --- |
| `spec_revision` | 场景与 Feature 定义、参数、依赖、IR 变更及父规范版本 | 把执行状态或后来生成的 Artifact 写回原规范 |
| `materialization_manifest` | 一个规范版本在特定内核环境下的实际输出引用、状态与执行摘要 | 替换已有 B-Rep、STEP 或报告引用 |
| `publication_record` | 被接受的规范、物化清单、证据、父发布、发布者与授权事实 | 原地更新成另一候选或另一验收结论 |

草稿分支保存 `spec_head`；正式发布分支保存 `publication_head`。正式发布不是把“计算成功”写成规范版本的属性。一次规范可能有多份物化结果；只有明确接受的清单进入发布记录。

分支均具有稳定 `branch_id` 和单调 `head_generation`。移动、回退、恢复和重置 Head 都推进 Generation；删除后重建产生新 Branch ID。仅比较 Revision ID 无法发现 Head 先前变化后又回到原值的 ABA。

`publication_record` 可以引用预先持久化的候选清单，但只有正式事务提交后才成为可交付发布。导出和预览始终显示它实际绑定的规范、清单和发布身份。

## 3. 数据模型与约束

下表是逻辑模型，不是可直接执行的迁移。实现必须补齐复合外键、唯一约束、状态检查和权限。

| 对象 | 关键字段与约束 |
| --- | --- |
| Project / Branch | `tenant_id, project_id, branch_id, kind, head, head_generation`；Head 类型匹配分支种类 |
| Run | 项目、当前 Goal/Plan、工作/发布分支、`control_epoch`、状态、每 Run 事件计数器 |
| GoalRevision | Run、版本号、不可变合同摘要；`UNIQUE(run_id, revision_no)` |
| PlanRevision | Run、目标版本、不可变计划摘要、状态；只有完整验证后的计划可激活 |
| Task | Run、稳定 Task ID、不可变 TaskSpec/AcceptanceSpec、资源需求；修改定义产生新 Task |
| PlanTask | `plan_id, task_id` 成员关系；计划内逻辑 TaskKey 唯一；支持保留未变化 Task |
| PlanDependency | 计划、Task、前置 Task；两端必须属于该计划且 DAG 无环 |
| Attempt | Task、执行 Epoch、固定目标/计划/规范输入、Worker、状态；`UNIQUE(task_id, execution_epoch)` |
| FinalizeLease | Task 唯一租约行，Owner、单调 Finalize Epoch、Until、目标 Attempt/Candidate、Control Epoch；每 Task 最多一个有效持有者 |
| ExecutionStep | Attempt、稳定 Step ID、请求摘要、作业/响应引用、计费状态；重复 Step 不覆盖已有结果 |
| Candidate | Attempt、规范版本、不可变物化清单、Base Publication、输入分支代数、候选摘要 |
| Evidence | Candidate/清单、目标与验收规范、验证器/策略/内核环境、依赖摘要、结论和报告 |
| Assessment | 当前目标与指定发布/候选下的验收裁定；引用 Evidence；追加记录并维护当前投影 |
| Approval | 候选摘要、动作、目标/策略、分支条件、所需角色、到期、决策和操作者 |
| PublicationOperation | 租户、项目、操作类型、幂等 Key、规范化请求摘要、成功 Publication ID；上述四项组合唯一 |
| PublicationRecord | Task、Attempt、规范、清单、父发布、证据；`UNIQUE(task_id)`、`UNIQUE(attempt_id)` |
| DeliveryRecord | Publication、导出 profile、文件摘要、交换验收 Evidence；不可变，后续导出追加新记录 |
| BudgetAccount / Entry | 限额、已花费、已预留；按 Step/账单事件唯一记账；金额使用定点数 |
| RuntimeEvent | `run_id, event_seq, event_id`；每 Run 序号唯一，Event ID 唯一 |
| OutboxDelivery | `event_id, destination` 唯一；投递租约、重试、下一次时间、确认/死信状态 |
| Artifact / Pin | 摘要、大小、媒体类型、存储状态、保留/引用；待发布清单及审批候选具有有效 Pin |

同一张表里的单列外键不能证明对象属于同一工程。Run→Task→Attempt→Candidate→Evidence→Publication 的关联必须通过复合约束或受控事务验证相同 Tenant、Project、Run 和 Task。

Goal、Plan、TaskSpec、AcceptanceSpec、规范、清单、证据、发布、账本事实不可改写。可变字段仅用于当前状态投影、租约和可靠投递；更正以新事实和取代关系表达。

生效计划的成员与依赖不允许原地追加。建立新计划时一次性验证成员完整性、目标关联和无环性，再原子切换当前计划，防止并发增边分别检查通过后形成环。

## 4. 事务纪律与时间

M2 采用短事务和明确行锁，统一次序为：

1. Run 行；跨 Run 操作按 Run ID 排序。
2. Budget/Quota 账户，按账户 ID 排序。
3. Task 行，按 Task ID 排序。
4. Branch 行，按 Branch ID 排序。
5. 对应 Attempt、Approval、Operation、Artifact Pin 等从属行，按稳定 ID 排序。

所有控制、领取、续租、恢复、发布与完成 API 遵守同一顺序。不能有一条路径先锁 Task 再锁 Run。M2 对同一 Run 的协调和事件分配有意串行化；几何计算仍可并行。将来优化锁粒度必须重新证明竞争测试。

Read Committed 配合上述锁与唯一约束是 M2 的明确选择；不可把普通查询的快照当作权限锁。事务只做数据库检查与元数据写入，绝不持锁等待用户、LLM、OCCT、对象上传或网络返回。死锁/序列化失败重试整个短事务，沿用请求幂等键。[PostgreSQL 行锁与死锁](https://www.postgresql.org/docs/current/explicit-locking.html)

租约使用数据库时间。取得相关锁之后读取 `clock_timestamp()`，而不是用事务开始时固定的 `now()` 判断当前是否过期。发布授权的线性化判定点是：持有相关锁、检查租约/控制/分支条件并决定接受的时刻；之后直到 Commit 相关控制写入不能越过这些锁。[PostgreSQL 时间函数](https://www.postgresql.org/docs/current/functions-datetime.html)

建议参数：执行租约 120 秒、心跳 20 秒、Finalize 租约 30 秒、元数据事务超时 5 秒。这些数值不是已验证 SLO；需依据实际 OCCT 作业、网络和负载资格测试调整。

## 5. Claim 与执行租约

Claim 请求带 Worker 身份、Run、资源能力、稳定 Claim Key 与请求摘要。

事务步骤：

1. 检查调用者工程权限和 Worker 资源类别；相同 Claim Key 返回原 Claim，不额外领取 Task。
2. 锁 Run，要求 `running`、当前 Goal/Plan 可执行；锁预算及资源账户。
3. 在当前 `plan_tasks` 内选择 Ready Task；`SKIP LOCKED` 仅用于候选队列选择。
4. 重新确认依赖在指定目标和输入工程状态下有有效验收，不能只看前置 Task 曾经 Accepted。
5. 检查重试/时间/成本上限，锁 Task，递增执行 Epoch，建立租约及 Attempt。
6. Attempt 固定 Goal、Plan、输入 Spec、Base Publication、分支代数、Control Epoch 和上下文摘要。
7. 原子预留预算/配额，写 Claim 结果、事件及 Outbox，提交。

Claim 返回前 Attempt 必须已经存在。无可调度任务与系统失败分别返回；不能用空结果掩盖预算不足、依赖失效或计划尚未协调。

`FOR UPDATE SKIP LOCKED` 可用于队列消费者，但不能提供一般查询的全局一致视图。[PostgreSQL SELECT](https://www.postgresql.org/docs/current/sql-select.html)

所有执行写入都校验当前 Task、Attempt、Owner、Execution Epoch、Control Epoch 和有效租约。续租不允许复活已过期租约。失权 Worker 可保存诊断和非权威缓存内容，不能更新 Task、登记可信验收或请求正式发布。

一个 Attempt 内的 Temporal Activity 重试不创建新的业务 Attempt。Step ID、请求摘要、外部 Job ID 和已确认响应先持久化；重复 Step 返回已有结果或恢复同一作业。若提供方不支持幂等，必须显式记录重复调用及不确定费用。

## 6. Task 与 Attempt 状态

| Task 状态 | 允许后继 | 主要条件 |
| --- | --- | --- |
| proposed | blocked / ready / superseded | 当前计划已验证并激活 |
| blocked | ready / superseded / cancelled | 依赖恢复或被取代 |
| ready | leased / superseded / cancelled | Claim 原子成功 |
| leased | running / retry_wait / superseded / cancelled | Worker 确认或租约回收 |
| running | candidate / retry_wait / failed / superseded / cancelled | 结果持久化或结构化错误 |
| candidate | verifying / retry_wait / superseded / cancelled | 清单及产物已可读 |
| verifying | awaiting_approval / ready_to_publish / retry_wait / failed / superseded / cancelled | 验收结果决定 |
| awaiting_approval | ready_to_publish / retry_wait / superseded / cancelled / failed | 批准有效或旧授权失效；无执行租约 |
| ready_to_publish | finalizing / verifying / retry_wait / superseded / cancelled | 获取短 Finalize 租约、需要重验或旧授权失效 |
| finalizing | accepted / ready_to_publish / verifying / retry_wait / superseded / cancelled | 原子发布、租约回收、重验或新输入下重试 |
| retry_wait | ready / failed / superseded / cancelled | 退避、预算及重试政策允许 |
| accepted / failed / superseded / cancelled | 终态 | 不原地恢复旧定义；修复创建新 Task |

Attempt 单独记录 started、executing、candidate、verifying、verified、awaiting_approval、finalizing、published、rejected、failed、abandoned。Attempt 的终态不会因重试被覆盖。

一次 modeling Task 只能有一次正式 Publication。artifact/verification Task 不移动分支，其 Accepted 与 Evidence/DeliveryRecord 同事务。Accepted 后的后续修复必须是新 Task，并保存 `repairs_task_id`。未发布的失败 Attempt 可以按有限策略重试；总尝试次数包含初次执行，避免 `max_retries` 的口径歧义。

租约过期后，恢复器持同样的锁次序标记 Attempt Abandoned、撤销执行权、协调预算/外部作业，再使 Task 进入 RetryWait。已生成的候选可被后续 Attempt 显式采用，但必须重新检查输入及验收。采用时创建属于新 Attempt 的 Candidate envelope，保留 `source_candidate_id` 和可复用的不可变内容引用；不改变旧 Candidate 的归属或旧 Attempt 的输入事实。

上述规则用于活动计算租约。Finalize 租约过期时，在控制条件仍有效的前提下将 Task 从 finalizing 退回 ready_to_publish，保留 verified Attempt/Candidate，递增 Finalize Epoch 后才可重新领取；旧持有者不能写入。控制/计划变更后，仍在当前计划且定义未变的 Task 转 retry_wait，旧 Attempt 记 rejected（reason=control_changed）；已移出计划的 Task 转 superseded。Cancel 转 cancelled。暂停保留已验证候选和等待状态，Run 状态禁止发布。

## 7. 目标变更、计划保留与控制

Goal Change 在单个事务中比较预期 Goal，创建新版本、推进 Control Epoch、失效相关审批，记录事件并通过 Outbox 唤醒 Controller。API 成功表示目标已持久化，不表示几何已重新计算。

首版采用保守授权：任何目标/权限变更或当前计划切换均推进 Control Epoch，使所有旧 Attempt 失去发布权。独立计算可完成为缓存候选；Controller 通过新 Attempt、新上下文和重新验证采用它。不能给旧 Attempt 改 Goal/Epoch 来伪装重新授权。Plan 仅保留未变化的 Task 定义和历史，运行中权限不会因成员被保留而自动继承。

重规划生成新 PlanRevision。TaskSpec 和 AcceptanceSpec 未变的 Task 可作为新计划成员继续排队；已接受成果只有在证据适用性被重新确认后计入新 Goal。被改变的定义创建新 Task，与旧 Task 建立取代关系。

首版不承诺跨目标自动复用任意 Evidence。无法证明依赖完整或规范兼容时，重新验证。集合查询的成员增删也属于依赖变化。

Graceful Pause 的冻结语义：停止新 Claim，现有计算允许到持久化候选，不再自动发布；没有活动执行/Finalize 租约后进入 Paused。Resume 重新协调当前目标、计划、分支、证据和预算，再取得新执行或发布授权。

Pause 与 Publish 持同一 Run 锁线性排序：已在 Pause 事务之前接受的发布保留；Run 进入 pausing/paused 后不能获取或使用新的发布权。停在未验证候选的执行 Attempt 结束为 abandoned（原因为 paused），释放计算租约，候选 Pin 保留；Resume 由新 Attempt 采用候选并重验，不续活旧租约。

Cancel 先持久化 `cancelling`、推进 Control Epoch 并撤销发布权，再通过 Outbox 尽力取消外部作业。未结束的不可中断 OCCT 进程不能阻止业务权限撤销；费用仍需对账。控制 API 返回时区分“撤权已确认”和“计算进程已退出”。

## 8. 候选、验收与审批

Candidate 在验证前冻结 Spec、Manifest、Base Publication、IR/ChangeSet、内核构建环境、公差政策及输入摘要。任何内容或 Base 变化都产生新 Candidate。

Evidence 至少绑定：候选摘要、规范/物化身份、当前目标、AcceptanceSpec、Criterion、Validator ID/版本、策略摘要、输入依赖摘要、内核配置与数值公差、报告 Artifact、Pass/Fail/Inconclusive。

Verifier 是受信任服务，验证结果不能由 Executor 自报。发布协调器验证来源、绑定关系及验收覆盖；有报告文件不等于通过。B-Rep 几何有效也不自动证明尺寸、公差、装配干涉、可编辑性或 STEP 交付满足合同。

Assessment 是某目标/工程下的裁定；Evidence 是一次不可变检查事实。历史 Pass 不永久计入进度。P0 在工程输入变化时默认重新验证受影响检查，无法确定影响时扩大检查范围。

完成验证进入 AwaitingApproval 或 ReadyToPublish 前，持久化候选、证据和 Pin，释放计算租约与计算配额。verified/awaiting_approval 是持久等待阶段，其 Attempt 不因计算租约为空被恢复器判为执行故障。等待期间不得无限续租 OCCT Worker，也不能保持数据库事务。

Approval 绑定候选摘要、动作、Goal、控制/策略、预期发布 Head/Generation、角色和到期时间。批准不等于发布；拒绝、过期、撤权和目标变化均阻止使用该批准。内容变化必须重新审批。

获批后协调器取得新的短 Finalize 租约，记录它与旧 Attempt/Candidate 的关系，重新检查发布条件。未审批且政策要求审批的候选不得发布。

获取/续 Finalize 租约须持 Run/Task 锁，校验 running、候选/审批/控制条件；每次新持有者都递增 Finalize Epoch，过期租约不能续活。发布检查 Owner、Epoch、Until、Candidate 和 Control Epoch，不能以原计算 Epoch 代替 Finalize 凭据。

## 9. Publish 原子协议

请求字段至少包括 Run、Task、Attempt、Candidate、Spec、Manifest、Evidence、Approval（如需）、Finalize Epoch、Control Epoch、Goal/Plan、目标 Branch ID/Head/Generation、幂等 Key。若候选依赖当前草稿分支，还包含其预期 Spec Head/Generation。

请求摘要包含所有具有语义的字段，使用固定的规范化编码、单位和版本。不能把不稳定时间戳或 JSON 键序误当作业务差异。

发布事务伪算法：

1. 鉴权，按统一顺序锁 Run、Task、相关 Branch 和从属记录。
2. 查同作用域 Operation：相同摘要且已成功则返回原 Publication；不同摘要则 `IDEMPOTENCY_CONFLICT`。
3. 重新检查 Run 为 running、Task 尚未发布、Attempt/Candidate 关系、Finalize 租约、当前 Goal/Plan 成员、Control Epoch 与发布权限。
4. 检查正式分支 Head/Generation，以及被声明依赖的草稿 Head/Generation；不得仅比较内容哈希。
5. 检查 Spec、Manifest、Artifact 持久化状态与 Pin；检查可信 Evidence 覆盖 publication 阶段政策的必需条件和有效 Approval。delivery 阶段条件另由最终完成事务检查。
6. 创建不可变 PublicationRecord、Assessment 和正式引用；CAS 更新正式 Branch Head/Generation。
7. 将 Task Accepted、Attempt Published，完成幂等 Operation，释放 Finalize 租约，追加事件与 Outbox。
8. Commit 后返回 Publication ID、Spec、Manifest、正式 Head/Generation 与事件游标。

上述写入共享一个 PostgreSQL 事务；任何失败都不能留下部分发布。已存在且授权可读的成功幂等结果不因原租约已过期而消失。不同 Key 对同一 Task/Attempt 的重复发布由唯一约束拒绝并返回既有结果的明确关联。

对象上传、STEP 验证和几何重算发生在事务之外。已花费成本不随本事务回滚；本事务可以关联既有结算事实，但不能承担所有成本登记。

首版只支持严格 CAS。同 Base 的并行任务可同时计算，但第二个提交会冲突。冲突处理必须读取新 Head、在新 Base 重放/修订 IR、重求值、重新验证，然后形成新 Candidate 和新 Publish 请求；不做 B-Rep 拓扑合并，不修改旧 Operation 摘要。重基建立新 Attempt 与新输入快照，旧 Attempt 记录 conflict/rejected；相同 action_key 的合法输出可复用。

结构化失败包括 `STALE_EXECUTION_LEASE`、`STALE_FINALIZE_LEASE`、`STALE_CONTROL`、`STALE_GOAL`、`PLAN_INELIGIBLE`、`BRANCH_CONFLICT`、`VALIDATION_STALE`、`APPROVAL_INVALID`、`ARTIFACT_NOT_DURABLE`、`TASK_ALREADY_PUBLISHED`、`BUDGET_UNAVAILABLE`、`PUBLICATION_NOT_AUTHORIZED`。

人工 IR 的正式发布由服务端创建 `execution_mode=manual` 的短确定性 Run/Task/Attempt，仍经过同一验证和发布流程；不需要 LLM 或 Temporal。Agent Run 才由 Temporal 驱动。不存在人工直接改 Branch 表的旁路。

## 10. 预算与资源账本

- Claim/计费 Step 开始前原子预留预计最大成本，维持 `spent + reserved <= limit`。
- 已确认消耗通过稳定供应商账单/Step Key 记账一次；失败、取消、验证拒绝和分支冲突同样计费。
- 已确认结束且不存在未结费用时释放预留；超时且供应商费用未知不能当作零费用。
- 实际金额超过预留必须真实记账并触发预算异常和停止新调用，不能丢账以维持漂亮的不变量。
- 因而硬上限只承诺在调用/作业层可执行的资源上界；不可控供应商超额应明确为剩余风险与对账流程。
- 计算配额与金额预算分开；待审批候选保留存储额度，但不占计算槽位。
- 预算限额变更经授权、审计和幂等请求，不由 LLM 修改账户数字。

`spent + reserved <= limit` 是接纳新调用时的门槛；账单超出预留时允许账户进入 overspent 并冻结新调用，不以数据库 CHECK 拒绝真实成本。失权 Attempt 的迟到计费响应由独立受信任对账入口处理，不能为补记账恢复旧执行权。几何写入撤权和成本事实登记采用不同授权。

预算不足或费用待结不阻止已经验证、无需新增费用的元数据发布；overspent/unsettled 单独展示与对账。若发布需要付费重验/重算，先重新预留，否则返回 `BUDGET_UNAVAILABLE` 并等待；不能为发布绕过计费上限。

## 11. 事件、Outbox 与恢复

事件序号按 Run 分配：事务持 Run 行锁更新其计数器，再插入 RuntimeEvent；锁保持到提交。SSE 游标为 `(run_id, event_seq)`，不是全局 BIGSERIAL。PostgreSQL Sequence 的分配不会因回滚撤销，不能单独表达事务提交顺序。[PostgreSQL Sequence](https://www.postgresql.org/docs/current/functions-sequence.html)

客户端可重复接收事件，按 Event ID 去重，并用游标重新查询；事件只是失效/变化通知，最终状态从 API 获取。快照响应携带对应游标，避免快照与后续订阅之间缺口。保留期外游标返回明确的 SnapshotRequired。

Outbox 与业务事实同事务提交；Dispatcher 使用投递租约，外发后再确认，崩溃时允许重复投递。下游幂等，不承诺网络恰好一次。同一事件多目标分别确认，永久失败进入可观察的死信并允许受控重放。

Run Start 采用稳定 Workflow ID，并显式配置开放实例冲突及关闭实例复用政策。数据库持久化启动代数和终态，阻止陈旧 Start Outbox 在 Workflow 关闭或历史过期后重新启动业务。Temporal 的 Reuse Policy 受历史保留期约束，稳定 ID 不能替代数据库去重。[Temporal Workflow ID](https://docs.temporal.io/workflow-execution/workflowid-runid)

Workflow 用有界批次调度，批次结束且子任务/消息处理器排空后 Continue-As-New。业务 Run ID 不变，不能用 Temporal Execution Run ID 作为长期业务身份。[Temporal Continue-As-New](https://docs.temporal.io/develop/python/workflows/continue-as-new)

恢复器检查过期执行/Finalize 租约、未结束外部作业、孤立候选、Outbox、非终态但无编排的 Run、未结账单和 Artifact Pin。每次恢复具有稳定协调 Key，使用与正常路径相同的条件写入。

首个闭环关闭自动物理 GC，只输出可达性与待回收报告。后续启用删除时：先将无引用、无有效 Pin 且超过保留期的对象标为删除候选；发布/续 Pin 与删除状态在数据库中互斥；经过宽限期再删，并通过 RT-18 删除竞争测试。清单缺块、哈希不符或存储不可读时阻止发布并进入修复，不能仅相信曾经上传成功。

## 12. 完成、交付与工业资格

Completed 事务锁定 Run 与正式 Branch，固定 Goal、Publication、Spec、Manifest、验收策略及交付摘要；确认全部必需验收/硬约束、STEP 交付物和有效审批，撤销剩余发布权并记录终态。达到次数、时间或预算上限只产生停止原因，不能直接 Completed。

STEP 交付必须由独立进程重新导入并按合同校验单位、形体、装配/名称及几何指标。最终声明绑定实际接受的文件摘要；STEP 的存在不证明参数化编辑历史被完整保留。

发布之后的原生包/STEP 导出产生追加式 DeliveryRecord，绑定 Publication、export profile、文件摘要与交付 Evidence；不补写原 materialization。若发布前已生成导出，可复用 Artifact，但必须验证它对应最终确切 Publication。Completed 固定选用的 DeliveryRecord；候选发布成功不代表交付验收已经完成。

以后人工修改不改写历史完成事实。继续工作创建新 Run 或明确的新运行代数；读取完成结果始终返回当时交付版本。

资格等级分开记录：M1～M2 证明事务和真实 OCCT/STEP 闭环；M3～M4 证明工程功能、Worker/Temporal 重启与重规划；M5～M6 的工业试用资格另需 CAD 回归语料、权限、压力、备份恢复、主备切换和对象存储耐久性实测。

耐久性合同必须说明进程、主机、磁盘、区域故障的 RPO/RTO。`fsync`、WAL 刷盘、同步提交及备库策略影响已确认事务是否会丢失；流复制默认异步。未配置同步备库时，仅写 `synchronous_commit=on` 不等于跨节点零数据丢失。[PostgreSQL WAL](https://www.postgresql.org/docs/current/runtime-config-wal.html)、[PostgreSQL 复制](https://www.postgresql.org/docs/current/warm-standby.html)

首版资格不覆盖区域故障 RPO=0、任意 CAD 模型均能正确求解、完整拓扑引用自动修复、外部计算 Exactly-Once 或未经测量的性能百分位。

## 13. 必须执行的故障验收矩阵

每项检查数据库、Branch/Publication、Artifact 引用、Task/Attempt、授权、预算和事件；只看 HTTP 成功率不算通过。

| ID | 注入/竞争 | 必须观察到的结果 | 门槛 |
| --- | --- | --- | --- |
| RT-01 | 100 个并发 Claim | 每 Task 一个有效租约/Attempt；预算与槽位不超配 | M2 |
| RT-02 | 相同 Claim Key 重试/摘要变化 | 返回原 Claim；不同摘要拒绝 | M2 |
| RT-03 | 过期 Worker 在重新领取后回写 | 状态、续租、候选及发布均不能污染权威状态 | M2 |
| RT-04 | 锁等待跨过租约截止 | 用锁后数据库实际时间拒绝过期权限 | M2 |
| RT-05 | Goal/Cancel 与 Claim/Publish 竞争 | 结果对应唯一合法事务顺序；撤权后旧结果不发布 | M2/M3 |
| RT-06 | 跨 Run/Project 引用和并发成环 | 数据约束/事务拒绝，计划不激活 | M2 |
| RT-07 | 同 Base 两候选并发发布 | 一个成功、另一个冲突；新 Base 重验后继续 | M2 |
| RT-08 | Head ABA、分支删除重建 | 旧 Generation/Branch ID 请求失败 | M2 |
| RT-09 | 同 Key 并发、换 Key 同 Task | 一次 Publication、一次验收事实 | M2 |
| RT-10 | Commit 成功后响应丢失 | 幂等重试返回原结果，即使旧租约已过期 | M2 |
| RT-11 | 发布各写点进程/连接故障 | 全部提交或全部回滚，没有部分 Accepted/Head | M2 |
| RT-12 | 换清单、Spec、公差、Goal、AcceptanceSpec | 旧 Evidence 不得为新候选背书 | M2 |
| RT-13 | 审批超过计算租约、审批后改目标/Head | 不占计算槽；过期授权拒绝或重新验证/审批 | M3 |
| RT-14 | 暂停/恢复/取消时不可中断 OCCT | 保留可信候选；撤权即时生效；费用继续对账 | M3 |
| RT-15 | 失败、取消、未知费用、重复账单 | 真实消耗不漏不重；未知预留不被提前释放 | M2/M3 |
| RT-16 | Outbox 重复、SSE 断线、游标过期 | 无静默丢事件；去重或明确重建快照 | M2/M3 |
| RT-17 | Temporal 重启、续篇、关闭后陈旧 Start | 继续合法业务或保持终态，不重复发布 | M3 |
| RT-18 | Pin/Publish 竞争、清单缺块；后续启用物理 GC | 已接受对象不被回收；缺块阻止接受；删除并发资格另验 | M2；删除启用前 |
| RT-19 | STEP 独立进程重新导入 | 合同规定指标与单位通过，文件摘要绑定交付 | M1 |
| RT-20 | 主备切换、备份恢复、对象存储恢复 | 实测符合明确 RPO/RTO，引用和授权可协调 | M6 |

没有运行的项目标为“未验证”。基线文档、模拟时钟单测或 API Schema 检查不能代替上述真实数据库、真实 OCCT 和故障环境测试。
