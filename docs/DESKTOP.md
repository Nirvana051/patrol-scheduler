# 桌面版（Windows / macOS / Linux）

一句话：**后端只保留 Python，应用自带完整运行时，Electron 只做壳。** 三个平台装的是同一份 Python 3.12、
同一份锁定依赖、同一来源的 ffmpeg、同一个 Noto Sans CJK 字体和同一个 Chromium，所以「在 Ubuntu 开发机上看到的表现」
能在现场的 Windows / mac 上复现。优先级（用户定的）：用户体验 > 开发时间 > 安装包体积。

曾经有一段把后端用 Node 重写并套 Electron 的尝试（135 个提交，已放弃，见 `task.md` §12）——这次**不是**那条路：
壳里一行业务逻辑都没有，后端就是仓库里的 `app/`，壳只通过 HTTP 和它说话。

## 1. 组成

| 目录 | 是什么 | 谁维护 |
|------|--------|--------|
| `runtime/`（不进仓库） | 固定版本 Python 3.12 + 按 `requirements.lock` 装好的依赖 + ffmpeg + Noto CJK 字体 + `manifest.json` | `scripts/build_runtime.py` 组装；来源与哈希钉在 `runtime.lock.json` |
| `desktop/` | Electron 壳：`main.js`（三百多行）、`package.json`（electron-builder 配置）、图标、macOS entitlements | 手写 |
| `app/ web/ mock_gateway/ audio_server/ scripts/` | 后端与前端，原样打进安装包 | 和以前一样 |
| `.github/workflows/desktop.yml` | 三平台 CI：组装 runtime → 用它跑全量用例 → 壳自检 → 打包 → 产物 / Release | 手写 |

安装后的布局（以 `resources/` 为根；AppImage、.app、NSIS 都一样）：

```
resources/
  app.asar            壳（main.js 与图标）
  backend/            app/ web/ mock_gateway/ audio_server/ scripts/ config/env.example requirements.lock runtime.lock.json
  runtime/            python/<cpython-3.12.x-平台>/  ffmpeg/ffmpeg(.exe)  fonts/NotoSansCJKsc-Regular.otf  manifest.json
```

后端怎么找到自己的运行时：壳拉起 `python -m app.main` 时传 `PS_RUNTIME_DIR`；`app/platform.py` 据此回答
「用哪个 ffmpeg、哪个字体」（先 runtime，再 `PS_FFMPEG_DIR` / `PS_FONT`，最后才是 PATH 与系统字体目录）。
没有 runtime 的裸环境（Ubuntu 开发机的 `.venv`）行为与以前完全一样。

## 2. 用户看到的

- **安装**：Windows 跑 `PatrolScheduler-x.y.z-win-x64.exe`（按用户安装，不要管理员；可改目录）；macOS 打开 dmg 拖进「应用程序」；
  Linux 给 AppImage 加可执行权限后双击，或装 deb。
- **第一次启动**自动进「设置」页：填云端地址、机器人别名、密钥，保存即生效（设置存在数据库里，**不用再碰 .env**）。
  想先看效果：托盘菜单勾「仿真模式」。
- **窗口与托盘**：关掉窗口只是收进托盘，巡检与定时计划继续跑。托盘菜单：打开页面 / 打开设置 / 仿真模式 / 状态… /
  打开数据目录 / 打开日志目录 / 开机自启 / 检查更新… / 退出。
- **退出 = 先停机器人**：有执行在跑会先弹确认；然后壳调后端的 `POST /api/shutdown`（只绑本机、每次启动随机口令），
  uvicorn 优雅退出 → `ctx.stop()` 中止执行并 `DELETE /task`，最多等 40 秒（与 systemd 单元的 `TimeoutStopSec` 一致）。
  不走信号是因为 Windows 没有 SIGTERM、Node 也发不出 Ctrl+Break。
- **仿真模式**：壳自己拉起内置的 mock 网关，后端指过去；**数据目录独立**（`data-mock`），演示数据不会混进真机的库
  （这也顺手解决了 T27「settings 表里的 CX_* 覆盖 mock 环境变量」）。
- **自动更新**：接 GitHub Releases。后台检查到新版先下载，装不装由用户点；若有执行在跑会先停机器人再重启安装。
- **后端意外退出**自动拉起并通知；10 分钟内超过 5 次就停手并指向日志。端口被占自动换下一个。

数据与日志的位置（`userData`）：

| 平台 | 路径 |
|------|------|
| Windows | `%APPDATA%\PatrolScheduler\`（`data\`、`data-mock\`、`config\.env`、`logs\`） |
| macOS | `~/Library/Application Support/PatrolScheduler/` |
| Linux | `~/.config/PatrolScheduler/` |

`logs/desktop.log` 是壳的日志，`logs/backend.out` 是后端的标准输出，后端自己的滚动日志仍在 `data/logs/app.log`。

## 3. 构建与发布

```bash
python3 scripts/build_runtime.py --dev     # 组装 runtime/（约 400 MB；--dev 另装 pytest/ruff，--prune 裁掉标准库里用不到的）
make runtime-test                          # 用 runtime 的解释器跑全量用例 —— 「三平台一致」的第一条验收
cd desktop && npm ci && cd ..
make desktop-smoke                         # 壳自检：拉起后端(仿真) → 健康检查 → 抓图 → 优雅退出（不开窗口，CI 也跑它）
python3 scripts/build_desktop.py           # 出本平台的安装包到 desktop/dist/（会先 --check runtime、同步版本号、再 smoke）
```

- **版本号只有一个来源**：`app/__init__.py` 的 `__version__`。`build_desktop.py` 把它写进 `desktop/package.json`，
  安装包文件名、「关于」、更新元数据都用它；`make docs-check` 核对它与 CHANGELOG 一致。
- **锁**：`requirements.lock`（Python 包）与 `runtime.lock.json`（Python 版本、ffmpeg 轮子、字体，含 sha256）。
  改了任何一个，`build_runtime.py --check` 会报「过时」，重跑组装即可。重新钉来源用 `--pin`
  （从 PyPI 元数据取 `imageio-ffmpeg` 四个平台轮子的哈希，下载后读出各平台的 ffmpeg 版本）。
- **ffmpeg 版本**：Linux 7.0.2、Windows / macOS 7.1（上游静态构建就是这样分的），同一大版本，抓帧用到的参数集一致；
  只有 ffmpeg 没有 ffplay —— 桌面版不需要：窗口本身就是本机扬声器，`browser` 汇出直接播。
- **CI**（`.github/workflows/desktop.yml`）：push 到 `main` / `V1`、PR、手动触发都会在 ubuntu-22.04 / windows-2022 /
  macos-14（arm64）/ macos-15-intel（x64）各跑一遍，产物挂在 Actions；打 `v*` 标签时发布到 GitHub Releases
  （连同 electron-updater 用的 `latest*.yml`）。发布一版的动作：改 `__version__` + CHANGELOG → `git tag v0.8.0` → push。
  工作流设了 `PYTHONUTF8=1`：Windows runner 的 stdout 默认 cp1252，脚本打印中文会直接炸。
- **macOS 的更新通道按架构分**：electron-updater 在 mac 上要求更新元数据里有 zip（所以 mac 目标是 `dmg zip`），而 arm64 与 x64
  两个作业各自发布会互相覆盖同一个 `latest-mac.yml`。`build_desktop.py` 在 mac 上传 `-c.publish.channel=latest-<arch>`，
  壳里 `autoUpdater.channel = 'latest-' + process.arch`，于是 Release 里是 `latest-arm64-mac.yml` 与 `latest-x64-mac.yml` 各一份。
- **签名**（用户侧事项，见 TODO D8）：CI 有 `CSC_LINK` / `CSC_KEY_PASSWORD`（Windows 与 macOS 的证书）与
  `APPLE_ID` / `APPLE_APP_SPECIFIC_PASSWORD` / `APPLE_TEAM_ID`（公证）时自动签；没有就出未签名包：
  Windows 装时 SmartScreen 拦一次（更多信息 → 仍要运行），macOS 要在「系统设置 → 隐私与安全性」里点「仍要打开」，
  **且 macOS 上的自动更新要求签名**。macOS 的 hardened runtime 需要 `desktop/build/entitlements.mac.plist` 里那几项
  （放开 JIT 与库校验，否则随包的 Python 加载不了 numpy / Pillow 的扩展模块）。

## 4. 后端为此做的改动（v0.8.0）

- `app/platform.py`：runtime 目录、ffmpeg / 字体查找顺序、`TTS_COMMAND` 的切分（Windows 不吃反斜杠）、子进程参数
  （Windows 不闪黑框）。`snapshot.py` / `tts/base.py` / `pano.py` 都改走它。
- `POST /api/shutdown` 与 `GET /api/platform`（`app/api/system.py`）；`/api/health` 多了 `platform` 一节
  （Python 版本、runtime 目录、ffmpeg、字体），三平台对账一眼看出用的是不是同一套。
- `.env` 读取用 utf-8-sig（记事本的 BOM），位置可用 `PS_ENV_FILE` 指定；媒体相对路径拼 URL 一律 `as_posix()`。
- `requirements.lock` 里 uvloop 标记为非 Windows；`soak.py` 的 `/proc` 采样有 psutil 用 psutil、否则只在 Linux 读。
- **SSE 生成器改成异步**（`app/api/stream.py`）：原来同步生成器跑在线程池里，客户端断开后要等 15 s keepalive 把线程放出来、
  再靠解释器回收生成器才释放订阅 —— Python 3.10 碰巧很快，3.12 下 25 s 都等不到（`tests/test_stream.py` 在统一到 3.12 时抓到）。
  这是「换一个 Python 小版本就会变」的典型例子，也是为什么要把解释器版本钉死并在三平台跑同一套用例。

## 5. 「表现一致」的验收标准与已知差异

验收（CI 全绿即满足前两条）：

1. 全量 pytest 用 runtime 里的解释器在 Linux / Windows / macOS 全部通过。
2. 壳自检 `--smoke` 三平台通过：后端报的 Python 版本 = runtime 的，ffmpeg 与字体都来自 runtime。
3. 同一份演示数据在仿真模式跑一趟，三平台的执行结果与事件序列一致（人工对比「执行监控」）。
4. 全景标注截图三平台一致（字体同一份）。
5. 一台 Windows 和一台 mac 上对真机做 RTSP 抓帧与播报冒烟（用户侧）。

已知差异（都不影响巡检主流程）：Windows 没有 uvloop，用标准 asyncio 事件循环；Linux 桌面上浏览器朗读的兜底
（`TTS_ENGINE=none`）可能无声，主路径是 edge 合成的 mp3；edge-tts 三平台都要联网；桌面版不带 ffplay，
`local` 汇出只在 PATH 里有 ffplay 时可用。

## 6. 排障

| 现象 | 原因 / 处置 |
|------|------------|
| `electron .` 像普通 node 一样报 `Cannot read properties of undefined (reading 'setAppUserModelId')` | 环境里有 `ELECTRON_RUN_AS_NODE=1`（VS Code / Claude Code 之类的 Electron 宿主会带）。`make desktop-smoke` 与 `build_desktop.py` 已自动清掉；手动跑用 `env -u ELECTRON_RUN_AS_NODE npx electron .` |
| 壳报「找不到 Python 运行时」 | 开发机没组装 `runtime/`：`python3 scripts/build_runtime.py`；没 runtime 时壳会退到 `.venv`（仅开发态） |
| 后端 60 秒没起来 | 看 `logs/backend.out`。常见：端口全被占、数据目录不可写、`config/.env` 里的地址写错 |
| Linux AppImage 双击没反应 | 需要 FUSE：`sudo apt install libfuse2`；或 `APPIMAGE_EXTRACT_AND_RUN=1 ./PatrolScheduler-*.AppImage` |
| macOS「无法打开，因为无法验证开发者」 | 未签名包：系统设置 → 隐私与安全性 → 仍要打开（一次即可）。彻底解决要签名与公证 |
| Windows「Windows 已保护你的电脑」 | 未签名包：更多信息 → 仍要运行。彻底解决要代码签名证书 |
| 全景标注里的中文是方块 | 没用上 runtime 字体：`/api/platform` 看 `font`；开发机没 runtime 时装 `fonts-noto-cjk` 或设 `PS_FONT` |
| 抓帧报「本机没有 ffmpeg」 | `/api/platform` 看 `ffmpeg`；桌面版应指向 `resources/runtime/ffmpeg/`；开发机设 `PS_FFMPEG_DIR` 或装系统 ffmpeg |
| 退出卡 40 秒 | 后端优雅退出超时（多半是云端不响应 `DELETE /task`），40 秒后壳强制结束；看 `data/logs/app.log` |
| `build_runtime.py --check` 报锁过时 | `requirements.lock` 或 `runtime.lock.json` 改过，重跑组装 |

## 7. 开发

- 改后端不用重打包：`npm run mock`（或 `make desktop`）用的是仓库里的代码与 `runtime/`（没有则 `.venv`）。
- 壳的日志在 `~/.config/PatrolScheduler/logs/desktop.log`（Linux），`console.log` 只在 `--smoke` 下输出。
- 壳的命令行开关：`--mock`（仿真模式启动）、`--smoke`（自检，不开窗口）、`--screenshot=x.png`（页面画完截一张就优雅退出）、
  `--open=#/settings`（启动时打开哪个视图）、`--scroll-to=#b-health`（截图前把某元素滚进视口）。
  `docs/screenshots/10-*.jpg`、`11-*.jpg` 就是这样截的：`npx electron . --mock --open=#/settings --scroll-to=#b-health --screenshot=x.png`。
- 图标由 `desktop/build/make_icons.py` 生成（与网页 favicon 同一图形），electron-builder 由 512 px 的 PNG 派生 ico / icns。
- 壳故意不用 preload / IPC：页面就是现有的 `web/`，壳不往页面里注入任何东西。需要壳做的事都走托盘菜单。
