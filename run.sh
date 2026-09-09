#!/usr/bin/env bash
# 启动巡检调度系统。
#   ./run.sh              读 config/.env（没有则默认连本机 mock 网关 18443）
#   ./run.sh --mock       同时拉起 mock 网关（后台），退出时一起关掉
#   MOCK_SPEED=3 ./run.sh --mock   让仿真机器人跑快一点
set -euo pipefail
cd "$(dirname "$0")"
PY="${PS_PYTHON:-.venv/bin/python}"

# ── Python 环境净化（跨平台稳定性的关键一条）─────────────────────────────
# 这台机器的 ~/.bashrc 里 source 了 /opt/ros/humble/setup.bash 与 tianyi 工作区，
# 于是每个 shell 都带着 11 个目录的 PYTHONPATH，而它排在 venv 的 site-packages
# **前面** —— venv 里装的 numpy/Pillow/requests 会被 ROS 或工作区里的同名包顶掉，
# 版本随 `apt upgrade` 悄悄变化（实测：venv 里 445 个包，其中 numpy/pytest/scipy
# 各有两个版本并存）。清掉之后 venv 里只剩 55 个必需的包，全部来自 venv 自身。
# 没有 ROS 的机器上这几行是无害的空操作。
unset PYTHONPATH PYTHONHOME
export PYTHONNOUSERSITE=1                # 也别用 ~/.local/lib 里的用户级包
[ -x "$PY" ] || { echo "缺少可用的 Python 环境。先执行: ./bootstrap.sh"; exit 1; }
MOCK_PID=""
if [ "${1:-}" = "--mock" ]; then
  $PY -m mock_gateway.server --port "${MOCK_PORT:-18443}" --speed "${MOCK_SPEED:-1.0}" ${MOCK_ARGS:-} &
  MOCK_PID=$!
  trap '[ -n "$MOCK_PID" ] && kill $MOCK_PID 2>/dev/null || true' EXIT
  sleep 1
fi
exec $PY -m app.main
