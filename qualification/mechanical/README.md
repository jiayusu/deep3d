# 0.3.0 机械 CAD 工程回归

观测日期：2026-10-27。范围：当前支持域内的参数化源、草图/实体操作、刚性装配、独立重开检查与存储绑定。这不是冻结的 V0.1 验收，不能计入 G1～G7、客户 CAD 互操作或工业资格。

| 检查 | 实际结果与边界 |
| --- | --- |
| Windows Rust workspace | 51 项通过，包含真实 PostgreSQL 的 13 项运行时检查；随后修改证据绑定，单独复查全部 22 项存储测试通过 |
| Windows CAD 最终集成 | 32 项通过：`test_mechanical_project.py` 与 `test_project_worker.py`；包含 3 种源、无 Agent 参数编辑、历史保留、中文名称及错误目标/干涉拒绝 |
| Linux CAD 隔离回归 | 108 项通过：实体操作、草图、内核与原有法兰验证器；未在此容器执行 Rust 原生工程集成 |
| 静态检查与安装 | workspace clippy `-D warnings`、Rust 格式、Python Ruff 检查与本轮修改文件格式通过；锁定依赖 wheel 和 CAD 镜像构建成功 |
| 验收输入设计 | 4 份 JSON Schema 有效，草案符合 schema，10/20/5 案例 ID 集合和新增文档链接有效；没有冻结或执行正式验收 |

Windows 使用 Python 3.12.9、Rust/Cargo 1.88.0，CPU 为 AMD Ryzen 7 8745H（8 核、16 逻辑处理器）。Linux 使用 Python 3.12.12 镜像，运行时断网、非 root、只读根文件系统，限制 2 CPU、1 GiB 内存、128 PID；这些不是 G4 性能测量。

最终本地 CAD 镜像：`sha256:4254d4ec6fc1d2529c08e08d415611237ad104565dc875974b3ee9e93f1d3a9e`。该镜像没有推送镜像仓库；第三方应按 Dockerfile 重建，不能推定本地镜像已公开可下载。

Linux 首轮失败的原因是 SolveSpace wheel 需要 `libatomic.so.1`。两个 CAD 执行 Dockerfile 已加入 `libatomic1` 和构建时导入检查，修复后才得到上面的 108 项通过。装配清单新增验证摘要绑定，存储反例确保篡改实例或 BOM 后不能沿用原验证报告。

复查命令（先完成 [开发环境](../../README.md) 的依赖安装；workspace 的数据库测试需隔离 PostgreSQL 和 `DEEP3D_TEST_DATABASE_URL`）：

```powershell
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo build -p deep3d-cli --locked
.venv\Scripts\python.exe -m pytest tests/test_mechanical_project.py tests/test_project_worker.py -q
uv build --wheel
docker build -f deploy/cad/Dockerfile -t deep3d-cad:mechanical .
docker run --rm --network none --cpus 2 --memory 1g --pids-limit 128 --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m --cap-drop ALL --security-opt no-new-privileges deep3d-cad:mechanical python -m pytest tests/test_mechanical_features.py tests/test_sketch.py tests/test_kernel.py tests/test_verifier.py -q -p no:cacheprovider
```

同一工作区的原始日志位于 `artifacts/cad-mechanical/`：`rust-windows.log`、`storage-final.log`、`native-final.log`、`python-linux-final.log` 和 `docker-build-latest.log`。它们不进入 Git；克隆后的复查应生成自己的日志。代码、回归源与复查方法已保存，不把本机记录称为另一开发者的干净环境复现。

工程能力与限制见 [CAD 能力范围](../../docs/cad-capabilities.md)，目标差距见 [当前状态](../../docs/current-state.md)。
