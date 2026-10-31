#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
# 默认只预检。管理员明确传入 serve 才在本机启动HTTPS，不执行远程部署。
exec "${PYTHON:-python}" scripts/operations.py "${1:-preflight}"
