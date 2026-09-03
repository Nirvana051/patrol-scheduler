# 问题与 TODO（与 ../../Sample_web_api/docs/task.md §9 同步）


## 1 必须在真机上才能关闭的
| # | 问题 | 影响 | 处置 / 状态 |
|---|------|------|-----------|
| T1 | **本机没有真机 API 密钥**，全部验证在 mock 上完成 | mock 与真实网关的差异（响应字段细节、时序、任务下发后的状态推进、HLS/RTSP 可用性）只能靠真机暴露 | 拿到 `CX_KEY` 后按 `docs/OPERATIONS.md`：`scripts/real_smoke.py`（只读）→ 上游 `04_verify_flow.py` → 单点任务 → 全流程；差异回填到 mock |
| T3 | 全景图中「机头正前方」对应的列未知 | 角度范围 ↔ 实际方位 | 已做「设置 → 机头校准」工具；真机首帧校准一次 |
| T6 | RTSP 抓一帧的耗时与成功率未知（ffmpeg TCP 连接可能 2–10 s） | 检查时长、settle 时间 | 记录 `snapshot` 事件耗时；已加 `SNAPSHOT_SOURCE=hls` 备选（8554 被挡时用） |
| T7 | `waypoint_reached` 产生时机身可能仍在减速/转向 | 抓图模糊、角度偏 | `SETTLE_SECONDS`（默认 2 s）按实测调；必要时到点后再读一次 `/position` 的 yaw 修正角度零点 |
| T8 | 分段下发间隙：每段结束 → 检查（3–15 s）→ 下一段起步，真机 `nav_preprocess` 耗时未知 | `not_started_timeout`（25 s）是否够 | 实测后调；执行记录里每段有下发/到达时刻可回看 |
| T9 | 巡检途中丢定位 | 需人工重新定位 | 已做：默认策略「停下并暂停」，提示用当前最近航点重新定位后点继续，恢复后从当前位置重规划该段；真机验证提示时机与恢复流程 |

## 2 功能缺口（不阻塞 mock 演示）
| # | 问题 | 处置 / 状态 |
|---|------|-----------|
| T2 | 云端 API 没有机器狗扬声器端点 | 汇出插件化：browser / local / **zmq（对接用户已有 `tts_cmq_dev/robot-audio`，需机器人端加 `play_tts` 动作或改为传音频文件）** / webhook |
| T4 | 云端不暴露地图点云下载 | 手工上传 `.pcd/.ply` + 体素下采样接口已通；`PointCloudProvider.fetch_from_robot` 留桩 |
| T10 | 真 VLM 效果未验证（mock 只交替回答） | prompt 模板、JSON 解析、附带范围标注整图都已具备；先用编辑器「试问 VLM」在参考图上标定，再建评测集（roadmap） |
| T11 | 单机器人 | `robots` 表 + 每机器人一组线程（roadmap） |
| T12 | 无登录鉴权；`TTS_COMMAND` 可在设置页改成任意命令 | 默认只绑 127.0.0.1；局域网暴露需反向代理 + 鉴权，并把危险设置移出网页 |
| T14 | 媒体与事件无限增长 | `scripts/cleanup_media.py` 已有，需 cron 化 |
| T15 | 前端只有无头截图 + DOM 抽查，无自动化交互测试 | 引入 Playwright（需 pip + 浏览器驱动） |
| T18 | mock 局限：无 `nav_preprocess`/充电桩状态、无真实速度曲线、丢事件补发只部分复刻 | 真机差异回填 |

## 3 已解决（留档）
T21 edge-tts 合成无超时，通宵观察中真的卡死了一条执行（04:43 起 `inspecting` 不动，定时计划被跳过）→ 合成放到工作线程并带 `TTS_TIMEOUT`（默认 20 s），超时记错误、执行继续；启动时把上次进程残留的「进行中」执行标记为中止（`runs_reconciled`）。
T13 外部任务识别（`task_started.path` 与本段不符 → 中止且不停对方任务）；T16 systemd 单元（`deploy/`）；T17 时间戳带时区偏移；T19 幂等诚实失败（409 后查 `GET /task`，路径一致即按已下发）；T20 控制权 409 等待/重试次数可配（任务选项）；幂等键跨库撞键（加实例段）；测试残留执行线程污染共享 mock（`RunManager.shutdown`）；`pkill -f` 误杀自身 shell（pid 文件）；无头 Chrome 遇 SSE 不结束（`?nosse=1`）；`item_seq` 被对账写入覆盖（独立列 + schema v2）；mock `preempt seconds=0` 被当缺省；控制权测试在前置检查被拦（改为途中抢占）。

---
