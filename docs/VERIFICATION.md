# 验证记录

## Garmin → Apple 健康：血压调查

2026-10-15，Python 3.15.0：577 项离线测试通过，无跳过；其中 27 项新增测试覆盖血压明细、UTC 跨日、脉搏缺省、多测量标志、重复冲突及异常响应。格式、静态检查和独立实现审查通过。

中国区账户近 30 天的只读查询返回 10 条手动血压测量，均含脉搏；解析模块对该响应验证通过。Mac 的 `HKHealthStore.isHealthDataAvailable()` 返回 false。手机 Connect 的血压授权项尚待核对；本切片只完成调查与解析，尚未写入 Apple 健康。

## 本次架构与工具调整

2026-10-14，Python 3.15.0：550 项离线测试通过，无跳过；Ruff 检查及 40 个 Python 文件格式检查通过，Markdown、Shell 和 Git 差异检查通过。测试使用临时目录、模拟 API 及固定 SDK，覆盖模块迁移、对账恢复、防重、归档与诊断。复核命令见 [CONTRIBUTING](../CONTRIBUTING.md)。

CI 已配置 Python 3.12 / 3.14，尚未取得远端运行结果；本次未执行真实 Garmin 操作、云端清理或后台注册。

## 历史设备验收：2026-09-29

Garmin 中国区完整历史替换并独立读回：Apple 与远端各 173 条，全部匹配、多余 0；核验体重、体脂率、BMI，瘦体重仅本地保留。私密证据在 state/backups/ 与 state/operations/。

真实归档后云端清理、LaunchAgent 注册/权限、iPhone 自动化以及迁移后设备行为仍待验收。
