# 实现依据与核验范围

查阅日期：2026-09-29。以下为主要官方/作者源；本机已完成 Garmin 中国区 173 条完整历史替换及体重/体脂/BMI 读回，并收到用户生成的四字段 iCloud JSON。未直接操作 iPhone 快捷指令内部动作或后台自动化，详见 TEST_REPORT.md。

## Apple

- 健康 App 的第三方权限、实际数据来源检查：
  https://support.apple.com/zh-cn/108779
- HealthKit 身体指标类型（体重、BMI、体脂、瘦体重等）：
  https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier
- 瘦体重与饮水量的类型含义，不替代肌肉量或体水分率：
  https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/leanbodymass
  https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/dietarywater
- 静息能量消耗类型，不直接等同于称重报告的基础代谢率：
  https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/basalenergyburned

- 快捷指令 JSON 与词典/列表：
  https://support.apple.com/zh-cn/guide/shortcuts/apd0f2e057df/ios
- 快捷指令词典动作：
  https://support.apple.com/zh-cn/guide/shortcuts/apd43b69f337/ios
- App 打开/关闭触发（“已关闭”包括切出）：
  https://support.apple.com/zh-cn/guide/shortcuts/apde31e9638b/ios
- 个人自动化设置：
  https://support.apple.com/zh-cn/guide/shortcuts/apd602971e63/ios
- Health 数据访问保护与锁定规则：
  https://support.apple.com/guide/security/protecting-access-to-users-health-data-sec88be9900f/web
  注：数据保护规则不构成快捷指令动作在锁屏状态下可运行的保证。
- iCloud Drive 文件与保留下载：
  https://support.apple.com/guide/mac-help/work-with-folders-and-files-in-icloud-drive-mchl1a02d711/mac
- launchd / LaunchAgent 与定时执行：
  https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
- iCloud 数据安全类别：
  https://support.apple.com/en-us/102651

## Garmin 非官方上传客户端的作者源

- 项目、Python 要求、登录令牌示例、非官方性质：
  https://github.com/cyberjunky/python-garminconnect
- PyPI 固定版本 0.3.16（2026-09-18 发布）：
  https://pypi.org/project/garminconnect/0.3.16/
- 固定版本中国区选项、add_body_composition、get_body_composition：
  https://raw.githubusercontent.com/cyberjunky/python-garminconnect/0.3.16/garminconnect/__init__.py
  同一源码提供 get_weigh_ins（weight/range）和 get_daily_weigh_ins（dayview）。已通过真实返回确认：get_body_composition 的 dateRange 只包含每日最后值；完整替换及日常对账必须使用 range 的 dailyWeightSummaries/allWeightMetrics 或 dayview/dateWeightList，且每日 numOfWeightEntries 与明细长度一致。
- 固定版本 FIT 编码（注意 timestamp 中的 mktime）：
  https://raw.githubusercontent.com/cyberjunky/python-garminconnect/0.3.16/garminconnect/fit.py
- 固定版本登录、令牌缓存和网络访问：
  https://raw.githubusercontent.com/cyberjunky/python-garminconnect/0.3.16/garminconnect/client.py
- 作者登录示例（master 会变化，安装仍固定依赖）：
  https://raw.githubusercontent.com/cyberjunky/python-garminconnect/master/example.py

## 远端身体数据字段

- 另一 Garmin 客户端作者的 weight-service 解析：weight 以 gram 存储，bodyFat 为百分数：
  https://raw.githubusercontent.com/abrander/garmin-connect/master/Weight.go

此非官方返回格式可能变化。脚本只识别 dateWeightList 及可明确解析的时间；结构不认识或时间不完整时停止写入，不把未知结果当成空列表。读回核验所用模拟数据是测试夹具，不是来自用户真实账号。

## 安装

- Homebrew Python 3.12：
  https://formulae.brew.sh/formula/python@3.12
- Homebrew 官方安装与环境说明：
  https://brew.sh/

## 非来源事实的内容

快捷指令变量名、JSON schema、目录、去重与补传策略、配对窗口、CLI 和测试均为本次设计，不是 Apple 或 Garmin 的官方集成方案。说明中的具体测量数值均为示例，不代表用户数据。
