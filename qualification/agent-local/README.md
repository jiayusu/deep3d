# 本地 Agent 验证记录

`summary.json` 由 `scripts/record_agent.py` 从实际日志和演示产物生成，记录源码/日志哈希、平台测试范围、候选与 STEP 绑定。它将真实几何上的脚本模型执行与真实 LLM 请求分开记录。

当前线上模型请求因 HTTPS 服务认证/访问拒绝而停在 `waiting_provider`，尚无真实 LLM 成功交付记录。这不影响本地几何、候选隔离、CAS、幂等和恢复测试独立给出结果。工业资格与云端里程碑均未据此宣布完成。

复现 Windows：

```powershell
uv sync --frozen
.\scripts\cargo.ps1 build --workspace --locked
.\scripts\cargo.ps1 test --workspace --locked
uv run --frozen pytest -q
.venv\Scripts\python.exe scripts/demo_agent.py --scripted --out artifacts/agent-offline-001
```

Linux 使用 Python 3.12、相同 OCP 锁和 Rust 1.88，构建 `target/debug/deep3d` 后执行同一套测试/演示。记录中的 Linux Rust 范围是 domain/storage 单元与工程测试，Python 范围是 Agent 传输/真实 CAD 集成测试；Windows 为全量套件。全部测试均有明确范围，未把跳过真实 CLI 的用例计作通过。

原始日志、`.deep3d`、STEP、Run 状态和模型消息保留在本机 `artifacts/agent-local/`，没有上传到仓库。记录中的源码摘要指实际受测工作树文件字节，换平台换行格式可能不同；工程内的几何验证器摘要始终绑定实际运行文件。
