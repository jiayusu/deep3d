# 项目协作规则

## 任务收尾时更新进度看板

用户要求用根目录 [progress.html](progress.html) 持续展示项目现状。每个实质开发、设计或验收任务结束前都必须完成以下工作：

1. 更新 [docs/progress.json](docs/progress.json)，记录本次任务的实际结果、依据与证据、未解决阻碍、下一步和实际更新时间。并行协作由收尾负责者集中更新，子任务向其提供事实。
2. 核对看板与 [docs/current-state.md](docs/current-state.md)、源代码及实际执行记录一致；任务改变了能力或验收结论时，同步修改对应状态文档。
3. 能力与验收判断只使用“通过／未通过／证据不足”，注明适用范围；不填主观完成百分比。工程回归不能冒充 G1～G7，未执行的验收不能记为通过，未启动的任务不能显示为后台运行。
4. 运行生成命令，再检查生成结果：

   ```powershell
   .venv\Scripts\python.exe scripts/update_progress.py
   .venv\Scripts\python.exe scripts/update_progress.py --check
   ```

5. 在已有提交或推送授权的范围内，将数据和生成的 HTML 一并纳入本次提交。最终回复提供看板入口。

`progress.html` 由 `docs/progress.json` 和 `docs/progress.template.html` 生成，不直接手改；需要调整页面时修改模板后重新生成。没有实际工作发生的纯问答不伪造新进度记录。此规则是任务收尾流程，不创建定时任务，也不表示存在后台监控进程。
