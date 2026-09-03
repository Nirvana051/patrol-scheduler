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

## 23:00–23:20 首次端到端 + 截图验收
- 演示任务（消防栓 → 通道方格 → 北侧出口 → 返回起点）在 mock 上 30 s 跑完 4 段、3 次检查（mock VLM 交替 yes/no，edge-tts 合成 0.7–1.1 s），事件 94 条落库。
- 踩坑：seed 脚本第一次定位失败 drift 4.6 m —— 因为上一轮 curl 冒烟把仿真机器人开到了航点 3，而 seed 用航点 1 做初值。**正好复现宪法 C8「node_id 必须是真实所在航点」**。给 seed 加了 `--reset`（mock 专用复位）。
- 踩坑：`pkill -f "python -m app.main"` 把自己的 shell 也杀了（模式匹配到自身命令行）。改为 pid 文件 + `pgrep -f "[a]pp.main"` 技巧。
- 踩坑：无头 Chrome `--virtual-time-budget` 在页面有 SSE 长连接时永不结束 → 前端加 `?nosse=1` 静态模式（拉一次状态，不开 EventSource），截图脚本走这个入口。
- 无头截图 8 张，JS 控制台无报错。修正：静态模式下总览卡片为空（状态到达未触发重绘 → setStatus 统一广播）；hls.js 改为普通 script 引入；任务编辑器里可直接新建任务航点。
- 测试：mock 契约、API、端到端全部通过；`/mock/preempt` 的 `seconds=0` 被 `or 60` 吞掉 → 修复；裁切宽度像素取整 ±1 → 放宽断言。
