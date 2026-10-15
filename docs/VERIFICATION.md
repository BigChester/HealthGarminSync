# 验证记录

## Garmin → Apple 健康：血压与脉搏

2026-10-15，Python 3.15.0：640 项离线测试通过。Swift 37 项测试及 Python → Swift → Python 批次、回执、恢复测试通过，覆盖血压关联、同次脉搏、重复导入、修订、权限拒绝、部分写入、回执核验、永久归档与云端清理。格式、静态检查和独立实现审查通过。

中国区账户近 30 天的只读查询返回 10 条手动血压测量，均含脉搏；真实按日查询确认 UTC 跨日记录按本地日期返回。用户确认手机 Connect 没有收缩压、舒张压写入开关，当前原生路径无法授权写入血压。Mac 的 `HKHealthStore.isHealthDataAvailable()` 返回 false，写入端由 iPhone 应用提供。

本地 Apple SDK 类型检查、HealthKit 样本构造测试通过。[六项 CI](https://github.com/BigChester/HealthGarminSync/actions/runs/38662957778) 全部通过，包括四项 Python 回归、Xcode 16.4 的 Swift 与跨语言测试、iOS 18.5 模拟器启动及首屏截图，以及最新 Apple SDK 构建。本轮未向真实 Apple 健康写入数据；iPhone 签名安装、健康授权与实际导入仍需设备验收。复核命令见 [CONTRIBUTING](../CONTRIBUTING.md)。

## 0.1.0 架构与本地部署

2026-10-14，Python 3.15.0：550 项离线测试通过，无跳过；Ruff 检查及 40 个 Python 文件格式检查通过，Markdown、Shell 和 Git 差异检查通过。测试使用临时目录、模拟 API 及固定 SDK，覆盖模块迁移、对账恢复、防重、归档与诊断。复核命令见 [CONTRIBUTING](../CONTRIBUTING.md)。

Python 3.12 / 3.14 的 Ubuntu、macOS 四项 CI 均通过。本地服务按 30 分钟运行，真实同步独立核验 186 条测量，永久归档 19 份原始 JSON 并完成对应云端清理；数据库校验及诊断通过。

## 历史设备验收：2026-09-29

Garmin 中国区完整历史替换并独立读回：Apple 与远端各 173 条，全部匹配、多余 0；核验体重、体脂率、BMI，瘦体重仅本地保留。私密证据在 state/backups/ 与 state/operations/。
