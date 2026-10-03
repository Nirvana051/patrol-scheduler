# 去 Windows 上测桌面版（交接单，分支 `muti-plat`，v0.8.0）

写给去 Windows 机器上做验证的人。目标是回答一个问题：**在 Ubuntu 上验过的那套东西，在 Windows 上是不是同样的表现。**
Ubuntu 上已经过的：全量用例（`.venv` 与 runtime 各一遍）、壳自检、打包后的 AppImage 自检、真实窗口截图。
Windows 上**一次都没跑过**，下面每一步都可能在第一次就暴露问题，照实记下来就是成果。

按顺序做 A → B → C → D，前一步没过就停在那一步把现象记下来（看 §6 的常见问题），不用硬往下走。
真机那一步（E）可选。整套大约 1 小时，其中下载占一半。

## 0. 准备机器

| 需要 | 怎么装 | 怎么确认 |
|------|--------|----------|
| Windows 10 或 11，x64 | — | `winver` |
| Git for Windows | https://git-scm.com | `git --version` |
| Node.js 22 LTS | https://nodejs.org （LTS） | `node -v` 是 v22.x；`npm -v` |
| Python 3.12（只用来跑 `scripts/*.py`，后端用的是 runtime 自带的） | https://www.python.org/downloads/windows/ ，安装时勾 **Add python.exe to PATH** | `python --version` 是 3.12.x，而且**不是** Microsoft Store 的空壳（见 §6） |
| uv（可选，没有脚本会自己装一份） | `winget install astral-sh.uv` | `uv --version` |
| 能访问 GitHub、PyPI、npm | — | 浏览器打开 https://pypi.org |

磁盘留 3 GB。杀毒软件会让 `npm ci` 与 runtime 组装慢好几倍，可以先对仓库目录加排除项。

全程用 **PowerShell**（不要用 VS Code 里的终端跑 Electron，原因见 §6）。先打开长路径支持，node_modules 和 runtime 的路径都很深：

```powershell
# 管理员 PowerShell 跑一次即可
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force
git config --global core.longpaths true
```

## 1. 取代码

```powershell
cd D:\
git clone -b muti-plat git@github.com:Nirvana051/patrol-scheduler.git
cd patrol-scheduler
git log --oneline -1          # 应该是 muti-plat 分支最新那条
```

仓库带 `.gitattributes`（全部 LF），不用改 `core.autocrlf`。

## 2. 步骤 A：组装 runtime 并用它跑全量用例

```powershell
$env:PYTHONPATH = $null; $env:PYTHONNOUSERSITE = '1'; $env:PYTHONUTF8 = '1'
python scripts\build_runtime.py --dev --prune
```

期望：逐行 ✅，最后一行 `完成：D:\patrol-scheduler\runtime  共 4xx MB`。它做的事：用 uv 下载 Python 3.12.13（python-build-standalone）、
按 `requirements.lock` 装依赖、从 PyPI 的 imageio-ffmpeg 轮子里取出 ffmpeg 7.1、下载 Noto Sans CJK 字体，然后自检（10 个包能 import、字体能画中文、`ffmpeg -version` 能跑）。

然后用 **runtime 里的解释器**跑全量用例（路径以 `runtime\manifest.json` 里的 `python.exe` 为准，一般就是下面这个）：

```powershell
$py = Join-Path (Resolve-Path runtime) ((Get-Content runtime\manifest.json | ConvertFrom-Json).python.exe -replace '/', '\')
& $py -m pytest
```

（`manifest.json` 里的 `python.exe` 是相对 `runtime\` 的路径；`-q -p no:cacheprovider` 已在 `pytest.ini` 的 addopts 里，再加一个 `-q` 会连结尾的汇总行都不打。）

期望：**176 用例**全部通过，约 4 到 6 分钟。会有一条用例因为「需要 ffmpeg」而跳过是不对的，Windows 上 runtime 里有 ffmpeg，它应该跑；
`tests/test_build_runtime.py` 里的符号链接那一段在 Windows 上不执行（用例本身照常通过，不显示为跳过）。

把结尾的汇总行记下来。有失败就把整条 `FAILED` 与它上面的回溯复制下来。

## 3. 步骤 B：壳的自检（不开窗口）

```powershell
cd desktop
npm ci
npx electron . --smoke
cd ..
```

期望：几秒后打印一行 JSON，`"smoke":"ok"`，`python` 是 `3.12.13`，`ffmpeg` 与 `font` 的路径都在 `runtime\` 下，`mode` 是 `mock`。
它做了：拉起内置 mock 网关与后端、健康检查、刷新机器人状态、抓一张合成全景、调 shutdown 接口优雅退出。
记下 `start_ms` 与 `stop_ms`（Ubuntu 上是 2 秒 / 0.2 秒）。

想看窗口：`npx electron . --mock`。托盘图标在右下角（可能被收进「隐藏的图标」里）。

## 4. 步骤 C：出安装包并安装

```powershell
python scripts\build_desktop.py --target "nsis zip"
```

期望：`desktop\dist\PatrolScheduler-0.8.0-win-x64.exe`（安装包）、`PatrolScheduler-0.8.0-win-x64.zip`、`latest.yml`。
这一步会再跑一次自检，并且第一次会下载 electron-builder 的工具（winCodeSign、nsis），几分钟。

双击安装包。**未签名**，SmartScreen 会弹「Windows 已保护你的电脑」：点「更多信息」→「仍要运行」。安装到用户目录即可，不需要管理员。

## 5. 步骤 D：人工清单（装好后）

逐项打勾，不符合的写现象。数据目录在 `%APPDATA%\PatrolScheduler\`。

- [ ] 第一次启动弹「第一次启动」提示，窗口直接落在「设置」页。
- [ ] 右下角托盘有图标；菜单项齐全：打开页面 / 打开设置 / 仿真模式 / 状态… / 打开数据目录 / 打开日志目录 / 开机自启 / 检查更新… / 退出。
- [ ] 托盘勾「仿真模式」：几秒后页面刷新，左下角出现 **MOCK · 仿真**，顶栏「在线」绿灯。
- [ ] 设置页最下面「系统自检 → 运行环境」一行：`Python 3.12.13 · 自带运行时 runtime · ffmpeg 有 · 中文字体 NotoSansCJKsc-Regular · win32`。
- [ ] 总览点「抓一张全景」出图；图上的中文标注**不是方块**。
- [ ] 灌演示数据并跑一趟（端口若不是 8088，看托盘「状态…」里的端口）：
  ```powershell
  & $py scripts\seed_demo.py --base http://127.0.0.1:8088 --reset --init --run --mock-speed 6
  ```
  「执行监控」里一趟 4 段、3 次检查跑完，状态 completed；检查结果有全景、裁切、回答、播报句。
- [ ] 播报：有网时窗口里会放 edge 合成的 mp3（电脑扬声器出声）；没网则浏览器朗读或只显示文字，不报错。
- [ ] 关掉窗口：程序不退出，右下角弹「已收到托盘」通知；点托盘图标窗口回来，页面状态还在。
- [ ] 托盘「打开数据目录」打开的是 `%APPDATA%\PatrolScheduler\data-mock`；「打开日志目录」里有 `desktop.log`、`backend.out`、`mock.out`。
- [ ] 「开机自启」勾上再取消，`Win+R` → `shell:startup` 或任务管理器「启动」里能看到相应变化。
- [ ] 「检查更新…」：现在没有 Release，应提示检查失败或已是最新，**不能**崩。
- [ ] 执行一趟进行中时点托盘「退出」：先弹确认「有巡检正在执行」；点「中止并退出」后 **5 秒内**进程消失（任务管理器里没有 PatrolScheduler 与 python.exe 残留）。
- [ ] 再次启动：仿真模式还记得，之前的执行记录还在。
- [ ] 把「仿真模式」取消：页面变回真机模式（没有凭据时顶栏会显示离线，这是对的），数据目录切到 `data`。
- [ ] 控制面板卸载：干净，`%APPDATA%\PatrolScheduler` 保留（刻意不删用户数据）。

## 6. 步骤 E（可选）：真机

设置页填 `CX_HOST` / `CX_ROBOT` / `CX_KEY`，`SNAPSHOT_SOURCE` 改 `rtsp`，保存。总览「抓一张全景」应 3 秒左右出真实画面，事件里没有 `snapshot_degraded`。
`TTS_SINKS` 加 `robot` 后点「试听 TTS」，机器人出声。细则看 [OPERATIONS.md](OPERATIONS.md)。

## 7. 常见问题

| 现象 | 处置 |
|------|------|
| `python` 弹出 Microsoft Store 或报「找不到」 | 设置 → 应用 → 高级应用设置 → 应用执行别名，关掉两个 python 别名；确认装了 python.org 的 3.12 并在 PATH |
| `npm` / `npx` 报「禁止运行脚本」 | PowerShell 执行策略：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`，或改用 `npm.cmd` / `npx.cmd` |
| `npx electron .` 像普通 node 一样报 `Cannot read properties of undefined (reading 'setAppUserModelId')` | 终端里有 `ELECTRON_RUN_AS_NODE=1`（VS Code 的终端会带）。换独立的 PowerShell，或 `Remove-Item Env:ELECTRON_RUN_AS_NODE` |
| build_runtime 报 uv 相关错误 | 没装 uv 时脚本会 `pip install uv` 到 `runtime.cache\tools\`；若公司网络拦 GitHub，uv 下载 Python 会失败，换网络 |
| 装依赖时报 EXTERNALLY-MANAGED | 不应出现（脚本会删那个标记）；出现了把 `runtime\python\...\Lib\EXTERNALLY-MANAGED` 删掉重跑 |
| 用例里 `test_lavfi_source_grabs_jpeg_via_pipe` 被跳过 | 说明后端没找到 runtime 里的 ffmpeg：确认 `runtime\ffmpeg\ffmpeg.exe` 存在、`runtime\manifest.json` 存在，且测试是在仓库根目录跑的 |
| 全景标注里中文是方块 | `runtime\fonts\NotoSansCJKsc-Regular.otf` 不在，或后端没识别到 runtime；设置页「运行环境」能看出来 |
| SmartScreen 拦截 | 更多信息 → 仍要运行。正式版要签名（TODO D8） |
| 托盘「退出」后 python.exe 残留 | 记下来，这是最重要的一类问题（退出应走 `/api/shutdown` 优雅停止）。顺手看 `%APPDATA%\PatrolScheduler\logs\desktop.log` 最后几行 |
| 端口 8088 被占 | 壳会自动换下一个端口，看托盘「状态…」 |
| 杀毒软件删了 `runtime\ffmpeg\ffmpeg.exe` 或 electron | 加排除项后重新 `python scripts\build_runtime.py` |
| 页面上的全景 / 检查图片全裂（`/media/...` 404），但文件明明在数据目录里；或卸载后开始菜单、桌面的快捷方式还在、控制面板里没有卸载项 | 安装包或程序是从 **MSIX 打包的应用**（如 Claude 桌面版）里开的终端起的：那里有文件系统 / 注册表虚拟化，写 `%APPDATA%` 与 HKCU 会被重定向到 `%LOCALAPPDATA%\Packages\<应用>\LocalCache\`。用资源管理器双击安装包与快捷方式就正常 —— 不是产品问题 |
| 用例汇总行没打出来 | `pytest.ini` 里已有 `-q`，命令行再加 `-q` 就成了 `-qq`。直接 `-m pytest` |

## 8. 记什么、怎么交回来

1. 步骤 A 的最后一行与 pytest 汇总行；步骤 B 的那行 JSON；步骤 C 的产物文件名与大小。
2. §5 清单的打勾结果，不符合的附截图。
3. 出问题时的三份日志：`%APPDATA%\PatrolScheduler\logs\desktop.log`、`logs\backend.out`、`data-mock\logs\app.log`。
4. 设置页「运行环境」那一行的原文。

推送分支后 GitHub Actions 会自动跑四个平台的作业（`.github/workflows/desktop.yml`），其中 win-x64 作业做的就是步骤 A、B、C；
它绿了而本机没过，差异多半在本机环境（§7）；它红了，把作业日志里第一处红字贴回来。

把结果记进 [TODO.md](TODO.md) §0 的 D7 那一行，问题按 Windows / 通用分开。
