# iPhone 快捷指令搭建

链路：PICOOC → Apple 健康 → iPhone 快捷指令 → iCloud Drive → Mac。项目没有可导入的 `.shortcut`，请手工搭建四个子快捷指令，再用总快捷指令保存一份 `schema_version=2` JSON。界面中的“词典”也可能叫“字典”。

## 准备来源、权限与目录

在健康 App 各指标的“显示所有数据”中核对真实样本的数值、时间和来源，记下准确来源名。PICOOC 的健康写入权限与快捷指令的读取权限都需开启；只导出已确认的 PICOOC 来源，避免导入 Garmin 写回的数据。

iPhone 文件 App 选择 iCloud Drive → 快捷指令 / Shortcuts → Health → inbox。总快捷指令固定保存到此目录；以 Mac 实际收到文件为准。手机与 Mac 使用同一 iCloud 账户，Mac 下载目录内文件。

## 搭建「导出 PICOOC 体重」

快捷指令标签 → +，重命名。变量用“设定变量 / Set Variable”建立，引用时选择变量胶囊，不输入同名文字。

1. 添加“查找健康样本 / Find Health Samples”：类型体重，开始日期过去 7 天，来源为已核对的 PICOOC，开始日期从旧到新，关闭数量限制，不分组/聚合，单位 kg（若可选）。使用数据库查询，而非过滤前一步输入。
1. “计数 / Count”明确输入查询结果；“如果”计数为 0，添加“停止并输出 / Stop and Output”且无内容，然后结束分支。
1. “重复每一项 / Repeat with Each”输入查询结果；在重复块内依次执行下表。

| 动作                             | 输入 / 输出                                                   |
| -------------------------------- | ------------------------------------------------------------- |
| 获取健康样本详细信息 → 开始日期  | 输入重复项目 / Repeat Item                                    |
| 格式化日期 → 设定变量 SampleTime | `yyyy-MM-dd'T'HH:mm:ss.SSSXXX`                                |
| 获取健康样本详细信息 → 值        | 输入重新选重复项目                                            |
| 文本 → 设定变量 RawValue         | 只放上一步值变量，保留单位，不计算或四舍五入                  |
| 获取健康样本详细信息 → 来源      | 输入仍为重复项目 → SourceName                                 |
| 词典                             | 文本键 at = SampleTime、value = RawValue、source = SourceName |

4. 词典须是重复块最后动作；结束重复后“设定变量 Samples = 重复结果 / Repeat Results”，保持列表/词典类型。
1. 新建空词典，“设定词典值 / Set Dictionary Value”：输入空词典，键 samples，值变量 Samples；“停止并输出”返回更新后的完整词典。“无处输出时”选择不执行操作。
1. 解锁手机手动运行，授予体重读取权限，核对 samples 的 at/value/source；子指令不生成文件。

详情动作不可见时，检查重复项目胶囊仍为健康样本类型并选择属性。若系统不能提取来源，必须先明确来源过滤并验证，才能填入已确认的来源名称。单样本词典可接受，不能将列表转 JSON 字符串或再套数组；不要在重复块词典后放通知/快速查看而改变重复结果。

## 复制为另外三个子快捷指令

长按体重子快捷指令 → 复制，逐个重命名并修改“查找健康样本”的类型及单位。每个副本保留相同日期、来源过滤、逐样本提取及返回 `samples` 的动作。

| 名称               | 健康样本类型                   | 单位 / value                              |
| ------------------ | ------------------------------ | ----------------------------------------- |
| 导出 PICOOC 体脂   | 体脂率 / Body Fat Percentage   | 保留原始文本，如 23.5%                    |
| 导出 PICOOC BMI    | 身体质量指数 / Body Mass Index | 裸数字或 BMI、kg/m² 后缀；不要沿用 kg / % |
| 导出 PICOOC 瘦体重 | 瘦体重 / Lean Body Mass        | kg；仅本地保留                            |

各副本手动授权并核对原始记录；空结果返回无内容，不填假值。BMI 和瘦体重使用原始样本，不自行计算；瘦体重不得大于体重。

体脂 `23.5%` 可直接识别；裸 `23.5` 对应 23.5% 时 Mac 使用 percent，裸 `0.235` 对应同一比例时使用 fraction。瘦体重裸数字沿用 Mac 体重单位。单位设置见 [README](../README.md#%E5%88%9D%E5%A7%8B%E5%8C%96%E4%B8%8E%E7%A6%BB%E7%BA%BF%E6%A3%80%E6%9F%A5)。

## 组合总快捷指令

新建 `同步 PICOOC 到 Garmin`。

### 时间与必需的体重

| 顺序 | 动作                  | 设置                                                           |
| ---- | --------------------- | -------------------------------------------------------------- |
| 1    | 日期 → 设定变量       | 当前日期 → RunAt                                               |
| 2    | 格式化日期 → 设定变量 | 输入 RunAt，自定义 `yyyy-MM-dd'T'HH:mm:ss.SSSXXX` → ExportedAt |
| 3    | 格式化日期 → 设定变量 | 输入重新选 RunAt，自定义 `yyyyMMdd-HHmmss-SSS` → FileStamp     |

RunAt 是导出时间，不能替代测量时间；步骤 3 输入为日期变量，不是步骤 2 的文本。`MM` 为月份、`mm` 为分钟，使用 `yyyy`，`XXX` 输出实际时区偏移。区域选英语（美国）可确保西文数字。

```text
运行快捷指令：导出 PICOOC 体重（输入无）
设定变量 WeightPayload = 本次快捷指令结果
如果 WeightPayload 没有任何值
    停止并输出：无内容
结束如果
获取词典值：输入 WeightPayload，键 samples
设定变量 WeightSamples = 上一步词典值
如果 WeightSamples 没有任何值
    停止并输出：无内容
结束如果
词典：空词典，删除默认键值项
设定词典值：输入空词典，键 weight，值 WeightSamples
设定变量 Metrics = 上一步更新后的词典
```

### 加入有值的可选指标

对下面三行依次添加相同动作，把示例中的变量与键替换为对应行。每次调用都使用本次返回结果，每次更新后重新赋值 Metrics。

| 子快捷指令         | Payload 变量 | Samples 变量 | metrics 键 |
| ------------------ | ------------ | ------------ | ---------- |
| 导出 PICOOC 体脂   | FatPayload   | FatSamples   | body_fat   |
| 导出 PICOOC BMI    | BMIPayload   | BMISamples   | bmi        |
| 导出 PICOOC 瘦体重 | LBMPayload   | LBMSamples   | lbm        |

以体脂为例：

```text
无 / Nothing
设定变量 FatSamples = 上一步无内容
运行快捷指令：导出 PICOOC 体脂（输入无）
设定变量 FatPayload = 本次快捷指令结果
如果 FatPayload 有任何值
    获取词典值：输入 FatPayload，键 samples
    设定变量 FatSamples = 上一步词典值
结束如果
如果 FatSamples 有任何值
    设定词典值：输入 Metrics，键 body_fat，值 FatSamples
    设定变量 Metrics = 上一步更新后的词典
结束如果
```

BMI 和瘦体重也要先用 Nothing 初始化对应 Samples，再调用、取键并条件加入。空指标省略。保留各样本自己的测量时间，Mac 负责配对；不要按列表顺序或数量配对。查询或授权错误时停止本次运行。

### 顶层词典与一次保存

添加顶层“词典”，只填 `schema_version`（数字 2）和 `exported_at`（文本变量 ExportedAt）。再添加“设定词典值”：输入该顶层词典，键 `metrics`，值为最后更新的 Metrics。设定变量 `Payload` 为更新后的完整顶层词典。

| 动作             | 输入 / 设置                                                                       |
| ---------------- | --------------------------------------------------------------------------------- |
| 从输入中获取文本 | 整份 Payload → JSON 文本                                                          |
| 设定名称         | `health-[FileStamp].json`，方括号部分插入变量                                     |
| 存储文件         | 命名后的文件，固定 iCloud Drive/快捷指令/Health/inbox；关闭询问位置及覆盖已有文件 |

仅在这一步把词典转为文本，子快捷指令不保存文件。允许首次目录访问；通知只能表示“已导出”，Garmin 结果需在 Mac 查看。测试时可快速查看 JSON，存储动作仍明确选择 JSON 文本。

### 手工验收

解锁手机运行总快捷指令，inbox 应新增一份 JSON，再运行新增另一份。完整结构见 [演示 JSON](../samples/health.example.json)；该文件带 demo 标记，程序拒绝上传，勿放正式 inbox。

文件名不能是 `.json.txt`。metrics 是词典，各指标是样本列表或单个样本词典，不能是整个子指令返回值、嵌套数组或带引号的 JSON。`at` 是测量时间，`exported_at` 是导出时间。

Mac 执行 `./health-garmin-sync inspect`、`./health-garmin-sync preview`，核对来源、单位及时间，再按 [README](../README.md#%E9%A6%96%E6%AC%A1%E7%99%BB%E5%BD%95%E4%B8%8E%E8%AF%95%E4%BC%A0) 试传。出现 WAIT 先检查候选时间；已有 Garmin 记录缺 BMI / 体脂时，用 [compare / repair-one](OPERATIONS.md#%E4%BF%AE%E5%A4%8D%E6%8C%87%E5%AE%9A%E8%AE%B0%E5%BD%95) 修复。

## 自动化与补采

完成 Mac 单条试传后，在快捷指令 → 自动化 → App，选实际 PICOOC，勾“已关闭”，设“立即运行”（旧版关闭“运行前询问”），运行总快捷指令。称重后等 PICOOC 完成健康写入，再切回桌面；不需强制结束 App。

再建每日固定时间自动化，选择通常会解锁手机的时刻，立即运行同一总快捷指令，并保留手动入口。首次授权须手工完成；读取健康可能要求解锁，失败后解锁手动导出。设备验收链路：称重 → PICOOC 确认 → 切出 → iCloud 新文件 → Mac `./health-garmin-sync run` → Garmin 核对。

近 7 天重复导出用于补上偶发失败，Mac 去重。超过 7 天从未导出的数据需将四个查询范围一起改为 30 天或实际历史范围，导出后恢复 7 天。历史较多时按月导出，每份不超过 4 MB / 合计 20,000 样本。完整性按健康原始数据验收，不能仅凭文件数量判断。

已有 JSON 即使 Mac 长期关机也能恢复后处理；成功核验后清理云端的条件和本地恢复见 [归档与备份](OPERATIONS.md#%E5%BD%92%E6%A1%A3%E4%B8%8E%E5%A4%87%E4%BB%BD)。Apple 历史修改/删除不自动删除 Garmin 记录，需重新对账。

已有导出升级时，在健康/PICOOC 开启可用的 BMI、瘦体重权限，按上述副本接入；旧文件若误用 `bodt_fat`，先移出 inbox 并重新导出正确 `body_fat`。
