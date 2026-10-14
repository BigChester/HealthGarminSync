# 验证记录

## 本次架构与工具调整

2026-10-14，Python 3.15.0：550 项离线测试通过，无跳过；Ruff 检查及 40 个 Python 文件格式检查通过，Markdown、Shell 和 Git 差异检查通过。测试使用临时目录、模拟 API 及固定 SDK，覆盖模块迁移、对账恢复、防重、归档与诊断。复核命令见 [CONTRIBUTING](../CONTRIBUTING.md)。

CI 已配置 Python 3.12 / 3.14，尚未取得远端运行结果；本次未执行真实 Garmin 操作、云端清理或后台注册。

## 历史设备验收：2026-09-29

Garmin 中国区完整历史替换并独立读回：Apple 与远端各 173 条，全部匹配、多余 0；核验体重、体脂率、BMI，瘦体重仅本地保留。私密证据在 state/backups/ 与 state/operations/。

真实归档后云端清理、LaunchAgent 注册/权限、iPhone 自动化以及迁移后设备行为仍待验收。
