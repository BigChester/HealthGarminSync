# HealthGarminSync：PICOOC → Apple 健康 → Garmin 中国区

当前版本：远端对账版，2026-09-29（导出 schema_version 仍为 2）。每次由 iPhone 总快捷指令生成一份 `schema_version=2` 的合并 JSON，包含体重、体脂率、可选 BMI 和瘦体重（lbm）。瘦体重仅本地保留，不上传为 Garmin 肌肉量。

免费本地方案：iPhone 自带快捷指令导出体重、体脂率和可选 BMI，经 iCloud Drive 到 Mac，Mac 每 30 分钟检查并上传新测量。无付费 App、无公网服务器、无端口映射、无 SSH 服务。

## 验证状态

当前版本包含 182 项离线单元测试，涵盖合并 JSON、单位转换、时区、时间配对、重复数据、已存在记录、上传超时、完整明细查询、历史替换、SQLite 连接生命周期、配置校验和 CLI 锁冲突。2026-10-02 开发回归与 2026-09-29 历史设备验证分别记录在 [TEST_REPORT.md](TEST_REPORT.md)，最新离线输出见 [docs/development-test-output.txt](docs/development-test-output.txt)。

当前数据来源仅为 Apple 健康。最新真实 JSON 已包含 PICOOC 写入的体重、体脂率、BMI 和瘦体重；其中前三项上传 Garmin，瘦体重保留在原始文件、本地测量状态及备份中。水分、骨量、肌肉量等缺少原始数据时无法补齐。四字段快捷指令及获取范围见 [SHORTCUTS.md](SHORTCUTS.md)。预览会显示每个可用指标的覆盖条数，BMI 和瘦体重后到仍使用原测量 ID，对账后按指定记录补 BMI。

**当前 Mac 的真实 Garmin 中国区单条上传及补 BMI 验证已通过：原始测量时间、体重、体脂率和 BMI 读回匹配，本地状态为 verified。** 新四字段 JSON 已在正式 iCloud inbox 被读取，修复前有私密备份；瘦体重仅本地保存。自动写入保持关闭。macOS LaunchAgent 注册和后台权限仍需在设备验收。详见 `TEST_REPORT.md`。

**完整历史替换已完成：173 条 Apple 测量全部与 Garmin 明细匹配，远端总数 173、多余记录 0。** 覆盖 2024-09-04 至 2026-09-28；原有匹配记录保留 1 条，删除其余旧记录 55 条，新增/重建 172 条。同一天的多次称重全部保留并核验。私密备份在 state/backups/full-20260929T152109782803Z/，自动写入仍为 false，历史起始配置已调整为 2024-09-04。

本包不含可一键导入的 `.shortcut`；iPhone 需按照 `SHORTCUTS.md` 搭建。

本项目仅保留当前版本的程序、说明和示例。当前程序仍能读取既有 `schema_version=1` 导出，与合并格式使用相同的记录 ID 和去重数据库；无需为了整理文件而重置运行数据。

## 安装包内容

```text
HealthGarminSync/
├── README.md          Mac 安装、试传与日常维护
├── SHORTCUTS.md       iPhone 合并 JSON 快捷指令搭建
├── health_garmin.py         Mac 主程序
├── replace_history.py       显式完整历史替换、私密备份与读回验证
├── hg                      命令入口
├── setup.sh                虚拟环境、依赖和离线测试
├── requirements.txt        固定 garminconnect==0.3.16
├── tests/
│   ├── test_sync.py         解析、配对与上传回归测试
│   ├── test_exports.py      合并导出与数据兼容测试
│   ├── test_reconciliation.py 远端对账、修复与恢复测试
│   ├── test_replace_history.py 完整历史替换与中断保护测试
│   ├── test_remote_details.py 完整明细与每日汇总区分测试
│   ├── test_cli.py          配置校验与 CLI 锁冲突测试
│   └── test_database.py     SQLite 连接生命周期测试
├── samples/health.example.json  带 demo 标记，禁止上传
├── docs/
│   ├── SOURCES.md          实现依据
│   ├── offline-test-output.txt  2026-09-29 离线测试输出
│   ├── offline-cli-smoke.txt    2026-09-29 CLI 冒烟测试输出
│   └── development-test-output.txt 最新开发离线回归输出
└── TEST_REPORT.md           当前版本验证报告
```

运行后会生成 `config.json`、`state/`、`.venv/`、`requirements.lock.txt`。这些运行文件均不包含在交付包中。`state/tokens/` 中的登录令牌按密码保护，不上传 Git、iCloud 或发给他人。

## 第一步：准备手机和共享目录

1. 确认 PICOOC 已将实际体重和体脂率写入 iPhone Apple 健康。
2. iPhone 与 Mac 的 iCloud 使用同一个 Apple 账户，并均开启 iCloud Drive。App Store 的购买账户/商店地区不是这里判断共享文件的依据。
3. 使用现有目录：iPhone 文件 App → iCloud Drive → 快捷指令（Shortcuts）→ Health → inbox。总快捷指令的保存位置选择这个目录。
4. 按 `SHORTCUTS.md` 建体重、体脂率子快捷指令和总快捷指令，手动运行总快捷指令一次。
5. Mac Finder 中打开现有 inbox，确认新增一份 `health-时间戳.json`，能打开并看到 `schema_version=2` 和包含两类样本的 `metrics` 词典。
6. 对文件夹选择“保留下载 / Keep Downloaded”（系统提供此选项时），至少保证文件不是尚未下载的占位状态。

当前 Mac 数据目录已确认存在：

```text
/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/inbox
```

其中 `Mobile Documents` 含空格。终端命令用引号包住整条路径即可；引号内的 `~` 不加反斜杠。

## 第二步：在 Mac 本地安装

将 ZIP 在 Mac 上解压，得到 `HealthGarminSync` 文件夹。放到非 iCloud 同步目录，例如 `~/Applications/HealthGarminSync`。不要把整个项目放到启用了 iCloud 同步的桌面或文稿里。

默认 Safari 下载目录为 `~/Downloads` 时，可以执行：

```bash
mkdir -p "$HOME/Applications"
# 确认目的目录为空，避免覆盖现有配置和运行状态。
ditto "$HOME/Downloads/HealthGarminSync" "$HOME/Applications/HealthGarminSync"
cd "$HOME/Applications/HealthGarminSync"
```

下载位置或解压目录名不同，则在 Finder 中复制到上述位置，或先输入 `cd ` 再把解压目录拖到终端。

你已有 Homebrew 的情况下：

```bash
brew install python@3.12
bash setup.sh
```

安装脚本会创建虚拟环境，安装固定 `garminconnect==0.3.16` 和上游建议的 `curl_cffi`，运行离线测试，并将实际依赖版本保存到 `requirements.lock.txt`。

已有可用 Python 3.12+ 可以跳过 Homebrew 安装 Python；`setup.sh` 也会尝试找到它。终端显示 `brew: command not found` 时，按 Homebrew 官方安装说明恢复现有 Homebrew 的 shell 环境或安装，不要对系统 Python 使用 `sudo pip`。

这里固定的是本次核对的 Garmin 库版本，不是承诺将来接口永远不变。不要在无人值守任务里自动升级依赖。

## 第三步：初始化配置

```bash
./hg init --inbox "/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/inbox"
```

这条命令直接使用现有 inbox。省略 `--inbox` 时，macOS 会弹出目录选择器，请选择同一目录；不要填 `iCloud Drive/...` 这样的显示名称。

随后回答：

- 从哪天开始上传：建议选第一次试传那一天，例如 `2026-09-29`；默认是初始化时 Mac 的当天日期。更早记录不会上传。不是把历史数据改成今天。
- PICOOC 来源名称：填 Apple 健康真实显示的名称，默认 PICOOC。

默认配置：

| 配置 | 默认值 | 用途 |
|---|---:|---|
| is_cn | true | Garmin 中国区 |
| enabled | false | 先不自动写入 |
| weight_bare_unit | kg | 裸体重数字按公斤 |
| fat_bare_unit | percent | 裸体脂数字按百分数 |
| pair_window_seconds | 0 | 默认仅配对完全相同的测量时刻 |
| settle_seconds | 120 | 自动处理时给刚导出的批次留两分钟合并时间 |
| max_records_per_run | 10 | 限制单次处理数量 |

初始化也可以显式指定：

```bash
./hg init --inbox "/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/inbox" --since 2026-09-29
```

日期按你的真实试传日期调整；换用其他 Mac 时，路径也要使用那台机器上的实际目录。已有配置需要调整 inbox 时，先执行 `./hg disable`，再用 `./hg settings --inbox "完整路径"` 更新并重新预览。

## 第四步：检查原始输出和本地预览

```bash
./hg inspect
./hg preview
```

`inspect` 展示最近文件中的原始样本，`preview` 解析、校验、配对；两者都不会登录或写入 Garmin。

对照 PICOOC/Apple 健康核对体重、体脂率、时间，以及 PICOOC 来源：

- `23.5%`：显式百分数，脚本自动识别。
- `23.5` 对应健康的 23.5%：保持 percent。
- `0.235` 对应健康的 23.5%：改成 fraction 后重试。

```bash
./hg settings --fat-unit fraction
./hg preview
```

不是让你无条件执行 fraction。只有实际 JSON 以 0.x 表示比例时才执行。恢复百分数模式：

```bash
./hg settings --fat-unit percent
```

显示 `WAIT` 表示体脂或 BMI 配对有歧义。没有体脂样本时会预览纯体重测量。先用 inspect 对照原始时刻。只有确认同次测量在不同指标之间确实存在不超过一分钟的写入偏差，才使用：

```bash
./hg settings --pair-window 60
./hg preview
```

同一时间窗口内出现多次候选时，脚本仍会拒绝配对。不应为了让程序“通过”随意扩大窗口。

来源不同则按实际来源名设置：

```bash
./hg settings --source 'PICOOC'
```

若输出体重单位为 `lb` 且文本没有单位后缀，明确设置：

```bash
./hg settings --weight-unit lb
```

正常 preview 每条会显示一个 16 位记录 ID、状态、时间、公斤体重、可选体脂和 BMI；缺失字段显示 —。复制你要试传的那一行 ID。不要使用包内示例文件代替真实测量。

## 第五步：登录 Garmin 中国区

```bash
./hg login
```

在本机终端输入 Garmin 中国区邮箱、密码，以及系统要求时的验证码。密码不回显、不存入配置文件、不进入 iCloud；登录令牌只保存在本机 `state/tokens/`。

这个命令会向 Garmin 发起登录请求，但不上传身体测量。不要将密码放到命令参数或发到聊天中，也不用关闭双重验证。账号当前使用的登录限制、风控或地区接口异常，仍可能导致失败。

## 第六步：只试传一条真实测量

```bash
./hg preview
./hg upload-one 这里替换为预览中的16位ID
```

程序先显示本条数据；输入 `UPLOAD` 才会尝试写入中国区。

程序会先查询前后日期范围内的已有记录，避免已存在的测量被重复添加；提交后再读取并核对时间、体重、体脂率及可用 BMI。

状态解释：

| 状态 | 含义与处理 |
|---|---|
| pending | 本地尚未提交的测量，仍需远端比对 |
| verified | 已从 Garmin 返回数据核对匹配；也可能是此前就已存在的相同记录 |
| verified_manual | 你在 Garmin 人工确认过，程序仅记录了该确认 |
| conflict / verified_conflict | 远端值不同、缺字段或时间对应有歧义，需 compare 查看分类并人工处理 |
| remote_missing | 以前已验证、现已远端缺失，核对后允许补传 |
| repair_uncertain | 有未完成的备份修复操作，用 verify-operation 核对 |
| uncertain | 请求可能已成功，但未可靠读回核对；暂停重复提交 |
| inflight | 提交前的持久状态，异常中断后按 uncertain 处理 |
| ignored | 你选择跳过的本地任务 |

随后在 Garmin Connect 中国区对应日期的体重/身体成分详情中核对真实显示。不要只看到 `verified` 就省略首次人工检查，尤其要核对体脂百分数和原始测量时间。

第一次试传最好选 Garmin 里尚未存在的测量。如果同一时刻已手动填过只有体重的记录，脚本不会自动给它覆盖补体脂；需要你自己决定如何处理那条旧记录。

### 出现 uncertain 或 conflict

先查询，暂时不要重复执行上传或删除本地数据库：

```bash
./hg remote 2026-09-29
./hg status
```

`remote` 只读，结果保存在本机 `state/remote-日期.json`，包含个人健康数据，不要公开。

确定 Garmin 已有正确记录，但脚本未识别返回结构时：

```bash
./hg resolve 这里替换为ID --present
# 按提示输入 PRESENT，仅更新本地状态
```

确定 Garmin 没有收到这条记录时：

```bash
./hg resolve 这里替换为ID --absent
# 按提示输入 ABSENT，再手动执行 upload-one
```

仅想跳过该本地任务：

```bash
./hg resolve 这里替换为ID --ignore
```

这些命令不会删除、修改或覆盖 Garmin 的已有记录。`--absent` 不应在结果仍不确定时使用。Garmin 没有为此第三方上传路径提供本文已确认的服务端幂等键，不能承诺绝对 exactly-once。

## Garmin 对账与历史整理

### 以完整 Apple 导出替换选定历史范围

仅在已从 Apple 健康完整导出全部需要保留的 PICOOC 测量后使用。先选择实际最早日期对账，例如当前完整导出的 2024-09-04：

```bash
./hg compare --since 2024-09-04
./hg replace-all --plan state/plans/本次实际清单文件名.json --complete-export
```

核对终端显示的目标总数、保留数、删除数、重建/新增数，再输入 `REPLACE ALL`。该操作以选定日期范围的 Apple 测量为准：保留匹配记录，删除其他 Garmin 记录（包括近时刻但时间不同的旧记录），新增或重建目标测量。不会因为某条被列为 conflict 就保留一条时间错误的旧测量；本地测量时间过近或来源配对有歧义时仍会拒绝整批操作。

执行前先检查清单和双方最新数据，备份 Garmin 全范围响应、SQLite、配置、目标测量和原始导出文件；按日期处理，每次删除后确认原 ID 消失，每次新增后核验原时间、体重、体脂和 BMI。最终重新分月读回，要求全部目标匹配、远端总数等于目标数，不允许附近隐藏重复。LBM 仅本地保留。此手动命令独立于日常 run 的单次数量限制，不自行打开自动同步。

本次完整导出有 173 次测量，范围为 2024-09-04 至 2026-09-28。完整明细核对旧 Garmin 有 56 条，其中 1 条已匹配；1970-01-01 至导出最早日期之前没有体重记录。替换范围覆盖全部已发现 Garmin 体重，实际保留 1 条、删除 55 条、新增或重建 172 条。首次使用 dateRange 每日最后值接口仅看到 53 条，本版已经改用 weight/range 的 allWeightMetrics 和 dayview 完整明细；禁止用日汇总判断同一天其他测量不存在。

发生删除或上传超时、读回未匹配或数据变化时整批暂停，保存操作日志，不重复请求。终端会显示日志路径，可以使用：

```bash
./hg verify-operation state/operations/实际full操作日志.json
```

该命令仅查询。未完成的整批日志会阻止目标记录再次自动上传；不能用 resume-operation 自动重跑整批删除。私密备份在 state/backups/full-时间戳/。

日常同步使用 Apple Health 为数据来源，先批量查询 Garmin，再决定是否写入。记录匹配使用绝对测量时间（允许 FIT 秒级编码的 1 秒误差）和数值；体重/体脂允许 0.021 的编码误差，BMI 允许 0.11 的误差。同一天的不同称重不会仅凭数值相同就合并。60 秒内存在无法唯一对应的记录会列为 conflict。

### 只读查看差异

```bash
./hg compare
# 历史对账：日期替换为你已完整导出的范围
./hg compare --since 2020-01-01 --until 2026-09-29
```

compare 会登录 Garmin 查询，但不会上传、删除或自动处理差异；关闭自动写入时也可使用。历史日期参数只影响本次对账，不改变 config.json 的 since。它只能比对已到达 inbox 的 Apple 数据，不能直接读取 iPhone 健康数据库。做完整历史清理时，建议把这次完整历史导出放在独立目录，并用 --inbox 指定，避免混入已经在 Apple 中修订或删除的旧导出。只有一条 Apple 样本时，不能将清单视为完整历史。`--inbox` 可显式指定另一导出目录，用于本地验证副本。

| 分类 | 含义与处理 |
|---|---|
| matched | 时间、体重和 Apple 提供的可选字段一致，跳过写入 |
| missing | 首次发现的缺失测量，启用同步后新增 |
| repair | 同次测量缺体脂/BMI，留待指定记录修复 |
| replace | 同次测量数值不同，留待指定记录替换 |
| conflict | 时间附近有歧义、多个 Apple 来源重叠或远端字段未知，停止该条 |
| review_missing | 本地以前已验证的记录在 Garmin 消失，人工核对后才允许补传 |
| uncertain | 上传或修复结果未确定，只读核对，延迟出现后转为 verified |
| ignored | 已人工忽略，继续保留该决定 |
| EXTRA | Garmin 在选定日期内有多余记录，自动同步保留 |

清单保存到 `state/plans/compare-时间戳.json`，包含选定日期、Apple 目标值和完整远端查询快照；摘要在 `state/last_compare.json`。这些文件包含个人健康数据，属于本机 state，已被 .gitignore 排除。

首次启用 run 时，复核 config.json 的 since 之后已保留的所有 Apple 测量；以后复核近 30 天和所有更早的待处理、结果不确定、远端缺失或已修订记录。完整历史复核可随时执行 compare。已有 verified 状态不会阻止近期远端复核。自动写入关闭时 run 仍只做离线预览。

### 修复指定的一条记录

当前 SDK 没有项目已验证的原地补字段路径，因此 repair-one 使用“备份 → 删除指定记录 → 确认删除可见 → 重建 → 读回核验”。每次仅操作清单中的一条，并在写入前重新查询；Apple 或 Garmin 的值已变化时，要求重新生成清单。

```bash
./hg repair-one 这里替换为Apple记录ID --plan state/plans/这里替换为清单文件名.json
# 核对原记录和目标数值，再输入 REPLACE 记录ID
```

备份包括原始 Garmin 响应、配置、清单和 SQLite，保存在 `state/backups/时间戳/`。操作记录在 `state/operations/`，删除前持久化。Apple 未提供的已有体脂和 BMI 会保留；原记录包含工具尚不支持保留的其他身体成分字段时，拒绝删除。

### 清理指定的一条多余记录

仅当你已完整导出选定日期内需要保留的 Apple 来源数据，才使用此命令。它不会根据近 7 天导出来自动清空 Garmin 历史。

```bash
./hg delete-extra 这里替换为Garmin记录ID \
  --plan state/plans/这里替换为清单文件名.json --complete-export
# 核对记录后输入 DELETE Garmin记录ID
```

已经匹配的记录保留；歧义范围内的 Garmin 记录不会被列为可删除的 EXTRA。不要将 Apple 未导出的日期误认为需要清理。

### 延迟读回、中断和补传

本次真实单条上传出现过延迟可见性，因此“不立即读到”不能当成失败重发。修复、删除和恢复都有持久操作记录，未完成的操作会阻止自动补传。

```bash
./hg verify-operation state/operations/这里替换为操作记录文件名.json
# 如果 App 已确认目标不存在、原记录也已删除，才恢复这次已备份的修复：
./hg resume-operation state/operations/这里替换为操作记录文件名.json
# 核对后输入 RESUME Apple记录ID；不会再次删除
```

`remote_missing`（以前验证过、现已缺失）需要先在 Garmin 核对，再执行 `./hg resolve 记录ID --absent` 并输入 ABSENT，允许下一次补传。存在未完成修复记录时必须使用 verify-operation / resume-operation，不通过 resolve 跳过操作记录。

纯体重历史样本也能对账和新增；可选体脂/BMI 不会被凭空补齐。存在多个体脂或 BMI 候选时仍为 WAIT，不会降级为纯体重来绕过歧义。体脂或 BMI 后到时会生成 repair，自动 run 不删除原记录。


## 第七步：开启 Mac 自动处理

成功核对至少一条后：

```bash
./hg enable
# 阅读预览，确认后输入 ENABLE
./hg run
./hg install-agent
./hg status
```

使用的是当前用户的 LaunchAgent，每 30 分钟检查一次。先批量查询选定的近期和待处理测量，已匹配的更新核验状态，仅明确缺失的新记录会新增。uncertain 记录只读核对，不盲目重传；冲突和多余记录留在清单中。

运行条件：Mac 已开机，你已登录该 macOS 用户，网络可用，文件已同步/下载。锁屏与注销不同；系统睡眠、注销或关机期间不能保证执行，唤醒/登录后等待后续检查，或手动 `./hg run`。

`install-agent` 通过 launchctl 加载任务，RunAtLoad 的首次运行可能与安装进程锁重叠而退出；不影响后续间隔运行。安装后可手动 `./hg run` 验证一次。

定时任务使用绝对 Python 路径，不依赖你平时终端的 venv 激活或 shell 配置。安装后不要移动项目目录；移动前卸载，移动后重新建虚拟环境并安装任务。

## 第八步：开启 iPhone 自动化

按 `SHORTCUTS.md` 第 5 节：退出 PICOOC 后立即运行总快捷指令；另设每天固定时刻作为补传机会。保留主屏幕手动运行入口。

iOS 可能要求解锁才能读取 Health；不保证固定时刻必达。首次隐私授权、文件访问授权和自动化确认必须在手机上完成。手机导出只说明到达 iCloud，不等于 Garmin 已上传成功。

## 日常维护命令

```bash
cd "$HOME/Applications/HealthGarminSync"
./hg status                       # 本地任务状态、最近处理记录
./hg preview                      # 不联网的样本预览
./hg run                          # 已 enable 时立即同步，否则只预览
./hg login                        # 需要时重新验证账号
./hg disable                      # 关闭自动写入，保留数据
./hg uninstall-agent              # 卸载当前用户的定时任务
```

查看日志：

```bash
tail -n 80 state/sync.log
tail -n 80 state/agent.out.log
tail -n 80 state/agent.err.log
```

`sync.log` 自动轮换。LaunchAgent 标准输出日志没有自动轮换；需要时在暂停任务后自行归档，通常每轮只有简短状态。脚本不把密码、令牌或原始登录异常全文写进日志，但预览/查询输出可能包含健康数值，应只留在你的账户下。

本版以本地状态和日志排错，没有实现向 iPhone 推送失败提醒。

## 典型问题

| 现象 | 排查方式 |
|---|---|
| Mac 看不到 JSON | 确认保存到 iCloud Drive，而非“我的 iPhone”；两端 iCloud 账号一致；文件可完整下载 |
| inspect 有数值、preview 没有 | 核对 since 日期、allowed_sources、健康权限和样本类型 |
| metrics 或指标样本是字符串 | 总快捷指令用“设定词典值”放入真正的词典和样本列表；仅在保存前将整份 Payload 转为文本 |
| 体脂输出 0.x 报错 | 与健康显示对照后，明确选择 fraction；不要盲目乘 100 |
| 缺少体脂 | 可作为纯体重历史同步；出现 WAIT 时检查多候选配对歧义 |
| BMI 后到 | compare 列为 repair；按指定记录修复，自动 run 不删除原记录 |
| 配置类型错误或 settings 被拒绝 | enabled 必须是 JSON 布尔值 true/false，不能是字符串；类型错误会在远端处理前被拒绝，settings 校验失败保留原配置 |
| 显式命令提示“未执行”并返回 2 | run.lock 已被其他进程持有，等待该进程结束后再执行；定时 run 遇到同样冲突会跳过并返回 0 |
| 401/403/验证码失败 | 检查中国区登录和网络；运行 login；不关闭双重验证、不连续快速重试 |
| 429 | 停止主动重试，稍后再核对；不要增加轮询频率 |
| 上传超时 | 检查 uncertain；先在 Garmin 查看，再决定 resolve，别删 SQLite 去“重置” |
| 手动成功、定时失败 | 检查用户登录、项目位置、解释器、iCloud 下载和 macOS 权限；看 agent.err.log |
| Operation not permitted | 不用 chmod 777、不关闭 SIP；确认执行主体对导出目录的文件权限/TCC 授权 |

### macOS 权限的具体处理

先在终端手动运行 `./hg preview`，接受系统对导出目录的访问授权。若只有 LaunchAgent 无法访问，终端获得的权限未必覆盖后台 Python。

查看 `./hg status` 显示的解释器，另执行：

```bash
./.venv/bin/python -c 'import os,sys; print(os.path.realpath(sys.executable))'
```

在系统设置 → 隐私与安全性中，先检查“文件与文件夹”里对应进程的授权。确实被 TCC 拒绝、且没有更窄的可用授权时，才考虑对这次实际使用的 Python 可执行文件授予“完全磁盘访问权限”；这是较宽权限，该解释器运行的其他脚本也可能受益，需要你权衡。不要给任意脚本/整个开发工具链泛化授权，也不要把密码目录搬进共享云盘作为绕过。

授权后重新卸载/安装 LaunchAgent 并验证。Homebrew 更新解释器路径后，可能需要重新核对这一权限。脚本不会自动修改 macOS 隐私设置。

## 历史数据与数据安全

- 手机默认每次导出近 7 天；超过此窗口尚未导出的数据，需要临时扩大窗口导出一次。
- 旧文件仍在 inbox 时，Mac 可以后续处理。脚本不自动删除云端文件；它们也是健康数据副本。
- 只导出体重、体脂率、可选 BMI 和瘦体重。不要把整个健康数据库搬到云盘。
- JSON 是普通 iCloud Drive 文件，不自动继承原生 Health 数据的全部保护机制。共享文件夹、Apple 账户和 Mac 账户的安全都需自行维护。
- SQLite 是防重复的重要状态，备份时保持私密；不要为解决登录问题删除整个 state 目录。
- `python-garminconnect` 为非官方客户端；更新接口或认证机制可能需要维护。没有保证永久可用。
- 日常 run 只读对账和新增；repair-one / delete-extra / replace-all 会在明确选择记录或范围并确认后备份、修改 Garmin。本工具不回写 Apple 健康。

## 来源及实现细节

见 `docs/SOURCES.md`。特别注意固定版 0.3.16 的 FIT 时间编码会通过 `mktime(t.timetuple())` 使用进程本地时区。代码只在提交时将**本进程**切到 UTC 并传入 UTC 时刻，之后恢复，避免 iPhone 与 Mac 时区不同造成偏移，不修改 macOS 系统时区。
