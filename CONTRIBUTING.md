# 开发贡献

使用 Python 3.12+，在本地非 iCloud 目录开发。`master` 保存稳定版本，日常开发在 `develop`；复杂功能或修复使用 `feat/*` 隔离，优先快进合并，完成后删除临时分支。按可独立验证的切片提交。

## 环境与检查

```bash
python3.12 -m venv .venv
make dev
make format
make check
make test
```

`make dev` 安装 requirements.txt 和 requirements-dev.txt；`format` 运行 Ruff 和 mdformat，`check` 检查 Python / Markdown 格式、Shell 语法及 Git 差异，`test` 运行 unittest。默认解释器 `.venv/bin/python`，可用 `make PYTHON=python ...` 指定。macOS 用户安装入口为 `make setup` 或 `bash scripts/setup.sh`。

需要提交钩子时：

```bash
.venv/bin/python -m pre_commit install
```

本地钩子调用 `make check`。CI 在 Python 3.12 与较新稳定版执行检查和离线测试；以工作流实际结果为准。

直接复核：

```bash
.venv/bin/python -m unittest discover -s tests -v
bash -n scripts/setup.sh health-garmin-sync
git diff --check
```

## 实现与验证边界

模块职责见 [架构](docs/ARCHITECTURE.md)。关键实现须独立审查；测试使用临时目录和模拟 API，覆盖实际失败/恢复边界。开发验证与设备验收分别记录在 [验证记录](docs/VERIFICATION.md)。

真实 Garmin 操作、源数据修改和后台任务注册仅按用户明确需求执行。保持未知请求防重、完整称重明细核对和瘦体重仅本地保留规则。配置、state、虚拟环境、凭据及健康数据不进 Git，生成日志也不提交。

用户可见变化写入 [CHANGELOG](CHANGELOG.md) 的 Unreleased，保留必要操作与结论，避免重复历史记录。
