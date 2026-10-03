# 变更记录

## [0.8.1] - 2026-10-03 —— Windows 实测后的修正 + 现场用户安装指南

在 Windows 11 x64 上按 `docs/WINDOWS_TEST.md` 从零跑了两轮 A → D（组装 runtime、全量用例、壳自检、打包、图形界面安装、
托盘各项、执行中退出、卸载），记录与问题清单在 `docs/TODO.md` §0。

**修**

- 没装 uv 时 `scripts/build_runtime.py` 第一步就退出 —— `pip install --target` 在 Windows 上也把 `uv.exe` 放进 `bin\`
  （不在 `Scripts\`），现在两处都找。CI 用 setup-uv，碰不到这条。
- 在设置页改了云端地址（仿真 ↔ 真机）后，左下角的模式标识不刷新（只在 SSE `hello` 时设一次，桌面壳又没有刷新键，
  只能重启程序）。现在每次收到状态快照都核对 `mode`，保存后几秒内自动变。

**新增**

- `docs/guide/`：给现场用户的《巡检调度系统 桌面版 安装与配置指南》PDF（21 页，Windows 实拍截图，带书签）及源文件、生成脚本。

**文档**

- `WINDOWS_TEST.md`：§2 的 `$py` 补上 `runtime\` 前缀；`pytest.ini` 已带 `-q`，文档与 CI 不再重复加（否则汇总行不打）；
  §7 加「从 MSIX 打包应用（如 Claude 桌面版）的终端里起程序会被文件系统 / 注册表虚拟化」一条。
- `.gitattributes` 加 `*.pdf binary`。

**测试**：Windows 11 上 runtime（3.12.13）跑全量 176 passed；`--smoke` 起 2.4–3.7 s / 停 0.2–0.4 s；
图形界面安装 33 s；托盘「中止并退出」0.9–2.3 s 内进程全部退出并停下机器人；卸载干净、用户数据保留。

## [0.8.0] - 2026-10-02 —— 跨平台桌面版：自带运行时 + Electron 壳（三平台同一份）

**为什么**：要在 Windows / macOS / Linux 上「点击就能用」，并尽可能复现 Ubuntu 开发机上的表现。
优先级（用户定）：用户体验 > 开发时间 > 安装包体积。上一次跨平台尝试把后端用 Node 重写（已放弃，`task.md` §12），
这次**后端只保留 Python**：应用自带固定版本的 Python 3.12、锁定依赖、ffmpeg、Noto CJK 字体，Electron 只做壳。
全文见 `docs/DESKTOP.md`。

**新增**

- `scripts/build_runtime.py` + `runtime.lock.json`：组装 `runtime/`（Python 3.12.13 经 uv 下载 python-build-standalone、
  按 `requirements.lock` 装依赖、ffmpeg 取自 PyPI `imageio-ffmpeg` 四个平台的轮子、Noto Sans CJK），
  来源与 sha256 全部钉死；`--check` 核对锁没变、解释器与 ffmpeg 能跑；`--pin` 重新钉来源。
- `desktop/`：Electron 壳（`main.js`）。拉起后端、等健康检查、窗口 + 托盘、仿真模式（内置 mock 网关 + 独立数据目录）、
  退出前确认并走 `POST /api/shutdown` 优雅停止（先停机器人，最多 40 s）、后端崩溃拉起、端口自选、单实例、
  自动更新（GitHub Releases）、`--smoke` 自检模式。
- `scripts/build_desktop.py`：核对 runtime → 同步版本号到 `desktop/package.json` → npm ci → 自检 → electron-builder
  （Windows nsis+zip / macOS dmg+zip / Linux AppImage+deb）。`.github/workflows/desktop.yml` 三平台矩阵。
- `app/platform.py`：ffmpeg / 字体 / 命令切分 / 子进程参数的统一入口（先 runtime，再环境变量，再 PATH）。
- `POST /api/shutdown`（只在设置了 `PS_SHUTDOWN_TOKEN` 时存在）与 `GET /api/platform`；`/api/health` 带 `platform`。
- `make runtime` / `runtime-check` / `runtime-test` / `desktop` / `desktop-smoke` / `dist`。

**改**

- **SSE 生成器改成异步**（`app/api/stream.py`）：客户端断开后立刻释放订阅与「有页面在看」计数。原来同步生成器在线程池里
  等 15 s keepalive、再靠解释器回收生成器才释放 —— Python 3.10 碰巧很快，3.12 下 25 s 都等不到（`tests/test_stream.py`
  在统一到 3.12 时抓到；不释放 = 状态轮询一直按高频跑）。
- `config/.env` 读取用 utf-8-sig（Windows 记事本的 BOM 会让第一行键失效），位置可用 `PS_ENV_FILE` 指定。
- 媒体相对路径拼 URL 的 6 处改 `as_posix()`（Windows 的 Path 是反斜杠）。
- `requirements.lock`：uvloop 标记为 `sys_platform != "win32"`（没有 Windows 轮子，照锁装会失败）；`make lock` 会保留该标记。
- `bootstrap.sh`：优先建 Python 3.12 的 `.venv`（有 uv 时自动下载同一份 python-build-standalone；3.10 已停止维护）。
- `scripts/soak.py`：资源采样有 psutil 用 psutil，否则只在 Linux 读 `/proc`；临时目录用 `tempfile`；解释器用 `sys.executable`。
- 字体候选改走 `platform.font_candidates()`：runtime 的 Noto CJK 优先，其次各系统自带 CJK 字体。

- **进程退出时 SSE 自己结束**：uvicorn 优雅退出会等在途请求，而 SSE 永不结束 —— 有网页开着时 `ctx.stop()`（中止执行、
  `DELETE /task` 停机器人）要到 40 s 强杀前都跑不到（桌面壳退出实测 44 s；systemd 停服务同样如此）。
  改：SSE 每轮检查 `server.should_exit`、`/api/shutdown` 广播 `shutdown`、`timeout_graceful_shutdown=5` 兜底。退出降到 4 s。
- 设置页「系统自检」多一行**运行环境**（Python / 自带运行时 / ffmpeg / 中文字体）。
- 桌面壳的 `--screenshot=` / `--open=` / `--scroll-to=`（文档截图与 CI 产物用）。
- 只读代码审查（子代理）后修的：Windows CI 的 stdout 编码（`PYTHONUTF8` + `reconfigure`）；macOS 自动更新要 zip 目标且按架构分通道
  （`latest-arm64` / `latest-x64`）；崩溃重启不再漏 mock 网关；退出流程不可重入、`Backend.stop()` 幂等；
  后端子进程 `PYTHONDONTWRITEBYTECODE=1`；Windows 上 uv 的 junction 别名能删掉；`macos-15-intel` runner。

**测试**：`tests/test_platform.py` 12 条（查找顺序、命令切分、BOM、URL 正斜杠、shutdown 接口的 404/403/501/200）、
`tests/test_build_runtime.py` 9 条（组装脚本不联网的部分）、`tests/test_stream.py` 加 2 条（退出时 SSE 结束）。
全量用例用 `.venv`（3.12，重建后）与 `runtime/`（3.12）各跑一遍全绿；Electron `--smoke` 在开发态与打好的 AppImage 里各过一次。

## [0.7.0] - 2026-09-15 —— 到点流程提速：常驻读流 + 播报预合成 + 分阶段计时

**现象**：用户反馈真机上「到点 → 抓帧 → 判读 → 播报」偏慢，怀疑是 mp3 太大传输慢。
拉 run 247 的事件时间线量化（到点后计）：稳定等待 3.0 s（任务选项 `settle_seconds=3`）→ 抓帧 0.5–2.3 s（抖）
→ 判读 1.7–2.4 s → 合成 + 推送 + **等播完** 3.4–4.1 s → 下发下一段 0.7 s，每点约 10–12 s 站着不动。
mp3 一句只有 ~20 KB，**不是**瓶颈；瓶颈是等关键帧、每次现合成、以及 `TTS_ROBOT_WAIT=1` 干等机器狗念完。

**根因与实测**：一次性起 ffmpeg 抓一帧必须等到**下一个关键帧**，真机直播流 GOP ≈ 3 s，
单独实测 1.0–3.4 s 抖动；`-fflags nobuffer -analyzeduration 0` 等低延迟参数无效（0.1–2.6 s，仍取决于接入时机）。
常驻解码则连上后每 0.5 s 一帧，随时可取。

**改**

- **常驻读流器** `LiveStreamSource`（`SNAPSHOT_KEEPALIVE=1`，默认开，只套在 rtsp / hls / http 源外面）：
  执行器在**下发下一段时**就把流连上、常驻解码，内存里只留最新一帧，到点 `grab()` 直接取 ≤ 1.5 s 内的新鲜帧
  （实测 ≤ 0.5 s 且不抖）。没预热 / 等不到新帧 → **退回一次性抓帧**（原逻辑），永远不会比以前慢。
  体检语义不变：纵向细节度不够换下一帧（最多 `SNAPSHOT_MAX_ATTEMPTS` 次）；ffmpeg 刚报过解码错误的 1 s 内的帧视为可疑、直接等干净帧。
  空闲 `SNAPSHOT_KEEPALIVE_IDLE`（120 s）没人抓帧自动断开；15 s 没新帧自动重连；换源 / 退出时停掉。
- **播报预合成缓存**：合成结果按（引擎, 声音, 文本）存 `media/tts_cache/`；任务规划完成时把每个任务航点
  三种答案下的句子全部 `prewarm()`（后台、逐句、带超时），到点 `speak()` 直接命中（edge 每句 0.5–1.3 s 省掉）。
  推给机器人的字节每次相同，机器人端按内容哈希命中缓存、不再转码。`cleanup_media` 也清 `tts_cache`，命中会刷新 mtime。
- **分阶段计时**：每条 `tts` 事件的文案后面带「到点后等 x s · 抓帧 x s(常驻流) · 判读 x s · 播报 x s(缓存)」，
  `data.stages` 有毫秒级明细；SSE 推出去的检查结果也带 `stages`（不落库）。以后慢在哪一眼可见。
- `ctx.snapshot.stop()` 在换抓图源与进程退出时调用；`/api/settings` 的抓图源描述带 `+live(读流中|空闲)`。

**用户侧还要改的设置**（代码不替用户改）：`TTS_ROBOT_WAIT=0`（入队即走，机器狗边走边播，省 2.5–3.5 s/点）；
任务选项 `settle_seconds` 3 → 1–2（真机到点即停，摄像头稳定不需要 3 s）。
预计每点从 10–12 s 降到 4–5 s：等待 1–2 + 抓帧 ≤0.5 + 判读 ~2 + 推送 ~0.5 + 下发 ~0.7。

**测试**：`tests/test_pipeline_speed.py` 5 条（读流器冷启动退回 / 热取帧 / 空闲自停 / 解码错误后等干净帧 / 体检不合格语义、
缓存与预合成、Null 引擎不预合成、句子收集），全量 **153 用例**通过。

## [0.6.0] - 2026-09-14 —— 机器人自带扬声器：接上云端新增的语音播报接口

**背景**：上游 `Sample_web_api` 5fe1e59（2026-09-14）新增 `POST …/tts`（机器人本地 piper **离线**合成一段文字）、
`POST …/tts/audio`（播放调用方发过去的 mp3/wav，≤ 5 MB）、`GET/DELETE …/tts`（状态 / 打断）。
此前云端没有扬声器端点（T2），我们只能自带 `audio_server` 部署到机器狗上。要求机器人端固件 ≥ 2026-09-14。

**改**

- SDK 同步到 5fe1e59（`app/vendor/certaintyx.py` 仍是原样拷贝，多了 `tts / tts_audio / tts_status / tts_stop`）。
- 新增汇出 **`robot`**（`TTS_SINKS` 里加 `robot`）：没有音频文件（`TTS_ENGINE=none`）就把文字发给机器人本地合成；
  有音频文件（edge 引擎，音色好）就原样推 `/tts/audio`；推不成（机器人缺 ffmpeg / 格式不认 / 超 5 MB）
  **自动退回文字** —— 永远有声音。四个设置项：`TTS_ROBOT_MODE`（auto / text / audio）、`TTS_ROBOT_VOLUME`
  （机器人会记住，留空不改）、`TTS_ROBOT_WAIT`（默认 0：入队即走、边走边播；1 = 念完再走下一段）、`TTS_ROBOT_INTERRUPT`。
- 与巡检下发共用全局限速器与控制权：409 提示「现场有人操作」；队列满 20 → 429；
  旧固件的一页 HTML 404 识别为「固件太旧」并指向 `http` 汇出兜底，**不**去重试。
- `wait=1` 时机器人最多等 50 s，比 SDK 默认 30 s 超时长，所以 robot 汇出用自己的 `RobotClient`（60 s），仍经全局限速器。
- 新接口 `GET /api/robot/tts`（引擎 / 声卡 / 队列 / 音量）、`DELETE /api/robot/tts`（打断，记 `tts_stop` 事件）；
  设置页 TTS 组新增四项，试听旁多了「🐕 机器人播报状态」「⏹ 打断」。`/api/settings` 的 `adapters.tts.robot` 回显汇出参数。
- mock 网关复刻整组端点与错误码（400 / 403 / 409 / 413 / 429 / 503、旧固件 HTML 404、幂等重放、三种上传方式、
  按文件头识别格式且优先于声明、wait 50 s → `pending`、volume 记住、`/v1` 索引 17 条）；
  `/mock/fault` 新增 `tts_firmware_old` / `tts_unavailable` / `tts_no_ffmpeg`；`/mock/state` 多 `tts` 摘要。
- `audio_server/` 与 `http` 汇出**保留**：给固件还没升级的机器人兜底。

**默认值不变**：`TTS_SINKS=browser`（mock 开发）。真机建议 `TTS_SINKS=robot,browser`；
想省掉 edge 的联网依赖就 `TTS_ENGINE=none`，文字由机器人自己念。

**测试**：mock 契约 4 条 + robot 汇出 / 接口 / 端到端 6 条，全量 **148 用例**通过。

## [0.5.0] - 2026-09-10 —— 抓帧体检：不再拿没收敛的画面去判读

**现象**：留档的全景有时是一条条上下恒定的竖带，有时一半正常一半糊；模型对着噪声给出
「不是」或「无法判断」。用户在另一台机器上大面积撞到。

**根因**：接入一条正在直播的 H.264 流时，解码器往往从刷新周期中间开始，画面要一个刷新周期
才收敛 —— 这一刻抓下来就是竖带。**不是丢包**（抓帧走的是 `-rtsp_transport tcp`），是抓帧时机。
而要命的是 **ffmpeg 这时退出码仍是 0、图片照样输出**，只在 stderr 里报
`non-existing PPS` / `decode_slice_header error` / `no frame!` / `concealing …`；
原来的判断是 `returncode != 0 or not stdout`，两条都不成立，于是糊图被静默送去判读。
上游教程 SDK 的 `snapshot()` 是同样的写法。

实测证据：构造一段从 GOP 中间接入的 H.264，**退出码 0、输出 24 KB、stderr 104 行全是解码错误**。
线上真机 30 张里抓到 1 张（`runs/213/leg02_tw10_193701_pano.jpg`，纵向细节 **0.17**，
正常是 2.58–3.84），它正是唯一那条「prompt 非空却答无法判断」的检查。

**修**

- 抓完做两道体检：① ffmpeg 的 **stderr** 有没有解码错误特征 ② 画面**纵向细节度**够不够
  （竖带画面每列上下恒定 → 该值趋近 0）。两道都放在重抓循环里，所以检测到就能立刻重抓。
- 不合格自动重抓（`SNAPSHOT_MAX_ATTEMPTS`，默认 3）；**重抓到底仍不合格就跳过判读** ——
  答案记 `error`、记一条 error 级 `snapshot_degraded` 事件、图仍然留档。
  既不拿糊图去问模型，也不白花一次付费调用。
- 只对 `rtsp` / `hls` 这类 ffmpeg 抓真实流的源生效；`synthetic` / `file` / `lavfi` 不体检
  （合成场景本来就很平坦，实测 Gazebo 空白墙只有 0.38，会被误判）。
- 两个设置项（设置页与 `config/.env` 都能改）：`SNAPSHOT_MAX_ATTEMPTS`、
  `SNAPSHOT_MIN_DETAIL`（0 = 关掉画面体检，留给画面本来就极平坦的现场）。
- **schema v7**：`inspections.frame_score` / `frame_attempts` 落库，事后能审计
  「哪些判读是基于坏图做的」；老行为 NULL（不是 0 —— 那会被误读成「画面全糊」）。

**测试**：`tests/test_snapshot_qc.py` 19 条 + 迁移用例 1 条，全量 **138 用例**通过。
两道体检**各自可独立考到**：分别把其中一道删掉，都有对应用例变红
（第一版的 stderr 用例是空的 —— 它第一帧用了竖带图，画面体检顺手就挡住了，
把 stderr 检查整段删掉照样全绿，已改成用「画面正常但 stderr 报错」的组合）。

## [0.4.10] - 2026-09-10 —— 只保留 Python 这一条线；文档与代码对齐

**为什么**：那段跨平台重写（135 个提交，从未推送）已确认放弃，本仓库只走 Python 这一条线。
借这次把「文档说的」和「代码做的」逐条对了一遍。

**修（都是文档与代码不符，不是新功能）**

- **`/api/health` 一直报 `0.1.0`**：`app/main.py` 里硬编码 `version='0.1.0'`，从 v0.1 一路漂到
  v0.4.9 都没跟着走 —— 升级后想确认「跑的是哪一版」会被它骗。改成版本号只有一个来源
  （`app/__init__.py` 的 `__version__`），`make docs-check` 会核对它与 CHANGELOG 最新条目一致
  （这条检查本身也验过会红）。
- **README 教的建 venv 方式正是会出事的那条**：`python3 -m venv --without-pip --system-site-packages`
  —— 它就是造出「445 个包、numpy/pytest 各两个版本」的原因。改成 `./bootstrap.sh --dev`，
  并写清 `PYTHONPATH` 这个坑与手工跑时的规避写法。
- **`deploy/README.md` 的两条 cron 照抄跑不起来**：cron 的工作目录是家目录，而那两行用的是
  相对路径 `.venv/bin/python` / `data/logs/` —— cron 不报错，只是静默不跑。加上 `cd`。
- **vendored SDK 的出处写错**：`README.md` 与 `app/robot/client.py` 都写 `a23fc40`，
  而本地拷贝的 md5 与上游 `6167083` 逐字节相同（这次用 md5 核对过）。
- `docs/task.md` 写着 `schema 版本 v5`，代码是 v6。
- `docs/HANDOFF.md`：仓库状态（81 提交、`main` 已推送、用 SSH）、
  **qwen3.5-flash 已在真机判读 26 次**（原文写「只差 DashScope 密钥没实测过」），
  并把三条环境坑（`PYTHONPATH` 顶包、venv 里没有 pip、Pillow 12 变严格）写进 §5.4。
- `docs/TODO.md`：T30（yaw 对齐超时）已由机器人侧解决 → 移入「已解决」并留下回归验证方法；
  T10 按实测更新（缺的不是密钥，是**人工标注**：0 条改判 → `make eval` 没样本）；
  删掉与那段重写相关的条目。未决条目 25 → 23。
- `docs/README.md` / `README.md` / `docs/TEST_PLAN.md` §0 / `docs/HANDOFF.md` §3 §9：
  统一指向 `bootstrap.sh` 与 `docs/TEST_REPORT.md`。

**清理**

- 删掉那段重写留下的 1.2 GB 磁盘残留（`node_modules` 655 MB、`dist` 480 MB、
  以及被替换掉的旧 `.venv.old` 85 MB），以及相关分支与标签。

## [0.4.9] - 2026-09-09 —— Ubuntu 上的环境稳定性

**为什么**：这台机器的系统 Python 就是机器人的 ROS 2 + CUDA + torch 环境，而 `~/.bashrc`
里 `source /opt/ros/humble/setup.bash` 会设置 `PYTHONPATH`（11 个目录），它排在 venv 的
`site-packages` **前面**。原 `.venv` 又是 `include-system-site-packages=true` 建的，于是
venv 里能看到 **445 个包**，numpy/pytest/scipy/PyYAML 等各有两个版本并存，`requests`/`Pillow`
实际来自 apt、`numpy` 来自 `~/.local` —— **`apt upgrade` 一动，巡检系统 import 到的东西就变了**。
外加 venv 里没有 pip（装不了修不了），而系统 python 的 `ensurepip` 也被删了，
文档里教的 `python3 -m venv .venv && .venv/bin/pip install` 在这台机器上跑不通。

**新增**

- `bootstrap.sh`：找 3.10+ 解释器（优先 `uv`，退回 `python -m venv`）→ 建**隔离** venv →
  按 `requirements.lock` 装精确版本 → 自检「每个包都来自 venv 内部、`sys.path` 无外来目录」。
  `ensurepip` 与 `uv` 都缺时给出两条明确出路。干净 clone 上 **9.2 秒**跑完，随后 `pytest` 118/118。
- `scripts/soak.py`：Python 版浸泡测试，22 类场景轮换（正常/故障注入/人工操作/运维动作/网络抖动），
  核对每类的预期结局与幂等键唯一性，每轮记 RSS/线程数/fd 数。app 与 mock 网关都跑成子进程走真实 HTTP。
  **VLM=mock、TTS=none，不消耗任何付费接口。**
- `docs/TEST_REPORT.md`：本轮测试报告（含隔离改造的前后对比）。

**修**

- **`.venv` 重建为隔离环境**（`include-system-site-packages=false`），包数 **445 → 55**，
  `sys.path` 外来目录 11 → 0；`requirements.lock` 重新生成为 52 个精确版本。
- `start.sh` / `run.sh` / `bootstrap.sh` 启动前 `unset PYTHONPATH PYTHONHOME` 并设
  `PYTHONNOUSERSITE=1`；`Makefile` 的 11 处 python 调用统一走 `PYRUN`；
  systemd 单元加 `UnsetEnvironment=PYTHONPATH PYTHONHOME`。没有 ROS 的机器上是无害空操作。
- **`app/media/pano.py`：合成图里门扇矩形的坐标是反的**（小图上宽度变负 → `x1 < x0`）。
  Pillow 9 静默吞掉、Pillow 12 直接 `ValueError` —— 这个门框在小图上一直没画出来过。
  修法是「画不出来就不画」，而不是去钉住已 EOL 的 Pillow 9。
- `docs/USAGE.md` §1 换成 `bootstrap.sh`，并写明 `PYTHONPATH` 这个坑与手工跑 `.venv/bin/python`
  时的规避写法。

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
