# 开发日志（2026-09-03 夜 → 09-04 晨）

时间为 CST。每条：做了什么 / 遇到什么 / 怎么处理。

## 22:00–22:15 读宪法、摸环境
- 通读 Sample_web_api 的 README、6 篇 docs、全部示例（Python/curl/Node）与 SDK。整理出 16 条硬约束（task.md §1）。
- 环境：Ubuntu 22.04，Python 3.10（无 fastapi/flask/uvicorn），无 Node，有 ffmpeg / Chrome / GPU / pulseaudio，pip 联网可用。
- **没有真机 API 密钥**（`examples/curl/env.sh` 不存在，环境变量里也没有）。决定：自建 mock 云端网关，整晚对着它开发与测试；真机联调留待用户提供 `CX_KEY`。
- 云端免鉴权端点实测可达：`GET /v1`、`GET /v1/status-codes`、`/healthz` 返回已存为 mock 夹具（真实状态码表，不在代码里抄表）。
- 用户已有 `tts_cmq_dev/robot-audio`（ZMQ 播报服务，豆包 TTS，跑在机器人上）—— TTS 汇出预留 `zmq` 插件与之对接。

## 22:15–22:30 建环境、写主文档
- `python3 -m venv --without-pip --system-site-packages`（系统没有 ensurepip），`pip3 --python` 装 fastapi/uvicorn/edge-tts/anthropic；系统 cv2/PIL/numpy/pytest 透过 system-site-packages 可见。
- edge-tts 中文合成实测成功（3.3s mp3）；Chrome 无头截图、ffmpeg lavfi 合成图、Noto CJK 字体均可用。
- 写完 task.md v1（重写版）。

## 22:30–23:00 mock 网关 + 后端骨架
- 生成 45 点演示地图（环形走廊 + 两条支路，编号沿路径顺序，相邻 0.5–3 m）。
- mock 网关按契约逐条复刻；用 curl 逐项验证：401 / 503 / Location=1 / 字典序键 / 未初始化下任务 200 但 idle / 设备启停 task_id / 400「任务 ID 不存在」/ drift_exceeded 与成功定位 / 幂等重放头 / 起点 waypoint_reached / 状态词 navigating→completed / 急停空体=取消 / viewer 403 / 5 rps 限流 429。
- 后端：schema、config、db、bus、SDK 包装（限速）、导航图、全景/抓图/点云、VLM/TTS 适配器、状态轮询、事件监听、ops、检查流水线、执行器、REST/SSE、入口。首个提交 b2c2998。

## 23:00–23:10 前端与测试
- 前端 7 个视图 + 地图画布 + 全景角度编辑器（两条可拖拽竖线，支持跨缝）写完。
- 测试套件写完（单测 / mock 契约 / API / 端到端）。
