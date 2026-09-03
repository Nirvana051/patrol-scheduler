# 问题与 TODO（与 ../../Sample_web_api/docs/task.md §9 同步）


## 1 必须在真机上才能关闭的
| # | 问题 | 影响 | 处置 / 状态 |
|---|------|------|-----------|
| T1 | **本机没有真机 API 密钥**，全部验证在 mock 上完成 | mock 与真实网关的差异（响应字段细节、时序、任务下发后的状态推进、HLS/RTSP 可用性）只能靠真机暴露 | 拿到 `CX_KEY` 后按 `docs/OPERATIONS.md`：`scripts/real_smoke.py`（只读）→ 上游 `04_verify_flow.py` → 单点任务 → 全流程；差异回填到 mock |
| T3 | 全景图中「机头正前方」对应的列未知 | 角度范围 ↔ 实际方位 | 已做「设置 → 机头校准」工具；真机首帧校准一次 |
| T6 | RTSP 抓一帧的耗时与成功率未知（ffmpeg TCP 连接可能 2–10 s） | 检查时长、settle 时间 | 记录 `snapshot` 事件耗时；若不稳定，`SNAPSHOT_SOURCE` 增加 `hls`（取最新分片解一帧）备选 |
| T7 | `waypoint_reached` 产生时机身可能仍在减速/转向 | 抓图模糊、角度偏 | `SETTLE_SECONDS`（默认 2 s）按实测调；必要时到点后再读一次 `/position` 的 yaw 修正角度零点 |
| T8 | 分段下发间隙：每段结束 → 检查（3–15 s）→ 下一段起步，真机 `nav_preprocess` 耗时未知 | `not_started_timeout`（25 s）是否够 | 实测后调；执行记录里每段有下发/到达时刻可回看 |
| T9 | 巡检途中丢定位目前只记录（可配置为段失败） | 需人工介入 | 做「在最近航点重新定位后继续」的引导流程（roadmap R2） |

## 2 功能缺口（不阻塞 mock 演示）
| # | 问题 | 处置 / 状态 |
|---|------|-----------|
| T2 | 云端 API 没有机器狗扬声器端点 | 汇出插件化：browser / local / **zmq（对接用户已有 `tts_cmq_dev/robot-audio`，需机器人端加 `play_tts` 动作或改为传音频文件）** / webhook |
| T4 | 云端不暴露地图点云下载 | 手工上传 `.pcd/.ply` + 体素下采样接口已通；`PointCloudProvider.fetch_from_robot` 留桩 |
| T10 | 真 VLM 效果未验证（mock 只交替回答） | prompt 模板、JSON 解析、附带范围标注整图都已具备；先用编辑器「试问 VLM」在参考图上标定，再建评测集（roadmap） |
| T11 | 单机器人 | `robots` 表 + 每机器人一组线程（roadmap） |
| T12 | 无登录鉴权；`TTS_COMMAND` 可在设置页改成任意命令 | 默认只绑 127.0.0.1；局域网暴露需反向代理 + 鉴权，并把危险设置移出网页 |
| T13 | 对账只看 terminal/visited，现场若下发了另一个任务会被误认为「我的」 | 比对 `task_started.path` 与本段 path，识别「不是我的任务」→ 段中止 |
| T14 | 媒体与事件无限增长 | `scripts/cleanup_media.py` 已有，需 cron 化 |
| T15 | 前端只有无头截图 + DOM 抽查，无自动化交互测试 | 引入 Playwright（需 pip + 浏览器驱动） |
| T16 | 无守护进程（systemd 单元） | 提供 `deploy/patrol-scheduler.service` |
| T17 | 时间戳为本地 ISO 无时区 | 统一带时区或 UTC |
| T18 | mock 局限：无 `nav_preprocess`/充电桩状态、无真实速度曲线、丢事件补发只部分复刻 | 真机差异回填 |
| T19 | 幂等的两种「诚实失败」（首次请求进行中 409 / 响应过大 409）当前按下发失败处理 | 识别后改为「查 GET /task 再决定」 |
| T20 | 控制权 409 只等 30 s 重试一次 | 可配置退避 + 界面提示等待现场放手 |

## 3 已解决（留档）
幂等键跨库撞键（加实例段）；测试残留执行线程污染共享 mock（`RunManager.shutdown`）；`pkill -f` 误杀自身 shell（pid 文件）；无头 Chrome 遇 SSE 不结束（`?nosse=1`）；`item_seq` 被对账写入覆盖（独立列 + schema v2）；mock `preempt seconds=0` 被当缺省；控制权测试在前置检查被拦（改为途中抢占）。

---
