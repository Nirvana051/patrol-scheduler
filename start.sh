#!/usr/bin/env bash
# 一键启停巡检调度系统（后台运行，pid/日志在 data/）。
#   ./start.sh            按 config/.env 启动（真机或 mock 由 CX_HOST 决定），打开浏览器
#   ./start.sh --mock     先起本机 mock 网关再起调度系统（不改 config/.env，临时指向 mock）
#   ./start.sh --audio    同时起本机播报服务 audio_server（127.0.0.1:5566），用来听 TTS
#   ./start.sh --status   看运行状态与机器人前置检查
#   ./start.sh --stop     全部停掉
#   ./start.sh --restart  重启（等价 --stop 后 ./start.sh，参数可叠加：--restart --mock --audio）
set -u
cd "$(dirname "$0")"
PY=.venv/bin/python
LOG=data/logs; RUN=data/run; mkdir -p "$LOG" "$RUN"
PORT="${PS_PORT:-8088}"; MOCK_PORT="${MOCK_PORT:-18443}"; AUDIO_PORT="${AUDIO_PORT:-5566}"
MOCK=0; AUDIO=0; ACTION=start
for a in "$@"; do case "$a" in
  --mock) MOCK=1;; --audio) AUDIO=1;; --status) ACTION=status;; --stop) ACTION=stop;; --restart) ACTION=restart;;
  -h|--help) sed -n 2,9p "$0"; exit 0;; *) echo "未知参数 $a"; exit 1;; esac; done

alive() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
stop_one() { if alive "$1"; then kill "$(cat "$RUN/$1.pid")" 2>/dev/null; for _ in $(seq 1 40); do alive "$1" || break; sleep 0.25; done; alive "$1" && kill -9 "$(cat "$RUN/$1.pid")" 2>/dev/null; echo "已停止 $1"; fi; rm -f "$RUN/$1.pid"; }
wait_http() { for _ in $(seq 1 60); do curl -s -m 1 "$1" >/dev/null 2>&1 && return 0; sleep 0.5; done; return 1; }

do_stop() { stop_one app; stop_one mock; stop_one audio; }
do_status() {
  for n in app mock audio; do alive "$n" && echo "● $n 运行中 (pid $(cat "$RUN/$n.pid"))" || echo "○ $n 未运行"; done
  if alive app; then
    curl -s -m 5 "http://127.0.0.1:$PORT/api/health" | $PY -c "import json,sys; d=json.load(sys.stdin); print(f\"  模式 {d['mode']}  云端 {d['host']}  机器人 {d['robot']}  线程 {d['threads']}  事件监听 {'已连' if d['events']['connected'] else '未连'}\")" 2>/dev/null
    curl -s -m 20 "http://127.0.0.1:$PORT/api/robot/preflight" | $PY -c "
import json,sys; d=json.load(sys.stdin)
print('  前置检查', '全部通过' if d['ok'] else '未通过')
for c in d['checks']: print('   ', '✅' if c['ok'] else '❌', c['text'])" 2>/dev/null || echo "  （前置检查读取失败）"
    echo "  页面 http://127.0.0.1:$PORT   日志 $LOG/app.out"
  fi
}

case "$ACTION" in
  stop) do_stop; exit 0;;
  status) do_status; exit 0;;
  restart) do_stop;;
  start) if alive app; then echo "调度系统已在运行，先停掉再启动（等价 --restart）"; do_stop; fi;;
esac

[ -x "$PY" ] || { echo "缺少 .venv，先执行: python3 -m venv --without-pip --system-site-packages .venv && pip3 --python .venv/bin/python install -r requirements.txt"; exit 1; }
[ -f config/.env ] || [ "$MOCK" = 1 ] || { echo "没有 config/.env（真机凭据）。真机：cp config/env.example config/.env 并填 CX_KEY；仿真：./start.sh --mock"; exit 1; }

if [ "$MOCK" = 1 ]; then
  (nohup $PY -m mock_gateway.server --port "$MOCK_PORT" --speed "${MOCK_SPEED:-2}" --prestarted --prelocalized > "$LOG/mock.out" 2>&1 & echo $! > "$RUN/mock.pid")
  wait_http "http://127.0.0.1:$MOCK_PORT/healthz" || { echo "mock 网关没起来，看 $LOG/mock.out"; exit 1; }
  export CX_HOST="http://127.0.0.1:$MOCK_PORT" CX_ROBOT=ntu-dog-00001 CX_KEY="cx_mock0001_$(printf '0%.0s' $(seq 1 48))" SNAPSHOT_SOURCE=synthetic
  echo "mock 网关 http://127.0.0.1:$MOCK_PORT（机器人已初始化、站在航点 1）"
fi
if [ "$AUDIO" = 1 ]; then
  (nohup $PY -m audio_server --host 127.0.0.1 --port "$AUDIO_PORT" > "$LOG/audio.out" 2>&1 & echo $! > "$RUN/audio.pid")
  wait_http "http://127.0.0.1:$AUDIO_PORT/health" && echo "播报服务 http://127.0.0.1:$AUDIO_PORT（设置里 TTS_SINKS 加 http、地址填这个）"
fi
(nohup env PS_LOG_LEVEL="${PS_LOG_LEVEL:-warning}" $PY -m app.main > "$LOG/app.out" 2>&1 & echo $! > "$RUN/app.pid")
wait_http "http://127.0.0.1:$PORT/api/health" || { echo "调度系统没起来，看 $LOG/app.out"; tail -20 "$LOG/app.out"; exit 1; }
do_status
if [ -n "${DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1 && [ "${NO_BROWSER:-0}" != 1 ]; then xdg-open "http://127.0.0.1:$PORT" >/dev/null 2>&1 & fi
