# HealthGarminSync：PICOOC → Apple 健康 → Garmin 中国区

iPhone 快捷指令将 Apple 健康中的 PICOOC 测量导出到 iCloud Drive，Mac 本地 CLI 每 30 分钟检查并同步到 Garmin 中国区。合并 JSON 使用 `schema_version=2`，包含体重、体脂率、可选 BMI 和瘦体重（lbm）；瘦体重仅本地保存，不映射为 Garmin 肌肉量。兼容既有 `schema_version=1` 导出和记录 ID。

开发回归与历史设备验收见 [TEST_REPORT.md](TEST_REPORT.md)。iPhone 搭建步骤见 [SHORTCUTS.md](SHORTCUTS.md)，本包不含可导入的 `.shortcut`。

## 1. 准备手机和共享目录

1. 确认 PICOOC 已将体重、体脂率写入 Apple 健康。
2. iPhone 与 Mac 使用同一 iCloud 账户，并开启 iCloud Drive。
3. 在 iPhone 文件 App 中选择 iCloud Drive → 快捷指令（Shortcuts）→ Health → inbox，作为总快捷指令的保存目录。
4. 按 [SHORTCUTS.md](SHORTCUTS.md) 搭建并手动运行总快捷指令。
5. 在 Mac Finder 中确认 inbox 出现可打开的 `health-时间戳.json`，包含 `schema_version=2` 和 `metrics` 词典；对目录选择“保留下载”（系统提供时）。

当前 Mac 的 inbox 路径如下；其他 Mac 请使用实际绝对路径。路径含空格，终端中需用引号包住：

```text
/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/inbox
```

## 2. 在 Mac 安装

解压到本地非 iCloud 同步目录，例如 `~/Applications/HealthGarminSync`。默认下载目录下已解压时可执行：

```bash
mkdir -p "$HOME/Applications"
# 确认目的目录为空，避免覆盖现有配置和运行状态。
ditto "$HOME/Downloads/HealthGarminSync" "$HOME/Applications/HealthGarminSync"
cd "$HOME/Applications/HealthGarminSync"
brew install python@3.12
bash setup.sh
```

已有 Python 3.12+ 可跳过 `brew install`。`setup.sh` 创建 `.venv/`，安装固定 `garminconnect==0.3.16` 和 `curl_cffi`，运行离线测试并保存 `requirements.lock.txt`。找不到 Homebrew 时按其官方说明安装或恢复 shell 环境；依赖升级需手动验证。

主要文件：`hg` 为命令入口，`health_garmin.py` 为主程序，`agent_runner.py` 为后台日志入口，`replace_history.py` 处理完整历史替换，`tests/` 为离线测试，`samples/health.example.json` 为禁止上传的演示数据。

运行生成的 `config.json`、`state/`、`.venv/` 和依赖锁文件不随包交付。`state/tokens/`、备份及导出含凭据或健康数据，应留在本机并保持私密。

## 3. 初始化配置

```bash
./hg init --inbox "/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/inbox"
```

省略 `--inbox` 时会选择目录。按提示填写上传起始日期（默认 Mac 当天）和 Apple 健康中的准确来源名（默认 PICOOC）。起始日期以前的记录不会上传。也可显式指定日期：

```bash
./hg init --inbox "/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/inbox" --since 2026-09-29
```

| 配置 | 默认值 | 用途 |
|---|---:|---|
| is_cn | true | Garmin 中国区 |
| enabled | false | 自动写入关闭 |
| weight_bare_unit | kg | 裸体重数字按公斤 |
| fat_bare_unit | percent | 裸体脂数字按百分数 |
| pair_window_seconds | 0 | 仅配对相同测量时刻 |
| settle_seconds | 120 | 自动处理等待批次合并两分钟；0 关闭等待 |
| max_records_per_run | 10 | 单次处理数量上限 |

已有配置用 `settings` 修改；启用后须先 `./hg disable`，修改完重新预览。例如更新导出目录：

```bash
./hg settings --inbox "完整路径"
```

## 4. 检查导出和单位

```bash
./hg inspect
./hg preview
```

两者均不联网：`inspect` 显示原始样本，`preview` 校验、配对并显示记录 ID、时间、公斤体重及可选指标。对照 Apple 健康核对数值、来源和原始测量时间。

- `23.5%` 自动识别为百分数；裸数字 `23.5` 对应 23.5% 时保留 percent。
- 裸数字 `0.235` 对应 23.5% 时使用 fraction。
- 裸体重为磅且无单位后缀时使用 lb。

```bash
./hg settings --fat-unit fraction
./hg settings --fat-unit percent   # 恢复百分数模式
./hg settings --weight-unit lb
./hg settings --source 'PICOOC'
./hg preview
```

上述设置按实际输出选择。`WAIT` 表示体脂或 BMI 配对有歧义；没有体脂样本可形成纯体重测量。确认同次测量的指标存在不超过一分钟的写入偏差时，可调整：

```bash
./hg settings --pair-window 60
./hg preview
```

窗口内有多个候选仍拒绝配对。复制要试传的真实测量的 16 位 ID。

## 5. 登录并试传一条

```bash
./hg login
./hg preview
./hg upload-one 这里替换为预览中的16位ID
```

`login` 在终端输入中国区邮箱、密码和所需验证码，只登录，不上传测量。密码不保存到配置；可恢复令牌保存到 `state/tokens/` 后才报告成功。

`upload-one` 显示目标数据，输入 `UPLOAD` 才写入。它先查完整远端明细，跳过已有匹配记录，提交后读回核验。首次试传后在 Garmin Connect 中国区人工核对时间、体重、体脂率和 BMI。同次已有纯体重记录不会自动覆盖补字段，需按下文修复。

| 状态 | 含义与处理 |
|---|---|
| pending | 尚未提交，仍需远端比对 |
| verified | 远端读回匹配，包括原本已存在的记录 |
| verified_manual | 已人工确认，程序记录该决定 |
| conflict / verified_conflict | 值不同、缺字段或对应有歧义，用 compare 查看 |
| remote_missing | 以前已验证、现已缺失，人工核对后允许补传 |
| repair_uncertain | 有未完成修复，用 verify-operation 核对 |
| uncertain | 请求结果未确定，暂停重复提交 |
| inflight | 提交前持久状态，中断后按 uncertain 处理 |
| ignored | 已人工跳过 |

### 未确定结果的处理

先查询完整明细并在 Garmin 核对：

```bash
./hg remote 2026-09-29
./hg status
```

`remote` 只读，结果保存到 `state/remote-日期.json`。只有确定结果后才选择以下一种处理：

```bash
./hg resolve 这里替换为ID --present  # 输入 PRESENT，仅记录人工确认
./hg resolve 这里替换为ID --absent   # 输入 ABSENT，允许之后补传
./hg resolve 这里替换为ID --ignore   # 跳过本地任务
```

`resolve` 不修改 Garmin。结果仍不确定时先等待、查询；不要重发请求或删除 SQLite。存在未完成修复日志时，使用下文操作恢复流程。

## 6. 对账、修复与历史整理

### 只读查看差异

```bash
./hg compare
./hg compare --since 2020-01-01 --until 2026-09-29
```

`compare` 登录查询 Garmin，不写入；日期仅影响本次对账，不改变配置。它比对已到达 inbox 的 Apple 数据。完整历史整理应把完整导出放在独立目录，用 `--inbox "完整路径"` 指定，避免混入 Apple 已修订或删除的旧导出。

| 分类 | 含义与处理 |
|---|---|
| matched | 时间及 Apple 提供的字段一致，跳过写入 |
| missing | 首次缺失，启用同步后新增 |
| repair | 同次测量缺体脂/BMI，指定记录修复 |
| replace | 同次测量值不同，指定记录替换 |
| conflict | 时间、来源或远端字段有歧义，停止该条 |
| review_missing | 以前验证的记录消失，人工核对后补传 |
| uncertain | 结果未确定，继续只读核对 |
| ignored | 保留人工忽略决定 |
| EXTRA | 选定日期内的远端多余记录，自动同步保留 |

手动清单保存到 `state/plans/compare-时间戳.json`；同步清单覆盖保存到 `state/plans/sync-latest.json`，最近摘要在 `state/last_compare.json`。匹配按绝对时间（允许 1 秒编码误差）和数值：体重/体脂误差 0.021，BMI 误差 0.11。同日多次称重分别核对；60 秒内无法唯一对应时为 conflict。判断缺失使用完整称重明细，不能用每日最后值代替。

### 修复指定记录

```bash
./hg repair-one 这里替换为Apple记录ID --plan state/plans/这里替换为清单文件名.json
# 核对原记录和目标值，输入 REPLACE 记录ID
```

删除前检查重建条件，随后备份、删除指定记录、确认删除、重建并读回核验。执行前重新查询，双方值变化需重新生成清单。备份在 `state/backups/时间戳/`，持久操作日志在 `state/operations/`。Apple 未提供的原有体脂和 BMI 会保留；原记录含无法保留的其他身体成分字段时拒绝删除。

### 删除指定多余记录

前提是已完整导出选定日期内全部需要保留的 Apple 来源测量。

```bash
./hg delete-extra 这里替换为Garmin记录ID \
  --plan state/plans/这里替换为清单文件名.json --complete-export
# 核对记录，输入 DELETE Garmin记录ID
```

匹配及歧义范围内的记录不会成为可删除 EXTRA。导出未覆盖的日期不能视为需要清理。

### 以完整导出替换历史范围

先完整导出全部需要保留的 PICOOC 测量，选择实际起始日期生成清单：

```bash
./hg compare --since 2024-09-04
./hg replace-all --plan state/plans/本次实际清单文件名.json --complete-export
# 核对目标、保留、删除及新增/重建数量，输入 REPLACE ALL
```

该命令以选定日期范围的 Apple 数据为准，保留匹配项、删除其余 Garmin 记录并新增或重建目标；包括清理近时刻但时间不同的旧记录。来源配对或本地测量时间有歧义时拒绝整批操作。

执行前复核最新数据，备份全范围 Garmin 响应、SQLite、配置、目标及原始导出到 `state/backups/full-时间戳/`。逐次确认删除和新增，最后读回要求全部目标匹配、远端总数一致且无重复。此命令不受日常单次处理数量限制，也不启用自动同步。

### 延迟读回与中断恢复

超时、读回未匹配或数据变化会暂停操作并保存日志；未完成日志阻止自动补传。先只读核对：

```bash
./hg verify-operation state/operations/这里替换为操作记录文件名.json
```

输出区分本次核验结果与历史操作阶段；匹配返回 0，未匹配或查询失败返回 2。查询失败也会记录本次未通过，供 `doctor` 排查。

仅在已确认目标缺失、原记录已删除时，恢复已备份的单条修复：

```bash
./hg resume-operation state/operations/这里替换为操作记录文件名.json
# 输入 RESUME Apple记录ID；不会再次删除
```

整批替换日志只能用 `verify-operation` 核对，不能用 `resume-operation` 重跑整批删除。`remote_missing` 经人工核对后用 `resolve 记录ID --absent`，输入 `ABSENT` 允许补传；有未完成修复时须先处理操作日志。

## 7. 开启自动处理

至少成功核对一条后执行：

```bash
./hg enable                        # 核对预览，输入 ENABLE
./hg run
./hg install-agent
./hg status
```

当前用户的 LaunchAgent 每 30 分钟运行。首次复核配置起始日后的全部 Apple 测量，以后复核近 30 天及更早的待处理、未确定、缺失或修订记录。没有待复核测量时不登录 Garmin，历史瘦体重补录仅更新本地。只新增明确缺失项；冲突、多余记录和后到字段的 repair 留待手动处理。关闭自动写入时 `run` 仅离线预览。

Mac 需开机、登录该用户、联网且文件已下载；睡眠、注销或关机期间暂停。安装时首次执行可能遇到进程锁，后续间隔仍会运行，可手动 `./hg run` 验证。任务使用绝对 Python 路径；移动项目前卸载，移动后重建虚拟环境并重新安装任务。

iPhone 自动化按 [SHORTCUTS.md](SHORTCUTS.md) 第 5 节设置：退出 PICOOC 后运行总快捷指令，另设每日固定时间，并保留手动入口。首次授权需在手机完成，读取 Health 可能要求解锁。

## 日常维护与排错

```bash
cd "$HOME/Applications/HealthGarminSync"
./hg doctor
./hg doctor --json                # 供自动化读取
./hg status
./hg preview
./hg run
./hg login                        # 需要时重新登录
./hg disable                      # 关闭写入，保留数据
./hg uninstall-agent
tail -n 80 state/sync.log
tail -n 80 state/agent.out.log
tail -n 80 state/agent.err.log
```

`doctor` 离线只读检查环境、配置、导出、运行摘要和 LaunchAgent，配置损坏或进程锁被占用时也可运行。未确定请求、操作最近核对失败等错误返回 2，冲突、待修复和远端缺失报告为提醒，其余返回 0；后台执行与权限需实际验收。虚拟环境缺失时，用 Python 3.12+ 执行 `python3 health_garmin.py doctor`。

`status` 显示详细本地状态；LaunchAgent 查询限时 5 秒，查询失败返回 2。

最近一次取得锁的 `run` 尝试保存在 `state/last_attempt.json`，覆盖更新；失败和中断会由 `doctor` 报告。缺少结束记录时只提示待核查，进程可能仍在运行。`state/last_run.json` 保留最近完成的同步摘要及本地状态计数快照；测量不再出现在导出中时，未确定状态仍会提示核对。

`state/last_agent_attempt.json` 同样覆盖更新，记录后台入口的日志初始化和主程序调用阶段，便于定位同步开始前的失败。自动写入启用时，入口摘要超过两小时会提示核查定时任务。

`sync.log` 自动轮换。后台的 `agent.out.log` 和 `agent.err.log` 每文件最多 1 MB，各保留 3 份备份；旧超大日志保留尾部。已有任务运行 `./hg install-agent` 切换到新入口。预览、查询和日志留在本机账户下，失败通过本地状态和日志查看。

| 现象 | 排查方式 |
|---|---|
| Mac 看不到 JSON | 核对 iCloud 账户、保存位置及文件下载状态 |
| inspect 有值、preview 没有 | 核对 since、allowed_sources、健康权限和样本类型 |
| metrics 或样本变成字符串 | 快捷指令用“设定词典值”放入词典和列表，仅保存前转整份 Payload 为文本 |
| 体脂 0.x 报错 | 对照健康显示后选择 fraction |
| 缺体脂 / WAIT | 无体脂可同步纯体重；WAIT 需解决多候选歧义 |
| 未来时间被拒绝 | 核对 at / exported_at、时区及手机时间，重新导出 |
| BMI 后到 | compare 生成 repair，手动修复指定记录 |
| 配置或 settings 被拒绝 | enabled 必须为 JSON 布尔值；校验失败保留原配置 |
| 显式命令返回 2、提示未执行 | run.lock 被占用，等进程结束；定时 run 遇锁跳过并返回 0 |
| 定时任务卸载失败 | 运行 doctor 核对服务；未确认卸载时保留任务文件，配置损坏时也可运行 uninstall-agent |
| 401/403/验证码失败 | 检查中国区账号、网络，重新 login，避免连续快速重试 |
| 令牌续期保存失败 | 核对 state/tokens 权限和磁盘，恢复后重新 login |
| 429 | 暂停重试，稍后查询 |
| 上传超时 | 查询 uncertain，核对后 resolve |
| 手动成功、定时失败 | 检查用户登录、解释器、iCloud 下载和权限，查看 agent.err.log |
| Operation not permitted | 检查执行进程的文件权限及 TCC 授权 |

macOS 权限：先手动 `./hg preview` 接受目录授权。后台 Python 可能需要单独授权；用以下命令确认实际解释器：

```bash
./.venv/bin/python -c 'import os,sys; print(os.path.realpath(sys.executable))'
```

在系统设置 → 隐私与安全性 → 文件与文件夹检查对应进程。确被 TCC 拒绝且无更窄授权时，可为实际 Python 授予完全磁盘访问权限；该权限也覆盖其运行的其他脚本。授权后重新卸载、安装并验证 LaunchAgent；Homebrew 更新解释器路径后需重新检查。

手机默认导出近 7 天；更早未导出的记录需扩大窗口导出一次。Mac 会继续读取 inbox 的旧文件，不自动删除云端副本。备份 SQLite 和 state 时保持私密，登录故障只需重新 login。日常同步只对账和新增，修复/删除/替换按上述确认流程执行，不回写 Apple 健康。

实现依据见 [docs/SOURCES.md](docs/SOURCES.md)。固定库的 FIT 时间编码使用进程本地时区；程序提交时临时切本进程到 UTC，结束后恢复，以保留原始测量时间。

离线回归：

```bash
.venv/bin/python -m unittest discover -s tests -v
bash -n setup.sh hg
git diff --check
```
