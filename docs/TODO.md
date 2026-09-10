# 问题与 TODO（与 ../../Sample_web_api/docs/task.md §9 同步）


## 1 必须在真机上才能关闭的
| # | 问题 | 影响 | 处置 / 状态 |
|---|------|------|-----------|
| T1 | 开发期间没有真机 API 密钥，全部验证在 mock 上完成 | mock 与真实网关的差异只能靠真机暴露 | **13:56 已拿到密钥并跑只读冒烟**：鉴权/别名/在线/事件流 SSE/HLS 都通；但 `/telemetry` `/position` `/maps` `/task` 与透传全部 **502「机器人端服务不可用：无法连接 127.0.0.1:8761」**——机器人端 agent 在线、但机器人本地 API 服务没起来（当前画面是 Gazebo 仿真场景）。需要机器人侧把本地服务（8761）拉起来后再继续 ②③④ 与单点任务 |
| T31 | **执行器判「到达」只认云端信号，完全不看机器人自己的位置**：`_waitArrival` 只认 `waypoint_reached`（waypoint==target 且无 nextTarget）、`task_completed`、以及 5 秒对账里的 `status_code==4`。而段失败之后的 `_relocate_current_node` **恰恰是靠位置**判出「已在航点 12（距 0.15 m），无需导航」的 —— 同一个判断，晚了 27–37 秒 | 云端不确认最后一个点时（见 T30），只能干等到它超时 | 可选改法：等待阶段就加一条「位置进入目标航点容差内且速度 ≈ 0 持续 N 秒 → 判到达」，并记一条**独立事件**（不能静默）。<br>风险要写清：这等于我们替云端下「到了」的结论；若机器人是被挡住而停在 0.5 m 外，我们会照样拍照判读，拍到的画面可能不是要看的角度。**所以必须配明确容差 + 醒目事件，且默认关闭**。等产品决定 |
| T3 | 全景图中「机头正前方」对应的列：全景是 360° 等距投影，横轴就是方位角，但**哪一列朝向机器人前进方向**取决于相机安装朝向与拼接零点。09-05 真机全景里前进方向上的物体都落在图像中心 → **Gazebo 相机零点 = 机头（180°）成立**；真狗的 Insta360 仍需校准 | 任务航点的角度范围若以「相对机头」理解，零点错了整个范围就偏了 | 已做「设置 → 机头校准」工具（抓一张、把蓝线拖到机头方向、保存 `FORWARD_DEG`）。13:56 抓到的真实帧是 Gazebo 场景、看不到机身，校准要等机器人在真实环境里：让机器人朝一个已知地标停下（或读 `/position` 的 yaw），在全景里找到该地标所在列即机头列 |
| T6 | RTSP 抓一帧的耗时与成功率 | 检查时长、settle 时间 | **13:56 实测**：RTSP（TCP）4/4 成功，每帧 2.9–3.1 s（1280×640 H.264 15 fps，主要是等首个关键帧）；HLS 0.8 s（但画面落后 2–3 s）。默认仍用 RTSP；若要更快可 `SNAPSHOT_SOURCE=hls`。注意当前流内容是静止的仿真场景（4 帧 md5 完全相同） |
| T7 | `waypoint_reached` 产生时机身可能仍在减速/转向 | 抓图模糊、角度偏 | `SETTLE_SECONDS`（默认 2 s）按实测调；必要时到点后再读一次 `/position` 的 yaw 修正角度零点 |
| T8 | 分段下发间隙：每段结束 → 检查（3–15 s）→ 下一段起步，真机 `nav_preprocess` 耗时未知 | `not_started_timeout`（25 s）是否够 | 实测后调；执行记录里每段有下发/到达时刻可回看 |
| T25 | 通宵进程内存缓慢增长（09-05 夜 90 → 104 MB，≈+1 MB/h，第一夜同规模下平台化在 95–101 MB） | 长期运行若不平台化需重启 | 观察项；下一步用 `tracemalloc` 快照对比或改用 `systemd` 每周重启 |
| T9 | 巡检途中丢定位 | 需人工重新定位 | 已做：默认策略「停下并暂停」，提示用当前最近航点重新定位后点继续，恢复后从当前位置重规划该段；真机验证提示时机与恢复流程 |

| T22 | **机器人端间歇性不理会 `DELETE /task`**（09-05 Gazebo 实测 9 次停止 4 次被忽略：云端回 200「任务已停止」但机器人走到终点才停；其余 1 s 内 `task_stopped`） | 「中止 / 跳过 / 停任务」并不能让机器人停下；只能靠急停或等它走完 | 已做：停任务后核实 `/task` 真的 terminal（`stop_wait_seconds`），核实不了记 error 事件 + 通知，可选自动软件急停（`estop_if_stop_unconfirmed`）；mock 加 `ignore_stop` 复现。实测软件急停 5–7 s 后才停住（Gazebo），且不取消云端任务、取消急停后继续走。**真狗上必须重测**停止指令的忽略率与急停滑行距离 |

| T26 | **`/position` 200 不代表定位新鲜**：机器人端停发 `/global_localization` 后云端回放缓存值（2026-09-07 现场：位姿冻结 20 小时、仍 200），我们原先 `position_ready OR fresh` 会误判就绪 | 会在机器人「其实不知道自己在哪」时放行执行 | **已修**：改为「有遥测则要求新鲜且 /position 非 503」，前置检查明确写出「定位数据已陈旧 N 小时」；mock 加 `freeze_telemetry` 注入 + 用例 |

| T27 | `settings` 表里的 `CX_*` 覆盖 `config/.env`，`start.sh --mock` 导出的 mock 环境变量因此失效（库里现存 `CX_ROBOT=<另一台机器人的别名>`） | `--mock` 会连 mock 网关却去找这台机器人，事件流连不上 | 变通：`PS_DB_PATH=/tmp/mock.db ./start.sh --mock`（另起一个库，也顺带不污染真机统计）。彻底做法：给 mock 模式独立数据目录，或加「环境变量优先」开关 |

## 2 功能缺口（不阻塞 mock 演示）
| # | 问题 | 处置 / 状态 |
|---|------|-----------|
| T32 | **ROS 的 `PYTHONPATH` 会顶掉 venv 里的包**（根因仍在）：`~/.bashrc` 里 `source /opt/ros/humble/setup.bash` 与本机的 ROS 工作区，`PYTHONPATH` 带 11 个目录且排在 venv 的 `site-packages` **前面**。实测老 `.venv` 里能看到 **445 个包**（整个 ROS 2 + torch + CUDA + PyQt5），numpy/pytest/scipy/PyYAML 等各有两个版本并存，谁生效取决于 `sys.path` 顺序 —— `apt upgrade` 动一下 ROS，巡检系统 import 到的东西就悄悄变了 | **已在脚本层解决**：`start.sh` / `run.sh` / `bootstrap.sh` / `Makefile`（11 处）都先 `unset PYTHONPATH PYTHONHOME` 且设 `PYTHONNOUSERSITE=1`；`.venv` 重建为隔离环境（`include-system-site-packages=false`），包数 445 → **55**。systemd 单元加了 `UnsetEnvironment=PYTHONPATH PYTHONHOME` 保险。<br>**没解决的**：**手工直接跑 `.venv/bin/python` 仍会中招**（在交互 shell 里）。彻底做法：在 `.venv/bin/` 放一个净化包装脚本，或写一个 `sitecustomize.py` 在启动时剔除外来路径。文档已在 USAGE §1 写明手工跑要带 `env -u PYTHONPATH` |
| T33 | **Pillow 12 的严格化：其余绘图代码没逐个审计**。已修 `app/media/pano.py` 里合成图的门扇矩形（小图上宽度变负 → `x1 < x0`，Pillow 9 静默吞掉、Pillow 12 直接 `ValueError`，也就是说那个门框在小图上一直没画出来过） | 同类隐患（`x1<x0` / `y1<y0`）可能还在别的 `rectangle` / `ellipse` 调用里，只在特定尺寸下才炸 | 把 pano.py 里所有绘图坐标过一遍，或加一个「坐标必须已归一化」的小工具函数统一收口 |
| T34 | **`requirements.lock` 的升级没有流程**：现在是手工 `pip freeze`。52 个精确版本里，FastAPI 栈钉在生产已验证的版本，`requests` / `Pillow` 是这次换成 venv 内的当前稳定版 | 升级依赖时不知道该跑哪些用例才算安全（这次靠全量 118 用例才发现 Pillow 的严格化） | 定一个清单：改 Pillow 必跑 `tests/test_pano.py`，改 numpy 必跑 `tests/test_pointcloud.py`（点云下采样有逐位比对），改 fastapi/pydantic 必跑 `tests/test_api.py`。写进 USAGE 或 TEST_PLAN |
| T35 | **隔离 venv 更占磁盘**：`.venv` 199 MB（老的继承系统包只有 85 MB）—— 因为不再借用系统的 numpy/Pillow/scipy，自己各存一份 | 磁盘（这台机器 1.7T 空闲，不构成问题；小机器上要留意）| 记录事实，不处置。旧的 `.venv.old` 已删除 |
| T2 | 云端 API 没有机器狗扬声器端点 | **已解决（14:20）**：本项目自带 `audio_server/`（纯标准库 HTTP 服务，部署到机器狗/现场 PC，只需 python3 + ffplay），调度系统合成好 mp3 直接推过去；不再依赖 `tts_cmq_dev`（zmq 汇出已移除）。剩余：真机上装一次、听一次 |
| T4 | 云端不暴露地图点云下载 | 手工上传 `.pcd/.ply` + 体素下采样接口已通；`PointCloudProvider.fetch_from_robot` 留桩 |
| T10 | 真 VLM 的**准确率**未验证 | 通路已实测：`qwen3.5-flash`（DashScope 兼容模式）在真机上判读过 **26 次**（09-07 21:04 → 09-10，用户自己跑的），库里共 591 条检查 / 216 趟执行。**缺的是标注**：人工改判 0 条，所以 `make eval`（拿改判结果当标注算准确率）没有样本可用。下一步：在「执行监控」里对几十条检查做人工复核（判通过/判不通过），再跑 `make eval` 出总体与按航点的准确率。另外**空 prompt 的任务航点会稳定产出「无法判断」**——线上 591 条里的「无法判断」几乎都是这么来的，不是模型判不出来（给那些点填上 prompt，或从任务的检查列表里去掉）|
| T11 | 单机器人 | `robots` 表 + 每机器人一组线程（roadmap） |
| T12 | 无登录鉴权；`TTS_COMMAND` 可在设置页改成任意命令 | 默认只绑 127.0.0.1；局域网暴露需反向代理 + 鉴权，并把危险设置移出网页 |
| T14 | 媒体与事件无限增长 | `scripts/cleanup_media.py` 已有，需 cron 化 |
| T15 | 前端只有无头截图 + DOM 抽查，无自动化交互测试 | 引入 Playwright（需 pip + 浏览器驱动） |
| T18 | mock 局限：无 `nav_preprocess`/充电桩状态、无真实速度曲线、丢事件补发只部分复刻 | 真机差异回填 |

## 3 已解决（留档）
T30 **最后一个航点的 yaw 对齐超时 → 云端 `task_failed` 错误码 `0x0000`**（09-08 19:30 起）。底层加入 yaw 角控制后，机器人到位还要原地转到地图记录的朝向；去程要转 63°（转到只差 6° 就被切断）、回程要转 180°（每次转到不同程度）。云端等不到「到达」确认就判失败，且 `errorCode` 填 0 —— 云端码表把 0 译作 `SUCCESS / 无错误`，所以事件里显示成「failed:0x0000 SUCCESS（无错误）」，字面自相矛盾。库里 7 次这类失败**全部**是 `visited = total − 1`（只差最后一个点），而机器人其实到了（重定位报 0.13–0.42 m）；同一条路径在 09-07/09-08 成功过 21 次、最后一跳只要 1.3–2.4 s，之后变成 26–37 s 然后失败。代价是每段白等 26–37 s（一趟 123 s 里有 70 s 是它），巡检结果本身不受影响 —— 重试 → 重定位 → 「已在航点 X，无需导航」自愈，run 212–215 全部 completed。**已由机器人侧解决（yaw 对齐耗时过长）。** 回归验证（下一趟真机执行后做）：① 不再出现 `task_failed 0x0000` ② 最后一跳耗时回到 1.3–2.4 s ③ 不再有 `leg_retry` / `relocated`。查法：`SELECT ts,type,message FROM events WHERE run_id=<新 run> ORDER BY id`，看最后一个 `waypoint_reached` 与 `leg_arrived` 之间的间隔。
T29 前置检查失败时（我们一次都没下发过）执行器仍发 `DELETE /task`，会停掉**不是我们下发的**任务（现场有人的巡检、或机器人重定位留下的空任务）。改为只停自己下发过的；前置提示改为写清云端任务的地图/航点数/目标，并认出「路径为空 = 重定位残留」。附带：真机实测**机器人端重定位期间任务状态就是 NAVIGATING（地图与路径都为空）**——刚点过「定位」就执行必然被前置检查拦，这是 09-07 现场 7 次被拦的全部原因；mock 加 `/mock/relocalizing` 复现。
T28 TTS 合成跑在 `ThreadPoolExecutor` 的非守护线程里，合成一卡进程就退不掉（30 s+），`start.sh --stop` 落到 SIGKILL → 应用来不及中止执行与 `DELETE /task`，机器人会继续走。改成每次合成起守护线程 + join 超时（实测卡死后仍 0.26 s 干净退出）。
T24 真实云端在任务被中途替换时不发新的 `task_started`（状态无跃迁）→ 外部任务识别改为按事件 `total`/`visited` 与对账 `path` 比对（09-05 演练 D 抓到，run #119 曾误标完成）。
T23 mock → 真机切换后真机云端事件静默丢失（唯一索引全局按 `cloud_seq`，与 mock 时期的行撞号）→ schema v6 按 (gateway, cloud_seq) 唯一，监听器暴露 `dropped` 计数。
T21 edge-tts 合成无超时，通宵观察中真的卡死了一条执行（04:43 起 `inspecting` 不动，定时计划被跳过）→ 合成放到工作线程并带 `TTS_TIMEOUT`（默认 20 s），超时记错误、执行继续；启动时把上次进程残留的「进行中」执行标记为中止（`runs_reconciled`）。
T13 外部任务识别（`task_started.path` 与本段不符 → 中止且不停对方任务）；T16 systemd 单元（`deploy/`）；T17 时间戳带时区偏移；T19 幂等诚实失败（409 后查 `GET /task`，路径一致即按已下发）；T20 控制权 409 等待/重试次数可配（任务选项）；幂等键跨库撞键（加实例段）；测试残留执行线程污染共享 mock（`RunManager.shutdown`）；`pkill -f` 误杀自身 shell（pid 文件）；无头 Chrome 遇 SSE 不结束（`?nosse=1`）；`item_seq` 被对账写入覆盖（独立列 + schema v2）；mock `preempt seconds=0` 被当缺省；控制权测试在前置检查被拦（改为途中抢占）。

---
