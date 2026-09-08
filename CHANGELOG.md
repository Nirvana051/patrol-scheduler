# 变更记录

## [0.4.8] - 2026-09-08
- **修**：新增 `.gitattributes`（`* text=auto eol=lf`）—— 在 Windows 上 clone（`core.autocrlf=true` 是 Git for Windows 的默认值）会把 `*.sh` 转成 CRLF，`bash` 直接报 `‘bash\r’: No such file or directory`
- **修**：`requirements.txt` 补上 Pillow / requests / numpy —— 之前当成"系统 site-packages 里有"，新机器上装完仍起不来
- `start.sh` / `run.sh` / `Makefile` 认 `PS_PYTHON`：已有 conda/venv 环境可以直接指过来，不必再建 `.venv`
- `docs/USAGE.md`：§1 换成任何机器都能照抄的安装命令，§7 新增「换了一台机器，脚本全报 \r 错」

## [0.4.7] - 2026-09-07
- 新增 `docs/USAGE.md` 使用说明与 `docs/README.md` 文档索引；`docs/HANDOFF.md` 全面刷新
- 截图集更新到当前 UI（含任务编辑器的起始点）；`SCREENSHOTS.md` 重写
- 新增 `scripts/check_docs.py`（`make docs-check`）：文档与代码一致性体检

## [0.4.6] - 2026-09-07
- **修**：前置检查失败时不再发 `DELETE /task` —— 那可能是现场有人下发的巡检、或机器人重定位留下的空任务，不该我们停（只停本次执行自己下发过的）
- 前置检查提示写清云端任务的地图/航点数/目标，并区分「路径为空 = 刚做过定位的残留」与「别人的任务」
- 总览的前置检查失败项加快捷按钮（停任务 / 取消急停），点完自动重查
- 点「执行」前先查前置检查：不通过就摆出原因与「停掉云端任务并重试」，不再白产生一条 aborted 记录
- mock 新增 `/mock/relocalizing`：复刻重定位期间「NAVIGATING 但地图/路径为空」的状态

## [0.4.5] - 2026-09-07
- 前端静态资源改为 `Cache-Control: no-cache`：浏览器不再缓存旧的 ES 模块（改了 UI 却看不到变化的根因）
- `#/tasks/<id>` 可直接打开该任务的编辑器（与任务航点页一致）

## [0.4.4] - 2026-09-07
- 任务规划新增**起始点**：在任务里指定起始导航航点（`options.start_node`），执行与路线预览都从它出发；留「自动」则保持原行为（按机器人当前位置取最近航点）
- 指定起点时核对机器人与它的实际距离，超 `start_node_max_distance`（默认 3 m）记警告事件；起点不在当前地图里则前置阶段直接拒绝执行
- 任务列表新增「起始点」列；编辑器的起点下拉跟随所选地图，换图后失效的起点会标出来

## [0.4.3] - 2026-09-07
- 修：TTS 合成改用守护线程（原先 `ThreadPoolExecutor` 的非守护线程被卡住时，进程 30 s+ 退不掉，`start.sh --stop` 只能 SIGKILL，应用来不及中止执行并 `DELETE /task` 停机器人）

## [0.4.2] - 2026-09-07
- 新增 `qwen` VLM 提供方（阿里 DashScope 兼容模式）：默认地址、模型 `qwen3.5-flash`，**自动带 `enable_thinking:false`**（非流式调用缺这个参数会 400）
- 新增 `VLM_EXTRA_BODY`：JSON 对象合并进请求体，用于服务商私有参数（如 `vl_high_resolution_images`）
- VLM 服务端错误改为带出 `error.message` 与 code，排查密钥/模型名不再靠猜
- 新增 `scripts/vlm_probe.py`（`make vlm`）：一张图 + 一句问题的连通性探针，可临时覆盖 provider/model/key
- 手册加「接 VLM（以通义千问为例）」一节；110 用例

## [0.4.1] - 2026-09-07
- 同步上游 `Sample_web_api@6167083` 的 SDK（透传通道 429/网络重试、`device_start|stop`/`localize` 自动幂等键、`events(limit=)`、`watch_task` idle 宽限）；包装层去掉重复的 429 重试
- **修 T26**：`/position` 200 不代表定位新鲜（机器人端停发话题后云端回放缓存值）——定位就绪改为要求 `received_at` 新鲜，前置检查明确报「定位数据已陈旧 N 小时」
- mock 新增 `freeze_telemetry`（陈旧遥测）与 `robot_version`（legacy/new：Location 0/1、estop 缺 active 400）注入
- 宪法 C3/C9/C11 按上游「新旧机器人端双轨」更新；107 用例

## [0.4.0] - 2026-09-06 07:30（真机联调版）
- 真机（Gazebo 经真实云端）跑通全流程；通宵每 10 分钟一趟四点巡检零失败
- schema v6：云端事件唯一性按 (gateway, cloud_seq)——修复 mock→真机切换后真机事件静默丢失（T23）
- 停任务后核实机器人真的停了（`stop_wait_seconds`），核实不了报 error + 通知，可选自动软件急停（`estop_if_stop_unconfirmed`）（T22）
- 外部任务识别改为按事件 `total`/`visited` 与对账路径比对（真实云端中途替换任务不发 `task_started`，T24）
- 透传通道 429 退避；HTML 版 502 错误文本清洗
- mock 对齐真机：idle 无 progress、`nav_preprocess` 阶段、完成后回 idle、替换任务不发 task_started、nginx HTML 502 注入、`ignore_stop` 注入
- `start.sh` 一键启停；`docs/TEST_PLAN.md`、`docs/HANDOFF.md`；真机全景样张；104 用例

## [0.3.0] - 2026-09-04 14:20
- 新增 `audio_server/`：纯标准库的播报服务（扬声器端），部署到机器狗或现场 PC；顺序队列、可选口令、dry 模式
- TTS 汇出 `zmq`（依赖 pyzmq 与外部 robot-audio 服务）移除，改为 `http` 推送到 audio_server；新增 `TTS_AUDIO_SERVER_URL/TOKEN`
- 真机只读冒烟与 RTSP/HLS 抓帧实测记录；`docs/TEST_PLAN.md` 全流程测试方案；96 用例

## [0.2.2] - 2026-09-04 04:53
- TTS 合成加超时（`TTS_TIMEOUT`，默认 20 s）：通宵观察中 edge-tts 卡死过一条执行，现在超时记错误、文本仍推给浏览器、执行继续
- 启动时把上次进程残留的「进行中」执行标记为中止（`runs_reconciled`）
- 测试夹具支持启动前预置数据；92 用例

## [0.2.1] - 2026-09-04 02:40
- 事件监听：网关重启后 seq 回退时重置游标并记 `event_cursor_reset`（否则静默漏事件）
- 检查记录保存抓图时位姿（schema v5），执行监控显示抓图时机头 yaw
- 执行器：新尝试清错误文字；检查流水线异常不再触发急停
- mock：导航预处理阶段（status 2 → 3）；复位清理避障/丢定位标志
- 开发实例重启脚本 `scripts/dev_restart.sh`；SSE 与机器人操作接口用例；90 用例

## [0.2.0] - 2026-09-04 02:35
- 幂等键加入按 DB 生成的实例段 `ps-{instance}-r{run}-l{leg}-a{attempt}`（mock 测试抓到的跨库撞键 bug）
- 执行器：先订阅再取游标再下发；掉线超时判段失败；中途到达写入段进度；进程停止时中止执行并停机器人任务
- 失败/中止后可从剩余航点重跑（`from_seq`）；`run_legs.item_seq` 列，schema v2 增量迁移
- 设置页「机头校准」；任务编辑器内直接新建任务航点；地图页自动加载点云背景
- 真机准备：`scripts/real_smoke.py` 只读冒烟、`docs/OPERATIONS.md` 上线手册
- 文件日志 `data/logs/app.log`
- 停滞判定（`stall_timeout`）、外部任务识别、幂等诚实失败处理、控制权重试可配、时间戳带时区
- 丢定位策略 `lost_localization_action`（默认：停下并暂停 → 人工重定位 → 继续）
- 定时计划（每天固定时刻 / 每 N 分钟；schema v3）
- 检查人工改判（schema v4）、按航点统计 `/api/stats`、失败通知 webhook、事件 CSV 导出
- HLS 抓帧备选源、`/api/health` 系统自检、清理脚本、systemd 单元、Makefile
- 导出/导入（任务航点、任务、定时）、重建图后重定向到新地图最近导航航点、VLM 评测脚本（人工复核当标注）
- 测试：故障场景（掉线/控制权/暂停继续跳过/丢定位/停滞/外部任务）、VLM 适配器（假客户端/假服务）、抓帧管道、监听回退、迁移、定时、通知；共 74 用例

## [0.1.0] - 2026-09-04
首个可跑通的版本（全部对着 mock 网关验证）。
- mock 云端网关：复刻文档契约与已知坑（状态词不对称、Location 恒 1、透传只认 ID、幂等重放、限流、急停空体、事件流）
- 后端：SQLite 数据模型；SDK 限速包装；SSE 事件监听落库；状态轮询；导航图最短路；分段执行器；抓图→裁切→VLM→TTS 流水线；VLM（mock/OpenAI 兼容/Anthropic）与 TTS（edge/command/none；browser/local/zmq/webhook）适配器；REST + SSE
- 前端：总览、地图与导航航点、任务航点（全景角度编辑器）、任务规划、执行监控、任务事件、设置
- 测试：单测 + mock 契约 + API + 端到端执行
