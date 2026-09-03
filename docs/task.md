# 巡检调度系统（patrol-scheduler）—— 开发管理文档

> 本文档是本次开发的**唯一管理入口**：目标、宪法约束、架构、数据模型、业务流程、开发计划、进度日志、问题清单与未来规划都在这里。
> 代码仓库：`/home/leo/agent/scheduler/`（独立 git 仓库；本文档的同步副本在其 `docs/task.md`，细粒度日志在 `docs/DEVLOG.md`）。
> 本文档在 `Sample_web_api` 里**未被 git 跟踪**（上游仓库 `kafeiyin00/Sample_web_api` 会持续演进），`git clean` 会删掉它 —— 以 `scheduler/docs/task.md` 的副本为备份。

- 开发时段：2026-09-03 22:00 → 2026-09-04 08:00（CST）
- 环境：Ubuntu 22.04，Python 3.10（venv `scheduler/.venv`，`--system-site-packages`），无 Node，有 ffmpeg / Chrome（无头截图验收）/ GPU
- 真机凭据：**本机没有 `CX_KEY`**，开发全程对着自建的 **mock 云端网关**进行；切到真机只需改 `config/.env`

---

## 0. 一句话目标

在 certaintyX 机器狗云端 API 之上，做一个**本地托管的网页操作台**：
用 SQLite 管理「任务航点 / 任务 / 执行记录 / 事件」；从 API 读地图与导航航点；在任务规划面板把导航航点变成带 **prompt + 角度范围 + 答案模版** 的任务航点；
执行任务时**分段下发**（机器狗不能在航点暂停）——每到一个任务航点，收到 `waypoint_reached` 后从 RTSP 抓一张全景，按角度范围裁切，交给 VLM 用 prompt 做「是 / 不是」判断，再按答案模版做 TTS 播报；全部过程有事件面板可看。

---

## 1. 宪法：必须遵守的约束（来自 `Sample_web_api` 文档与示例）

以下每条都在代码里有对应实现，编号用于日志与测试用例引用。**改代码前先对照这张表。**

| 编号 | 约束 | 出处 | 在本系统中的落实 |
|------|------|------|-----------------|
| C1 | 日常读写只走 `/v1/robots/{别名}/…`（冻结契约，认别名）；透传通道 `/api/robots/{机器人ID}/api/…` **只认机器人 ID**，传别名得到误导性的 502 | README §2.2 | 复用 SDK `certaintyx.py`（原样拷贝），透传由 `RobotClient._passthrough` 自动用缓存的 `robotId` |
| C2 | 写操作需要控制权；用 `auto` 模式密钥，停止写入 30s 后自然释放；现场人可抢走控制权（409），程序抢不回来 | README §2.3 | 不做后台续期；409 视为正常，UI 提示「现场有人操作」，执行器退避重试有限次后暂停 |
| C3 | 写操作必须带 `Idempotency-Key`，**重试时复用同一个键**；有意的重新下发（新一次尝试）才换新键 | README §2.4 | 分段任务键格式 `ps-r{run}-l{leg}-a{attempt}`；同一 attempt 内 SDK 自动复用 |
| C4 | 限流 5 rps，429 按 `Retry-After` 退避；轮询间隔不小于 1s；要到达通知用事件流，不轮询 | README §2.5 | 全局令牌桶限速（4 rps）；状态轮询 ≥2s；到达用 SSE `events?stream=1`，`since` 游标续接 |
| C5 | 状态词写读不对称：写 `running` 读回 `navigating`，失败读回 `paused`；判断用响应里的 `active`/`terminal` 布尔或 SDK 的集合 | api-reference「任务状态词」 | 执行器只用 `terminal`/`active`/`status_code`，永不 `== 'running'` |
| C6 | `error_code` 与 `status` 正交；255 既是暂停也是失败，看 `error_code` 区分 | status.md | 结束判定：`status_code==4` 完成；`255` 看 `error_hex` |
| C7 | 不要在代码里抄状态码表，用 `GET /v1/status-codes` 或响应里的 `*_name/*_text` 字段 | README §2.6 | 前端展示只用云端附带的语义字段；mock 网关直接回放真实抓取的表 |
| C8 | 只下发任务机器人不会动：真机必须先 ② 启动设备 → ③ 等启动 → ④ 定位（`node_id` 必须是机器人**真实所在**航点，客户端超时 ≥60s）→ ⑤ 确认 `/position` 200 | full-patrol.md | 「机器人初始化」面板显式提供这三步；执行前置检查要求定位就绪，否则拒绝执行并给出原因 |
| C9 | 判断定位就绪**不能**用 `/perception.Location`（恒为 1）；用 `telemetry.global_localization.received_at`（Unix 秒）新鲜度或 `/position` 是否 200 | api-reference「判断定位是否就绪」 | `StatusPoller` 计算 `loc_age`，就绪 = `received && age<5s`；`Location`/`location_valid` 仅展示不判断 |
| C10 | 事件游标必须在**下发之前**取，否则漏掉起点的 `waypoint_reached`；用服务端 `nextSince` 续接；云端只留 500 条内存，不持久 | full-patrol ⑦、arrival-events | 每段下发前记录 `cursor=events().seq`；常驻监听线程把所有事件落库（`events` 表）；每 5s 用 `GET /task` 对账兜底 |
| C11 | 急停请求体必须显式 `{"active": true}`；空体等于取消急停；急停不是硬件急停也不是锁 | api-reference POST /estop | 只经 SDK `estop()/clear_estop()`；UI 大红按钮二次确认；文案明确「非硬件急停」 |
| C12 | 抓帧必须 `-rtsp_transport tcp`；全景是 2:1 等距投影，分辨率别写死；`rtspPath` 从概览接口读 | video.md | `RtspFfmpegSource` 固定 TCP；分辨率从抓到的图读；`rtspPath` 来自 `info()` |
| C13 | 密钥不进前端、不进仓库；用别名不用机器人 ID 做地址；地图名带时间戳不硬编码 | README §5 | 密钥只存服务端 `config/.env`（gitignored），`/api/settings` 返回掩码；地图名总是从 `/maps` 选 |
| C14 | 停任务不等于停设备；收尾 = 停任务 → 停设备 → 等停完；②–④ 是一次性初始化，之后重复 ⑥⑦ 即可 | full-patrol ⑧ | 执行结束默认**不停设备**（连续多任务）；「结束作业」按钮才做完整收尾 |
| C15 | 出异常先急停再收尾（示例脚本结构）；正常的任务失败（避障/规划失败）则停任务即可 | 03_full_patrol.py | 执行器：未预期异常 → `estop()`（可配置）+ `stop_task()`；`task_failed` → 仅 `stop_task()` 并按重试策略处理 |
| C16 | `visited` 下发瞬间就含起点、起点会报一条到达；两次观测之间走过两个点会补发两条；重连后不会补假到达 | arrival-events | 到达判定看 `waypoint == 本段终点` 且 `nextTarget` 为空，或 `task_completed`；起点到达仅记录 |

---

## 2. 术语（必须区分）

| 术语 | 含义 | 来源 | 存储 |
|------|------|------|------|
| **导航航点** nav waypoint | 地图自带的航点：`node_id`（字符串）、`pose`（位置 + 四元数）、`neighbors` 拓扑 | `GET /maps/{name}/waypoints` | `nav_waypoints`（只读缓存，可重新同步） |
| **任务航点** task waypoint | 巡检点：在某导航航点上（或手工坐标）+ 检查 prompt + 全景角度范围 + 答案模版 | 本系统定义 | `task_waypoints`（**单表**，增删改查） |
| **任务** task | 有序的任务航点列表 + 地图 + 执行选项 | 本系统定义 | `tasks` + `task_items` |
| **子任务 / 段** leg | 从当前航点到下一个任务航点的一次云端 `POST /task`（路径由 `neighbors` 图最短路给出） | 本系统规划 | `run_legs` |
| **执行** run | 一次任务的完整执行过程（多段） | 本系统 | `runs` |
| **检查** inspection | 到点后的抓图 → 裁切 → VLM → TTS 一整条记录 | 本系统 | `inspections` |
| **事件** event | 云端事件（`waypoint_reached` 等）+ 系统事件（下发、抓图、VLM、TTS…） | 云端 + 本系统 | `events` |

---

## 3. 架构

```
浏览器（原生 HTML/CSS/JS，无构建）
   │  REST /api/*        SSE /api/stream（状态、事件、执行进度、TTS 指令）
   ▼
FastAPI（scheduler/app，单进程多线程）
   ├─ api/*               路由层
   ├─ robot/client        SDK 包装：限速令牌桶、robotId 缓存、透传
   ├─ robot/events        常驻 SSE 监听线程（since 游标续接，断线重连，落库，广播）
   ├─ robot/status        状态轮询线程（telemetry/position/task，≥2s）
   ├─ planning/graph      导航航点图：四元数→yaw、最近航点、BFS 最短路
   ├─ executor/runner     执行状态机：分段下发 → 等到达 → 检查 → 下一段
   ├─ executor/inspection 抓图 → 角度裁切 → VLM → TTS
   ├─ media/snapshot      RtspFfmpegSource | SyntheticPanoSource | FileSource
   ├─ media/pano          角度↔像素、跨缝裁切、标注
   ├─ media/pointcloud    PCD 读取 + 体素下采样（接口，后续接真机点云）
   ├─ vlm/*               MockVlm | OpenAICompatVlm（Qwen-VL/Ollama/vLLM/OpenAI）| AnthropicVlm（官方 SDK）
   ├─ tts/*               EdgeTts | CommandTts | 汇出：浏览器 / 本机扬声器 / robot-audio ZMQ（用户已有服务）/ Webhook
   └─ db                  SQLite（WAL），schema.sql 迁移
   │
   ▼  HTTPS（X-API-Key）
certaintyX 云端网关  ──隧道──▶ 机器狗          ← 开发/测试时换成 mock_gateway（同一契约）
```

单机器人（v1）。所有对云端的调用经同一个限速器；所有写操作经 SDK 自带幂等键。

### 3.1 模式切换

`config/.env` 里 `CX_HOST` 指向 `https://certaintyx.sg:8443` 即真机；指向 `http://127.0.0.1:18443` 即 mock。页面顶栏常驻显示当前模式（MOCK 绿色 / REAL 橙色），真机模式下所有会让机器人动的按钮都二次确认。

### 3.2 mock 云端网关（`scheduler/mock_gateway`）

按宪法文档逐条仿真，用于无密钥开发与自动化测试：
- 端点与响应形状与 `api-reference.md` 一致（信封、字段名、嵌套层数）；`/v1/status-codes` 回放 2026-09-03 从生产抓取的真实 JSON。
- 行为复刻：状态词不对称（写 running 读 navigating，失败读 paused）；`Location` 恒为 1；未定位时 `/position` 503；透传传别名 → 502「机器人不在线」；缺 `active` 的急停体 = 取消急停；同键重放 + `Idempotent-Replay: true`；5 rps 限流 429 + `Retry-After`；未知 `task_id` → 400。
- 机器人运动学仿真：沿 path 匀速运动（`MOCK_SPEED`，默认 1 m/s，可加速），`visited` 下发瞬间含起点，逐点产生 `waypoint_reached`（含 `index/total/nextTarget`），结束 `task_completed`；`events` 500 条环形缓冲 + `since` + `stream=1` SSE（心跳注释帧、`Last-Event-ID`）。
- 故障注入 `/mock/*`：下一段避障失败（`task_failed` 0x234B）、掉线、`ros_available=false`、现场抢控制权（409）、丢事件（测对账）。
- 演示地图：`fixtures/map_demo.json`，~40 个航点的环路 + 支路，编号相邻的点相距 0.5–3 m（贴近真实 87 点地图的密度）。

---

## 4. 数据模型（SQLite，`app/schema.sql`）

```
settings(key PK, value, updated_at)                       — 运行时可改的配置（VLM/TTS/抓图源/角度零点…），密钥除外
maps(name PK, synced_at, waypoint_count, pointcloud_path)
nav_waypoints(map_name, node_id, x,y,z, qx,qy,qz,qw, yaw, neighbors JSON)   PK(map_name,node_id)
task_waypoints(id, name, map_name, nav_node_id, x,y,z,yaw,
               prompt, angle_from, angle_to, answer_template JSON,
               reference_image, enabled, created_at, updated_at)             — 用户要求的「单表」
tasks(id, name, map_name, description, options JSON, created_at, updated_at)
task_items(id, task_id FK, seq, task_waypoint_id FK)      — 任务内的有序航点
runs(id, task_id, task_name, map_name, status, mode, current_leg, total_legs, started_at, ended_at, error, summary JSON)
run_legs(id, run_id FK, seq, task_waypoint_id, from_node, to_node, path JSON, idempotency_key,
         status, attempt, dispatched_at, arrived_at, ended_at, cloud_task JSON, error)
inspections(id, run_id, leg_id, task_waypoint_id, waypoint_name, prompt, angle_from, angle_to,
            image_path, crop_path, vlm_provider, vlm_raw, answer, expected, passed,
            tts_text, tts_audio_path, tts_status, latency_ms, created_at)
events(id, ts, source cloud|system, type, cloud_seq UNIQUE, run_id, leg_id, level, message, data JSON)
```

`answer_template` 示例：
```json
{"expected": "yes",
 "on_pass": "消防栓门已关闭，检查通过。",
 "on_fail": "注意：消防栓门未关闭，请及时处理。",
 "on_unknown": "无法判断消防栓状态，请人工复核。"}
```
`passed = (answer == expected)`；`answer ∈ {yes, no, unknown, error}`。

### 4.1 全景角度约定

- 全景为 2:1 等距投影，**图像宽度 ↔ 360°**：`angle = x / W × 360`，左边缘 0°，中心 180°，右边缘 360°（与左边缘为同一经线）。
- 角度范围 `[angle_from, angle_to]` 沿 x 增大方向计；`from > to` 表示**跨缝**（如 330°→30°），裁切时把右段与左段拼接。
- 「机器人正前方对应的角度」`forward_deg` 是设置项（默认 180°，即图像中心），只影响界面上的第二行「相对机头」刻度，不影响存储的绝对角度。真机第一次抓图后按实际画面校准。

---

## 5. 业务流程（执行一个任务）

```
[前置检查]  在线 · 非急停 · ROS 可用 · 定位新鲜（C9）· 无其他执行中 · 任务≥1 个启用航点
   │  任一不满足 → 拒绝执行，界面给出原因与修复入口（初始化面板 / 取消急停）
[定位当前节点]  pos=/position → 最近导航航点 cur（>3m 则警告）
for 每个任务航点 T（按 seq）:
   [规划]   route = BFS(cur → T.nav_node)（neighbors 图）；cur==目标 → 无需导航
   [下发]   cursor = events().seq            ← 必须在下发前（C10）
            POST /task {map_name, path=route}  Idempotency-Key=ps-r{run}-l{seq}-a{attempt}（C3）
   [等待]   消费 cloud_seq>cursor 的事件：
              waypoint_reached → 记录进度；waypoint==终点且 nextTarget 空 → 到达
              task_completed   → 到达
              task_failed      → 段失败（errorHex），stop_task，按 max_retries 重试（新 attempt 新键）或标记失败并暂停执行
              task_stopped     → 外部停止 → 执行标记 aborted
              obstacle / localization / emergency → 记录（丢定位可配置为暂停）
            每 5s GET /task 对账（C10 兜底）：terminal && status_code==4 → 到达；255 → 失败；
            超过 leg_timeout 仍 idle/无进展 → 「任务未执行」（多半是没初始化，C8）
   [稳定]   settle_seconds（默认 2s）让机身稳住
   [检查]   snapshot（RTSP TCP）→ 存全景 → 按 [angle_from, angle_to] 裁切 → VLM(prompt, 全景+裁切)
            → 解析为 yes/no/unknown → 按 answer_template 选 TTS 文本 → 合成 → 汇出到扬声器
            → inspections 落库 → 事件 + SSE 推给页面
   cur = T.nav_node
[结束]   可选 return_to_start 再走一段；runs.status=completed；不停设备（C14）
[任何未预期异常]  estop()（可配置）→ stop_task() → runs.status=failed（C15）
[人工干预]  暂停（当前段完成后停）/ 跳过当前航点 / 中止（DELETE /task）/ 急停
```

---

## 6. 网页（本地托管，`http://127.0.0.1:8088`）

左侧导航 + 顶栏（模式徽标、机器人在线/控制权/急停/定位四个状态灯、急停大按钮）。视图：

| 视图 | 内容 | 关键交互 |
|------|------|---------|
| **总览** | 机器人卡片（位置、定位年龄、急停、ROS、租约）、当前执行、最近检查结果、HLS 实况 | 初始化三步按钮（启动设备 / 定位 / 停设备）、停止任务、急停/取消 |
| **地图与导航航点** | 2D 俯视画布：航点 + 邻接边 + 机器人实时位置 + 点云背景（下采样） | 选地图、同步航点（API 读取）、悬停看 id/坐标、点击「添加为任务航点」、上传点云 |
| **任务航点** | 单表 CRUD | 编辑器：名称 / 导航航点（自动带出 x,y,z,yaw）/ 手工坐标 / prompt / 全景角度编辑器（两条可拖拽竖线 + 刻度 + 范围显示）/ 答案模版 / 参考图（抓一张 / 上传）/ 试问 VLM / 试听 TTS |
| **任务规划** | 任务 CRUD；有序航点列表（拖拽排序）；执行选项 | 从任务航点表或地图加入；路线预览（各段路径、总长）；「执行」 |
| **执行监控** | 当前/历史执行；段进度时间线；小地图轨迹；每个检查的全景/裁切/答案/TTS | 暂停 / 跳过 / 中止；回放 TTS |
| **任务事件** | 云端 + 系统事件统一时间线，可按类型/执行过滤，实时追加 | since 游标翻页 |
| **设置** | 连接（host/别名/密钥掩码）、VLM（provider/base_url/model/key）、TTS（引擎/声音/汇出）、抓图源、forward_deg | 保存即生效；「测试连接」 |

设计语言：浅色专业控制台风格，系统字体 + Noto Sans CJK；状态色 绿/黄/红 只用于状态；所有让机器人动的操作在 REAL 模式二次确认；深色模式跟随系统。

---

## 7. 对外接口（本系统 `/api`）

```
GET  /api/health
GET  /api/robot/status                      聚合快照（在线/租约/急停/ROS/定位年龄/位姿/任务语义字段）
POST /api/robot/init/device-start|device-stop|localize   透传三步（C8）
POST /api/robot/estop {active}              (C11)
DELETE /api/robot/task
POST /api/robot/snapshot                    立即抓一张全景 → /media/…
GET  /api/robot/video                       {hls, rtsp}
GET  /api/maps                              云端地图列表 + 本地同步状态
POST /api/maps/{name}/sync                  拉取航点进 nav_waypoints
GET  /api/maps/{name}/waypoints             缓存（含 yaw）
GET  /api/maps/{name}/route?from=&to=       BFS 预览
POST /api/maps/{name}/pointcloud            上传 pcd/ply；GET …?voxel=0.2 下采样点
GET/POST /api/task-waypoints ; GET/PUT/DELETE /api/task-waypoints/{id}
POST /api/task-waypoints/{id}/reference-image   {source: capture|upload}
POST /api/task-waypoints/{id}/test-vlm      用参考图或现抓图试问
POST /api/tts/test {text}
GET/POST /api/tasks ; GET/PUT/DELETE /api/tasks/{id} ; GET /api/tasks/{id}/plan ; POST /api/tasks/{id}/run
GET  /api/runs ; GET /api/runs/{id} ; POST /api/runs/{id}/pause|resume|skip|abort
GET  /api/runs/{id}/inspections ; GET /api/inspections/{id}
GET  /api/events?since=&type=&run_id=&limit=
GET  /api/stream                            SSE → 浏览器
GET/PUT /api/settings
```

---

## 8. 开发计划与进度

| 阶段 | 时间 | 内容 | 状态 |
|------|------|------|------|
| P0 环境与文档 | 22:00–23:00 | 读宪法、摸环境、venv、本文档、仓库骨架 | ✅ 完成 |
| P1 mock 网关 | 22:30–23:00 | 契约仿真 + 运动学 + 事件流 + 故障注入 + 自测 | ✅ 完成（curl 逐项验证 + pytest 契约测试） |
| P2 后端核心 | 22:40–23:00 | DB、SDK 包装、事件监听、状态轮询、规划、执行器、检查流水线、VLM/TTS 适配器、REST | ✅ 完成 |
| P3 前端 | 22:45–23:10 | 七个视图，全景角度编辑器，地图画布，SSE 实时 | ✅ 首版完成，截图验收通过 |
| P4 测试 | 23:00–23:20 | 单测 + 对 mock 的端到端执行 + 无头浏览器截图验收 | ✅ 首版通过；继续加固中 |
| P5 收尾 | 06:00–08:00 | 文档、日志、TODO、路线图、打 tag | ⬜ |

进度日志（详见 `scheduler/docs/DEVLOG.md`）：

- 2026-09-03 22:05 读完全部宪法文档与示例；确认无真机密钥、`GET /v1` 与 `/v1/status-codes` 可免鉴权抓取（已存为 mock 夹具）。
- 2026-09-03 22:12 venv 建好（fastapi 0.141 / uvicorn 0.52 / edge-tts / anthropic 1.3）；Chrome 无头截图、ffmpeg 合成图、edge-tts 中文合成均验证通过。
- 2026-09-03 22:25 本文档 v1 写完；仓库骨架初始化。
- 2026-09-03 23:00 mock 网关契约逐项验证通过；后端 + 前端 + 测试首版提交（b2c2998, eea9499）。
- 2026-09-03 23:10 演示任务在 mock 上端到端跑通：4 段 / 3 次检查 / TTS / 94 条事件；8 个视图无头截图无 JS 报错。

---

## 9. 问题与 TODO（持续更新，最终版见 `scheduler/docs/TODO.md`）

| # | 问题 | 影响 | 状态 / 处置 |
|---|------|------|-----------|
| T1 | 本机没有真机 API 密钥，所有验证在 mock 上完成 | 真机联调前需要一次「对照 `04_verify_flow.py` 的实测」 | 待用户提供 `CX_KEY` 后跑 `scripts/real_smoke.sh`（只读）|
| T2 | 云端 API 没有「扬声器」端点 | TTS 无法直接送到机器狗 | 汇出做成插件：浏览器 / 本机 / 用户已有 `robot-audio` ZMQ 服务 / Webhook；机器狗端待接口 |
| T3 | 全景图中「机头正前方」对应的列未知 | 角度范围与实际方位的对应需现场校准 | 设置项 `forward_deg`，真机首帧校准 |
| T4 | 云端不暴露地图点云下载 | 地图页点云背景需手工上传 | 保留 `pointcloud` 上传 + 下采样接口 |
| T5 | 机器人不能在航点暂停 | 只能分段下发；每段结束云端任务进入 terminal | 已按分段设计；段间有 settle 时间 |

---

## 10. 未来规划（草案，最终版见 `scheduler/docs/ROADMAP.md`）

1. 真机联调：只读冒烟 → 单段下发 → 全流程；按实测校准 `forward_deg`、settle 时间、leg 超时。
2. 多机器人：`robots` 表 + 每机器人一组监听/轮询/执行线程。
3. 定时任务（cron）与任务队列；失败自动重试策略细化（避障失败 → 换路线）。
4. 全景 → 透视重投影（py360convert）后再给 VLM，减少等距投影的拉伸干扰。
5. 检查结果统计与报表（按航点的通过率、误报回看与人工改判）。
6. 权限与审计（多用户、操作日志）。
