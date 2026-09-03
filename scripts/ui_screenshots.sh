#!/usr/bin/env bash
# 用无头 Chrome 给每个视图截图 + 收集 JS 控制台错误。用法: scripts/ui_screenshots.sh [base_url] [out_dir]
BASE="${1:-http://127.0.0.1:8088}"; OUT="${2:-shots}"; mkdir -p "$OUT"
ROUTES="dashboard map waypoints tasks runs events settings ${EXTRA_ROUTES:-}"
for r in $ROUTES; do
  name="${r//\//_}"
  timeout 60 google-chrome --headless=new --disable-gpu --no-sandbox --hide-scrollbars --window-size=1440,1000 \
    --enable-logging=stderr --v=0 --virtual-time-budget=6000 --screenshot="$OUT/$name.png" "$BASE/?nosse=1#/$r" 2>&1 \
    | grep -E "CONSOLE|Uncaught|Error" | grep -v -E "hls|favicon|ERR_BLOCKED|GPU|dbus|sandbox" | sed "s/^/[$name] /" | head -20
done
ls -la "$OUT"
