# patrol-scheduler · 巡检调度系统

> 建立在 [kafeiyin00/Sample_web_api](https://github.com/kafeiyin00/Sample_web_api)（certaintyX 机器狗云端 API 教程）之上。
> `app/vendor/certaintyx.py` 是该仓库 SDK 的原样拷贝，随上游更新同步。
> 凭据只放在 `config/.env`（已 gitignore），仓库里不含任何密钥。

在 certaintyX 机器狗云端 API 之上的本地网页操作台：管理任务航点 / 任务 / 执行 / 事件，
按「当前航点 → 下一个任务航点」分段下发巡检；到点后抓全景 → 按角度范围裁切 → VLM 判「是 / 不是」→ 按答案模版 TTS 播报。

主管理文档（目标、宪法约束、架构、计划、进度、问题、规划）：`../Sample_web_api/docs/task.md`（同步副本 `docs/task.md`）。
**文档索引 `docs/README.md`**（按「要干什么」挑一篇），**使用说明 `docs/USAGE.md`**（日常操作从这里开始），开发日志 `docs/DEVLOG.md`，问题清单 `docs/TODO.md`，路线图 `docs/ROADMAP.md`，变更记录 `CHANGELOG.md`，**真机上线手册 `docs/OPERATIONS.md`**，界面截图 `docs/SCREENSHOTS.md`，全流程测试方案 `docs/TEST_PLAN.md`，最近一轮测试报告 `docs/TEST_REPORT.md`。
本系统自身的 REST 文档在运行时的 `/api/docs`（FastAPI 自动生成）。默认只绑定 127.0.0.1、无登录；要暴露到局域网请自行加反向代理与鉴权。

## 功能

- **任务航点**（单表）：导航航点或手工坐标 + 检查 prompt + 全景角度范围（两条可拖拽线）+ 答案模版（是/不是 → 三句播报）+ 参考图；试问 VLM、试听 TTS。
- **任务与执行**：**可指定起始点** + 有序任务航点 → 分段下发（起点 → 第一个任务航点 → 下一个…，`neighbors` 最短路）；到点 → 抓全景 → 裁切 → VLM → TTS；暂停/跳过/中止、失败后从剩余航点重跑、定时计划。
- **看护**：前置检查（在线/急停/ROS/定位新鲜/云端空闲/控制权）、掉线/停滞/外部任务/控制权 409/丢定位（停下等重定位）判定与记录、进程退出先停机器人。
- **可观测**：云端 + 系统事件时间线（云端事件挂到执行/段）、按航点统计与人工改判、失败通知 webhook、CSV 导出、系统自检、文件日志。
- **适配器**：VLM（mock / 通义千问 DashScope / OpenAI 兼容 / Anthropic 官方 SDK）、TTS（edge-tts / 命令行 / 浏览器朗读）与汇出（浏览器 / 本机 / 自带 `audio_server` 播报服务 / webhook）、抓图源（RTSP / HLS / 合成 / 文件）。
- **mock 云端网关**：按 Sample_web_api 文档契约仿真，含运动学与故障注入，整套测试都对着它跑。

## 跑起来

```bash
./bootstrap.sh --dev                       # 建隔离的 .venv + 按 requirements.lock 装依赖 + 自检
cp -n config/env.example config/.env       # 填真机凭据；不填默认连本机 mock 网关（-n = 已存在就不动）

./start.sh --mock                          # 起 mock 网关(18443) + 调度系统(8088)，后台运行
make seed                                  # 灌演示数据并跑一遍
# 浏览器打开 http://127.0.0.1:8088
```

一键启停：`./start.sh`（真机，按 `config/.env`）/ `--mock`（仿真）/ `--audio`（同时起本机播报服务）/
`--status` / `--stop` / `--restart`。日志在 `data/logs/`。前台跑用 `./run.sh`。

真机：`config/.env` 里把 `CX_HOST/CX_ROBOT/CX_KEY` 换成真值，`SNAPSHOT_SOURCE=rtsp`，
先 `make smoke`（只读冒烟，不会让机器人动），再 `./start.sh`。详见 `docs/OPERATIONS.md`。
页面顶栏会显示 **REAL**，所有让机器人动的按钮都会二次确认。先在「总览」做 ② 启动设备 → ④ 定位，再执行任务。

> ⚠️ **别用 `python3 -m venv --system-site-packages`**（本文档曾经这么教）。
> 机器人这类机器的系统 Python 往往就是 ROS / CUDA 环境，而 `source /opt/ros/*/setup.bash`
> 还会设置 `PYTHONPATH` —— 它排在 venv 的 `site-packages` **前面**，把 venv 里装的
> numpy / Pillow / requests 顶掉，版本随 `apt upgrade` 悄悄变。实测过一次：venv 里能看到
> **445 个包**，numpy/pytest/scipy 各有两个版本并存。`bootstrap.sh` 建的是隔离环境（**55 个包**），
> `start.sh` / `run.sh` / `Makefile` 启动前都会清掉 `PYTHONPATH`。
> **自己在命令行直接跑 `.venv/bin/python` 时记得也清**：`env -u PYTHONPATH PYTHONNOUSERSITE=1 .venv/bin/python …`
> —— 或者干脆走 `make`，它已经代你清好了。详见 `docs/USAGE.md` §1。

## 常用命令（Makefile）

`make start` / `make stop` / `make status` / `make run` / `make mock` / `make test` / `make lint` /
`make docs-check` / `make shots` / `make seed` / `make smoke` / `make clean-media` / `make backup` /
`make vlm` / `make eval` / `make audio-server` / `make lock`（`make` 不带参数看全部说明）

## 测试

```bash
make test          # 全量 118 用例：单测 + mock 契约 + API + 端到端执行（约 4 分钟）
make lint          # ruff 静态检查（ruff.toml）
make docs-check    # 文档体检：链接/命令/版本号/用例数/schema 版本与代码是否一致
make shots         # 无头 Chrome 截图每个视图并收集 JS 错误（需调度系统在跑）

# 浸泡测试：22 类场景轮换（正常/故障注入/人工操作/运维动作/网络抖动），
# 核对每类的预期结局与幂等键唯一性，每轮记 RSS/线程数/fd 数。VLM 用 mock，不消耗任何付费接口。
env -u PYTHONPATH PYTHONNOUSERSITE=1 .venv/bin/python scripts/soak.py --rounds 22
```
依赖精确版本见 `requirements.lock`（`bootstrap.sh` 按它安装；更新用 `make lock`）。
最近一轮测试的完整结果见 `docs/TEST_REPORT.md`。

## 目录

```
bootstrap.sh    一键准备 Python 环境（隔离 venv + 精确版本 + 自检）
start.sh        一键启停（后台，pid/日志在 data/）；run.sh 是前台版
audio_server/   播报服务（扬声器端，纯标准库，部署到机器狗或现场 PC）
app/            FastAPI 后端（api/ 路由，robot/ 云端连接与线程，executor/ 分段执行，media/ 全景与点云，vlm/ tts/ 适配器）
app/vendor/     certaintyx.py —— Sample_web_api 仓库 6167083 的 SDK 原样拷贝，不改
web/            原生 HTML/CSS/JS 前端（无构建）
mock_gateway/   按文档契约仿真的云端网关 + 机器狗运动学
tests/          pytest（118 用例）
scripts/        演示数据、浸泡测试、真机冒烟、备份/清理、截图、文档体检
docs/           管理文档副本、使用说明、上线手册、测试方案与报告、开发日志、TODO、路线图
```
