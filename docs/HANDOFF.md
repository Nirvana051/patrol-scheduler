# 交接文档（HANDOFF）

> 给下一个接手的人（或下一段上下文里的我）：读完这一页就能继续干活。
> 日常怎么用看 [USAGE.md](USAGE.md)；真机安全细则看 [OPERATIONS.md](OPERATIONS.md)；设计与问题清单看 [task.md](task.md)。
> 最后更新见文末。

## 1. 这是什么

certaintyX 机器狗云端 API（`https://certaintyx.sg:8443`，教程仓库 [kafeiyin00/Sample_web_api](https://github.com/kafeiyin00/Sample_web_api)）之上的**本地网页巡检调度台**：
SQLite 管任务航点/任务/执行/事件；**分段下发**（机器狗不能在航点暂停）；到点抓全景 → 按角度裁切 → VLM 判「是/不是」→ 按答案模版 TTS 播报。

代码 `/home/leo/agent/scheduler/`（git，81 提交，tag `v0.1.0` … `v0.4.7`；远端 `git@github.com:Nirvana051/patrol-scheduler.git`（公开），**`main` 已推送**——见 §8）。
主管理文档是 `../Sample_web_api/docs/task.md`（那边未被上游仓库跟踪），`docs/task.md` 是它的同步副本，用 `scripts/sync_task_doc.sh` 同步。

## 2. 现状（一句话版）

- 真机密钥在 `config/.env`（gitignored）。**别再 `cp config/env.example config/.env`** —— 09-04 干过一次，把密钥覆盖成占位符，结果全是 401。
- **配置优先级：`settings` 表 > `config/.env` > 代码默认值**。用户在网页上改过的项会存进库并盖掉 `.env`；`start.sh --mock` 导出的 mock 环境变量也会被盖掉（T27，变通：`PS_DB_PATH=/tmp/mock.db ./start.sh --mock`）。查当前生效值：`GET /api/settings` 或直接看 `settings` 表。
- mock 上全流程 + 通宵 104 趟稳定；**真机（Gazebo）09-05 夜跑通完整流程**并通宵 56 趟零失败；09-07 用户自己在另一台真机（工厂/实验室场景，33 点地图）上跑通 `lab巡逻`，机头已校准（`FORWARD_DEG=269.3`）。
- VLM **已用真模型实测**：`qwen3.5-flash`（DashScope 兼容模式）在真机上判读过 **26 次**（09-07 21:04 → 09-10，用户自己跑的）；库里共 591 条检查、216 趟执行。剩下的缺口是**准确率还没系统评过**（0 条人工改判 → `make eval` 没有标注可用，T10）。

## 3. 怎么跑

```bash
cd /home/leo/agent/scheduler
./bootstrap.sh --dev  # 换机器/首次：建隔离 venv + 按 requirements.lock 装依赖 + 自检（约 10 s）
./start.sh            # 按 config/.env 启动（真机），后台，打开 http://127.0.0.1:8088
./start.sh --mock     # 仿真：本机 mock 网关 + 调度系统，不需要任何凭据
./start.sh --audio    # 叠加：本机播报服务 127.0.0.1:5566
./start.sh --status | --stop | --restart
make test             # 118 用例，约 4 分钟；make lint
make smoke            # 真机只读冒烟（scripts/real_smoke.py）
make vlm              # VLM 连通性探针（接新模型先跑这个）
make seed             # 仿真下灌演示数据并跑一趟
```

日志 `data/logs/app.log`（应用）与 `data/logs/app.out`（stdout）；pid 在 `data/run/`；库 `data/scheduler.db`（schema **v6**）。

## 4. 代码地图（改哪儿）

| 想改 | 看 |
|------|----|
| 与云端的调用 / 限速 | `app/robot/client.py`（**只做**全局令牌桶 + HTML 错误清洗；重试都在 SDK 里）。SDK 原样在 `app/vendor/certaintyx.py`，**不要本地改** —— 上游更新时 `cp ../Sample_web_api/examples/python/certaintyx.py app/vendor/`（最近同步：`6167083`） |
| 状态灯、定位是否就绪 | `app/robot/status.py`（C9：只认 `received_at` 新鲜度） |
| 云端事件监听、游标、落库 | `app/robot/events.py`（唯一性按 `(gateway, cloud_seq)`） |
| 分段执行状态机 | `app/executor/runner.py`（起点/下发/到达/失败/重试/暂停/丢定位/外部任务/停任务核实） |
| 抓图 → 裁切 → VLM → TTS | `app/executor/inspection.py`、`app/media/pano.py`、`app/media/snapshot.py` |
| VLM / TTS 适配器 | `app/vlm/*`（`QwenVlm` = DashScope，非流式必须带 `enable_thinking:false`）、`app/tts/base.py`（合成用**守护线程**，见 §5.4）；扬声器端 `audio_server/` |
| REST / SSE | `app/api/*.py`，装配在 `app/context.py`，入口 `app/main.py`（含前端 `no-cache`） |
| 定时计划 | `app/scheduler.py`（daily / interval，不引 cron 库） |
| 前端 | `web/js/views/*.js`（原生 JS 无构建；`?nosse=1` 静态模式供无头截图；`#/tasks/<id>`、`#/waypoints/<id>` 深链） |
| mock 云端网关 | `mock_gateway/`（按契约仿真 + 故障注入，见 §6） |
| 数据库 | `app/schema.sql`（v6）+ `app/db.py` 增量迁移 |

## 5. 必须记住的坑（血泪，全部真机踩过）

### 5.1 云端 API 侧
1. **幂等键 10 分钟内全局唯一**：换库/重装会撞键 → 云端只回放不执行；每次重新下发都要换新键（键格式 `ps-{instance}-r{run}-l{leg}-a{attempt}`）。
2. **订阅事件队列、取游标都要在下发之前**；到达判定还要 `GET /task` 对账兜底；要能识别「没开始 / 停滞 / 掉线 / 被外部任务替换 / 丢定位」五种「不再会到达」。
3. **网关重启后事件 seq 归零** → 游标要重置（已做，会记 `event_cursor_reset`）。
4. **真实网关偶发 nginx 版 HTML 502**（不是 JSON 信封）：错误文本要清洗，连续超过 `offline_timeout` 才判段失败。
5. **`/position` 200 不代表定位新鲜**：机器人端停发 `/global_localization` 后云端一直回放缓存值（09-07 实测冻结 20 小时仍 200）。定位就绪只认 `received_at` 新鲜度。
6. **`/perception.Location`**：2026-09-06 之前的机器人端恒为 1；新版才如实给 0。现场那台是 **legacy 版**。跨版本都别用它判断。
7. **急停请求体必须显式布尔 `{"active": true}`**：旧版机器人端把空体当「取消急停」，新版直接 400。

### 5.2 机器人行为（真机实测，与文档没写的）
8. **机器人端间歇性不理会 `DELETE /task`**：12 次停止里 4 次云端回 200「任务已停止」而机器人走到终点才停（成串出现）。系统停任务后会核实 `stop_wait_seconds`，核实不了记红色事件 + 通知，可选自动急停。
9. **软件急停会滑行 5–7 秒（约 5 m）才停住，且不取消云端任务**；取消急停后机器人接着走完。不是硬件急停。
10. **机器人端重定位期间，任务状态就是 `NAVIGATING`（`map_name` 与 `path` 都为空）**。刚点过「④ 定位」就执行，必然被前置检查的「云端有任务在跑」拦下 —— 09-07 现场 7 次被拦全是这个。提示里已按「路径为空 = 定位残留」区分。
11. **中途被替换的任务，云端不发新的 `task_started`**（状态没有跃迁）。外部任务识别靠事件里的 `total` 与本段路径长度比对、以及对账时比对 `path`。
12. **Gazebo 与真狗共用 roscore 时，绝不能调 `device/start|stop`**（会把仿真速度指令接到真狗上 / 杀真机节点）。那种环境定位现成，直接执行。

### 5.3 我们自己的设计约束
13. **前置检查失败时不要去停云端任务** —— 我们一次都没下发过，那可能是别人的巡检或定位残留（T29 修过一次）。只停 `dispatched_any` 为真的。
14. 进程退出前要中止执行并 `DELETE /task`（`RunManager.shutdown`）；启动时把库里残留的「进行中」执行标为中止（`runs_reconciled`）。

### 5.4 工程/环境
15. **TTS 合成必须跑在守护线程里**：原先用 `ThreadPoolExecutor`（非守护），合成一卡（edge-tts 会卡）进程 30 s+ 退不掉 → `start.sh --stop` 落到 SIGKILL → 应用来不及停机器人。
16. **`pkill -f` 会匹配到自己的命令行**（连 heredoc 内容也算）把 shell 杀掉 —— 重启用 `./start.sh --restart`，别把 kill 和 start 写在同一条命令里。
17. 测试共用一个 mock 机器人：残留执行线程会污染后面的用例（靠 `RunManager.shutdown`）。
18. 无头 Chrome 遇 SSE 长连接不结束 → 页面加 `?nosse=1`。
19. 前端资源已设 `no-cache`；老部署上「改了 UI 看不到」要硬刷新（Ctrl+Shift+R）。
20. **换机器部署先看换行符**：仓库经 Windows 中转/在 Windows 上 clone 后 `*.sh` 变 CRLF，`bash` 直接报 `‘bash\r’: No such file or directory`。已加 `.gitattributes`（`* text=auto eol=lf`）从源头堵住；老 clone 用 `sed -i 's/\r$//'` 修。同时 `requirements.txt` 原先把 Pillow/requests/numpy 当"系统装好的"，新机器上会缺——现已列全。解释器可用 `PS_PYTHON` 指到自己的环境。
21. **`PYTHONPATH` 会把 venv 里的包顶掉**（这台机器上最容易中的一条）：`~/.bashrc` 里
    `source /opt/ros/*/setup.bash` 会设置 `PYTHONPATH`（11 个目录），它排在 venv 的
    `site-packages` **前面**。原先的 `.venv` 又是 `--system-site-packages` 建的，于是 venv 里
    能看到 **445 个包**（整个 ROS 2 + torch + CUDA），numpy/pytest/scipy 各有两个版本并存，
    `requests`/`PIL` 实际来自 apt、`numpy` 来自 `~/.local` —— **`apt upgrade` 一动，import 到的东西就变了**。
    现在：`bootstrap.sh` 建**隔离** venv（`include-system-site-packages=false`，**55 个包**），
    `start.sh` / `run.sh` / `Makefile` / systemd 单元启动前都清掉 `PYTHONPATH`。
    **手工直接跑 `.venv/bin/python` 仍会中招** —— 要么走 `make`，要么
    `env -u PYTHONPATH PYTHONNOUSERSITE=1 .venv/bin/python …`（T32）。
22. **venv 里原先没有 pip**，而系统 python 的 `ensurepip` 也被删了 —— `python3 -m venv` 建不出
    能用的环境。`bootstrap.sh` 优先用 `uv`，都没有时明确告诉你装哪个（`python3-venv` 或 `uv`）。
23. **Pillow 12 比 Pillow 9 严格**：`rectangle` 收到 `x1 < x0` 会直接 `ValueError`（9 是静默吞掉）。
    升级依赖后必跑 `tests/test_pano.py` —— 合成图里的门扇矩形就是这么暴露的（在小图上一直没画出来过）。

## 6. mock 网关能仿什么

`mock_gateway/` 按上游文档逐条仿真，并复刻了真机实测到的行为：状态词写读不对称、`Location` 恒 1（可切 `new` 版）、未定位 `/position` 503、透传只认机器人 ID、幂等重放、5 rps 限流、急停空体陷阱、`nav_preprocess` 阶段、完成后回 idle、idle 无 `progress`、中途替换不发 `task_started`。

故障注入 `POST /mock/*`：`fault`（避障/规划失败）、`offline`、`ros`、`preempt`（现场抢控制权）、`obstacle`（停滞）、`lose-localization`、`drop-events`、`html502`（可只针对 `/task`）、`ignore_stop`（停止指令被忽略）、`freeze_telemetry`（定位陈旧）、`relocalizing`（重定位残留状态）、`robot-version`（legacy/new）、`teleport`、`speed`、`reset`。

## 7. 待办与规划

- 未完成/待真机确认：`docs/TODO.md`（与 `task.md §9` 同步）。此刻最重要的三件：**T10 真 VLM 效果**（等 DashScope 密钥）、**T22 真狗上的停止忽略率与急停滑行**（决定「中止」按钮的语义）、**T3 真狗 Insta360 的机头零点**。
- 路线图：`docs/ROADMAP.md`（与 `task.md §10` 同步）。

## 8. 仓库与推送状态

- 远端：`origin git@github.com:Nirvana051/patrol-scheduler.git`（用户选择**公开**）。**用 SSH**——
  `~/.ssh/id_ed25519` 已加到 GitHub，`ssh -T git@github.com` 通；HTTPS 那条没有凭据助手，别用。
- **`main` 已推送**（09-10）。之前的作者改写：全部提交的作者/提交者已改为
  `Zhongyuan Liu <zliu051@e.ntu.edu.sg>`（GitHub 按邮箱归属）；改写前的备份分支 `backup-stengg-email`，内容 diff 为空。
- 分支 `V1` 是当前工作分支。曾经有过一段跨平台重写（135 个提交，从未推送），**已确认放弃并删除**。
- 公开前已脱敏：另一台机器人的别名换成占位符；`ntu-dog-00001` / `R30_2026_001` / `cam-1c697ada870c` 保留（上游公开仓库文档里本来就有）。全历史扫过：无任何真实密钥，`config/.env` 与 `data/` 从未提交。
- 公开仓库还缺 LICENSE（选哪个是用户的决定）。

## 9. 文档索引

| 文档 | 什么时候看 |
|------|-----------|
| [USAGE.md](USAGE.md) | **日常操作入口**：装、启动、建任务航点、跑巡检、接 VLM/播报、维护、常见问题 |
| [task.md](task.md) | 总纲：宪法约束（C1–C16）、术语、架构、数据模型、业务流程、接口清单、进度、问题清单、规划 |
| [OPERATIONS.md](OPERATIONS.md) | 真机上线与安全细则、故障速查表 |
| [TEST_PLAN.md](TEST_PLAN.md) | 分步验收清单（含真机实测参考值与演练结果） |
| [TEST_REPORT.md](TEST_REPORT.md) | 最近一轮测试报告（含 `.venv` 隔离改造的前后对比）|
| [DEVLOG.md](DEVLOG.md) | 逐时开发日志（每个 bug 的现象→定位→修法都在这里） |
| [TODO.md](TODO.md) / [ROADMAP.md](ROADMAP.md) | 问题清单 / 未来规划（都由 task.md 同步生成） |
| [SCREENSHOTS.md](SCREENSHOTS.md) | 界面截图与真机全景样张 |
| `../CHANGELOG.md` | 版本变更 |
| `../deploy/README.md` | systemd、cron 备份、反向代理提醒 |

---
最后更新：2026-09-07 22:20（第三天：上游同步、通义千问接入、任务起始点、前置检查修复、使用说明与本文档刷新）。
