# 首个 CAD 执行模块

本次实现覆盖 M0/M1 的解析法兰子集，尚未完成 M0/M1 的全部工作包。产品方向仍是[持续受托的工程 Agent](product-principles.md)；当前模块先提供可以被工程内核和 Agent 共同调用的确定性基础。

## 已实现

- Python 3.12、OCP no-VTK 绑定固定版本，依赖与 wheel 哈希锁定。
- 严格 JSON 和不可变参数契约，显式 mm/rad/右手 Z-up，拒绝不合法单位、非有限数、重名 key、过深或过大输入。
- 圆柱毛坯、中心孔和环形安装孔，非破坏 OCCT 布尔；错误或未经审核的警告阻止成功返回。
- 操作内 Generated/Modified/Deleted 历史和完整性说明。局部形状索引仅用于诊断，不承担跨重算稳定命名。
- BREP 和 AP214IS/mm STEP；STEP 导入暂只接受显式毫米，其他单位明确拒绝。
- 从重新加载的真实 B-Rep 独立测量实体、闭合性、方向、子形状容差、解析孔径/孔位/轴线/贯穿范围、顶底面及体积交叉检查。
- 每个构造/BREP 验证/STEP 验证在新进程中执行，设置超时，输出 JSON 报告、日志和内容哈希。进程隔离不等于任意代码沙箱；仅运行受信任白名单操作。

当前参数合同的 1～1000 mm、3～32 个安装孔是输入与测试范围限制，并非此范围内所有组合都已通过工业资格。默认夹具：外径100、厚12、中心孔径30、4×孔径8、孔分布圆直径70，解析体积为 `26532π mm³`。

## 本次实测

| 环境/流程 | 结果 |
| --- | --- |
| Windows / Python 3.12.9 | 98 项测试通过 |
| Linux amd64 容器 / Python 3.12.12 | 98 项测试通过 |
| Windows 完整法兰流程 | 2 项正例通过、4 项错误几何按预期拒绝 |
| Linux 完整法兰流程 | 2 项正例通过、4 项错误几何按预期拒绝 |
| Linux 运行约束 | 断网、非 root、只读根文件系统、2 CPU / 1 GiB / 128 PID 上限 |

版本、源码/文件摘要、系统包与镜像 ID 见[实测记录](../qualification/m0/summary.json)。这是固定版本的基础 smoke 记录，完整里程碑和工业资格状态仍为未完成。

## 验收与拒绝

`deep3d-cad build` 用指定目标检查实际生成的几何。独立 `--goal` 可以让候选参数和验收目标不同；不能把候选自报尺寸当作测量证据。

`deep3d-cad qualify` 构造六组真实几何：

| 用例 | 预期 |
| --- | --- |
| 标准法兰 | BREP/STEP 均通过 |
| 错安装孔径 | BREP/STEP 均拒绝 |
| 错孔位 | BREP/STEP 均拒绝 |
| 安装孔数量不足 | BREP/STEP 均拒绝 |
| 错厚度 | BREP/STEP 均拒绝 |
| 修改厚度/孔距后的新目标 | BREP/STEP 均通过 |

测试还覆盖真实内核失败、空布尔、盲孔、整体位移、错误缩放、多实体、游离边、反向实体、输入不变、JSON 边界、超时及既有输出保留。资格输出中的 negative case 显示 `observed=fail, status=pass`，表示检查器按预期拒绝，不能误读为错误模型合格。

测量报告中的保护带用于当前开发验收，不是经过独立校准的工业计量不确定性证明。验收器只接受该夹具限定的完整解析圆柱面和两个平面端面；拆分/近似曲面不猜测等价。STEP 自写自读不能替代客户 CAD 互操作。

## Linux 容器

镜像基于固定 digest 的 Python 3.12.12 slim-bookworm；Python 依赖由 `requirements.lock` 的哈希验证。no-VTK OCP wheel 仍需 OpenGL/X11 动态库，因此镜像显式安装运行依赖。当前 Debian apt 软件源未固定 snapshot，完整 OS 包版本与最终镜像 ID应随实测记录保存；尚不声称逐位可重建的生产供应链。

```powershell
uv build --wheel
docker build -f deploy/cad/Dockerfile -t deep3d-cad:m0 .
docker run --rm --network none --cpus 2 --memory 1g --pids-limit 128 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m --cap-drop ALL --security-opt no-new-privileges deep3d-cad:m0 python -m pytest -q -p no:cacheprovider
```

保存容器生成的完整产物（PowerShell）：

```powershell
New-Item -ItemType Directory -Path artifacts/linux -Force | Out-Null
$taskOut = (Resolve-Path -LiteralPath artifacts/linux).Path
docker run --rm --network none --cpus 2 --memory 1g --pids-limit 128 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m --cap-drop ALL --security-opt no-new-privileges --mount "type=bind,source=$taskOut,target=/output" deep3d-cad:m0 python -m deep3d_cad qualify --out /output/run-001
```

镜像默认非 root 用户。测试与建模时不需要网络；输出目录必须是新的。Docker 负责资源与进程限制，后续云端 Worker 还需落实对象存储前缀授权和发布权限隔离。

## 当前产物与下一步

运行命令后，`artifacts/` 包含真实 BREP、STEP、输入与验收报告；该目录不进入 Git。可复查的环境、执行范围与结果摘要放在 [qualification/m0/](../qualification/m0/README.md)。具体结果以实际生成的报告为准。

2026-10-27 已增加 [Rust 工程内核](rust-engineering-kernel.md)：保存真实的类型化 Feature DAG、稳定 ID、单分支版本和 `.deep3d` 原生包，支持重开编辑及逐 Feature OCCT 求值。本页的98项结果是0.1.0阶段的历史记录；新增能力以 M1 记录为准。数据库原子正式发布和云端长程运行时仍未完成。
