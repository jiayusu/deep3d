# 参数化机械 CAD 能力：0.3.0

状态：已接入原生工程和候选物化链的有界机械操作集。本文不是 V0.1 目标契约的验收成绩，也不宣称通用商业 CAD、自由曲面或工业资格已经完成。

## 可执行操作

| 类型 | 当前支持 |
| --- | --- |
| 轮廓 | 圆、矩形、简单多边形、约束草图 |
| 草图实体 | 直线、圆、圆弧、外环及内孔；持久实体/约束名称 |
| 草图约束 | 固定、水平/竖直、距离、重合、平行/垂直、等长、角度、直径、点在线/圆上、相切、中点、水平/竖直对称 |
| 实体构造 | 拉伸、任意指定轴旋转，布尔切除/融合/交集 |
| 修饰 | 按几何条件选边的圆角和等距倒角 |
| 孔与阵列 | 轴向盲孔/通孔、直线阵列、圆周阵列；保留实例或融合 |
| 放置与结构 | 刚体旋转/平移、多实体组合、命名场景树、共享定义的刚性实例 |
| 交付 | 逐 Feature/Node BREP、命名 AP214/mm STEP 装配、BOM、原生工程及独立验证报告 |

源中的操作名为 `profile.circle/rectangle/polygon/sketch@1`、`solid.extrude/revolve/cut/fuse/common/fillet/chamfer/hole@1`、`shape.transform/linear_pattern/circular_pattern@1`、`assembly.compound@1`。操作定义以 Rust 类型和编译后的严格参数合同为准，未知字段或操作拒绝执行。

尺寸表达式可以是字面量、`参数×系数`，或 `linear`：偏置加若干 `参数×系数` 之和（1～16 项，单位必须一致），用于“横梁高度 = 底座厚度 + 立柱高度”这类跨组件关系；每一项参数都进入失效依赖。

草图使用锁定版本的 SolveSpace 绑定 `slvs==3.3`，每次从持久定义重建求解系统。欠约束、冲突、冗余、自交、开放轮廓、孔越界或相交会返回结构化错误。DOF=0 表示局部约束完成，不能解释成全局解唯一；初始种子也是源的一部分。

选边采用几何谓词和预期基数：所有边、平行于轴的直边、指定坐标平面内的边、指定坐标平面内且平行于给定方向的直边（`line_parallel_in_plane`）、指定半径/轴/坐标的圆边。缺失和多义均失败；不会把临时边数组索引作为可编辑的持久引用。

## 使用原生源

```powershell
uv sync --frozen
.\scripts\cargo.ps1 build --workspace --locked
New-Item -ItemType Directory -Force artifacts/my-cad | Out-Null
.\target\debug\deep3d.exe import --project artifacts/my-cad/plate.deep3d --spec fixtures/mechanical/constrained-plate.json
.\target\debug\deep3d.exe evaluate --project artifacts/my-cad/plate.deep3d --out artifacts/my-cad/plate-build
.\target\debug\deep3d.exe show --project artifacts/my-cad/plate.deep3d
```

三个工程源分别覆盖约束板件、旋转衬套及板/隔柱装配：

- `fixtures/mechanical/constrained-plate.json`：尺寸驱动草图 → 拉伸 → 外角圆角 → 孔 → 孔口倒角。
- `fixtures/mechanical/bushing.json`：参数化矩形绕轴旋转形成衬套。
- `fixtures/mechanical/rigid-stack.json`：两个共享板定义的实例、四个共享隔柱定义的实例及组件层级。

这些是开发回归源，不能作为自然语言 G1 成绩。新工程不再要求法兰绑定；旧法兰原生文件与计划保持可读。

参数修改和独立验收目标可在一个事务中保存。`acceptance.set` 修改的是用户/操作者提供的合同，不是让模型按生成结果放宽阈值。完整 GoalRevision 权限层按新系统设计后续接入。

```json
{
  "op": "acceptance.set",
  "acceptance": {
    "schema_version": "deep3d.mechanical-acceptance/1",
    "nodes": {
      "OUTPUT_NODE_UUID": {
        "solid_count": 1,
        "volume_mm3": 1000,
        "bounds_mm": [-10, -5, 0, 10, 5, 5]
      }
    }
  }
}
```

每个输出必须有独立实体数量及体积或包围盒目标；目标不是从候选几何反算而来。可选 `assembly` 检查聚合体，可选 `max_interference_volume_mm3` 检查不同输出组件之间的两两相交体积；它不检查同一多实体输出内部的实体重叠。范围内的体积/包围盒验证不能代替孔位、圆角半径等所有业务要求的检测器。

`model.step` 保留命名组件和共享零件实例；`assembly.json` 保存 Scene、实例和 BOM；`.deep3d` 保留参数、特征、完整版本和证据。STEP 不包含本系统完整建模历史。

并发运行时已支持通用长度参数名称及可选的独立机械验收目标。每个 Task 的目标随计划冻结；修改参数时仍要通过源参数存在性/单位、精确规范变化、几何验证和最后一次执行权检查。

## 当前边界

- 当前 Worker 每个计划最多 256 个 Feature、64 个几何输出；原生数据和计算实例还有单独上限。单次几何调用仍有 600 秒上限。
- 当前刚性装配通过显式变换与组件关系定位，没有通用三维装配配合求解或动力学。
- 表达式支持带单位常量和参数乘比例；完整代数表达式系统、扫掠/放样、抽壳、自由曲面及任意 STEP 导入为参数化 Feature 尚未实现。
- 已有依赖失效闭包，Worker 仍全量求值。真实增量缓存、变更解释和全量等价性属于 S4/G3 后续工作。
- 通用检测覆盖实体结构、闭合、方向、容差、合同体积/边界、实例名称/层级与适用干涉；更多条件必须有专门检测器才能声称达成。
- 部署使用受信任执行节点。代码/文件摘要绑定、独立进程验证不等于恶意 Worker 的远程可信认证。

复现与修改检查见 `tests/test_mechanical_project.py`，[本轮工程回归记录](../qualification/mechanical/README.md)列出实际执行范围；它们证明限定工程行为，不替代新契约的预注册自然语言、长期运行和干净环境验收。
