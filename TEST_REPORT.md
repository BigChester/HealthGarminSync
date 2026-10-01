# 验证记录

## 开发回归：2026-10-02

Python 3.14.8，278 项离线测试通过；垃圾回收后 ResourceWarning 为 0。Python/Shell 语法和 Git 差异检查通过，关键改动已独立审查。

覆盖导出配对、对账修复、状态持久化、只读诊断和后台运行。日志限额与运行证据包含临时子进程硬中断验证；开发验证使用临时数据和模拟 API。

复核命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
bash -n setup.sh hg
git diff --check
```

## 历史设备验收：2026-09-29

Garmin 中国区完整历史替换与独立读回完成：173 条 Apple 测量匹配，远端 173 条，多余记录 0。体重、体脂率和 BMI 已核验；瘦体重仅本地保留。私密运行证据保存在 `state/backups/` 和 `state/operations/`。

LaunchAgent 注册、后台权限和 iPhone 自动化触发仍待设备验收。
