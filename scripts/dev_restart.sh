#!/usr/bin/env bash
# 重启开发实例（mock 网关 + 调度系统），pid 记在 data/。写成脚本是因为 pkill -f 的模式会匹配到调用它的 shell 自己。
set -u
cd "$(dirname "$0")/.."
S="${SCRATCH:-/tmp}"
MOCK_PAT='mock_gateway'; MOCK_PAT="[${MOCK_PAT:0:1}]${MOCK_PAT:1}.server"     # 拼出 [m]ock_gateway.server，避免匹配到自己
if [ -f data/mock.pid ] && kill -0 "$(cat data/mock.pid)" 2>/dev/null; then kill "$(cat data/mock.pid)"; fi
pkill -f "$MOCK_PAT" 2>/dev/null; sleep 1
(.venv/bin/python -m mock_gateway.server --port "${MOCK_PORT:-18443}" --speed "${MOCK_SPEED:-6}" --prestarted --prelocalized > "$S/mock.log" 2>&1 & echo $! > data/mock.pid)
sleep 2
if [ -f data/app.pid ] && kill -0 "$(cat data/app.pid)" 2>/dev/null; then kill "$(cat data/app.pid)"; sleep 1; fi
(PS_LOG_LEVEL=warning .venv/bin/python -m app.main > "$S/app.log" 2>&1 & echo $! > data/app.pid)
for i in $(seq 1 30); do curl -s -m 1 "http://127.0.0.1:${PS_PORT:-8088}/api/health" >/dev/null 2>&1 && break; sleep 0.5; done
echo "mock pid $(cat data/mock.pid)  app pid $(cat data/app.pid)"
