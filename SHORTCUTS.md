# iPhone 快捷指令搭建说明

适用链路：PICOOC → Apple 健康 → iPhone 快捷指令 → iCloud Drive → Mac 本地脚本。

本包不含已签名、可一键导入的 `.shortcut` 文件。下列流程需在 iPhone 的「快捷指令」中搭建。体重、体脂、BMI、瘦体重四个子快捷指令只返回样本，总快捷指令将它们保存为一份 `schema_version=2` 的 JSON。体重必需；其他三项有实际样本时一并导出。瘦体重（lbm）仅在 Mac 保留，不上传为 Garmin 肌肉量。Mac 仅运行 Python 上传端，不运行读取健康样本的快捷指令。界面名称可能显示「词典」或「字典」，本文同时给出关键英文名。

当前完整历史文件 `health-20260929-225644-003.json` 已包含 173 次测量的四项指标，Mac 已通过解析和配对。全部 173 次测量已与 Garmin 体重、体脂及 BMI 明细匹配，多余记录为 0；瘦体重仅本地保存。已有快捷指令无须为此次数据再次搭建或导出。下文供以后维护或重建。

## 已有快捷指令：补齐 BMI 的操作顺序

无需重建现有体重、体脂导出。按以下顺序修改：

1. 健康 App → 头像 → 隐私下的「App」（部分版本显示「App 和服务」）→ PICOOC。在允许写入的项目中打开体重、体脂率、身体质量指数（BMI），以实际可选项目为准。若列表没有 BMI，不能通过快捷指令授予 PICOOC 不支持的写入能力。[Apple 权限说明](https://support.apple.com/zh-cn/108779)
2. 打开 PICOOC，确认 Apple 健康同步设置已开启，等待一次真实测量写入。健康 App 搜索「身体质量指数」，打开「显示所有数据」，点开该测量，确认数值、时间和来源为 PICOOC。开启权限不保证旧记录会补写；完整历史需另外检查。
3. 长按现有「导出 PICOOC 体重」→「复制」，重命名为「导出 PICOOC BMI」。只修改查找样本类型和单位，详见第 3 节。手动运行并允许读取 BMI，检查返回 samples 的实际数值。拒绝健康读取权限也可能表现为空结果，不能仅凭零样本断定没有数据。
4. 编辑总快捷指令「同步 PICOOC 到 Garmin」，按第 4 节 D 的动作顺序接入 BMI。键名必须为小写 `bmi`。同时把原来拼错的 `bodt_fat` 改成 `body_fat`。
5. 首次补齐历史：四个子快捷指令的日期范围使用同一起止日期。至少覆盖此前已经上传、但需要补 BMI 的测量；完成后恢复近 7 天。只给 BMI 扩大范围、却不导出对应体重，会使那些 BMI 没有配对对象。
6. 若旧错误文件 `health-20260929-165620-165.json` 仍在 `Health/inbox`，在 iPhone「文件」App 中将它移到 `Health/archive` 保留，再手动运行总快捷指令。该文件目前已不在正式 inbox，无须再次处理。改手机键名不会修复已经保存的旧 JSON；旧错文件留在 inbox 会阻止解析。不要移动其他正确的历史导出。
7. Mac 执行 `./hg inspect`、`./hg preview`、`./hg compare`。inspect 应出现 bmi 样本，preview 应显示实际 BMI 数字。已有 Garmin 记录缺 BMI 时会列为 repair，按第 7 节处理；不要重新 upload-one 制造第二条记录。

如果 Apple 健康没有 PICOOC 来源的 BMI，本次只能导出实际存在的指标；程序不会计算一个值冒充 PICOOC 的测量。水分、骨量和肌肉量还没有接入本版，不要把它们填进 bmi 或 body_fat。

## 仅经 Apple 健康：全部字段的获取范围

你的数据来源限定为 PICOOC → Apple 健康，不能直接取得 PICOOC 的完整导出文件。因此本版补齐的是这条链路中能对应 Garmin 身体成分的实际样本，不承诺补齐 PICOOC App 的全部报告项。

| PICOOC/身体成分指标 | Apple 健康与当前快捷指令 | Garmin 处理 |
|---|---|---|
| 体重 | 查询体重原始样本，来源 PICOOC | 上传 weight，已真实验证 |
| 体脂率 | 查询体脂率原始样本，来源 PICOOC | 上传 percent_fat，已真实验证 |
| BMI | 查询身体质量指数原始样本，来源 PICOOC；最新真实文件已存在 | 上传 bmi，已完成真实补字段及读回验收 |
| 瘦体重/去脂体重 | Apple 有 leanBodyMass 类型，最新真实文件已存在；导出键为 lbm | 正式 JSON 接受并在本地保留、显示；当前上传接口没有同义字段，不能填成肌肉量 |
| 肌肉量/骨骼肌量 | 本链路尚无可用原始样本；不能从瘦体重直接替代 | 缺少来源数据，不能补齐 muscle_mass |
| 骨量 | 本链路尚无可用原始样本 | 缺少来源数据，不能补齐 bone_mass |
| 体水分率 | Apple 的饮水量是摄入体积，不是体水分百分数 | 缺少来源数据，不能补齐 percent_hydration |
| 基础代谢率（kcal/天） | 静息能量是某段时间消耗的能量，不能直接当作 PICOOC 称重报告中的代谢率 | 没有同次测量的来源值，不补 basal_met |
| 活动代谢率（kcal/天） | 活动能量消耗不能直接当作称重报告代谢率 | 不补 active_met |
| 内脏脂肪质量/等级 | 当前 Apple 健康导出未提供这类同次测量数据；等级不能当 kg | 不补 visceral_fat_mass / visceral_fat_rating |
| 代谢年龄、体型等级 | 当前链路没有对应来源样本 | 不补 metabolic_age / physique_rating；不使用年龄或 Garmin 健康年龄替代 |
| 蛋白质比例、身体评分等报告项 | 当前链路没有对应来源样本；饮食蛋白质也不是身体蛋白质比例 | 当前上传接口没有同义字段，不混入其他指标 |

上述 Apple 类型及含义参见 [瘦体重](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/leanbodymass)、[饮水量](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/dietarywater)、[静息能量](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/basalenergyburned)。Garmin 底层可上传字段见固定版 [add_body_composition](https://github.com/cyberjunky/python-garminconnect/blob/0.3.16/garminconnect/__init__.py)。接口有字段不代表 Apple 健康中已经有它的数据。

检查手机的写入权限和「显示所有数据」后，没有的指标保持缺失。增加查找动作、导出全部健康 XML、改 JSON 键名，都不能取得从未写入的值。若以后能从 PICOOC 取得完整原始数据，再核对字段定义、单位与时间，扩展导入和远端验证。

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

子快捷指令只返回 `samples`，无需版本号、指标名称或导出时间。不要在这里添加转文本、设定文件名或存储文件动作。最终由总快捷指令保存一次。

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

不要根据猜测选单位。先同时看健康中显示的体脂率与 JSON 中的原始输出，再决定。更改单位不会改变苹果健康中的数据。

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
5. 手动运行副本并允许读取 BMI，对照健康中的同一条记录验收。不使用主图表的日平均值，不把去脂体重填成肌肉量。

没有样本时，计数为 0 的分支按第 1 节 A 返回无内容。总快捷指令继续导出体重和已有体脂，不加入 0、null、空文本或上一条 BMI。

### 瘦体重子快捷指令（本地保留）

再复制「导出 PICOOC 体重」，命名 `导出 PICOOC 瘦体重`。「查找健康样本」类型改为「瘦体重 / Lean Body Mass」，日期范围和 PICOOC 来源过滤与其他三项一致，单位选 kg。仍取原始开始日期、值、来源，返回 samples 词典。单独运行并授予读取权限，与健康中的同一条样本核对。

保留数值及单位，例如 `72.2 kg`；裸数值沿用 Mac 的 weight_bare_unit（当前为 kg）。不要输出百分比，也不要用体重减去体脂自行替代原始瘦体重样本。程序会检查瘦体重不得大于对应体重，配对有歧义时暂停该测量。

## 4. 组合成一个总快捷指令

新建 `同步 PICOOC 到 Garmin`。下文变量都通过「设定变量」建立；点参数选择变量，不要将变量名当作文字输入。涉及多个词典的动作需明确指定输入。

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

体脂率可选：无样本时导出纯体重；查询或权限错误仍应让本次停止，不使用缓存或 0 代替。

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

重新赋值 Metrics 的动作不能省略。不要把变量名作为文字输入，也不要将整个数组再套在数组里。

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

在「设定词典值」中手工输入键 `bmi`，值通过变量选择器插入 BMISamples；不能插入整个 BMIPayload，也不能填写文字“BMISamples”。重新设定 Metrics 的动作要放在对应「如果」内。BMI 没有数据时继续保存体重及体脂，避免留下前一次运行的 BMI。

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

键名必须为小写 `lbm`。它与 BMI 分开保存；瘦体重空结果不阻止其他指标导出。后面的顶层 metrics 一定使用最后更新的 Metrics。

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

最后“设定变量 Payload = 刚更新后的顶层词典”。外层只填写 `schema_version`、`exported_at` 和 `metrics`，不改键名为 `version`。BMI 可以完全省略，不必加空数组。

### F. 只在这里保存一次

依次添加：

| 动作 | 配置 |
|---|---|
| 从输入中获取文本 | 输入变量 Payload，得到完整 JSON 文本 |
| 设定名称 | `health-[FileStamp].json`，方括号部分表示插入 FileStamp 变量 |
| 存储文件 | 输入命名后的文件；固定为 iCloud Drive/快捷指令/Health/inbox；关闭询问位置；关闭覆盖已有文件 |

如果“从输入中获取文本”不好找，也可用“文本”动作，内容**只插入整个 Payload 词典变量**；保存前确认结果是一份有效 JSON。不要手工拼接引号和逗号。先用首选动作。

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

再建“时间”自动化，选你通常会使用、解锁手机的时间，例如当地 20:00，每天，立即运行同一总快捷指令。它只是补传机会，不是长期锁屏状态下的必达保证。

没有“Apple 健康出现新体脂样本”这一本文所使用的系统自动化触发器；我们用 App 退出和时间触发，而不是声称真正监听每次 Health 写入。

### 实际验收

称重→打开 PICOOC 确认新测量→切回桌面→检查 iCloud 有新导出→Mac `./hg run`→检查 Garmin。第一次看到任何授权提示都需手动完成。

iOS 仍可能要求解锁读取健康数据。系统的数据保护规则不等于快捷指令动作承诺“锁屏后十分钟内一定可运行”；本方案按可能需要解锁设计。失败后可以手动点总快捷指令补传，不能绕过系统权限。

## 6. 7 天窗口与长期停机

每次重新导出近 7 天是为了补上偶发失败，Mac 会根据样本去重。超过 7 天从未成功导出的手机数据不会凭空出现在 Mac；此时临时把四个查找范围改成 30 天，手动导出一次，再恢复 7 天。

如果旧 JSON 已经到达 inbox，即使 Mac 关机超过 7 天，文件仍保留，恢复后可以处理。Mac 不会自动删除导出文件。修改或删除 Apple 健康历史记录不会自动删除 Garmin 中已有记录，需要单独核对。

## 来源

Apple 快捷指令 JSON、词典、变量、App 触发和隐私机制的官方说明，以及开源上传端依据，见 `docs/SOURCES.md`。本文是搭建方案，不是已在你的 iPhone 上实际执行过的记录。

## 历史导出与对账

历史整理时，将各个「查找健康样本」的日期条件改为明确的历史起止范围，数据多时按月导出，来源仍需是已确认的 PICOOC。体脂/BMI 没有时可省略，体重仍导出；不要扩大来源到 Garmin 自己写回的数据。

每份文件最大 4 MB、样本合计最多 20,000 条；超过时缩小日期范围。历史导出完成后，使用 README.md 中的 compare 命令核对，日常导出再恢复近 7 天。数据是否完整需要按 Apple 健康实际历史验收，不能仅凭 Mac 的文件数量判断。

指标键名为 weight、body_fat、bmi、lbm。此前出现的 bodt_fat 拼写已在最新真实文件中改为 body_fat，旧错误文件也已不在正式 inbox。以后旧错误文件若被放回 inbox，仍会阻止解析。

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

当前真实账号已验证体重、体脂及 BMI，瘦体重保留在本地测量和备份。自动写入仍保持关闭，补齐步骤不会自行启用它。
