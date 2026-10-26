# 解析法兰基础验证记录

本目录是 M0/M1 子集的实际执行记录，**不是完整 M0、M1 或工业资格通过声明**。

[summary.json](summary.json)保存 Windows/Linux 各 98 项测试、Linux 六个真实几何 smoke 用例、依赖与源码哈希、最终镜像 ID 和系统包清单。实际几何从 BREP/STEP 重新加载后检查，验收器不读取构造过程的自报尺寸。

完整产物保存在本次工作区的 Git 忽略目录：

- [Linux 标准法兰 STEP](../../artifacts/linux/run-001/nominal/flange.step)
- [Linux 标准法兰 BREP](../../artifacts/linux/run-001/nominal/flange.brep)
- [STEP 测量报告](../../artifacts/linux/run-001/nominal/verify-step.json)
- [六组用例结果](../../artifacts/linux/run-001/qualification.json)
- [Windows 测试日志](../../artifacts/native-pytest.log)
- [Linux 测试日志](../../artifacts/linux-pytest.log)

克隆仓库不会包含忽略目录；可按 [CAD 执行模块说明](../../docs/cad-foundation.md)重建产物。已有目录受保护，复跑时选择新目录。

本次只建立解析法兰的开发验收保护带。未完成独立客户 CAD 接收、计量误差界校准、通用特征历史、Rust 工程内核、云端事务、长期 Agent、备份恢复与工业数据集资格。
