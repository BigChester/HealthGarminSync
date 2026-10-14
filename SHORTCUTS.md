# iPhone 快捷指令搭建说明

适用链路：PICOOC → Apple 健康 → iPhone 快捷指令 → iCloud Drive → Mac 本地脚本。

本包不含可导入的 `.shortcut`，需在 iPhone「快捷指令」中搭建。体重、体脂、BMI、瘦体重四个子快捷指令返回样本，总快捷指令保存一份 `schema_version=2` JSON。体重必需，其余有样本时导出；瘦体重（lbm）仅在 Mac 保留。界面中的「词典」也可能显示为「字典」。

已有四字段快捷指令可直接使用；历史设备验证见 [TEST_REPORT.md](TEST_REPORT.md)。

## 已有快捷指令：补齐 BMI 的操作顺序

已有体重、体脂导出时：

1. 健康 App → 头像 → 隐私下的「App」（部分版本显示「App 和服务」）→ PICOOC，打开可用的体重、体脂率和身体质量指数（BMI）写入权限。
2. 打开 PICOOC，开启 Apple 健康同步。健康 App 搜索「身体质量指数」→「显示所有数据」，核对实际样本的数值、时间和 PICOOC 来源；历史补齐时也检查旧记录。
3. 长按现有「导出 PICOOC 体重」→「复制」，重命名为「导出 PICOOC BMI」。只修改查找样本类型和单位，详见第 3 节。手动运行并允许读取 BMI，检查 samples 数值；空结果时也要检查读取权限。
4. 编辑总快捷指令「同步 PICOOC 到 Garmin」，按第 4 节 D 的动作顺序接入 BMI。键名必须为小写 `bmi`。同时把原来拼错的 `bodt_fat` 改成 `body_fat`。
5. 首次补齐历史：四个子快捷指令使用同一起止日期，覆盖需要补 BMI 的体重测量；完成后恢复近 7 天。
6. 若 inbox 中有使用 `bodt_fat` 的旧文件，将它移到 `Health/archive`，再运行总快捷指令；旧文件会阻止解析。
7. Mac 执行 `./hg inspect`、`./hg preview`、`./hg compare`。inspect 应出现 bmi 样本，preview 应显示实际 BMI 数字。已有 Garmin 记录缺 BMI 时会列为 repair，按第 7 节处理；不要重新 upload-one 制造第二条记录。

## Apple 健康字段范围

仅导入 PICOOC 写入 Apple 健康的以下原始样本：

| 指标 | 导出键 | Garmin 处理 |
|---|---|---|
| 体重 / Weight | `weight` | 上传 weight |
| 体脂率 / Body Fat Percentage | `body_fat` | 上传 percent_fat |
| 身体质量指数 / BMI | `bmi` | 上传 bmi |
| 瘦体重 / Lean Body Mass | `lbm` | 仅本地保留，不映射为肌肉量 |

其他指标不导入；字段依据见 [docs/SOURCES.md](docs/SOURCES.md)。

## 0. 确认现有文件夹与来源

使用现有目录：iPhone「文件」→「浏览」→「iCloud Drive」→「快捷指令 / Shortcuts」→「Health」→「inbox」。总快捷指令的存储动作固定选择此目录。

对应当前 Mac 的绝对路径：

```text
/Users/chester/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Health/inbox
```

不要选「我的 iPhone」。若手机界面名称不同，以 Mac 上该目录实际收到新文件为准。

在 Apple 健康中分别打开「体重」「体脂率」「身体质量指数」→「显示所有数据」→某次真实测量，核对时间、数值和来源。记下该来源的准确名称，例如 PICOOC。不能只看健康首页汇总；需要确认各指标样本由 PICOOC 写入。若 BMI 暂时没有，保留查询步骤、空结果时省略该指标即可。

## 1. 新建「导出 PICOOC 体重」

打开「快捷指令」→「快捷指令」标签→右上角「+」。重命名为 `导出 PICOOC 体重`。

下文的 `SampleTime`、`RawValue`、`SourceName`、`Samples` 都是建议的变量名。通过「设定变量 / Set Variable」建立；引用时点击输入框，选择已有变量，而不是把变量名作为文字打进去。

### A. 查找原始体重样本

1. 添加「查找健康样本 / Find Health Samples」，配置为从健康数据库查找所有样本，不是仅过滤上一个动作的输入。

   | 项目 | 设置 |
   |---|---|
   | 类型 | 体重 / Weight |
   | 开始日期 | 在过去 7 天内 |
   | 来源 | 是 PICOOC，或健康中显示的对应准确来源名 |
   | 排序 | 开始日期，从旧到新 |
   | 限制 | 关闭 |
   | 分组 / 聚合 | 无；不要按天求平均或总和 |
   | 单位（若提供选项） | kg |

2. 添加「计数 / Count」，输入明确选择「查找健康样本」的结果。
3. 添加「如果 / If」：计数等于 0。
4. 在该分支内添加「停止并输出 / Stop and Output」（旧版可能为停止此快捷指令），不输出健康数值；结束「如果」。这样没有样本时不生成假记录。BMI 子快捷指令查不到样本时也按此返回。

来源过滤应在查找阶段明确设置；Mac 端还会检查每条样本的来源，避免 Garmin 自己写回 Apple 健康的数据再次循环上传。

### B. 对每个样本组成一条词典

5. 添加「重复每一项 / Repeat with Each」，输入明确选择第 1 步查找得到的健康样本。
6. 在重复块内，添加「获取健康样本的详细信息 / Get Details of Health Sample」：输入是「重复项目 / Repeat Item」，详情选「开始日期」。
7. 添加「格式化日期」，自定义为 `yyyy-MM-dd'T'HH:mm:ss.SSSXXX`，设定变量 `SampleTime`。
8. 添加第二个「获取健康样本的详细信息」：输入重新选「重复项目」，详情选「值 / Value」。不要误选第 7 步的日期。
9. 添加「文本 / Text」，内容只放第 8 步「值」的变量；设定变量 `RawValue`。这里不乘 100，不去除单位，也不四舍五入。输出可为 `70.5` 或 `70.5 kg`。
10. 添加第三个「获取健康样本的详细信息」：输入仍为「重复项目」，详情选「来源 / Source」；设定变量 `SourceName`。
11. 添加「词典 / Dictionary」，三个键全部使用文本类型：

   | 键 | 类型 | 值 |
   |---|---|---|
   | `at` | 文本 | 变量 SampleTime |
   | `value` | 文本 | 变量 RawValue |
   | `source` | 文本 | 变量 SourceName |

12. 保证该「词典」是重复块里的最后一个动作，再结束重复。不要在词典后放快速查看、通知或其他输出，否则「重复结果」可能不再是词典列表。
13. 添加「设定变量」：`Samples` = 「重复结果 / Repeat Results」。保持其列表/词典类型，不先转为文本。

详情动作找不到时，也可插入「重复项目」变量，点变量胶囊，检查它是否仍是「健康样本」类型，再选择开始日期、值、来源等属性。

若当前系统只提供来源过滤、不能提取来源属性：必须先明确设置“来源是实际 PICOOC App”的过滤条件并验证结果，再在 `source` 字段填写该已验证的来源名称；不能在未筛选来源时，把所有数据直接标为 PICOOC。

### C. 返回样本词典

14. 添加「词典」，删除默认键值项，得到空词典。
15. 添加「设定词典值 / Set Dictionary Value」：输入第 14 步的空词典；键 `samples`；值选择变量 `Samples`，保持原始列表/词典类型。
16. 添加「停止并输出 / Stop and Output」：选择第 15 步更新后的完整词典。「无处输出时」如有选项，选择「不执行任何操作」。

子快捷指令只返回 `samples`，保持词典类型；文件由总快捷指令保存。

### D. 首次验收

保持 iPhone 解锁，手动运行子快捷指令，允许读取体重。在编辑器中检查返回结果包含 `samples`，每个样本都有 `at`、`value`、`source`。单独运行不会生成文件。

只有一个样本时，快捷指令可能返回单个样本词典；当前程序接受这种形式。不要将样本列表转为带引号的 JSON 字符串，也不要再套一层数组。

## 2. 复制为体脂率子快捷指令

长按「导出 PICOOC 体重」→「复制」，命名 `导出 PICOOC 体脂`。

将「查找健康样本」的类型改为「体脂率 / Body Fat Percentage」。其余样本字段和返回词典保持相同结构。若体重查询动作中有 kg 单位选项，复制后改为体脂率对应单位；不要保留 kg。

单独手动运行一次，授予读取体脂率权限并检查返回的 `samples`。

检查原始输出：

| JSON 中 value 的形式 | 含义及 Mac 设置 |
|---|---|
| `23.5%` 或 `23.5 %` | 已明确是百分数，脚本直接识别 |
| `23.5` | 核对健康中为 23.5%，Mac 使用 `percent` |
| `0.235` | 核对健康中为 23.5%，Mac 使用 `fraction`，由脚本乘 100 |

对照健康显示值与 JSON 原始值选择 Mac 单位。

## 3. 创建 BMI 子快捷指令

1. 长按「导出 PICOOC 体重」→「复制」，命名 `导出 PICOOC BMI`。
2. 打开副本，找到「查找健康样本」，配置：

   | 项目 | 设置 |
   |---|---|
   | 类型 | 身体质量指数 / Body Mass Index / BMI |
   | 开始日期 | 与体重、体脂相同，日常为过去 7 天 |
   | 来源 | 是已核对的 PICOOC 来源 |
   | 排序 | 开始日期，从旧到新 |
   | 限制、分组 | 限制关闭；不分组、不求平均 |
   | 单位（若提供） | BMI 对应单位；不要沿用 kg 或 % |

3. 保留第 1 节 B、C 的全部动作：对每条重复项目分别取开始日期、值、来源，组成 `at/value/source` 词典，最后返回 `samples` 词典。
4. 「值」仍只转为文本，例如 `22.5`。不要乘 100、除以 100、计算体重/身高，也不要将「当前日期」替代样本开始日期。程序接受裸数字、带 `BMI` 或 `kg/m²` 后缀的文本。
5. 手动运行副本并允许读取 BMI，对照健康中的同一条原始记录验收。

没有样本时，计数为 0 的分支按第 1 节 A 返回无内容。总快捷指令继续导出体重和已有体脂，不加入 0、null、空文本或上一条 BMI。

### 瘦体重子快捷指令（本地保留）

再复制「导出 PICOOC 体重」，命名 `导出 PICOOC 瘦体重`。「查找健康样本」类型改为「瘦体重 / Lean Body Mass」，日期范围和 PICOOC 来源过滤与其他三项一致，单位选 kg。仍取原始开始日期、值、来源，返回 samples 词典。单独运行并授予读取权限，与健康中的同一条样本核对。

保留数值及单位，例如 `72.2 kg`；裸数值沿用 Mac 的 weight_bare_unit（当前为 kg）。不要输出百分比，也不要用体重减去体脂自行替代原始瘦体重样本。程序会检查瘦体重不得大于对应体重，配对有歧义时暂停该测量。

## 4. 组合成一个总快捷指令

新建 `同步 PICOOC 到 Garmin`。变量通过「设定变量」建立；涉及多个词典时明确指定输入。

### A. 生成导出时间与文件名时间

依次添加：

| 顺序 | 动作 | 输入/设置 |
|---|---|---|
| A1 | 日期 | 当前日期 |
| A2 | 设定变量 | RunAt = A1 日期 |
| A3 | 格式化日期 | 输入 RunAt，自定义 `yyyy-MM-dd'T'HH:mm:ss.SSSXXX` |
| A4 | 设定变量 | ExportedAt = A3 输出 |
| A5 | 格式化日期 | 输入重新选择 RunAt，自定义 `yyyyMMdd-HHmmss-SSS` |
| A6 | 设定变量 | FileStamp = A5 输出 |

A5 不是再次处理 A3 已格式化文本。RunAt 是本次运行时间，不替换样本的测量时间。大写 `MM` 是月份，小写 `mm` 是分钟；使用小写 `yyyy`，不要写成 `YYYY`。`XXX` 输出对应日期的时区偏移，不要手工固定为 +08:00。若能设置区域格式，选英语（美国）以保证输出西文数字。

### B. 调用体重子快捷指令并取 samples

依次添加：

```text
运行快捷指令：导出 PICOOC 体重
设定变量 WeightPayload = 上一步快捷指令结果
如果 WeightPayload 没有任何值
    停止并输出：无内容
结束如果
获取词典值：从 WeightPayload 获取键 samples 的值
设定变量 WeightSamples = 上一步词典值
如果 WeightSamples 没有任何值
    停止并输出：无内容
结束如果
```

展开“运行快捷指令”时，无须把前一步日期当成输入；本子快捷指令自己查健康数据，输入设为无。零样本分支不返回内容，因此必须在取键前检查。查询错误或权限错误让本次停止，不要用上次缓存或0代替。

### C. 调用体脂率子快捷指令并取 samples

体脂率无样本时导出纯体重。

```text
无 / Nothing
设定变量 FatSamples = 上一步无内容
运行快捷指令：导出 PICOOC 体脂
设定变量 FatPayload = 这次快捷指令结果
如果 FatPayload 有任何值
    获取词典值：从 FatPayload 获取键 samples 的值
    设定变量 FatSamples = 上一步词典值
结束如果
```

不要比较两个列表数量来配对，也不要把第一条体重与第一条体脂直接拼接。每条样本保留自己的测量时间，Mac 负责配对。

### D. 组装 metrics 词典

依次添加：

```text
词典：空词典，删除默认键值项
设定词典值：键 weight，值选 WeightSamples，输入上一步空词典
设定变量 Metrics = 上一步更新后的词典
如果 FatSamples 有任何值
    设定词典值：输入 Metrics，键 body_fat，值选 FatSamples
    设定变量 Metrics = 上一步更新后的词典
结束如果
```

每次「设定词典值」后都要重新赋值 Metrics。

在上述动作后、顶层词典生成前，依次添加 BMI 动作：

```text
无 / Nothing
设定变量 BMISamples = 上一步无内容
运行快捷指令：导出 PICOOC BMI（输入无）
设定变量 BMIPayload = 这次快捷指令结果
如果 BMIPayload 有任何值
    获取词典值：从 BMIPayload 获取键 samples 的值
    设定变量 BMISamples = 上一步词典值
结束如果
如果 BMISamples 有任何值
    设定词典值：输入 Metrics，键 bmi，值选 BMISamples
    设定变量 Metrics = 上一步更新后的词典
结束如果
```

键为 `bmi`，值选择 BMISamples；在对应「如果」内更新 Metrics。BMI 为空时省略。

随后用同样方式加入瘦体重，在顶层词典生成前添加：

```text
无 / Nothing
设定变量 LBMSamples = 上一步无内容
运行快捷指令：导出 PICOOC 瘦体重（输入无）
设定变量 LBMPayload = 这次快捷指令结果
如果 LBMPayload 有任何值
    获取词典值：从 LBMPayload 获取键 samples 的值
    设定变量 LBMSamples = 上一步词典值
结束如果
如果 LBMSamples 有任何值
    设定词典值：输入 Metrics，键 lbm，值选 LBMSamples
    设定变量 Metrics = 上一步更新后的词典
结束如果
```

键为小写 `lbm`，为空时省略；顶层 metrics 使用最后更新的 Metrics。

### E. 组装顶层词典

添加一个“词典”，仅填写：

| 键 | 类型 | 值 |
|---|---|---|
| schema_version | 数字 | 2 |
| exported_at | 文本 | 变量 ExportedAt |

再添加“设定词典值”：

```text
输入：E步骤新建的顶层词典
键：metrics
值：变量 Metrics 的最终更新结果（含有值的 body_fat、bmi、lbm 时使用该结果）
```

最后「设定变量 Payload = 刚更新后的顶层词典」。

### F. 只在这里保存一次

依次添加：

| 动作 | 配置 |
|---|---|
| 从输入中获取文本 | 输入变量 Payload，得到完整 JSON 文本 |
| 设定名称 | `health-[FileStamp].json`，方括号部分表示插入 FileStamp 变量 |
| 存储文件 | 输入命名后的文件；固定为 iCloud Drive/快捷指令/Health/inbox；关闭询问位置；关闭覆盖已有文件 |

也可用「文本」动作，只插入整个 Payload 词典变量；保存前确认输出为有效 JSON。

允许首次文件夹/健康授权。可在存储完成后用通知提示“已导出到 iCloud，等待 Mac 处理”，不要提示“Garmin 上传成功”。测试阶段可在转换文本后加“快速查看”，实际存储输入仍明确选 JSON 文本。

### G. 核对保存结果

保持 iPhone 解锁，手动运行总快捷指令，允许访问指定文件夹。inbox 应只新增一份 `health-时间戳.json`，再次运行会新增另一份文件，不覆盖上一份。

下面仅展示结构，数值不是你的真实数据，不要复制到正式导出目录：

```json
{
  "schema_version": 2,
  "exported_at": "2020-02-20T09:00:00+08:00",
  "metrics": {
    "weight": [
      {
        "at": "2020-02-20T08:00:00+08:00",
        "value": "70 kg",
        "source": "PICOOC"
      }
    ],
    "body_fat": [
      {
        "at": "2020-02-20T08:00:00+08:00",
        "value": "23.5%",
        "source": "PICOOC"
      }
    ],
    "lbm": [
      {
        "at": "2020-02-20T08:00:00+08:00",
        "value": "53.55 kg",
        "source": "PICOOC"
      }
    ],
    "bmi": [
      {
        "at": "2020-02-20T08:00:00+08:00",
        "value": "22.5",
        "source": "PICOOC"
      }
    ]
  }
}
```

检查：文件名不是 `.json.txt`；`metrics` 是词典；weight/body_fat/bmi/lbm 是样本列表或单个样本词典，不能是整个子快捷指令返回值、带引号的 JSON 或嵌套数组；`at` 是实际测量时间，`exported_at` 是本次导出时间。包内 `samples/health.example.json` 带 `demo` 标记，程序会拒绝上传。

在 Mac 执行 `./hg inspect` 和 `./hg preview`，核对数值、时间和来源，再按 README 完成单条试传。总快捷指令只将数据送到 iCloud，上传结果需在 Mac 和 Garmin 查看。

## 5. 先完成 Mac 试传，再开启 iPhone 自动化

### 主触发：退出 PICOOC

「快捷指令」→「自动化」→「+」→「App」→选择你实际称重用的 PICOOC →勾选「已关闭」，不勾「已打开」→「立即运行」。旧版界面关闭「运行前询问」。

下一步选择总快捷指令 `同步 PICOOC 到 Garmin`；没有直接选择界面时，在自动化中添加“运行快捷指令”动作。

“已关闭”指关闭或切出 App，不要求从多任务界面强制结束 PICOOC。称重后先让 PICOOC 完成健康写入，再回桌面。

### 补充触发：每天固定时刻

再建“时间”自动化，选你通常会使用、解锁手机的时间，例如当地 20:00，每天，立即运行同一总快捷指令。

### 实际验收

称重→打开 PICOOC 确认新测量→切回桌面→检查 iCloud 有新导出→Mac `./hg run`→检查 Garmin。第一次看到任何授权提示都需手动完成。

iOS 可能要求解锁读取健康数据；失败后解锁并手动运行总快捷指令。

## 6. 7 天窗口与长期停机

每次重新导出近 7 天是为了补上偶发失败，Mac 会根据样本去重。超过 7 天从未成功导出的手机数据不会凭空出现在 Mac；此时临时把四个查找范围改成 30 天，手动导出一次，再恢复 7 天。

如果旧 JSON 已经到达 inbox，即使 Mac 关机超过 7 天，文件仍保留，恢复后可以处理。Mac 启用同步后归档时，只在相关测量核验完成、原始 JSON 已完整存入本地数据库后清理云端文件；历史仍可从本地归档读取和恢复，详见 README 第 8 节。修改或删除 Apple 健康历史记录不会自动删除 Garmin 中已有记录，需要单独核对。

## 来源

Apple 快捷指令 JSON、词典、变量、App 触发和隐私机制的官方说明，以及开源上传端依据，见 `docs/SOURCES.md`。

## 历史导出与对账

历史整理时，将各个「查找健康样本」的日期条件改为明确的历史起止范围，数据多时按月导出，来源仍需是已确认的 PICOOC。体脂/BMI 没有时可省略，体重仍导出；不要扩大来源到 Garmin 自己写回的数据。

每份文件最大 4 MB、样本合计最多 20,000 条；超过时缩小日期范围。历史导出完成后，使用 README.md 中的 compare 命令核对，日常导出再恢复近 7 天。数据是否完整需要按 Apple 健康实际历史验收，不能仅凭 Mac 的文件数量判断。

## 7. Mac 验收与补齐已有 Garmin 记录

在项目目录运行：

```bash
./hg inspect
./hg preview
./hg compare
```

inspect 检查 bmi 原始样本的数值、时间和来源；preview 对应测量应显示 BMI=实际数字，缺失时为 —。默认要求三指标同来源、同测量时刻，按时刻配对，不按列表顺序。出现 WAIT 要核对时间和候选；仅在确认同次测量的写入时间有小幅偏差后，才按 README 设置配对窗口。一个 BMI 候选对应多次称重时会暂停，不能复制给每次称重。

compare 只读查询 Garmin，终端打印本次生成的 `state/plans/compare-时间戳.json` 路径：

| 分类 | 操作 |
|---|---|
| matched | 体重、体脂及本次实际提供的 BMI 已匹配，无须再上传 |
| missing | 尚不存在的新测量，按 README 用 upload-one 单条试传并核验 |
| repair | 唯一对应的旧记录缺少 BMI 或体脂，使用下面的 repair-one |
| replace / conflict / review_missing / uncertain | 按 README 人工核对，不自动覆盖或重传 |

只有核对过 repair 条目后才执行（以下两项占位文字都需替换）：

```bash
./hg repair-one 实际16位ID --plan state/plans/本次实际清单文件名.json
```

确认屏幕显示的是正确原始时间、体重、体脂和 BMI，再按提示输入 `REPLACE 实际16位ID`。这条路径会先备份，再删除指定旧 Garmin 记录并重建包含 BMI 的记录，随后读回核验；自动 run 不替你删除旧记录。操作状态不确定时使用 verify-operation，不重复 repair-one。
