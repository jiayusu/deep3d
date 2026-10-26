# Rust 工程内核：保存、重开、编辑和版本

日期：2026-10-27。范围：M1 的本地工程内核，几何由 OCCT 执行，尚未包含云端发布协调器或 Agent。

## 当前可完成的流程

创建 `.deep3d` 工程 → 保存类型化参数与 Feature DAG → 独立进程重开 → 修改厚度/孔距 → 编译并执行真实 OCCT 特征 → 独立验证 BREP/STEP → 将几何及证据装入工程包 → 保留并读取新旧版本。

第一套工程是带孔法兰，含 5 个长度参数、17 个 Feature、根节点和零件节点。工厂创建该夹具，工程内核实际保存每个轮廓、拉伸和切除定义及其引用；求值器逐节点执行该图，不把它转换回固定法兰构造脚本。

## 构建与操作

需要 Rust 1.88+、Python 3.12、uv；Windows 还需 MSVC C++ 构建工具。

```powershell
uv sync --frozen
.\scripts\cargo.ps1 build --workspace --locked
New-Item -ItemType Directory -Path artifacts/project-example -Force | Out-Null
.\target\debug\deep3d.exe create --project artifacts/project-example/flange.deep3d --name "Flange"
.\target\debug\deep3d.exe show --project artifacts/project-example/flange.deep3d
.\target\debug\deep3d.exe evaluate --project artifacts/project-example/flange.deep3d --out artifacts/project-example/initial
.\target\debug\deep3d.exe edit --project artifacts/project-example/flange.deep3d --set thickness_mm=16 --set bolt_circle_diameter_mm=80
.\target\debug\deep3d.exe evaluate --project artifacts/project-example/flange.deep3d --out artifacts/project-example/edited
.\target\debug\deep3d.exe history --project artifacts/project-example/flange.deep3d
```

Windows 的 `scripts/cargo.ps1` 自动查找 Visual Studio 开发环境；Linux 可直接运行 `cargo build --workspace --locked`。`evaluate` 从当前工作目录的 `.venv` 查找 Python，其他位置用 `--python` 指定。

所有成功/错误均返回 JSON；失败返回码为 2。目标工程的父目录必须存在。新建工程、计划输出、求值目录与导出文件都拒绝覆盖既有内容。

完整演示可直接运行：

```powershell
.venv\Scripts\python.exe scripts/demo_project.py --out artifacts/project-demo-001
```

该脚本每一步都启动新的 Rust CLI 进程，演示重开、修改、重算、历史读取和从搬移后的原生包导出两版 STEP，保存 `demo.json`。

## 内核边界

| 模块 | 实现 |
| --- | --- |
| `core-domain` | 稳定 UUID、Scene 树、带单位参数、表达式引用、Feature DAG、事务候选、校验、拓扑编译、失效闭包 |
| `core-storage` | `.deep3d` 包、不可变 spec/transaction/materialization、内容哈希、版本历史、写锁、CAS、原子替换 |
| `deep3d-cli` | 创建/重开/编辑/事务/历史/计划/求值/导出；受信任 Python 子进程桥接 |
| Python `project_worker` | 白名单 Feature 图求值、真实 OCCT 轮廓/拉伸/切除、逐 Feature BREP、独立验证 |

当前操作集是 `profile.circle@1`、`solid.extrude@1`、`solid.cut@1`。长度统一 mm，角度 rad，右手 Z-up；轮廓位于 XY 基准，沿 Z 拉伸。参数表达式支持带单位常量及参数引用乘无量纲系数。完整草图求解、任意变换、圆角和装配仍在后续范围。

## 参数、特征与事务

Parameter、Feature、SceneNode 和 Project 均有独立 UUID；名称或层级变化不改变身份。Feature 引用参数 ID 和上游 Feature ID，Node 绑定 Feature 输出。参数名在工程内唯一，路径不承担身份。

事务在私有候选上依次应用，最终同时检查：单位、有限数、参数范围、ID、引用、树与 DAG 无环、端口类型、输出绑定和夹具约束。任何命令失败均不改原工程；不接受空事务或实际没有变化的事务。

支持的命令：`parameter.set/create`、`feature.create/replace/delete`、`node.create/delete/rename/reparent`。完整序列化类型以 `core-domain` 中版本化 serde 类型为当前实现合同；尚未提供网络 API 或完整对外 JSON Schema。

`apply --transaction FILE` 接受显式 `base_revision`、字符串 `expected_generation`、事务 ID、消息和命令列表。保存时锁定项目，重新读取并比较 **revision + generation**。同事务 ID、同规范内容的成功重试返回原版本；同 ID 异内容拒绝。

`edit` 是参数赋值便利入口。省略版本条件时，CLI 先读取当前 head，再以该 token 提交 CAS；有既定输入快照的调用方应同时传 `--expected-revision` 与 `--expected-generation`。幂等重试优先保存并重发完整 `apply` 事务；仅复用 `edit --transaction-id` 而改变默认读取版本，可能产生摘要冲突。

编译采用确定性拓扑顺序，生成已解析参数与显式端口的 ExecutionPlan。`dirty_features` 提供保守依赖闭包，几何无关的重命名不标记几何失效，精度策略变化使所有输出失效。当前 Worker 仍全量重算，未宣称具备增量执行或共享缓存。

## 原生文件结构

`.deep3d` 是 ZIP64 容器：

```text
manifest.json                 head、generation、revision 链、物化记录引用
objects/<64位SHA256>           规范、事务、计划、几何、报告和物化清单
```

规范/事务/清单使用 RFC 8785 JCS；64 位 generation 用十进制字符串，避免浏览器整数精度损失。解析启用正确浮点往返；不同 JSON 作者写出的 `100` 与 `100.0` 按相同规范数值比较。

相同字节按内容哈希去重；读取时检查 ZIP CRC、SHA-256、完整引用闭包、版本父链和事务重放。拒绝重复项、路径穿越、悬空引用、未知 schema 与超限包。容器/总解压大小上限256 MiB，单对象64 MiB，元数据4 MiB，条目数10000；求值器最多256个 Feature。

写入使用同目录临时文件、刷盘和原子替换。相邻 `.deep3d.lock` 使用 OS 文件锁，文件保留以维持锁身份；写锁仅覆盖短暂的检查和保存，不覆盖 OCCT 运算。锁竞争返回 `PROJECT_BUSY`，过期版本返回 `REVISION_CONFLICT`。Unix 若替换后目录刷盘失败，返回 `COMMIT_DURABILITY_UNKNOWN`，调用方应查询事务结果而非认定回滚。

当前实现每次事务原子重写原生包；对象在包内去重，但文件 I/O 尚未增量化。此选择适用于本阶段小工程。大工程和云端长期任务将使用 PostgreSQL 元数据及独立 CAS，原生包用于便携交付；本地文件锁不承担跨机器并发或云端发布的正确性。

## 定义、求值与历史

保存参数产生新的 Spec Revision，不要求先等待几何完成。求值绑定固定 revision 和规范计划，完成后只追加不可变 Materialization，不移动 head，也不修改既有版本的定义。

物化记录保存每个 Feature/Node 的几何 hash、BREP/STEP、目标/精度文件、验证报告、输入几何 hash、实际加载的适配器/验证器源码 hash 和环境。Rust 再次检查输入绑定、报告结果、内容及完整性；实际几何测量由受信任 OCCT Verifier 完成。

旧计算在用户编辑后返回，可以附加到旧 revision；不会给新 revision 绑定旧几何。错误几何保留诊断目录，不添加物化记录；新定义依然可编辑，先前有效版本继续存在。

`show --revision UUID` 读取历史定义及该版本的物化结果；返回的 head token 仍是当前分支。`export --materialization HASH --out FILE` 从工程包读取指定几何，不需要 Python、原始求值目录或网络。单分支历史尚无分支创建、任意旧版分叉编辑或图形撤销界面。

内容哈希用于完整性与关联检查，不能代替签名或服务端权限。当前边界是受信任本地用户与白名单执行器；云端用户隔离、审批、租约、原子正式发布和长期 Agent 尚未实现。

## 验收依据

- Rust：稳定身份、单位/类型/依赖、原子候选、历史、重试幂等、并发 CAS、锁、损坏/CRC/重复 ZIP、完整物化引用。
- 跨进程集成：关闭/重开后编辑，17 个真实 Feature 的 OCCT 重算，两版精确测量与 STEP 导出。
- 反例：合法类型但错误孔径的 Feature 可以作为草稿保存，几何验收拒绝并保持包字节不变。
- 竞争：旧 revision 正在计算时提交新编辑，旧结果完成后只归属旧版本。
- 移动工程包后导出两版 STEP，逐字节匹配，证明产物不依赖外部临时路径。

实测状态与示例入口见 [M1 记录](../qualification/m1/README.md)。当前交付不代表完整 M1 工业能力集已完成，尚未进行客户 CAD 互操作、工业语料与生产故障恢复资格。
