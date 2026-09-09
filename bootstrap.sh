#!/usr/bin/env bash
# 在一台新机器上把 Python 环境准备好（Ubuntu / macOS）。
#   ./bootstrap.sh              建 .venv（隔离）+ 按 requirements.lock 装依赖 + 自检
#   ./bootstrap.sh --dev        额外装 pytest / ruff（跑用例与语法检查要用）
#   ./bootstrap.sh --recreate   已有 .venv 也重建（旧的挪到 .venv.old）
#
# 为什么要隔离：venv 默认可能继承系统站点包，而这类机器上系统 Python 往往是
# ROS / CUDA 环境；更麻烦的是 `source /opt/ros/*/setup.bash` 会设置 PYTHONPATH，
# 它排在 venv 的 site-packages **前面**，把 venv 里装的包顶掉。所以这里
# 既建隔离 venv，也在装包与自检时清掉 PYTHONPATH（start.sh / run.sh 同样会清）。
set -eu
cd "$(dirname "$0")"

DEV=0; RECREATE=0
for a in "$@"; do case "$a" in
  --dev) DEV=1;; --recreate) RECREATE=1;;
  -h|--help) sed -n 2,6p "$0"; exit 0;;
  *) echo "未知参数 $a"; exit 1;; esac; done

unset PYTHONPATH PYTHONHOME
export PYTHONNOUSERSITE=1

say() { printf '%s\n' "$*"; }
ok()  { printf '  ✅ %s\n' "$*"; }
bad() { printf '  ❌ %s\n' "$*"; }

# ── ① 找一个 3.10+ 的解释器 ──────────────────────────────────────────────
PYBIN=""
for c in "${PS_PYTHON_BASE:-}" python3.13 python3.12 python3.11 python3.10 python3; do
  [ -n "$c" ] || continue
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PYBIN="$c"; break; fi
done
[ -n "$PYBIN" ] || { bad "找不到 Python 3.10+。Ubuntu: sudo apt install python3.10 python3.10-venv"; exit 1; }
ok "解释器 $PYBIN（$("$PYBIN" --version 2>&1)）"

# ── ② 建隔离 venv ────────────────────────────────────────────────────────
if [ -d .venv ] && [ "$RECREATE" = 0 ]; then
  ok ".venv 已存在（要重建加 --recreate）"
else
  [ -d .venv ] && { rm -rf .venv.old; mv .venv .venv.old; ok "旧 .venv 挪到 .venv.old"; }
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  if [ -x "$UV" ]; then
    "$UV" venv .venv --python "$PYBIN" --seed >/dev/null
    ok "用 uv 建好 .venv（自带 pip）"
  elif "$PYBIN" -c 'import ensurepip' 2>/dev/null; then
    "$PYBIN" -m venv .venv
    ok "用 python -m venv 建好 .venv"
  else
    bad "这台机器的 python 缺 ensurepip（venv 建不出 pip），也没有 uv。二选一："
    say  "     sudo apt install -y python3-venv       # 装上 ensurepip"
    say  "     curl -LsSf https://astral.sh/uv/install.sh | sh   # 或装 uv（不需要 sudo）"
    exit 1
  fi
fi
PY=.venv/bin/python
[ -x "$PY" ] || { bad "$PY 不存在（venv 建失败）"; exit 1; }

# 确认真的隔离了：pyvenv.cfg 里不能继承系统包
if grep -q 'include-system-site-packages *= *true' .venv/pyvenv.cfg 2>/dev/null; then
  bad ".venv 继承了系统站点包（ROS/CUDA 会漏进来）。用 ./bootstrap.sh --recreate 重建"
  exit 1
fi
ok "venv 是隔离的（include-system-site-packages = false）"

# ── ③ 装依赖：有 lock 就按 lock（可复现），否则按 requirements.txt ─────────
PIPQ="-q --disable-pip-version-check"
if [ -f requirements.lock ]; then
  "$PY" -m pip install $PIPQ -r requirements.lock
  ok "按 requirements.lock 装好（$(grep -c '==' requirements.lock) 个精确版本）"
else
  "$PY" -m pip install $PIPQ -r requirements.txt
  ok "按 requirements.txt 装好（建议之后 pip freeze > requirements.lock 固定版本）"
fi
if [ "$DEV" = 1 ]; then
  "$PY" -m pip install $PIPQ 'pytest>=8' 'ruff>=0.6'
  ok "开发依赖已装（pytest / ruff）"
fi

# ── ④ 自检：每个包必须来自 venv 内部 ─────────────────────────────────────
say ""
say "自检："
"$PY" - <<'PYCHK'
import os, sys
root = os.path.realpath('.venv')
bad = []
for m in ('fastapi', 'uvicorn', 'starlette', 'pydantic', 'requests', 'PIL', 'numpy', 'edge_tts', 'multipart'):
    try:
        mod = __import__(m)
    except Exception as e:
        bad.append(f'{m} 导入失败: {e}'); continue
    f = os.path.realpath(getattr(mod, '__file__', '') or '')
    v = getattr(mod, '__version__', '?')
    inside = f.startswith(root)
    print(f'  {"✅" if inside else "❌"} {m:10} {v:12} {"" if inside else "← 来自 venv 之外：" + f}')
    if not inside:
        bad.append(f'{m} 来自 venv 之外')
foreign = [p for p in sys.path if p and not os.path.realpath(p).startswith(root)
           and not os.path.realpath(p).startswith(os.path.realpath(sys.base_prefix))]
if foreign:
    print('  ❌ sys.path 里有外来目录（多半是 PYTHONPATH 没清）：')
    for p in foreign: print('       ', p)
    bad.append('sys.path 不干净')
raise SystemExit(1 if bad else 0)
PYCHK
say ""
ok "环境就绪。启动：./start.sh --mock      跑用例：./bootstrap.sh --dev 之后 make test"
