# 问题与 TODO（与 ../../Sample_web_api/docs/task.md §9 同步）

## 0 进行中：跨平台桌面版（2026-10-01 起，方案见 DESKTOP.md）

优先级：用户体验 > 开发时间 > 安装包体积；目标是 Windows / macOS / Linux 上尽可能复现 Ubuntu 开发环境里的表现。
做法：**后端只保留 Python**，应用自带完整运行时（固定版本 Python 3.12 + 锁定依赖 + ffmpeg + Noto CJK 字体），
Electron 只做壳（拉起后端、窗口、托盘、优雅退出、仿真模式、自动更新），不含业务逻辑。开发顺序：先开发、后测试。

| # | 步骤 | 内容 | 状态 |
|---|------|------|------|
| D1 | 后端可移植修补 | `app/platform.py`（ffmpeg / 字体 / 子进程参数统一入口，优先用 runtime 里的）；`pano.py` 字体改走它；`POST /api/shutdown`（只绑本机、启动口令）；`.env` 读取 utf-8-sig；`PS_ENV_FILE` 可指定；6 处相对路径拼 URL 改 `as_posix`；`requirements.lock` 里 uvloop 排除 Windows；`soak.py` 的 /proc 采样非 Linux 跳过；`TTS_COMMAND` 在 Windows 用非 POSIX 切分 | **已做**（`tests/test_platform.py` 12 条；SSE 生成器顺带改成异步，见 DEVLOG） |
| D2 | runtime 组装脚本 | `scripts/build_runtime.py`：用 uv 装固定版本 python-build-standalone，按锁装依赖，下载本平台 ffmpeg 静态二进制与 Noto Sans CJK，写 `runtime/manifest.json`；来源与哈希钉在 `runtime.lock.json`；Linux 上先跑通并让全量用例对着 runtime 的解释器全绿 | **已做**（Linux runtime 411 MB，自检通过；ffmpeg 来源最终用 PyPI `imageio-ffmpeg` 轮子） |
| D3 | Python 统一 3.12 | `bootstrap.sh` 优先用 uv 建 3.12 的 `.venv`（3.10 本月停止维护）；重建 `.venv` 后全量用例 | **已做**：`bootstrap.sh` 优先 3.12（uv 自动下载）；本机 `.venv` 已重建为 3.12.13，全量用例通过；老环境在 `.venv.old` |
| D4 | Electron 壳 | `desktop/`：main.js 拉起后端、等健康检查、窗口、托盘菜单（打开页面 / 仿真模式 / 状态 / 数据目录 / 开机自启 / 检查更新 / 退出）、退出前确认并走 shutdown 接口、崩溃拉起、单实例、端口自选、`--smoke` 自检模式 | **已做**：开发态 `--smoke` 通过（起 2.2 s / 停 0.4 s）；`--screenshot` 截到真实窗口（docs/screenshots/10、11）；托盘菜单的各项在本机没人工点过 |
| D5 | 打包与 CI | electron-builder（Windows NSIS + zip，macOS dmg + zip，Linux AppImage + deb；runtime 与后端代码作为 extraResources）；GitHub Actions 三平台矩阵：组装 runtime → 全量用例 → 打包 → 产物；打 tag 发 Release 与更新元数据 | **已写**：本机出了 Linux AppImage（260 MB，含 runtime 预编译 pyc）并用 `--smoke` 验过打包后的布局；工作流**未推送、未在 GitHub 上跑过**；子代理只读审查抓到的 4 条确认问题（Windows 编码、mac 更新通道、mock 泄漏、退出重入）已修 |
| D6 | 文档与记账 | `docs/DESKTOP.md`（架构、构建、发布、排障）；USAGE §1 桌面版；deploy/README 三平台；HANDOFF / task.md §12 改写决策；CHANGELOG 0.8.0；DEVLOG | **已做**（`make docs-check` 通过为准） |
| D7 | 测试（开发完成后） | Ubuntu 全量用例（.venv 3.12 与 runtime 各一遍）；Electron `--smoke`；本地出一个 AppImage 装上跑仿真；CI 三平台全绿；用户在 Windows / mac 上真机冒烟 | **部分**：Ubuntu 全量用例 `.venv`(3.12) 与 runtime(3.12) 各一遍、`--smoke` 开发态与 AppImage 各一次已过；**Windows 步骤 A–D 已跑（2026-10-03，记录见下）**：runtime 176 用例全过、壳自检过、NSIS 包能装能卸、仿真跑一趟 completed、托盘各项与执行中退出都过；只剩播报出声没人耳听；**CI 与 mac 未跑**（下一步：推送后看 Actions 四个作业） |
| D8 | 用户侧事项 | 签名（Apple Developer；Windows 证书或 Azure Trusted Signing）；一台 Windows 与一台 mac 做冒烟；GitHub Releases 作为更新渠道 | 等用户 |

**下一步（按顺序）**：① 提交并推送本次改动，看 Actions 四个作业（第一次跑大概率要修 Windows / mac 的小问题，比如 PBS 的 Windows 布局、
`npm ci` 的缓存键、xvfb 下的 AppImage）；② 用户在 Windows 与 mac 上装包做人工与真机冒烟（TEST_PLAN 附表）；③ 签名接上后出正式 Release。

**桌面版带出来的新问题（未做）**：
- **桌面版没有 cron**：媒体清理（`scripts/cleanup_media.py`）与数据库备份（`scripts/backup_db.py`）在 Linux 上靠 cron（deploy/README §2），
  装了桌面版的 Windows / mac 没有这一环，`data/media` 会无限增长（T14 的桌面版形态）。做法：进程内定时（`ScheduleRunner` 每天一次）+
  设置项「媒体保留天数」，默认 30 天；备份同理。
- **托盘菜单与自动更新没在 mac 上点过**（Windows 已逐项点过，见下方实测记录）：第一次 CI 产包后要在 mac 上人工过一遍 DESKTOP.md §2 的每一项。

**Windows 实测记录（2026-10-03，按 WINDOWS_TEST.md 跑 A → D）**

环境：Windows 11 Pro 10.0.26200 x64；Node 22.23.3 / npm 10.9.9、MinGit 2.56.0（都是免安装版）；跑脚本的 Python 是 Microsoft Store 的 3.12.10；
没开长路径（仓库放在短路径下）；本机没装 uv（走了脚本自己 pip 装 uv 那条路）。

- **A**：`build_runtime.py --dev --prune` ✅ 313 MB（`--prune` 只省 2 MB）；runtime 3.12.13 跑全量 **176 passed，4 分 28 秒，0 跳过**（ffmpeg 抓帧那条跑了）。
- **B**：`npx electron . --smoke` ✅ `start_ms` 2922 / `stop_ms` 419（打包前那次 3620 / 416），python 3.12.13，ffmpeg 与字体都在 `runtime\`，无残留进程。
- **C**：`build_desktop.py --target "nsis zip"` ✅ 2 分 54 秒：`PatrolScheduler-0.8.0-win-x64.exe` 196.9 MB、`.zip` 262.7 MB、`latest.yml`；未签名。
- **D**（✅ 过 · ⏸ 没测到 · ❌ 不符）：
  - ✅ 静默安装到 `%LOCALAPPDATA%\Programs\PatrolScheduler`，开始菜单与桌面都有「巡检调度系统」；控制面板有卸载项
  - ✅ 第一次启动弹「第一次启动」，窗口落在「设置」页；✅ 托盘图标在（默认收在「隐藏的图标」里）
  - ✅ 托盘菜单齐全：打开页面 / 打开设置 / 当前：仿真模式（灰） / 仿真模式 ✓ / 状态… / 打开数据目录 / 打开日志目录 / 开机自启 / 检查更新… / 退出（会先停下机器人）
  - ✅ 仿真模式：左下 MOCK · 仿真、顶栏在线绿灯、数据目录 `data-mock`；托盘取消勾 → 5 s 切到真机（数据目录 `data`、mock 网关进程退出、通知「已切到真机模式」、菜单显示「当前：真机模式」），
    再勾上 → 5 s 切回（mock 网关重新拉起、执行记录还在）
  - ✅ 「状态…」对话框：模式 / 云端 / 版本 / 端口 / 三个线程 ✓ / 执行中 / Python 3.12.13 / ffmpeg、runtime、字体都指向安装目录 / 数据目录
  - ✅ 设置页「运行环境」原文：`Python 3.12.13 · 自带运行时 runtime · ffmpeg 有 · 中文字体 NotoSansCJKsc-Regular · win32`
  - ✅ 总览「抓一张全景」出图，标注中文正常（不是方块）；检查结果的标注图（机头线、角度区间）中文也正常
  - ✅ `seed_demo.py --reset --init --run --mock-speed 6`：4 段、3 次检查全过，completed；全景 / 裁切 / 标注 / 回答 / 播报句 / edge mp3 都在且都能取到
  - ✅ 关窗：进程还在、右下角弹「巡检调度系统还在运行 / 已收到托盘…」通知，托盘单击或「打开页面」窗口回来；⏸ 播报从扬声器出声（本机扬声器静音，没人耳听）
  - ✅ 「打开数据目录」开的是 `%APPDATA%\PatrolScheduler\data-mock`，「打开日志目录」里有 `desktop.log` / `backend.out` / `mock.out`
  - ✅ 「开机自启」：勾上 → `HKCU\…\Run` 多一项 `cn.patrolscheduler.desktop = "…\PatrolScheduler.exe"`，取消 → 删掉；菜单勾选状态跟着变
  - ✅ 「检查更新…」：弹「检查更新失败 / No published versions on GitHub」，不崩；启动时的自动检查同样只记 warn
  - ✅ 执行中点托盘「退出」：弹「有巡检正在执行。退出会中止这趟执行并让机器人停下（最多等 40 秒）。」；点「取消」程序与执行照旧；
    点「中止并退出」**2.1 s** 内 6 个进程（壳 4 + 后端 + mock 网关）全部消失，无 python / ffmpeg 残留，`backend.out` 退出码 0；
    `app.log`：`shutdown_requested → run_abort_requested → task_stop_confirmed（1.5 s 后 IDLE）→ run_finished aborted`，mock 网关收到 `DELETE /v1/robots/…/task`
  - ✅ 再次启动：仿真模式记得，执行记录在（#3 显示 aborted / 人工中止；卸载重装后记录也在）
  - ❌ 真机模式下页面仍显示 MOCK · 仿真（数据目录切到 `data`、顶栏离线都对；见下「通用」第 1 条）
  - ⚠ 只出现过一次：执行中点托盘「退出」，确认框弹在了别的窗口（当时在前台的 Chrome）**后面**，看起来像「点了退出没反应」；
    之后 4 次（退出 1 次、在资源管理器在前台时点「状态…」3 次）对话框都正常到了最前。疑似当时用户正在操作别的窗口、Windows 前台锁不让它抢。
    若现场再遇到：给确认框传父窗口（先 `showWindow()` 再弹）或 `win.flashFrame(true)` 提示
  - ✅ 卸载：程序目录、卸载项、安装键、两个快捷方式都删了；`%APPDATA%\PatrolScheduler` 保留

**第二轮（同日，从零重跑一遍，全程在 MSIX 沙箱外）**：删掉 `runtime/`、`runtime.cache/`、`node_modules`，卸载旧包，用户数据目录整个挪走，再按 A → D 走一遍。

- **A**：组装 runtime 49 s（这次真的走了「没装 uv → pip 装 uv」，修过的那条路一次过）314 MB；全量 **176 passed in 271.72 s**；`--check`、`check_docs.py` 过。
- **B**：`npm ci` 过；`--smoke` 2656 / 426 ms（打包前那次 2396 / 211 ms）。**C**：126 s，`.exe` 197.7 MB、`.zip` 263.6 MB。
- **D**：这次用**图形界面安装**：「安装选项」（默认「仅为我安装」）→「选定安装位置」（`%LOCALAPPDATA%\Programs\PatrolScheduler`，所需 686.7 MB）→ 安装 33 s →「完成」（默认勾「运行 PatrolScheduler」）。
  其余各项与第一轮相同，全部 ✅；新增与补充：
  - ✅ 真机模式填占位云端（`https://cloud.invalid:8443`）并保存：「当前适配器」立刻变 REAL 真机、顶栏「云端不可达」；**左下角仍是 MOCK · 仿真，重启程序后才变 REAL · 真机**（见下「通用」第 5 条）
  - ✅ 真机 / 仿真两套设置互不串：真机里填的占位地址，切回仿真后仍连内置 mock 网关
  - ✅ 托盘「退出」（无执行）不弹确认，5 个进程 1.2 s 退完；执行中「中止并退出」7 个进程 0.9 s、另一次 6 个进程 2.3 s 退完，`DELETE …/task` → 200，云端 1.7 s 后 IDLE，执行 aborted
  - 记一笔：有一次点「中止并退出」时正好最后一段到点，执行记为 completed 而不是 aborted（机器人已经停在终点，不用再停）—— 行为合理
  - ✅ 退出确认框：这轮 3 次都自己到了最前；它没有父窗口，在任务栏上与主窗口合成一个按钮「PatrolScheduler - 2 个运行窗口」
  - ✅ 用户文档 PDF 附录 B 里的一键演示命令（只用安装目录自带的 Python）原样照抄能跑：39 s，completed
  - 只剩 ⏸ 播报出声没人耳听

问题 —— **Windows**：
1. **已修**：没装 uv 时 `build_runtime.py` 第一步就退出（「装了 uv 但找不到 …\tools\Scripts\uv.exe」）—— `pip install --target` 在 Windows 上也把 exe 放进 `bin\`。改成 `bin\` 与 `Scripts\` 都找。CI 用 setup-uv，碰不到这条。
2. **已修**：WINDOWS_TEST §2 的 `$py` 少了 `runtime\` 前缀（`manifest.json` 里的路径相对 `runtime\`），照抄会「找不到 python.exe」。
3. **已修**：文档与 CI 的 `pytest -q -p no:cacheprovider` 与 `pytest.ini` 的 addopts 叠成 `-qq`，结尾汇总行不打。
4. 记一笔（不是问题）：Electron 44 的 npm 包没有 postinstall，`npm ci` 不下载二进制，第一次 `npx electron` 时才下；`build_desktop.py` 的 `install.js` 兜底照常起作用。
5. 记一笔（测试方法）：从 MSIX 打包的应用（如 Claude 桌面版）里开的终端带文件系统 / 注册表虚拟化，从那里起的程序写 `%APPDATA%`、HKCU 会被重定向 —— 后端的 `/media` 全部 404（starlette 的 realpath 校验对不上）、安装包的注册表项外面看不到、卸载会漏删快捷方式。
   经 explorer 起（双击快捷方式）就都正常，**不是产品问题**；已写进 WINDOWS_TEST §7。

问题 —— **通用**（Ubuntu 上同样存在，未改）：
1. **真机模式没填凭据时显示成 MOCK**：`CX_HOST` / `CX_KEY` 回落到 `config.py` `DEFAULTS` 里的 mock 值，而 `mode` 按 host 是否 127.0.0.1 推断 →
   第一次启动的设置页预填 mock 地址与密钥、左下角 MOCK · 仿真，壳却没起 mock 网关，于是顶栏「云端不可达」。与 WINDOWS_TEST §5「页面变回真机模式」不符。
   做法待定：壳在真机模式下传个标记（如 `PS_DESKTOP_MODE=real`），`Config` 据此不回落 mock 默认值、`mode` 以壳为准。
2. **网络错误的原因被吞掉**：`app/robot/client.py` 去 HTML 标签的清洗把 urllib 的 `<urlopen error [WinError 10061] …>` 整段删了，
   `events.last_error` 只剩「GET /events 网络失败:」。做法：`certaintyx.py` 拼消息用 `e.reason`，或清洗只删真正的 HTML 标签。
3. `--smoke` 只核对抓图接口返回的 URL 形状，没去取那张图 —— 上面 Windows 第 5 条的 404 它就看不出来。做法：smoke 里再 `GET` 一次那个 URL。
4. 安装包带着开发依赖：`requirements.lock` 里钉着 pytest / ruff（连带 Pygments、pluggy、iniconfig），带不带 `--dev` 都会装进 runtime 并打进安装包，
   约 36 MB（ruff.exe 一个 25 MB）。做法：开发依赖从锁里拆出去（`--dev` 本来就会另装）。体积优先级最低，记着。
5. **已修（2026-10-03 晚）保存云端设置后左下角模式标识不刷新**：现在 `setStatus()` 每收到一次状态快照就核对 `mode`，变了就重画（实测改 `CX_HOST` 后 1–3 s 自动变，两个方向都对）。原问题：`web/js/app.js` 的 `renderMode()` 只在 SSE `hello` 时跑一次，存了 `CX_HOST` 后后端已是 real，
   左下角仍显示 MOCK · 仿真；桌面壳去掉了菜单，没有 F5，用户只能重启程序（托盘「打开设置」只改 hash，不重新加载）。
   做法：`PUT /api/settings` 重连后往总线发一条 `mode` 事件让前端 `renderMode()`，或设置页保存成功后 `location.reload()`。

已知残留差异（不影响巡检主流程）：Windows 没有 uvloop，用标准 asyncio；Linux 桌面上浏览器朗读兜底可能无声（主路径是 edge 合成的 mp3）；edge-tts 三平台都要联网；桌面版不带 ffplay，`local` 汇出只在 PATH 里有 ffplay 时可用（窗口本身就是本机扬声器）。


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
| T2 | 云端 API 没有机器狗扬声器端点 | **已解决，两条路**：① **09-14 起云端有了** `POST …/tts`（机器人本地 piper 合成）/ `…/tts/audio`（播我们的 mp3），本系统汇出 `robot`（`TTS_SINKS=robot`，v0.6.0）直接走它，要求机器人固件 ≥ 2026-09-14；② 固件没升的机器用自带 `audio_server/`（`http` 汇出）兜底。剩余：真机上把 `robot` 汇出听一次（`GET /api/robot/tts` 先看 `available` 与声卡） |
| T4 | 云端不暴露地图点云下载 | 手工上传 `.pcd/.ply` + 体素下采样接口已通；`PointCloudProvider.fetch_from_robot` 留桩 |
| T10 | 真 VLM 的**准确率**未验证 | 通路已实测：`qwen3.5-flash`（DashScope 兼容模式）在真机上判读过 **26 次**（09-07 21:04 → 09-10，用户自己跑的），库里共 591 条检查 / 216 趟执行。**缺的是标注**：人工改判 0 条，所以 `make eval`（拿改判结果当标注算准确率）没有样本可用。下一步：在「执行监控」里对几十条检查做人工复核（判通过/判不通过），再跑 `make eval` 出总体与按航点的准确率。另外**空 prompt 的任务航点会稳定产出「无法判断」**——线上 591 条里的「无法判断」几乎都是这么来的，不是模型判不出来（给那些点填上 prompt，或从任务的检查列表里去掉）|
| T11 | 单机器人 | `robots` 表 + 每机器人一组线程（roadmap） |
| T12 | 无登录鉴权；`TTS_COMMAND` 可在设置页改成任意命令 | 默认只绑 127.0.0.1；局域网暴露需反向代理 + 鉴权，并把危险设置移出网页 |
| T14 | 媒体与事件无限增长 | `scripts/cleanup_media.py` 已有，需 cron 化 |
| T15 | 前端只有无头截图 + DOM 抽查，无自动化交互测试 | 引入 Playwright（需 pip + 浏览器驱动） |
| T18 | mock 局限：无 `nav_preprocess`/充电桩状态、无真实速度曲线、丢事件补发只部分复刻 | 真机差异回填 |

## 3 已解决（留档）
T37 **抓帧可能抓到「还没收敛」的画面，而且会被静默送去判读**。接入直播 H.264 流时解码器常从刷新周期中间开始，抓下来是上下恒定的竖带（或一半正常一半糊）。要命的是 **ffmpeg 这时退出码仍是 0、图片照样输出**，只在 stderr 里报 `non-existing PPS` / `decode_slice_header error` / `no frame!` / `concealing …`；而原来的判断是 `returncode != 0 or not stdout`，两条都不成立 → 糊图当成正常结果，模型对着噪声给出「不是」或「无法判断」。上游教程 SDK 的 `snapshot()` 是同样的写法。实测：构造一段从 GOP 中间接入的流，退出码 0、输出 24 KB、stderr 104 行全是解码错误。线上真机 30 张里抓到 1 张（run 213 的 `runs/213/leg02_tw10_193701_pano.jpg`，纵向细节 0.17，而正常是 2.58–3.84），它正是唯一那条「prompt 非空却答无法判断」的检查。**已修**：抓完做两道体检（stderr 解码错误特征 + 画面纵向细节度），不合格自动重抓（`SNAPSHOT_MAX_ATTEMPTS`，默认 3）；重抓到底仍不合格就跳过判读、答案记 `error`、记一条 error 级 `snapshot_degraded` 事件，图仍留档。指标落库（schema v7 的 `frame_score` / `frame_attempts`）便于事后审计。用例 `tests/test_snapshot_qc.py`（19 条，两道体检各自可独立考到）。
T30 **最后一个航点的 yaw 对齐超时 → 云端 `task_failed` 错误码 `0x0000`**（09-08 19:30 起）。底层加入 yaw 角控制后，机器人到位还要原地转到地图记录的朝向；去程要转 63°（转到只差 6° 就被切断）、回程要转 180°（每次转到不同程度）。云端等不到「到达」确认就判失败，且 `errorCode` 填 0 —— 云端码表把 0 译作 `SUCCESS / 无错误`，所以事件里显示成「failed:0x0000 SUCCESS（无错误）」，字面自相矛盾。库里 7 次这类失败**全部**是 `visited = total − 1`（只差最后一个点），而机器人其实到了（重定位报 0.13–0.42 m）；同一条路径在 09-07/09-08 成功过 21 次、最后一跳只要 1.3–2.4 s，之后变成 26–37 s 然后失败。代价是每段白等 26–37 s（一趟 123 s 里有 70 s 是它），巡检结果本身不受影响 —— 重试 → 重定位 → 「已在航点 X，无需导航」自愈，run 212–215 全部 completed。**已由机器人侧解决（yaw 对齐耗时过长）。** 回归验证（下一趟真机执行后做）：① 不再出现 `task_failed 0x0000` ② 最后一跳耗时回到 1.3–2.4 s ③ 不再有 `leg_retry` / `relocated`。查法：`SELECT ts,type,message FROM events WHERE run_id=<新 run> ORDER BY id`，看最后一个 `waypoint_reached` 与 `leg_arrived` 之间的间隔。
T29 前置检查失败时（我们一次都没下发过）执行器仍发 `DELETE /task`，会停掉**不是我们下发的**任务（现场有人的巡检、或机器人重定位留下的空任务）。改为只停自己下发过的；前置提示改为写清云端任务的地图/航点数/目标，并认出「路径为空 = 重定位残留」。附带：真机实测**机器人端重定位期间任务状态就是 NAVIGATING（地图与路径都为空）**——刚点过「定位」就执行必然被前置检查拦，这是 09-07 现场 7 次被拦的全部原因；mock 加 `/mock/relocalizing` 复现。
T28 TTS 合成跑在 `ThreadPoolExecutor` 的非守护线程里，合成一卡进程就退不掉（30 s+），`start.sh --stop` 落到 SIGKILL → 应用来不及中止执行与 `DELETE /task`，机器人会继续走。改成每次合成起守护线程 + join 超时（实测卡死后仍 0.26 s 干净退出）。
T24 真实云端在任务被中途替换时不发新的 `task_started`（状态无跃迁）→ 外部任务识别改为按事件 `total`/`visited` 与对账 `path` 比对（09-05 演练 D 抓到，run #119 曾误标完成）。
T23 mock → 真机切换后真机云端事件静默丢失（唯一索引全局按 `cloud_seq`，与 mock 时期的行撞号）→ schema v6 按 (gateway, cloud_seq) 唯一，监听器暴露 `dropped` 计数。
T21 edge-tts 合成无超时，通宵观察中真的卡死了一条执行（04:43 起 `inspecting` 不动，定时计划被跳过）→ 合成放到工作线程并带 `TTS_TIMEOUT`（默认 20 s），超时记错误、执行继续；启动时把上次进程残留的「进行中」执行标记为中止（`runs_reconciled`）。
T13 外部任务识别（`task_started.path` 与本段不符 → 中止且不停对方任务）；T16 systemd 单元（`deploy/`）；T17 时间戳带时区偏移；T19 幂等诚实失败（409 后查 `GET /task`，路径一致即按已下发）；T20 控制权 409 等待/重试次数可配（任务选项）；幂等键跨库撞键（加实例段）；测试残留执行线程污染共享 mock（`RunManager.shutdown`）；`pkill -f` 误杀自身 shell（pid 文件）；无头 Chrome 遇 SSE 不结束（`?nosse=1`）；`item_seq` 被对账写入覆盖（独立列 + schema v2）；mock `preempt seconds=0` 被当缺省；控制权测试在前置检查被拦（改为途中抢占）。

---
