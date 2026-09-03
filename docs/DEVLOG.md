# 开发日志（2026-09-03 夜 → 09-04 晨）

时间为 CST，以 git 提交时间为准（并行开发，很多步骤同时进行，段落时间会有重叠）。每条：做了什么 / 遇到什么 / 怎么处理。

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

## 23:30–01:00 加固执行器 + 故障场景测试
- 执行器：订阅事件队列改到**下发之前**（与取游标同理，C10）；等待期间机器人掉线/网关不可达超过 `offline_timeout`（默认 90 s）直接判段失败，不再干等 `leg_timeout`；每条中途 `waypoint_reached` 写进段的 `cloud_task`（界面可看进度）。
- 附整张全景给 VLM 时改用**带角度范围标注**的图，让模型直接看到「第几度到第几度」的黄线。
- 加文件日志 `data/logs/app.log`（滚动 5×5 MB）。
- 新增故障场景测试：掉线、控制权被抢（前置检查拦截 / 途中 409）、暂停-继续-跳过、丢定位。
- **踩坑（测试互相污染）**：一个用例断言失败后，它的执行线程还在后台跑，会对共享的 mock 机器人下发/停止任务，把后面的用例全部拖成「任务下发成功但机器人没有动」。修复：`RunManager.shutdown()` 在应用停止时中止残留执行并等线程结束 —— 这同时也是真机上的正确行为（进程退出前先停下机器人）。
- 测试互相污染的另一处：控制权测试在前置检查阶段就被「控制权被 admin 持有」拦下了（这是正确行为），测试写错了；改成先跑起来、途中抢控制权、第二段 409。

## 01:05–01:15 mock 抓到一个真 bug：幂等键跨 DB 撞键
- 现象：同一 pytest 会话里第一个端到端用例通过，后面所有需要机器人移动的用例都变成「任务下发成功但机器人没有动」；单跑每个用例都通过。
- 排查：给用例加打印，看到第二个用例下发后 mock 里的任务 `path=[]`、`lease=None` —— **POST /task 根本没到处理函数**。原因是 mock 忠实复刻了云端的幂等重放：每个用例用全新的 DB，run id 都从 1 开始，幂等键 `ps-r1-l1-a1` 与上一个用例相同，10 分钟内同键 → 云端直接回放首次响应（200）而**不再执行**。执行器以为下发成功，等到 `not_started_timeout` 才判「没动」。
- 这在真机上同样会发生：换 DB、重装、或两套调度系统对同一台机器人，都可能在 10 分钟窗口内撞键。修复：幂等键加入按 DB 生成一次的随机实例段 `ps-{instance}-r{run}-l{leg}-a{attempt}`（`settings.PS_INSTANCE_ID`）。
- 教训写进 task.md C3。

## 01:15–01:20 全绿 → v0.1.0；真机准备
- 48 项测试全部通过，打 tag `v0.1.0`（mock 上端到端可用）。
- 新增 `scripts/real_smoke.py`：拿到密钥后的只读冒烟，逐项核对本系统依赖的契约点（对 mock 跑：18 项通过、HLS 404 为预期）。
- 新增 `docs/OPERATIONS.md` 真机上线手册：配置 → 只读冒烟 → 上游 04_verify_flow → 初始化 ②③④ → 任务航点/校准 → 执行看护 → 收尾 → 速查表。
- 设置页加「机头校准」：抓一张全景，把蓝线拖到机器人正前方，保存 FORWARD_DEG（TODO T3 的处置手段）。
- VLM 适配器测试：假 Anthropic 客户端（请求形状：image block + effort=low + fallbacks/betas；refusal → unknown；旧 SDK 无 fallbacks 参数时回退），假 OpenAI 兼容服务（Bearer、data URI、system prompt）。
- 演示点云：按走廊两侧墙面生成 7.5 万点 PCD；上传 → 体素 0.3 m 下采样到 9.7 千点；地图页打开时自动加载。
- `./run.sh --mock` 在备用端口实测：两进程都起来、退出时一起关掉。

## 01:20–01:21 收尾小项
- 任务编辑器补「未启动判定 / 掉线判定」两个超时选项；演示脚本在 REAL 模式拒绝 --init/--run/--reset。
- 新用例：手工坐标任务航点（无导航航点 → 取最近的）+ 返回起点 + 附整张全景；跨缝角度范围 330°→30° 原样记录。
- 地图页：有点云的地图打开即自动加载背景（截图确认走廊墙面点云渲染正常）；无头 DOM 检查全景编辑器两条线位置 52.78% / 64.44% 与 190° / 232° 一致。

## 01:21–01:25 从失败段重跑 + 迁移
- 新能力：执行失败/中止后，执行监控页出现「从「某航点」重跑剩余航点」按钮 → `POST /api/tasks/{id}/run?from_seq=N` 新建一次只含剩余航点的执行（前面已完成的不再走）。
- 为此给 `run_legs` 加 `item_seq` 列（记录段对应任务内第几个航点）。第一版把它塞进 `cloud_task` JSON，被后续对账写入覆盖 → 改成独立列，schema 升到 v2：空库一次建全，旧库 `ALTER TABLE` 增量迁移，并加了迁移测试。
- 新测试：ffmpeg 管道抓帧（lavfi 合成源）、抓图源构造分支、事件监听的轮询回退（`_poll_for`：落库、去重、游标推进）与 SSE 实连。

## 01:25–01:27 开发实例回归
- 开发实例重启：旧库 v1 → v2 迁移成功（`run_legs.item_seq` 列出现，无报错）。
- 注入避障故障跑演示任务：因任务 `max_retries=1`，第 1 段失败后自动换新幂等键重试成功，整趟完成（重试路径在真实进程里验证）；再用 `from_seq=2` 重跑剩余航点 → 3 段完成，执行名带「（从第 2 个航点重跑）」。
- `data/logs/app.log` 无 ERROR。

## 01:27–01:28 运维小件
- `/api/health` 扩成系统自检（版本/实例/DB 版本/线程存活/状态快照年龄/媒体占用/请求计数/适配器），设置页有「系统自检」卡片。
- `scripts/cleanup_media.py --keep-days N [--events] [--dry-run]`：清理已结束执行的媒体与过期事件。
- 令牌桶限速器单测（2 突发 + 10/s 排队），健康检查与清理脚本测试。
- 开发实例：把演示任务改成 `max_retries=0` 再注入避障故障 → 执行 #4 如实失败（第 1 段 0x234B，后续段标中止），截图确认执行监控显示失败原因与「从『3 号消防栓』重跑剩余航点」按钮。

## 01:28–01:31 停滞判定、事件归属
- 执行器新增 `stall_timeout`（默认 180 s）：云端任务 active 但长时间没有新的 `waypoint_reached`（持续避障/卡住）→ 段失败「停滞」，不必等满 `leg_timeout`。用例：dispatch 后注入 60 s 避障，4 s 判停滞。
- 从失败执行的截图发现：执行详情的事件列表只有系统事件，云端的 `task_failed`/`waypoint_reached` 没挂在执行上。改为监听线程落库时问执行管理器「当前执行/段」并打标（`run_id/leg_id`），执行详情里现在能看到云端事件；段失败原因附云端 `error_name/error_text`（如 0x234B OBSTACLE_FAILURE 避障失败）。
- 任务航点表加参考图缩略图列。

## 01:31–01:33 质量收尾
- ruff 静态检查接入（`ruff.toml`，E9/F/B006），清掉一个未使用变量；`requirements.lock` 锁定 venv 内 36 个包的精确版本。
- 演示脚本给每个任务航点抓参考图，任务航点表出现缩略图列。

## 01:38–01:43 关掉几条 TODO
- T13 外部任务识别：等待到达期间若 `task_started.path` 不是本段路径 → 判定现场下发了别的任务，本次执行中止且**不去停对方的任务**（用例：导航途中 mock 直接 start_task 另一条路径）。
- T19 幂等诚实失败：下发返回 409 时先查 `GET /task`，若云端任务已是本段路径则按已下发继续等，而不是当失败。
- T20 控制权 409 的等待秒数与重试次数改为任务选项（`lease_retry_seconds` / `lease_retries`），编辑器里可填。
- T17 所有时间戳改为带时区偏移的本地 ISO（`+08:00`），云端事件的 `ts` 毫秒也照此转换；清理脚本的 cutoff 同步改为 aware。
- T16 `deploy/patrol-scheduler.service` + 部署说明（cron 清理、反代提醒）。
- 68 用例全绿，ruff 干净。

## 01:45–02:05 丢定位引导恢复、HLS 备选、统计
- T9：任务选项 `lost_localization_action = pause | continue | fail`（默认 pause）：丢定位 → 停下云端任务、段回到 pending、执行暂停并提示「用当前最近航点重新定位后点继续」；恢复后从当前位置重规划该段。
- 又一次被幂等键教育：第一版恢复后把 attempt 减回去复用同一个键 → 云端（mock）回放首次响应、机器人不动 → 判「任务未执行」。改为 attempt 只增不减（键永远新），失败次数单独计数。
- T6 备选：`SNAPSHOT_SOURCE=hls` / 直接给 m3u8 地址，ffmpeg 从 HLS 取一帧。
- `/api/stats`：近 N 天按任务航点的检查次数/通过率/平均耗时、最近失败；总览页加统计表。

## 02:05–02:30 定时计划
- 新增定时执行：`schedules` 表（schema v3）+ 调度线程（每 20 s 检查到点计划）+ `/api/schedules` CRUD/立即触发 + 任务页「⏰ 定时」弹窗 + 总览「下次定时执行」。两种计划：每天固定时刻（多个 HH:MM）、每 N 分钟；到点时若已有执行在跑则跳过并记事件；不引入 cron 库。
- 丢定位暂停用例的最后一条断言写错（机器人只走了 0.4 m，最近航点仍是 1），改为断言从重新定位时的最近航点重规划。
