# 架构与运行边界

`health_garmin_sync` 是本地 Python 包，`.venv/bin/python -m health_garmin_sync` 调用 CLI，根目录 `health-garmin-sync` 仅负责选择项目虚拟环境。LaunchAgent 定时运行 `python -m health_garmin_sync.background`，一次调用完成后退出；项目没有常驻 HTTP 服务。

## 模块职责与依赖

| 模块           | 职责                                             |
| -------------- | ------------------------------------------------ |
| runtime        | 项目路径、运行环境、时钟、配置加载与校验         |
| domain         | Sample / Measurement、错误、时间与来源模型       |
| blood_pressure | Garmin 血压及测量脉搏的独立明细解析与 UTC 规范化 |
| file_io        | 原子文件写入、权限和日志持久化辅助               |
| exports        | JSON 解码、单位转换、去重与指标配对              |
| state          | 测量状态 SQLite、未确定请求、操作日志与上下文    |
| remote         | Garmin 完整称重响应解析与匹配                    |
| garmin         | 登录、令牌持久化、SDK 边界与 FIT 时间处理        |
| source_archive | 原始字节归档、核验、隔离清理及恢复导出           |
| sources        | 合并 inbox / 归档并加载测量                      |
| reconciliation | 远端快照对账、清单与复核范围                     |
| processing     | 单条提交、读回核验与防重                         |
| operations     | 指定修复、删除、核对及中断恢复                   |
| history        | 完整历史范围替换与备份                           |
| sync           | 预览、同步轮次、运行摘要与归档协调               |
| launchagent    | macOS 任务定义、注册、卸载和状态查询             |
| diagnostics    | 离线只读诊断                                     |
| background     | 后台日志入口及主程序调用证据                     |
| cli            | 命令解析、确认、配置操作和进程锁调度             |

依赖从入口与协调层流向数据模型、存储和外部接口。`exports` 不读取归档；`sources` 组合导出解析和归档访问，避免解析与归档互相依赖。Garmin 网络请求集中在客户端边界，诊断不登录远端。历史替换和单条恢复复用对账、状态与持久化规则。

## 路径、锁与持久状态

runtime 以项目位置定位配置、state、令牌、归档与日志，源码迁移不改变已有格式。inbox 使用绝对路径，归档按来源目录隔离。移动部署前卸载任务，移动后重建环境并注册路径。

CLI 与定时入口复用同步逻辑；state/run.lock 串行写入，定时 run 遇锁跳过，显式忙碌命令返回 2。doctor 只读现有证据，可在配置损坏或锁占用时运行。

提交前持久保存 inflight 和请求证据，忽略任务或导出消失不抹掉未知请求。修复/删除先备份并记阶段，未完成操作阻止自动补传，恢复不重复删除。完整远端明细及人工确认解除未知请求保护。

归档先提交原始字节、元数据与摘要并读回校验，再确认云端文件未变后清理；中断保留隔离证据供恢复。归档继续参与历史核对，未确定/未完成操作或未核验样本阻止清理。

命令见 [操作与恢复](OPERATIONS.md)，验证边界见 [验证记录](VERIFICATION.md)。
