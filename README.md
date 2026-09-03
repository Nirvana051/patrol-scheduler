# patrol-scheduler · 巡检调度系统

在 certaintyX 机器狗云端 API 之上的本地网页操作台：管理任务航点 / 任务 / 执行 / 事件，
按「当前航点 → 下一个任务航点」分段下发巡检；到点后抓全景 → 按角度范围裁切 → VLM 判「是 / 不是」→ 按答案模版 TTS 播报。

主管理文档（目标、宪法约束、架构、计划、进度、问题、规划）：`../Sample_web_api/docs/task.md`（同步副本 `docs/task.md`）。
开发日志 `docs/DEVLOG.md`，问题清单 `docs/TODO.md`，路线图 `docs/ROADMAP.md`，变更记录 `CHANGELOG.md`，**真机上线手册 `docs/OPERATIONS.md`**，界面截图 `docs/SCREENSHOTS.md`。
本系统自身的 REST 文档在运行时的 `/api/docs`（FastAPI 自动生成）。默认只绑定 127.0.0.1、无登录；要暴露到局域网请自行加反向代理与鉴权。

## 功能

- **任务航点**（单表）：导航航点或手工坐标 + 检查 prompt + 全景角度范围（两条可拖拽线）+ 答案模版（是/不是 → 三句播报）+ 参考图；试问 VLM、试听 TTS。
- **任务与执行**：有序任务航点 → 分段下发（当前航点 → 下一个任务航点，`neighbors` 最短路）；到点 → 抓全景 → 裁切 → VLM → TTS；暂停/跳过/中止、失败后从剩余航点重跑、定时计划。
- **看护**：前置检查（在线/急停/ROS/定位新鲜/云端空闲/控制权）、掉线/停滞/外部任务/控制权 409/丢定位（停下等重定位）判定与记录、进程退出先停机器人。
- **可观测**：云端 + 系统事件时间线（云端事件挂到执行/段）、按航点统计与人工改判、失败通知 webhook、CSV 导出、系统自检、文件日志。
- **适配器**：VLM（mock / OpenAI 兼容 / Anthropic 官方 SDK）、TTS（edge-tts / 命令行 / 浏览器朗读）与汇出（浏览器 / 本机 / robot-audio ZMQ / webhook）、抓图源（RTSP / HLS / 合成 / 文件）。
- **mock 云端网关**：按 Sample_web_api 文档契约仿真，含运动学与故障注入，整套测试都对着它跑。

## 跑起来

```bash
python3 -m venv --without-pip --system-site-packages .venv
pip3 --python .venv/bin/python install -r requirements.txt
cp config/env.example config/.env          # 填真机凭据；不填默认连本机 mock 网关

./run.sh --mock                            # 起 mock 网关(18443) + 调度系统(8088)
.venv/bin/python scripts/seed_demo.py --init --run --mock-speed 6   # 灌演示数据并跑一遍
# 浏览器打开 http://127.0.0.1:8088
```

真机：`config/.env` 里把 `CX_HOST/CX_ROBOT/CX_KEY` 换成真值，`SNAPSHOT_SOURCE=rtsp`，先 `.venv/bin/python scripts/real_smoke.py`（只读冒烟），再 `./run.sh`。详见 `docs/OPERATIONS.md`。
页面顶栏会显示 **REAL**，所有让机器人动的按钮都会二次确认。先在「总览」做 ② 启动设备 → ④ 定位，再执行任务。

## 常用命令（Makefile）

`make run` / `make mock` / `make test` / `make lint` / `make shots` / `make seed` / `make smoke` / `make clean-media` / `make lock`

## 测试

```bash
.venv/bin/python -m pytest              # 单测 + mock 契约 + API + 端到端执行（约 2 分钟）
.venv/bin/python -m ruff check .         # 静态检查（ruff.toml）
scripts/ui_screenshots.sh                # 无头 Chrome 截图每个视图并收集 JS 错误（需调度系统在跑）
```
依赖精确版本见 `requirements.lock`（`pip3 --python .venv/bin/python freeze --local` 生成）。

## 目录

```
app/            FastAPI 后端（api/ 路由，robot/ 云端连接与线程，executor/ 分段执行，media/ 全景与点云，vlm/ tts/ 适配器）
app/vendor/     certaintyx.py —— Sample_web_api 仓库 a23fc40 的 SDK 原样拷贝，不改
web/            原生 HTML/CSS/JS 前端（无构建）
mock_gateway/   按文档契约仿真的云端网关 + 机器狗运动学
tests/          pytest
scripts/        演示数据、截图、文档同步
docs/           管理文档副本、开发日志、TODO、路线图
```
