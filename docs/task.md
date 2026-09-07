# 巡检调度系统（patrol-scheduler）—— 开发管理文档

> 本文档是本次开发的**唯一管理入口**：目标、宪法约束、架构、数据模型、业务流程、开发计划、进度日志、问题清单与未来规划都在这里。
> 代码仓库：`/home/leo/agent/scheduler/`（独立 git 仓库；本文档的同步副本在其 `docs/task.md`，细粒度日志在 `docs/DEVLOG.md`）。
> 本文档在 `Sample_web_api` 里**未被 git 跟踪**（上游仓库 `kafeiyin00/Sample_web_api` 会持续演进），`git clean` 会删掉它 —— 以 `scheduler/docs/task.md` 的副本为备份。

- 开发时段：2026-09-03 22:00 → 2026-09-04 08:00（CST）
- 环境：Ubuntu 22.04，Python 3.10（venv `scheduler/.venv`，`--system-site-packages`），无 Node，有 ffmpeg / Chrome（无头截图验收）/ GPU
- 真机凭据：开发期间**没有 `CX_KEY`**，全程对着自建的 **mock 云端网关**进行；09-04 13:56 拿到密钥后已切到真机（`config/.env`），只读冒烟见 §9 T1

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
| C2 | 写操作需要控制权；用 `auto` 模式密钥，停止写入 30s 后自然释放；现场人可抢走控制权（409），程序抢不回来 | README §2.3 | 不做后台续期；409 视为正常，UI 提示「现场有人操作」；执行器按任务选项等待 `lease_retry_seconds` 重试 `lease_retries` 次，仍不行判段失败；前置检查发现控制权被人持有时直接拒绝执行 |
| C3 | 写操作必须带 `Idempotency-Key`，**重试时复用同一个键**；有意的重新下发（新一次尝试）才换新键；键在 10 分钟内**全局**不能重复，否则云端只回放不执行 | README §2.4 | 分段任务键 `ps-{instance}-r{run}-l{leg}-a{attempt}`（`instance` 为每个 DB 生成一次的随机段，避免换库/多套部署撞键 —— mock 测试实际抓到过这个 bug）；同一 attempt 内 SDK 自动复用。**上游 6167083（2026-09-06）起透传通道也支持幂等键**，SDK 自动为 `device/start|stop`、`localization/execute` 生成 |
| C4 | 限流 5 rps，429 按 `Retry-After` 退避；轮询间隔不小于 1s；要到达通知用事件流，不轮询 | README §2.5 | 全局令牌桶限速（4 rps）；状态轮询 ≥2s；到达用 SSE `events?stream=1`，`since` 游标续接 |
| C5 | 状态词写读不对称：写 `running` 读回 `navigating`，失败读回 `paused`；判断用响应里的 `active`/`terminal` 布尔或 SDK 的集合 | api-reference「任务状态词」 | 执行器只用 `terminal`/`active`/`status_code`，永不 `== 'running'` |
| C6 | `error_code` 与 `status` 正交；255 既是暂停也是失败，看 `error_code` 区分 | status.md | 结束判定：`status_code==4` 完成；`255` 看 `error_hex` |
| C7 | 不要在代码里抄状态码表，用 `GET /v1/status-codes` 或响应里的 `*_name/*_text` 字段 | README §2.6 | 前端展示只用云端附带的语义字段；mock 网关直接回放真实抓取的表 |
| C8 | 只下发任务机器人不会动：真机必须先 ② 启动设备 → ③ 等启动 → ④ 定位（`node_id` 必须是机器人**真实所在**航点，客户端超时 ≥60s）→ ⑤ 确认 `/position` 200 | full-patrol.md | 「机器人初始化」面板显式提供这三步；执行前置检查要求定位就绪，否则拒绝执行并给出原因 |
| C9 | 判断定位就绪**不能**用 `/perception.Location`（旧版机器人端恒为 1；2026-09-06 起的新版才如实给 0，但不能假设对面是哪个版本）。用 `telemetry.global_localization.received_at` 新鲜度；**`/position` 200 也不够** —— 机器人端停止发布该话题后云端会一直回放缓存值（2026-09-07 真机实测：位姿冻结在 20 小时前、`/position` 仍 200） | api-reference「判断定位是否就绪」+ 真机实测 | `StatusPoller`：有遥测时 就绪 = `received && age<5s && /position 非 503`，并标 `localization_stale`；拿不到遥测才退回 `/position`；`Location` 仅展示 |
| C10 | 事件游标必须在**下发之前**取，否则漏掉起点的 `waypoint_reached`；用服务端 `nextSince` 续接；云端只留 500 条内存，不持久 | full-patrol ⑦、arrival-events | 每段下发前记录 `cursor=events().seq`；常驻监听线程把所有事件落库（`events` 表）；每 5s 用 `GET /task` 对账兜底 |
| C11 | 急停请求体必须显式布尔 `{"active": true}`；旧版机器人端把空体当「取消急停」，2026-09-06 起的新版直接 400；急停不是硬件急停也不是锁，实测还有 5–7 s 滑行 | api-reference POST /estop | 只经 SDK `estop()/clear_estop()`；UI 大红按钮二次确认；文案明确「非硬件急停」 |
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
   ├─ vlm/*               MockVlm | QwenVlm（DashScope 兼容模式，自动带 enable_thinking:false）| OpenAICompatVlm（Ollama/vLLM/OpenAI）| AnthropicVlm（官方 SDK）
   ├─ tts/*               EdgeTts | CommandTts | 汇出：浏览器 / 本机扬声器 / 自带 audio_server（HTTP，机器狗端）/ Webhook
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
- 机器人运动学仿真：下发后先 `nav_preprocess`（status 2，`MOCK_PREPROCESS` 秒）再 `navigating`，沿 path 匀速运动（`MOCK_SPEED`，默认 1 m/s，可加速），`visited` 下发瞬间含起点，逐点产生 `waypoint_reached`（含 `index/total/nextTarget`），结束 `task_completed`；`events` 500 条环形缓冲 + `since` + `stream=1` SSE（心跳注释帧、`Last-Event-ID`）。
- 机器人端版本：`/mock/robot-version`（`legacy` 默认，与现场那台一致 —— Location 恒 1、estop 空体当 false；`new` = 2026-09-06 起已修复：Location 如实 0、estop 缺 active 返回 400）。
- 故障注入 `/mock/*`：冻结遥测（`freeze_telemetry`，模拟机器人端停发 /global_localization 而 /position 仍回放缓存）、停止指令被忽略（`ignore_stop`）、nginx 版 HTML 502、下一段避障/规划失败（`task_failed` 0x234B/0x234C）、掉线、`ros_available=false`、现场抢控制权（`preempt`，复刻「人可抢程序、程序抢不了人」）、持续避障（停滞）、丢定位、丢事件（测对账）、传送、复位、调速。
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
tasks(id, name, map_name, description, options JSON, created_at, updated_at)   — options 含 start_node（起始导航航点，null=自动）
task_items(id, task_id FK, seq, task_waypoint_id FK)      — 任务内的有序航点
runs(id, task_id, task_name, map_name, status, mode, current_leg, total_legs, started_at, ended_at, error, summary JSON)
run_legs(id, run_id FK, seq, task_waypoint_id, from_node, to_node, path JSON, idempotency_key,
         status, attempt, dispatched_at, arrived_at, ended_at, cloud_task JSON, error)
inspections(id, run_id, leg_id, task_waypoint_id, waypoint_name, prompt, angle_from, angle_to,
            image_path, crop_path, vlm_provider, vlm_raw, answer, expected, passed,
            tts_text, tts_audio_path, tts_status, latency_ms, human_passed, human_note, human_at (v4), capture_pose JSON (v5), created_at)
events(id, ts, source cloud|system, type, cloud_seq UNIQUE, run_id, leg_id, level, message, data JSON)
schedules(id, task_id FK, kind daily|interval, spec, enabled, last_run_at, last_result, next_run_at, created_at, updated_at)  — v3
```
schema 版本 v5：空库一次建全，旧库按版本 `ALTER/CREATE` 增量迁移（`app/db.py`）。

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
[确定起点]  任务里指定了 start_node → 用它（并核对机器人实际距它多远，超 start_node_max_distance 只警告；该航点不在地图里则直接拒绝执行）
            未指定 → pos=/position → 最近导航航点 cur（>3m 则警告）；读不到位姿 → 假定在首个任务航点
for 每个任务航点 T（按 seq）:
   [规划]   route = BFS(cur → T.nav_node)（neighbors 图）；cur==目标 → 无需导航
   [下发]   cursor = events().seq            ← 必须在下发前（C10）
            先订阅事件队列，再取游标，再 POST /task {map_name, path=route}  Idempotency-Key=ps-{instance}-r{run}-l{seq}-a{attempt}（C3，每次下发换新键）
   [等待]   消费 cloud_seq>cursor 的事件：
              waypoint_reached → 记录进度；waypoint==终点且 nextTarget 空 → 到达
              task_completed   → 到达
              task_failed      → 段失败（errorHex + 云端 error_name），stop_task，按 max_retries 重试（新键）或执行 failed
              task_stopped     → 外部停止 → 执行 aborted
              task_started(path ≠ 本段) → 现场下发了别的任务 → 执行 aborted，且不去停对方的任务
              localization(valid=false) → 按 lost_localization_action：pause（默认：停任务、段回 pending、暂停等人工重定位后继续）/ continue / fail
              obstacle / emergency → 记录
            每 5s GET /task 对账（C10 兜底）：terminal && status_code==4 → 到达；255 → 失败（原因附云端 error_name/error_text）；
            not_started_timeout 内仍 idle/无进展 → 「任务未执行」（多半是没初始化，C8）；
            云端不可达/掉线超过 offline_timeout → 段失败；active 但超过 stall_timeout 没有新到达 → 「停滞」；leg_timeout 兜底
   [稳定]   settle_seconds（默认 2s）让机身稳住
   [检查]   snapshot（RTSP TCP）→ 存全景 → 按 [angle_from, angle_to] 裁切 → VLM(prompt, 全景+裁切)
            → 解析为 yes/no/unknown → 按 answer_template 选 TTS 文本 → 合成 → 汇出到扬声器
            → inspections 落库 → 事件 + SSE 推给页面
   cur = T.nav_node
[结束]   可选 return_to_start 再走一段；runs.status=completed；不停设备（C14）
[任何未预期异常]  estop()（可配置）→ stop_task() → runs.status=failed（C15）
[人工干预]  暂停（当前段完成后停）/ 跳过当前航点 / 中止（DELETE /task）/ 急停；失败或中止后可「从第 N 个航点重跑剩余航点」（新执行）
[进程退出]  中止进行中的执行并 DELETE /task，再停线程（真机安全）
```

---

## 6. 网页（本地托管，`http://127.0.0.1:8088`）

左侧导航 + 顶栏（模式徽标、在线/控制权/急停/定位/云端任务五个状态灯、位姿、声音开关、停任务、急停大按钮）。视图：

| 视图 | 内容 | 关键交互 |
|------|------|---------|
| **总览** | 机器人卡片（位置、定位年龄、急停、ROS、租约、事件流状态）、云端任务语义字段、当前执行 / 下次定时执行、最近检查、近 30 天按航点统计、HLS 实况（mock 下为合成图） | 初始化三步按钮（启动设备 / 定位 / 停设备）、刷新（前置检查）、抓一张全景、停止任务、急停/取消 |
| **地图与导航航点** | 2D 俯视画布：航点 + 邻接边 + 机器人实时位置 + 点云背景（下采样） | 选地图、同步航点（API 读取）、悬停看 id/坐标、点击「添加为任务航点」、上传点云 |
| **任务航点** | 单表 CRUD | 编辑器：名称 / 导航航点（自动带出 x,y,z,yaw）/ 手工坐标 / prompt / 全景角度编辑器（两条可拖拽竖线 + 刻度 + 范围显示）/ 答案模版 / 参考图（抓一张 / 上传）/ 试问 VLM / 试听 TTS |
| **任务规划** | 任务 CRUD；**起始点**（指定导航航点，或「自动」= 按机器人当前位置取最近）；有序航点列表；执行选项（速度/步态/避障/稳定/各类超时/重试/丢定位策略/返回起点）；定时计划 | 从任务航点表或地图加入、就地新建任务航点；路线预览（各段路径、总长）；「执行」；「⏰ 定时」（每天固定时刻 / 每 N 分钟，立即触发） |
| **执行监控** | 当前/历史执行；段进度时间线（含中途「已过 k/n 点」）；小地图轨迹；每个检查的全景（带范围标注）/裁切/答案/TTS；本次执行的云端 + 系统事件 | 暂停 / 跳过 / 中止；回放 TTS；人工改判；失败或中止后「从某航点重跑剩余航点」；导出本次事件 CSV |
| **任务事件** | 云端 + 系统事件统一时间线，可按来源/类型/级别/执行/关键字过滤，实时追加 | 翻页加载更早；导出 CSV |
| **设置** | 连接（host/别名/密钥掩码）、VLM（provider=mock/qwen/openai_compat/anthropic、base_url/model/key/额外请求字段）、TTS（引擎/声音/汇出）、抓图源、执行默认值、通知 webhook、定时器间隔、机头校准（FORWARD_DEG）、系统自检、mock 演示场景 | 保存即生效；改 CX_* 自动重建连接；试听 TTS |

设计语言：浅色专业控制台风格，系统字体 + Noto Sans CJK；状态色 绿/黄/红 只用于状态；所有让机器人动的操作在 REAL 模式二次确认；深色模式跟随系统。

---

## 7. 对外接口（本系统 `/api`，运行时 `/api/docs` 有自动文档）

```
GET  /api/health
GET  /api/robot/status                      聚合快照（在线/租约/急停/ROS/定位年龄/位姿/任务语义字段/事件监听状态/进行中执行）
POST /api/robot/status/refresh              立即刷新一次
GET  /api/robot/preflight                   执行前置检查（可达/在线/急停/ROS/定位/云端空闲/控制权）
POST /api/robot/init/device-start|device-stop {wait}   透传 ②/⑧；GET /api/robot/init/device-status?task_id&starting 轮询
POST /api/robot/init/localize {map_name,node_id[,pose]}   透传 ④（C8）
POST /api/robot/estop {active}              (C11)
DELETE /api/robot/task                      停止云端任务（不停设备，C14）
POST /api/robot/snapshot                    立即抓一张全景 → /media/…
GET  /api/robot/video                       {hls, rtsp, rtsp_path}
GET  /api/robot/status-codes                云端权威状态码表（缓存 10 分钟，C7）
GET  /api/maps                              云端地图列表 + 本地同步状态
POST /api/maps/{name}/sync                  拉取航点进 nav_waypoints
GET  /api/maps/{name}/waypoints             缓存（含 yaw、边、包围盒、连通块数）
GET  /api/maps/{name}/route?from_node=&to_node=   最短路预览
POST /api/maps/{name}/pointcloud            上传 pcd/ply；GET …?voxel=&max_points= 体素下采样
GET/POST /api/task-waypoints[?map_name=] ; GET/PUT/DELETE /api/task-waypoints/{id}
POST /api/task-waypoints/{id}/reference-image          现抓一张作参考图；…/reference-image/upload 上传
POST /api/task-waypoints/{id}/test-vlm {use,prompt,angle_from,angle_to}   试问 VLM，返回裁切图与将播报的句子
POST /api/tts/test {text}
GET/POST /api/tasks ; GET/PUT/DELETE /api/tasks/{id} ; GET /api/tasks/{id}/plan[?from_node=]
POST /api/tasks/{id}/run[?from_seq=N]       执行（from_seq：从第 N 个航点重跑剩余）
GET  /api/runs ; GET /api/runs/active ; GET /api/runs/{id}（含 legs/inspections/events）
POST /api/runs/{id}/pause|resume|skip|abort
GET  /api/runs/{id}/inspections ; GET /api/inspections[?limit=] ; GET /api/inspections/{id}
PUT  /api/inspections/{id}/verdict {passed|null, note}   人工改判（统计按改判后算，VLM 原结论保留）
GET  /api/events?before_id=&after_id=&source=&type=&run_id=&level=&q=&limit= ; GET /api/events/export.csv[?run_id=]
GET  /api/stream                            SSE → 浏览器（hello/robot_status/event/run/leg/inspection/tts）
GET/PUT /api/settings                       密钥掩码；改 CX_* 自动重建连接
GET  /api/stats?days=30                     按任务航点的检查次数/通过率/平均耗时、执行状态统计、最近失败
GET/POST /api/schedules ; PUT/DELETE /api/schedules/{id} ; POST /api/schedules/{id}/fire   定时计划
GET  /api/health                            系统自检（版本/实例/DB 版本/线程/事件监听/媒体占用）
GET  /api/export[?map_name=] ; POST /api/import {data, map_name?, overwrite}   任务航点/任务/定时计划 JSON 备份与恢复
POST /api/task-waypoints/retarget {from_map, to_map, max_distance}   重建图后按 x,y 重定向到新地图最近导航航点
POST /api/demo/scene {door_open}            mock 演示：合成全景里的柜门开/关
```

---

## 8. 开发计划与进度（2026-09-03 22:00 → 09-04 08:00 CST）

| 阶段 | 时间 | 内容 | 结果 |
|------|------|------|------|
| P0 环境与文档 | 22:00–22:30 | 读宪法、摸环境、venv、本文档 v1、仓库骨架 | ✅ |
| P1 mock 网关 | 22:30–23:00 | 契约仿真 + 运动学 + 事件流 + 故障注入；curl 逐项验证 | ✅ |
| P2 后端核心 | 22:40–23:00 | DB、SDK 包装、事件监听、状态轮询、规划、执行器、检查流水线、VLM/TTS 适配器、REST/SSE | ✅ |
| P3 前端 | 22:45–23:10 | 七个视图、全景角度编辑器、地图画布、SSE 实时 | ✅ 截图验收 |
| P4 测试 | 23:00–01:15 | 单测 + mock 契约 + API + 端到端；查出并修复幂等键撞键、测试互相污染 | ✅ 48 → 89 用例全绿 |
| P5 加固与扩展 | 01:15–03:00 | 掉线/停滞/控制权/暂停跳过/丢定位场景；从失败段重跑；事件归属；外部任务识别；机头校准；系统自检；定时计划；人工改判；通知；导出导入/重定向；评测与备份脚本；schema v2→v5 增量迁移 | ✅ |
| P6 真机准备与收尾 | 01:15–08:00 | 只读冒烟脚本、上线手册、截图集、lint/lock、systemd、文档与规划 | ✅ |
| P7 通宵稳定性观察（mock） | 09-04 02:03–07:26 | 每 3 分钟自动执行一趟演示任务，后台监控内存/线程/失败/日志 | ✅ 104 趟：103 完成、1 中止（edge-tts 卡死 → T21 已修）；内存 95–101 MB 稳定；修复后零告警 |
| P8 真机联调（Gazebo 经真实云端） | 09-05 21:00–23:40 | 同步真实地图、首次真机执行、四点巡检、故障演练（中止/跳过/外部停止/外部任务/急停）、探针 | ✅ 全流程跑通；暴露并修复 T22（机器人间歇性不理会停止，12 次中 4 次）、T23（真机事件撞号丢失）、T24（中途替换任务云端不发 task_started）；急停滑行 5–7 s |
| P9 真机通宵计划 | 09-05 22:00–09-06 07:25 | 每 10 分钟一趟四点巡检（163 m，4 段 / 4 次检查），后台监控 | ✅ 见 §8 末尾「第二夜结果」 |

**交付物**：`/home/leo/agent/scheduler/`（git，65+ 次提交，tag `v0.1.0` … `v0.4.0`），约 4.4k 行后端 + 1.0k 行 mock + 1.2k 行前端 + 0.25k 行播报服务 + 1.9k 行测试（104 个用例，约 3.5 分钟跑完）+ 0.7k 行脚本；文档：本文、`docs/DEVLOG.md`（逐小时开发日志）、`docs/OPERATIONS.md`（真机上线手册）、`docs/TODO.md`、`docs/ROADMAP.md`、`docs/SCREENSHOTS.md`、`CHANGELOG.md`。

**关键时间线**（细节见 `scheduler/docs/DEVLOG.md`）：
- 22:05 宪法读完，确认无真机密钥；`GET /v1`、`/v1/status-codes` 免鉴权抓取存为 mock 夹具。
- 23:00 mock 网关契约验证通过；后端/前端/测试首版提交。
- 23:10 演示任务（消防栓 → 通道方格 → 安全出口 → 返回起点）在 mock 上 30 s 跑完 4 段 / 3 次检查 / TTS。
- 01:00 查出「测试互相污染」：残留执行线程对共享 mock 机器人下发/停止任务 → `RunManager.shutdown()`（也是真机上进程退出的正确行为）。
- 01:15 **mock 抓到真 bug**：幂等键 `ps-r{run}-l{leg}-a{attempt}` 换库后重复，云端 10 分钟内只回放不执行 → 加实例段。
- 01:20 全绿，`v0.1.0`。
- 01:26 开发实例 v1 → v2 迁移；避障故障 → 自动重试成功；从第 2 个航点重跑。
- 01:30 执行详情缺云端事件 → 云端事件挂到进行中的执行/段；段失败原因附云端错误名。
- 01:33 ruff 接入、`requirements.lock`、截图集。
- 01:43 外部任务识别、幂等诚实失败、控制权重试可配、时间戳带时区、systemd 单元。
- 02:05 丢定位「暂停 → 重定位 → 继续」流程、HLS 抓帧备选、按航点统计。
- 02:30 定时计划（schema v3）；02:55 人工改判（schema v4）、失败通知 webhook、CSV 导出；03:10 执行器复审；02:03 起通宵定时执行观察。
- 02:20 VLM 评测脚本（人工复核当标注）、导出/导入、重建图重定向。
- 04:47 通宵观察抓到 edge-tts 卡死执行的 bug（T21）→ 合成超时 + 启动对账残留执行；04:53 重启后 50 趟连续完成，零告警。
- 09-07 16:30 接入通义千问：`qwen` 提供方（DashScope 兼容模式）、`VLM_EXTRA_BODY` 透传私有参数、`make vlm` 连通性探针；等密钥实测。
- 09-07 16:00 拉取上游 `6167083`（SDK 加固 + 文档双轨）：同步 vendored SDK、去掉包装层重复的 429 重试；据此更新 C3/C9/C11；发现并修掉 T26（陈旧定位误判）。现场那台机器人仍是 **legacy 版**（Location 恒 1）。
- 07:26 观察收束：104 趟 / 103 完成 / 1 中止；`v0.2.2`。
- 09-05 21:15 第一次真机（Gazebo）执行成功；21:17 四点巡检 4 分钟完成；21:26 演练暴露 T22（机器人不理会停止）与 T23（真机事件撞号丢失）；21:50 演练 D 暴露 T24 并修复；22:00 起通宵计划；23:25–23:38 停止/急停探针。

**第二夜结果（09-05 22:00 → 09-06 07:25，真实云端 + Gazebo 机器人）**：每 10 分钟一趟四点巡检，56 趟全部完成、零失败，每趟 ≈243 s（4 段导航 51/36/22/108 s + 4 次检查各 3.1 s）；真机检查累计 233 次无 VLM/TTS 异常；云端事件 8804 条全部落库（`dropped=0`）、SSE 未断；进程 90 → 104 MB（约 +1 MB/h，未平台化前继续观察 → T25）；除演练/探针自己制造的中止外，零告警。
- 13:56 拿到真机密钥：只读冒烟 8/13 通过（机器人本地服务 8761 未起 → 机器人端接口 502）；RTSP 抓帧实测 3.0 s、HLS 0.8 s；全景当前为 Gazebo 仿真画面（样张 `docs/screenshots/real-pano-sample.jpg`）；系统已切到 REAL 模式运行，演示定时计划已全部停用。演示实例继续运行（每 3 分钟的计划已停用，保留每天 07:30/19:30 的演示计划）。

---

## 9. 问题清单（最终版；同 `scheduler/docs/TODO.md`）

### 9.1 必须在真机上才能关闭的
| # | 问题 | 影响 | 处置 / 状态 |
|---|------|------|-----------|
| T1 | 开发期间没有真机 API 密钥，全部验证在 mock 上完成 | mock 与真实网关的差异只能靠真机暴露 | **13:56 已拿到密钥并跑只读冒烟**：鉴权/别名/在线/事件流 SSE/HLS 都通；但 `/telemetry` `/position` `/maps` `/task` 与透传全部 **502「机器人端服务不可用：无法连接 127.0.0.1:8761」**——机器人端 agent 在线、但机器人本地 API 服务没起来（当前画面是 Gazebo 仿真场景）。需要机器人侧把本地服务（8761）拉起来后再继续 ②③④ 与单点任务 |
| T3 | 全景图中「机头正前方」对应的列：全景是 360° 等距投影，横轴就是方位角，但**哪一列朝向机器人前进方向**取决于相机安装朝向与拼接零点。09-05 真机全景里前进方向上的物体都落在图像中心 → **Gazebo 相机零点 = 机头（180°）成立**；真狗的 Insta360 仍需校准 | 任务航点的角度范围若以「相对机头」理解，零点错了整个范围就偏了 | 已做「设置 → 机头校准」工具（抓一张、把蓝线拖到机头方向、保存 `FORWARD_DEG`）。13:56 抓到的真实帧是 Gazebo 场景、看不到机身，校准要等机器人在真实环境里：让机器人朝一个已知地标停下（或读 `/position` 的 yaw），在全景里找到该地标所在列即机头列 |
| T6 | RTSP 抓一帧的耗时与成功率 | 检查时长、settle 时间 | **13:56 实测**：RTSP（TCP）4/4 成功，每帧 2.9–3.1 s（1280×640 H.264 15 fps，主要是等首个关键帧）；HLS 0.8 s（但画面落后 2–3 s）。默认仍用 RTSP；若要更快可 `SNAPSHOT_SOURCE=hls`。注意当前流内容是静止的仿真场景（4 帧 md5 完全相同） |
| T7 | `waypoint_reached` 产生时机身可能仍在减速/转向 | 抓图模糊、角度偏 | `SETTLE_SECONDS`（默认 2 s）按实测调；必要时到点后再读一次 `/position` 的 yaw 修正角度零点 |
| T8 | 分段下发间隙：每段结束 → 检查（3–15 s）→ 下一段起步，真机 `nav_preprocess` 耗时未知 | `not_started_timeout`（25 s）是否够 | 实测后调；执行记录里每段有下发/到达时刻可回看 |
| T25 | 通宵进程内存缓慢增长（09-05 夜 90 → 104 MB，≈+1 MB/h，第一夜同规模下平台化在 95–101 MB） | 长期运行若不平台化需重启 | 观察项；下一步用 `tracemalloc` 快照对比或改用 `systemd` 每周重启 |
| T9 | 巡检途中丢定位 | 需人工重新定位 | 已做：默认策略「停下并暂停」，提示用当前最近航点重新定位后点继续，恢复后从当前位置重规划该段；真机验证提示时机与恢复流程 |

| T22 | **机器人端间歇性不理会 `DELETE /task`**（09-05 Gazebo 实测 9 次停止 4 次被忽略：云端回 200「任务已停止」但机器人走到终点才停；其余 1 s 内 `task_stopped`） | 「中止 / 跳过 / 停任务」并不能让机器人停下；只能靠急停或等它走完 | 已做：停任务后核实 `/task` 真的 terminal（`stop_wait_seconds`），核实不了记 error 事件 + 通知，可选自动软件急停（`estop_if_stop_unconfirmed`）；mock 加 `ignore_stop` 复现。实测软件急停 5–7 s 后才停住（Gazebo），且不取消云端任务、取消急停后继续走。**真狗上必须重测**停止指令的忽略率与急停滑行距离 |

| T26 | **`/position` 200 不代表定位新鲜**：机器人端停发 `/global_localization` 后云端回放缓存值（2026-09-07 现场：位姿冻结 20 小时、仍 200），我们原先 `position_ready OR fresh` 会误判就绪 | 会在机器人「其实不知道自己在哪」时放行执行 | **已修**：改为「有遥测则要求新鲜且 /position 非 503」，前置检查明确写出「定位数据已陈旧 N 小时」；mock 加 `freeze_telemetry` 注入 + 用例 |

| T27 | `settings` 表里的 `CX_*` 覆盖 `config/.env`，`start.sh --mock` 导出的 mock 环境变量因此失效（库里现存 `CX_ROBOT=fulin-factory-car`） | `--mock` 会连 mock 网关却去找这台机器人，事件流连不上 | 变通：`PS_DB_PATH=/tmp/mock.db ./start.sh --mock`（另起一个库，也顺带不污染真机统计）。彻底做法：给 mock 模式独立数据目录，或加「环境变量优先」开关 |

### 9.2 功能缺口（不阻塞 mock 演示）
| # | 问题 | 处置 / 状态 |
|---|------|-----------|
| T2 | 云端 API 没有机器狗扬声器端点 | **已解决（14:20）**：本项目自带 `audio_server/`（纯标准库 HTTP 服务，部署到机器狗/现场 PC，只需 python3 + ffplay），调度系统合成好 mp3 直接推过去；不再依赖 `tts_cmq_dev`（zmq 汇出已移除）。剩余：真机上装一次、听一次 |
| T4 | 云端不暴露地图点云下载 | 手工上传 `.pcd/.ply` + 体素下采样接口已通；`PointCloudProvider.fetch_from_robot` 留桩 |
| T10 | 真 VLM 效果未验证（mock 只交替回答） | 通路已就绪：`qwen` 提供方（DashScope 兼容模式，自动带 `enable_thinking:false`）+ `make vlm` 探针 + 编辑器「试问 VLM」+ `make eval` 评测；**待用户提供 DashScope 密钥后实测** `qwen3.5-flash` 能否读图、判读准确率 |
| T11 | 单机器人 | `robots` 表 + 每机器人一组线程（roadmap） |
| T12 | 无登录鉴权；`TTS_COMMAND` 可在设置页改成任意命令 | 默认只绑 127.0.0.1；局域网暴露需反向代理 + 鉴权，并把危险设置移出网页 |
| T14 | 媒体与事件无限增长 | `scripts/cleanup_media.py` 已有，需 cron 化 |
| T15 | 前端只有无头截图 + DOM 抽查，无自动化交互测试 | 引入 Playwright（需 pip + 浏览器驱动） |
| T18 | mock 局限：无 `nav_preprocess`/充电桩状态、无真实速度曲线、丢事件补发只部分复刻 | 真机差异回填 |

### 9.3 已解决（留档）
T29 前置检查失败时（我们一次都没下发过）执行器仍发 `DELETE /task`，会停掉**不是我们下发的**任务（现场有人的巡检、或机器人重定位留下的空任务）。改为只停自己下发过的；前置提示改为写清云端任务的地图/航点数/目标，并认出「路径为空 = 重定位残留」。附带：真机实测**机器人端重定位期间任务状态就是 NAVIGATING（地图与路径都为空）**——刚点过「定位」就执行必然被前置检查拦，这是 09-07 现场 7 次被拦的全部原因；mock 加 `/mock/relocalizing` 复现。
T28 TTS 合成跑在 `ThreadPoolExecutor` 的非守护线程里，合成一卡进程就退不掉（30 s+），`start.sh --stop` 落到 SIGKILL → 应用来不及中止执行与 `DELETE /task`，机器人会继续走。改成每次合成起守护线程 + join 超时（实测卡死后仍 0.26 s 干净退出）。
T24 真实云端在任务被中途替换时不发新的 `task_started`（状态无跃迁）→ 外部任务识别改为按事件 `total`/`visited` 与对账 `path` 比对（09-05 演练 D 抓到，run #119 曾误标完成）。
T23 mock → 真机切换后真机云端事件静默丢失（唯一索引全局按 `cloud_seq`，与 mock 时期的行撞号）→ schema v6 按 (gateway, cloud_seq) 唯一，监听器暴露 `dropped` 计数。
T21 edge-tts 合成无超时，通宵观察中真的卡死了一条执行（04:43 起 `inspecting` 不动，定时计划被跳过）→ 合成放到工作线程并带 `TTS_TIMEOUT`（默认 20 s），超时记错误、执行继续；启动时把上次进程残留的「进行中」执行标记为中止（`runs_reconciled`）。
T13 外部任务识别（`task_started.path` 与本段不符 → 中止且不停对方任务）；T16 systemd 单元（`deploy/`）；T17 时间戳带时区偏移；T19 幂等诚实失败（409 后查 `GET /task`，路径一致即按已下发）；T20 控制权 409 等待/重试次数可配（任务选项）；幂等键跨库撞键（加实例段）；测试残留执行线程污染共享 mock（`RunManager.shutdown`）；`pkill -f` 误杀自身 shell（pid 文件）；无头 Chrome 遇 SSE 不结束（`?nosse=1`）；`item_seq` 被对账写入覆盖（独立列 + schema v2）；mock `preempt seconds=0` 被当缺省；控制权测试在前置检查被拦（改为途中抢占）。

---

## 10. 未来规划（最终版；同 `scheduler/docs/ROADMAP.md`）

**R1 真机联调**（Gazebo 部分已于 09-05 夜完成；真狗部分待现场）
1. ✅ 只读核对 → 单航点 → 四点全流程 → 通宵 10 分钟一趟（Gazebo）。
2. ✅ 实测：抓帧 3.0 s、到点检查 3.1 s、起步 0.25 s、速度 0.8 m/s、Gazebo 机头零点=180°；⬜ 真狗：重测速度/滑行/停止忽略率/机头零点。
3. ✅ 已回填 mock：idle 无 progress、nav_preprocess、完成后回 idle、中途替换不发 task_started、HTML 502、ignore_stop；⬜ 真狗差异待回填。

**R2 可靠性（2–3 周）**
4. 丢定位恢复流程已有（暂停 → 重定位 → 继续），真机上验证提示与恢复时机（T9）。
5. 外部任务识别（T13）、控制权退避策略（T20）、幂等诚实失败处理（T19）。
6. 守护与运维：systemd 单元、自动清理 cron、日志轮转已就位；健康检查接入监控。

**R3 判读质量（并行）**
7. 真 VLM 评测：`scripts/eval_vlm.py` 已能用人工复核过的检查当标注重跑当前 VLM 算准确率/误判清单；后续：积累样本、对比 provider/prompt/是否附整图。
8. 等距投影 → 透视重投影（py360convert）再给模型；对比评测。
9. 结果统计与人工改判已有第一版（按航点通过率、改判入库）；后续：改判样本导出为评测集、误报回看视图。

**R4 平台化（1–2 月）**
10. 多机器人（robots 表、每机器人一组监听/轮询/执行线程、页面切换）。
11. 定时任务已做简版（每天固定时刻 / 每 N 分钟，冲突时跳过）；后续：任务队列、cron 表达式、失败自动重试策略（避障失败换路线）。
12. 权限与审计（登录、角色、操作日志）；局域网部署（反向代理 + TLS）。
13. 通知：Webhook 已有（检查不通过 / 执行失败中止）；后续：IM 适配（企微/飞书/Slack 模板）、日报。

**R5 与现场系统打通**
14. 机器狗端播报：已改为本项目自带 `audio_server`（T2）；后续：播报音量/打断策略、多语言声音。
15. 点云自动获取（云端接口出来后接 `fetch_from_robot`，T4）；重建图后的任务航点迁移工具已有（导出/导入 + 按 x,y 重定向到新图最近航点），后续做 UI 引导与差异预览。
16. 巡检模板库：常见点位（消防栓、通道、配电箱、指示牌）的 prompt/答案模版可复用。

---

## 11. 验收指引（明早照着做）

```bash
cd /home/leo/agent/scheduler
make lint && make test                     # ruff 干净、80+ 用例全绿（约 2 分钟）
./run.sh --mock                            # 起 mock 网关 + 调度系统，浏览器打开 http://127.0.0.1:8088
make seed                                  # 另开终端：灌演示数据、初始化、跑一趟（约 30 s）
```
页面里看：总览（机器人/云端任务/初始化/最近检查/统计）→ 地图（航点 + 点云）→ 任务航点（编辑器拖角度线、试问 VLM、试听 TTS）→ 任务规划（执行 / 路线 / 定时）→ 执行监控（分段、检查、改判、重跑）→ 任务事件（筛选、导出）→ 设置（机头校准、系统自检、备份迁移）。
故障演示：`curl -X POST 127.0.0.1:18443/mock/fault -H 'Content-Type: application/json' -d '{"kind":"obstacle"}'` 后再执行一次，看失败、重试与「从某航点重跑」。

拿到真机密钥后：填 `config/.env` → `make smoke`（只读）→ 上游 `04_verify_flow.py` → 按 `docs/OPERATIONS.md` 初始化并跑单航点任务。

当前开发实例（本机已在跑）：调度系统 http://127.0.0.1:8088（pid 见 `data/app.pid`），mock 网关 http://127.0.0.1:18443（`scripts/dev_restart.sh` 可重启两者）。通宵每 3 分钟一趟的计划已停用，保留每天 07:30 / 19:30 的演示计划；执行记录、检查、事件都在「执行监控」「任务事件」里，日志在 `data/logs/app.log`。
