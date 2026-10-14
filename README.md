# HealthGarminSync：Apple 健康与 Garmin 中国区

iPhone 快捷指令把 Apple 健康中的 PICOOC 测量导出到 iCloud Drive，Mac CLI 每 30 分钟检查并同步到 Garmin 中国区。默认仅新增明确缺失的测量，修复和历史替换需手动确认。

可选的反向链路读取 Garmin 血压与测量脉搏，经 iCloud 批次交给 iPhone App 保存到 Apple 健康。血压成对保存，脉搏映射为测量时的心率；原始 API 响应永久保存在 Mac 本地数据库。原生 Connect 授权检查、App 安装、历史回填与定时导出见 [血压同步](docs/OPERATIONS.md#garmin-%E8%A1%80%E5%8E%8B%E4%B8%8E-apple-%E5%81%A5%E5%BA%B7)。

当前版本为 **0.1.0**；运行 `./health-garmin-sync --version` 查看版本。

## 安装与手机准备

1. 在 Apple 健康中确认 PICOOC 已写入真实测量，并核对准确来源名称。
1. iPhone 与 Mac 使用同一 iCloud 账户，开启 iCloud Drive。
1. 按 [docs/SHORTCUTS.md](docs/SHORTCUTS.md) 手工搭建四个子快捷指令和一个总快捷指令；项目没有可导入的 `.shortcut`。
1. 手动导出，确认 Mac 的 iCloud Drive/快捷指令/Health/inbox 收到可打开的 JSON；系统提供时选择“保留下载”。

将项目放在本地非 iCloud 目录，例如 `~/Applications/HealthGarminSync`，进入该目录后运行：

```bash
brew install python@3.12
bash scripts/setup.sh
```

已有 Python 3.12+ 可跳过安装。`scripts/setup.sh` 创建 `.venv/`，安装固定运行依赖、运行离线测试，并保存本机依赖锁文件。Homebrew 安装见 [官方说明](https://brew.sh/)。

`./health-garmin-sync` 是便利入口，等价于 `.venv/bin/python -m health_garmin_sync`。

## 初始化与离线检查

下面为当前 Mac 的 inbox；其他机器替换为实际绝对路径，路径中的空格须保留引号：

```bash
./health-garmin-sync init --inbox "/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/inbox"
./health-garmin-sync inspect
./health-garmin-sync preview
```

省略 `--inbox` 可选择目录。初始化时设置上传起始日期（默认 Mac 当天）与健康中的准确来源名（默认 PICOOC）；之前的记录不上传，也可用 `init --since YYYY-MM-DD` 指定。`inspect` 和 `preview` 不联网，先对照 Apple 健康核对数值、来源、原始测量时间和 16 位记录 ID。

| 数据   | 导出键   | 处理                       |
| ------ | -------- | -------------------------- |
| 体重   | weight   | 转为 kg 后上传             |
| 体脂率 | body_fat | 百分数上传                 |
| BMI    | bmi      | 有原始样本时上传           |
| 瘦体重 | lbm      | 仅本地保留，不映射为肌肉量 |

合并 JSON 使用 `schema_version=2`；兼容既有 version 1 导出及记录 ID。默认裸体重单位 kg，裸体脂单位 percent：`23.5%` 自动识别；裸 `0.235` 表示 23.5% 时改用 fraction。瘦体重裸数值沿用体重单位。

```bash
./health-garmin-sync settings --fat-unit fraction  # 按真实输出选择；默认为 percent
./health-garmin-sync settings --weight-unit lb     # 仅用于无单位后缀的磅数值
./health-garmin-sync settings --source 'PICOOC'
./health-garmin-sync preview
```

默认仅配对同来源、同测量时刻的指标；`WAIT` 表示候选有歧义，无体脂时可形成纯体重记录。确认同次写入有小幅偏差后，可用 `settings --pair-window 60` 放宽窗口，多个候选仍拒绝配对。修改配置须先 `disable`，完成后重新预览。

## 首次登录与试传

```bash
./health-garmin-sync login
./health-garmin-sync preview
./health-garmin-sync upload-one 实际16位记录ID
```

`login` 在终端输入中国区邮箱、密码及验证码，只登录；密码不保存，可恢复令牌写入 `state/tokens/`。`upload-one` 展示目标，输入 `UPLOAD` 才写入；提交前查询完整远端明细，匹配项跳过，提交后读回核验。在 Garmin Connect 中国区人工确认时间、体重、体脂率及 BMI。

已有记录缺字段时使用 [修复流程](docs/OPERATIONS.md#%E4%BF%AE%E5%A4%8D%E6%8C%87%E5%AE%9A%E8%AE%B0%E5%BD%95)。请求结果未确定时暂停提交，先用 `remote`、`compare` 和 Garmin 页面核对，再按 [恢复流程](docs/OPERATIONS.md#%E7%8A%B6%E6%80%81%E4%B8%8E%E6%9C%AA%E7%A1%AE%E5%AE%9A%E8%AF%B7%E6%B1%82) 处理；保留 SQLite 和操作日志。

## 开启自动处理

成功核对至少一条测量后运行：

```bash
./health-garmin-sync enable             # 核对预览，输入 ENABLE
./health-garmin-sync run
./health-garmin-sync install-agent
./health-garmin-sync status
```

LaunchAgent 每 30 分钟启动一次 CLI。Mac 需开机、登录该用户、联网且 iCloud 文件已下载。首次复核起始日后的全部测量，以后复核近 30 天及更早待处理、未确定、缺失或修订记录；单次默认最多处理 10 条，等待导出合并两分钟。无待复核数据时不登录 Garmin，历史瘦体重补录仅更新本地。

自动处理保留冲突和多余记录，后到字段留待人工修复；两向自动开关均关闭时 `run` 仅离线预览。iPhone 自动化和超过七天的补采见 [快捷指令说明](docs/SHORTCUTS.md#%E8%87%AA%E5%8A%A8%E5%8C%96%E4%B8%8E%E8%A1%A5%E9%87%87)。后台权限和运行限制见 [维护说明](docs/OPERATIONS.md#%E8%AF%8A%E6%96%AD%E4%B8%8E%E5%90%8E%E5%8F%B0%E8%BF%90%E8%A1%8C)。

## 日常命令与数据保留

```bash
./health-garmin-sync doctor             # 离线只读诊断；--json 供自动化读取
./health-garmin-sync status
./health-garmin-sync preview
./health-garmin-sync compare            # 联网只读对账，生成清单
./health-garmin-sync run
./health-garmin-sync disable            # 关闭自动写入
./health-garmin-sync uninstall-agent
```

新初始化默认开启原始导出归档：相关测量核验成功后，JSON 原始字节及元数据存入 `state/source_archive.sqlite3`，读回校验并确认文件未变才清理 iCloud 副本。未确定请求、未完成操作及未配对样本阻止清理。后续预览与对账合并本地归档和新导出，关闭清理也继续读取归档。

升级已有配置可在 `disable` 后执行 `settings --archive-after-sync on`；使用 `off` 暂停清理。恢复到尚不存在的目录：

```bash
./health-garmin-sync restore-exports --to "$HOME/Documents/HealthGarmin-restored"
```

请私密备份完整 `state/`（含归档、令牌和恢复证据）及配置。`config.json`、`state/`、`.venv/` 和本机锁文件不进入 Git。称重链路不回写 Apple 健康；反向血压由 iPhone 导入端写入。手机修改或删除称重历史后，用 [对账、删除及完整历史替换](docs/OPERATIONS.md) 核对处理。

## 进一步阅读

- [iPhone 手工搭建](docs/SHORTCUTS.md)：类型、权限、自动化与导出验收。
- [操作与恢复](docs/OPERATIONS.md)：清单、修复、历史替换、备份、未确定请求和故障排查。
- [架构](docs/ARCHITECTURE.md)与 [开发贡献](CONTRIBUTING.md)：模块职责和离线验证。
- [验证记录](docs/VERIFICATION.md)：离线测试与历史设备验收分开记录。
- [实现来源](docs/SOURCES.md)与 [变更记录](CHANGELOG.md)。
