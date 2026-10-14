# 对账、恢复与维护

本文命令在项目目录运行。`记录ID`、日期和清单文件名都需替换为实际值；写入命令先展示目标，按提示输入确认词。日常启用、安装与单条试传见 [README](../README.md)。

## Garmin 血压与 Apple 健康

`0.1.0` 的同步方向是 Apple 健康 → Garmin，处理体重、体脂率和 BMI。Garmin 血压及其脉搏属于独立接口，不会经过这条称重链路。

先在 iPhone「健康 → 共享 → App → Connect」检查收缩压、舒张压和心率写入权限，保持 Garmin Connect 在前台完成同步，再核对授权后的一次新测量。新测量与历史回填分别检查。

Garmin 官方公布的 Apple 健康共享清单未列血压；其论坛有 Index BPM 用户确认开启收缩压、舒张压后可以同步新测量。因此，需根据本机授权项、测量来源和实际新测量判断支持情况。手动录入血压所带的 `pulse` 也不能直接视为 Garmin 全天心率流。

本机已确认 Connect 没有收缩压、舒张压写入开关，当前原生路径无法获得血压写入授权；使用下述导入端完成补同步。

Garmin 中国区客户端可通过 `get_blood_pressure` 查询血压明细。读取时使用 `measurementSummaries[].measurements[]`，保留收缩压、舒张压、可选脉搏与原始时间；日最高、最低值不代表单次测量。`measurementTimestampGMT` 表示 UTC，不能按 Mac 本地时区解释。

### 安装 iPhone 导入端

Mac CLI 负责读取与发布，写入健康由 `ios/GarminHealthBridge/App/GarminHealthBridge.xcodeproj` 中的 App 完成。使用 Xcode 16+ 打开工程，选择自己的签名 Team 和连接的 iPhone，运行 `GarminHealthBridge`；最低 iOS 17。App 使用系统文件选择器访问 iCloud，不保存 Garmin 凭据。

在 Mac 项目目录读取指定历史范围：

```bash
./health-garmin-sync export-health \
  --since 2026-09-01 --until 2026-10-15 \
  --outbox "/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/garmin-to-health"
```

日期替换为实际需要的范围，交接目录须独立于称重 inbox。此命令只读 Garmin；iPhone 尚未导入时，重复执行保留当前批次。

在 iPhone App 中选择同一 `Health/garmin-to-health` **目录**，核对测量后点击「授权并同步到健康」。允许读取已有收缩压、舒张压和心率供查重，并允许写入。App 将血压组合成一条关联测量，把脉搏保存为同一时刻的心率；健康数据来源显示为本导入 App。

全部写入并读回匹配后，App 发布回执。下一次 Mac 导出消费回执并发布下一批。单批最多 100 条；云端只有当前 `pending.json` 与 `receipt.json`，已确认文件移至同卷本地私密目录校验后清理。完整 SDK 响应、本地队列和确认版本永久保存在 `state/garmin_health.sqlite3`。

### 定时导出与 iPhone 自动化

```bash
./health-garmin-sync settings --health-outbox "/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/garmin-to-health"
./health-garmin-sync enable-health  # 输入 ENABLE HEALTH
./health-garmin-sync run
./health-garmin-sync install-agent
./health-garmin-sync status
```

血压导出可独立于称重启用，无需先试传体重。首次扫描配置起点至中国区今天，之后覆盖未扫描的日期并重读近七天。Mac 每 30 分钟检查回执和新数据；iPhone 需解锁、健康授权有效且云文件已下载。App 可选择「打开 App 时自动同步」，或在快捷指令自动化中调用「同步 Garmin 血压」动作；该动作会打开 App。

`disable-health` 只停血压定时导出；`disable` 停止两向自动处理；`export-health` 是显式执行，不受自动开关影响。更改交接目录前先停自动处理。相同来源键的修订使用更高版本更新本 App 写入的数据，缺省脉搏不推算；删除 Garmin 记录不联动删除健康，来源键变化的修改按新测量处理。其他 App 的记录保留，冲突留待核对。

最近导出摘要为 `state/last_health_export.json`。Mac 收到的回执代表 iPhone 导入端报告成功，设备验收在健康 App 中核对时间、血压及脉搏。

来源见 [Garmin → Apple 健康](SOURCES.md#garmin--apple-%E5%81%A5%E5%BA%B7)。

## 状态与未确定请求

pending 待比对；verified / verified_manual 为已读回 / 人工确认；conflict / verified_conflict 为差异或歧义；remote_missing 曾验证后缺失；repair_uncertain 有未完成修复；uncertain / inflight 为结果未知 / 已持久准备提交；ignored 保留人工跳过决定。

### 核对后再决定

先查询完整明细并在 Garmin 核对：

```bash
./health-garmin-sync remote 2026-09-29
./health-garmin-sync status
```

`remote` 只读，结果保存到 `state/remote-日期.json`。只有确定结果后才选择以下一种处理：

```bash
./health-garmin-sync resolve 记录ID --present  # 输入 PRESENT，仅记录人工确认
./health-garmin-sync resolve 记录ID --absent   # 输入 ABSENT，允许之后补传
./health-garmin-sync resolve 记录ID --ignore   # 跳过本地任务
```

`resolve` 不修改 Garmin。结果仍不确定时先等待、查询；不要重发请求或删除 SQLite。存在未完成修复日志时，使用本文操作恢复流程。

已发出但结果未确定的请求被忽略后，仍保留防重复提交记录；核对后用 `PRESENT` 或 `ABSENT` 确认结果。

## 对账、修复与历史整理

### 只读查看差异

```bash
./health-garmin-sync compare
./health-garmin-sync compare --since 2020-01-01 --until 2026-09-29
```

`compare` 联网只读，日期不改变配置。完整历史放独立目录，用 `--inbox "完整路径"` 指定，避免混入已修订的旧导出。

| 分类           | 含义与处理                             |
| -------------- | -------------------------------------- |
| matched        | 时间及 Apple 提供的字段一致，跳过写入  |
| missing        | 首次缺失，启用同步后新增               |
| repair         | 同次测量缺体脂/BMI，指定记录修复       |
| replace        | 同次测量值不同，指定记录替换           |
| conflict       | 时间、来源或远端字段有歧义，停止该条   |
| review_missing | 以前验证的记录消失，人工核对后补传     |
| uncertain      | 结果未确定，继续只读核对               |
| ignored        | 保留人工忽略决定                       |
| EXTRA          | 选定日期内的远端多余记录，自动同步保留 |

手动清单保存到 `state/plans/compare-时间戳.json`；同步清单覆盖保存到 `state/plans/sync-latest.json`，最近摘要在 `state/last_compare.json`。匹配按绝对时间（允许 1 秒编码误差）和数值：体重/体脂误差 0.021，BMI 误差 0.11。同日多次称重分别核对；60 秒内无法唯一对应时为 conflict。判断缺失使用完整称重明细，不能用每日最后值代替。

### 修复指定记录

```bash
./health-garmin-sync repair-one Apple记录ID --plan state/plans/实际清单.json
# 核对原记录和目标值，输入 REPLACE 记录ID
```

删除前检查重建条件，随后备份、删除指定记录、确认删除、重建并读回核验。执行前重新查询，双方值变化需重新生成清单。备份在 `state/backups/时间戳/`，持久操作日志在 `state/operations/`；路径已占用时停止，不覆盖恢复证据。Apple 未提供的原有体脂和 BMI 会保留；原记录含无法保留的其他身体成分字段时拒绝删除。

### 删除指定多余记录

前提是已完整导出选定日期内全部需要保留的 Apple 来源测量。

```bash
./health-garmin-sync delete-extra Garmin记录ID \
  --plan state/plans/实际清单.json --complete-export
# 核对记录，输入 DELETE Garmin记录ID
```

匹配及歧义范围内的记录不会成为可删除 EXTRA。导出未覆盖的日期不能视为需要清理。

### 以完整导出替换历史范围

先完整导出全部需要保留的 PICOOC 测量，选择实际起始日期生成清单：

```bash
./health-garmin-sync compare --since 2024-09-04
./health-garmin-sync replace-all --plan state/plans/实际清单.json --complete-export
# 核对目标、保留、删除及新增/重建数量，输入 REPLACE ALL
```

以选定范围 Apple 数据为准，保留匹配项、删除其余并重建目标，包括近时刻但时间不同的记录；本地配对/时间有歧义时拒绝整批操作。

先复核最新数据，备份 Garmin 响应、SQLite、配置、目标及原始导出到 `state/backups/full-时间戳/`。逐项删除/新增后核验全部目标、总数及重复；不受单次数量限制，不启用自动同步。

### 延迟读回与中断恢复

超时、读回未匹配或数据变化会暂停操作并保存日志；未完成日志阻止自动补传。先只读核对：

```bash
./health-garmin-sync verify-operation state/operations/实际操作.json
```

输出区分本次核验结果与历史操作阶段；匹配返回 0，未匹配或查询失败返回 2。查询失败也会记录本次未通过，供 `doctor` 排查。

仅在已确认目标缺失、原记录已删除时，恢复已备份的单条修复：

```bash
./health-garmin-sync resume-operation state/operations/实际操作.json
# 输入 RESUME Apple记录ID；不会再次删除
```

整批替换日志只能用 `verify-operation` 核对，不能用 `resume-operation` 重跑整批删除。`remote_missing` 经人工核对后用 `resolve 记录ID --absent`，输入 `ABSENT` 允许补传；有未完成修复时须先处理操作日志。

## 归档与备份

归档开关和恢复命令见 [README](../README.md#%E6%97%A5%E5%B8%B8%E5%91%BD%E4%BB%A4%E4%B8%8E%E6%95%B0%E6%8D%AE%E4%BF%9D%E7%95%99)。归档永久保留 JSON 原始字节、文件名、目录、导出时间及 SHA-256。只有同步范围内全部样本唯一对应已核验测量才清理；待上传、缺字段、孤立或歧义样本继续保留，未确定请求和未完成操作暂停清理。预览、失败或等待导出合并的轮次不清理，源文件变化或清理失败保留数据供重试。

归档按来源目录隔离，独立 `--inbox` 仅读取该目录数据。恢复到新目录保持原始字节，同名不同内容使用摘要后缀；完整历史替换备份也包含归档导出。备份完整 state 与配置，云端清理后的原始历史由本地归档保留。

## 诊断与后台运行

```bash
./health-garmin-sync doctor
./health-garmin-sync doctor --json
./health-garmin-sync status
tail -n 80 state/sync.log
tail -n 80 state/agent.out.log
tail -n 80 state/agent.err.log
```

`doctor` 离线只读检查环境、配置、导出、进程锁、运行证据和 LaunchAgent；配置损坏或锁被占用时也可运行。错误返回 2，冲突、待修复和远端缺失为提醒。归档检查只看数据库文件可访问性，内容校验用 `preview`。虚拟环境缺失时，用 Python 3.12+ 执行 `python3 -m health_garmin_sync doctor`。

| 证据                          | 用途                                            |
| ----------------------------- | ----------------------------------------------- |
| state/last_attempt.json       | 最近取得锁的尝试；未结束时核查进程              |
| state/last_run.json           | 最近完成的同步摘要与未确定请求计数              |
| state/last_agent_attempt.json | 后台入口日志/调用阶段；启用后超过两小时提示检查 |

证据时间超前本机十分钟以上报错；导出更新或数量增加不表示远端缺失，用 compare 核对。sync.log 自动轮换；日志失败会报错但保留已保存状态。后台日志每份最多 1 MB、保留 3 份备份。status 查询 LaunchAgent 限时 5 秒，失败返回 2。

| 现象                          | 排查                                                 |
| ----------------------------- | ---------------------------------------------------- |
| 没有 JSON / 没有 preview 记录 | 检查 iCloud 下载、since、来源、类型及健康权限        |
| metrics / samples 成了字符串  | 保持词典/列表，仅保存前转完整 Payload 为文本         |
| WAIT / 后到字段               | 核对候选时间，使用 compare / repair-one              |
| 未来时间                      | 检查 at、exported_at、时区及手机时间                 |
| settings 被拒绝               | 先 disable；enabled 使用 JSON 布尔值                 |
| 命令忙碌返回 2                | 等锁释放；定时 run 遇锁跳过并返回 0                  |
| 401 / 403 / 验证码 / 令牌失败 | 检查网络、账号、state/tokens 权限与磁盘，重新 login  |
| 429 / 上传超时                | 暂停重试，按未确定请求流程只读核对                   |
| 手动成功、后台失败            | 检查登录用户、解释器、下载与权限，查看 agent.err.log |
| 卸载任务失败                  | doctor 核对服务；未确认卸载时保留任务文件            |

先手动 `./health-garmin-sync preview` 接受目录授权。后台 Python 可能需要单独的 macOS 文件访问授权，实际解释器可这样确认：

```bash
./.venv/bin/python -c 'import os,sys; print(os.path.realpath(sys.executable))'
```

在系统设置 → 隐私与安全性 → 文件与文件夹检查进程授权。确被 TCC 拒绝且无更窄权限时，可为实际 Python 授予完全磁盘访问权限（也覆盖其他脚本）。授权后重装并验收任务，解释器更新后重查。

Mac 睡眠、注销或关机期间暂停处理。移动项目前卸载任务，移动后重建虚拟环境并重新安装。首次安装可能遇到进程锁，后续仍会按间隔运行，可手动 `./health-garmin-sync run` 验证。
