# 实现来源

查阅日期：2026-09-29。

## Apple

- [健康数据来源与权限](https://support.apple.com/zh-cn/108779)
- [HealthKit 指标类型](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier)
- [快捷指令词典](https://support.apple.com/zh-cn/guide/shortcuts/apd43b69f337/ios)
- [App 打开/关闭触发](https://support.apple.com/zh-cn/guide/shortcuts/apde31e9638b/ios)
- [个人自动化](https://support.apple.com/zh-cn/guide/shortcuts/apd602971e63/ios)
- [iCloud 文件下载](https://support.apple.com/guide/mac-help/work-with-folders-and-files-in-icloud-drive-mchl1a02d711/mac)
- [LaunchAgent](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)

## Garmin 客户端

- [固定版本 garminconnect 0.3.16](https://pypi.org/project/garminconnect/0.3.16/)
- [API 实现](https://raw.githubusercontent.com/cyberjunky/python-garminconnect/0.3.16/garminconnect/__init__.py)
- [FIT 编码](https://raw.githubusercontent.com/cyberjunky/python-garminconnect/0.3.16/garminconnect/fit.py)
- [认证与令牌缓存](https://raw.githubusercontent.com/cyberjunky/python-garminconnect/0.3.16/garminconnect/client.py)
- [称重字段解析](https://raw.githubusercontent.com/abrander/garmin-connect/master/Weight.go)

实现要点：完整明细使用 `get_weigh_ins` / `get_daily_weigh_ins`，不使用仅含每日最后值的 dateRange 判断缺失。固定版本 FIT 编码使用 `mktime`，上传时在进程内使用 UTC。Garmin 体重单位为克，体脂为百分数。

## 安装

- [Homebrew](https://brew.sh/)
- [Homebrew Python 3.12](https://formulae.brew.sh/formula/python@3.12)
