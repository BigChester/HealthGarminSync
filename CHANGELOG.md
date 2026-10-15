# Changelog

按 [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) 记录用户可见变化。

## [Unreleased]

## [0.2.0] - 2026-10-15

### Added

- Garmin 血压与测量脉搏到 Apple 健康的历史回填、独立定时导出与 iPhone 导入端。
- 本地响应归档、有界云端批次、回执确认与版本恢复。

## [0.1.0] - 2026-10-15

### Added

- 将 PICOOC 的 Apple 健康 JSON 同步至 Garmin 中国区，支持体重、体脂率和 BMI；瘦体重仅本地保留。
- 测量对账、单条修复、历史替换及中断恢复。
- 原始导出的本地归档、恢复与同步后云端清理。
- `health-garmin-sync` CLI、macOS 定时同步与离线诊断。
- 模块化 Python 实现、Ruff / mdformat 格式检查、离线 CI 与贡献说明。

[0.1.0]: https://github.com/BigChester/HealthGarminSync/releases/tag/v0.1.0
[0.2.0]: https://github.com/BigChester/HealthGarminSync/compare/v0.1.0...v0.2.0
[unreleased]: https://github.com/BigChester/HealthGarminSync/compare/v0.2.0...develop
