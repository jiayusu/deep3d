# CAD 执行依赖与许可发布门槛

本文记录来源事实与待解决项，不提供完整法律合规结论，也不替根项目选择许可证。

| 组件 | 锁定版本 | 来源与用途 |
| --- | --- | --- |
| `cadquery-ocp-novtk` | 8.0.1.0.0 | OCP/OCCT Python 绑定；真实 B-Rep、布尔、曲面、STEP/XCAF |
| `cadquery-ocp-proxy` | 8.0.1.0.0 | 绑定所需的依赖包，版本与哈希见 `uv.lock` |
| `slvs` | 3.3（安装元数据写作 `v3.3`） | SolveSpace 求解器 Python 绑定；有限参数化草图 |

Python 发布文件的精确 SHA-256 由 `uv.lock` 和 `requirements.lock` 固定。Rust 依赖由 `Cargo.lock` 固定；锁定版本与可安装并不等于已完成全部第三方分发义务。

本机 `cadquery-ocp-novtk` 元数据声明包装包 Apache-2.0；底层 OCCT 与所含组件仍有各自许可，不能把包装层标注推定为所有底层代码的许可。

`slvs` 的 PyPI 元数据标注 MIT，而包说明及 SolveSpace 上游明确指向 GPLv3 或更新版本。这项差异需要在发布前核实，不能忽略。[slvs 3.3 发布信息](https://pypi.org/project/slvs/3.3/)、[SolveSpace 库说明](https://solvespace.com/library.pl)、[上游 COPYING](https://github.com/solvespace/solvespace/blob/master/COPYING.txt)

因此当前 S7/G7 的许可部分尚未满足：项目所有者还需确定根项目许可、CAD/草图执行模块的分发边界及第三方源码/NOTICE 提供方式。现有代码和记录没有宣称整套依赖已获得统一 MIT/Apache 许可。
