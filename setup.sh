#!/bin/bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
if [ "$(uname -s)" != Darwin ]; then
  echo '自动安装脚本面向 macOS。离线测试可直接运行 python3 -m unittest discover -s tests -v。' >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  if command -v brew >/dev/null 2>&1 && [ -x "$(brew --prefix python@3.12 2>/dev/null)/bin/python3.12" ]; then
    PY="$(brew --prefix python@3.12)/bin/python3.12"
  elif command -v python3.12 >/dev/null 2>&1; then
    PY="$(command -v python3.12)"
  elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; assert sys.version_info >= (3,12)' >/dev/null 2>&1; then
    PY="$(command -v python3)"
  else
    echo '需要 Python 3.12+。已有 Homebrew 时先执行：brew install python@3.12' >&2
    exit 1
  fi
  "$PY" -m venv .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info >= (3,12), "需要 Python >=3.12"'
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip freeze > requirements.lock.txt
chmod 700 hg
printf '\n安装及离线测试完成。下一步：./hg init\n'
