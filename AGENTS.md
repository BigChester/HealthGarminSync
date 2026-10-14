# 开发约定

- `master` 保存稳定版本，日常开发在 `develop`。需要隔离功能、复杂修复或并行开发时使用 `feat/*`，优先快进合并，完成后删除临时分支。
- 按独立切片提交，提交前完成相关验证。文档保留必要操作与结论，不提交生成日志，避免重复解释和警告。
- 根据任务类型、难度调用 subagents，默认使用 GPT-6.1 Sol：文档/小改动用低或中等推理力度，复杂对账/恢复用高力度；仅确有必要时使用 Astra。关键实现由独立 subagent 审查。

# 项目与验证

- Python 3.12+；入口 `python -m health_garmin_sync` 或 `health-garmin-sync`，业务模块位于 `health_garmin_sync/`，历史替换由 `history.py` 处理。
- 回归：`make test`；格式与静态检查：`make check`；Shell：`bash -n scripts/setup.sh health-garmin-sync`；提交检查：`git diff --check`。
- 开发测试使用临时目录和模拟 API。真实 Garmin 操作、源数据修改与后台任务注册按用户明确需求执行。
- `config.json`、`state/`、`.venv/`、凭据不进入 Git。未确定请求不自动重发，判断缺失使用完整称重明细，瘦体重仅本地保留。
- 验证记录区分离线测试与设备验收。
