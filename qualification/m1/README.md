# Rust 工程内核实测记录

日期：2026-10-27。范围：本地单分支工程内核与解析法兰。完整 M1 和工业资格仍未完成。

| 实验 | 结果 |
| --- | --- |
| Windows Rust | 23 项通过：10 领域、11 存储、2 项真实跨进程/OCCT 集成 |
| Windows Python | 122 项通过 |
| Linux Rust | 21 项领域/存储测试通过，CLI 构建成功 |
| Linux Python | 122 项通过 |
| Windows/Linux 完整工程演示 | 两边均通过：保存、重开、修改、重算、历史和便携导出 |
| Windows 包在 Linux 读取导出 | 通过，导出 STEP 字节一致 |

Linux 完整工程运行使用断网、非 root、只读根文件系统、2 CPU、1 GiB 内存和128 PID 上限。Rust 编译和取依赖在单独的 Rust 构建容器中进行，未宣称其构建阶段断网。

实际测试记录、源码哈希、示例版本与镜像身份见 [summary.json](summary.json)。记录由 [record_kernel.py](../../scripts/record_kernel.py)根据已完成的日志与演示生成。

本次工作区可直接读取：

- [可继续编辑的原生工程](../../artifacts/m1/windows-project-demo/flange.deep3d)
- [原始 STEP](../../artifacts/m1/windows-project-demo/original.step)
- [修改厚度和孔距后的 STEP](../../artifacts/m1/windows-project-demo/edited.step)
- [演示报告](../../artifacts/m1/windows-project-demo/demo.json)
- [Linux 演示报告](../../artifacts/m1/linux-project-demo/demo.json)

`artifacts/` 不进入 Git，克隆后可按 [使用说明](../../docs/rust-engineering-kernel.md)重建。原生包包含两版定义、17 个 Feature 的输出、BREP/STEP、验证报告和引用闭包，导出不需要原求值目录或 Python。

测试证明的范围包括：稳定 ID、类型/单位/DAG、原子候选、幂等、并发 CAS、文件损坏拒绝、物化绑定，以及旧计算不覆盖新编辑。数据耐久性、跨机器协作、云端发布、客户 CAD 互操作和工业语料尚需后续资格验证。
