#!/bin/sh
# 本机手动跑一轮：自动加载同目录的 local.env（不入库）
set -e
cd "$(dirname "$0")"
if [ -f local.env ]; then
  set -a
  . ./local.env
  set +a
fi
exec python3 apple_push.py "$@"
