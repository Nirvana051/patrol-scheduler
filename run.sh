#!/usr/bin/env bash
# 启动巡检调度系统。
#   ./run.sh              读 config/.env（没有则默认连本机 mock 网关 18443）
#   ./run.sh --mock       同时拉起 mock 网关（后台），退出时一起关掉
#   MOCK_SPEED=3 ./run.sh --mock   让仿真机器人跑快一点
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
[ -x "$PY" ] || { echo "缺少 .venv，先执行: python3 -m venv --without-pip --system-site-packages .venv && pip3 --python .venv/bin/python install -r requirements.txt"; exit 1; }
MOCK_PID=""
if [ "${1:-}" = "--mock" ]; then
  $PY -m mock_gateway.server --port "${MOCK_PORT:-18443}" --speed "${MOCK_SPEED:-1.0}" ${MOCK_ARGS:-} &
  MOCK_PID=$!
  trap '[ -n "$MOCK_PID" ] && kill $MOCK_PID 2>/dev/null || true' EXIT
  sleep 1
fi
exec $PY -m app.main
