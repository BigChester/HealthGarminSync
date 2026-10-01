# 验证记录

## 开发回归：2026-10-02

Python 3.14.8，182 项离线测试通过；垃圾回收后 SQLite ResourceWarning 为 0。Python/Shell 语法和 Git 差异检查通过，关键改动已独立审查。

本轮覆盖连接关闭、配置及设置校验、命令锁冲突、未确定请求的重复上传保护。

复核命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
bash -n setup.sh hg
git diff --check
```

## 历史设备验收：2026-09-29

Garmin 中国区完整历史替换与独立读回完成：173 条 Apple 测量匹配，远端 173 条，多余记录 0。体重、体脂率和 BMI 已核验；瘦体重仅本地保留。私密运行证据保存在 `state/backups/` 和 `state/operations/`。

LaunchAgent 注册、后台权限和 iPhone 自动化触发仍待设备验收。

## 开发复核待修复

- 单条修复：附近存在其他测量时，应在删除原记录前检查重建条件。
- 对账：唯一匹配附近的多余记录需要显式分类。
- 操作核验：当前读回失败时，应与历史完成状态区分。
