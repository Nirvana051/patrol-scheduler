# 交接文档（HANDOFF）

> 给下一个接手的人（或下一段上下文里的我）：读完这一页就能继续干活。细节见文末索引。最后更新见文末。

## 1. 这是什么

certaintyX 机器狗云端 API（`https://certaintyx.sg:8443`，教程仓库 `../Sample_web_api`）之上的**本地网页巡检调度台**：
SQLite 管任务航点/任务/执行/事件；分段下发（机器狗不能在航点暂停）；到点抓全景 → 按角度裁切 → VLM 是/不是 → 答案模版 TTS 播报。
代码 `/home/leo/agent/scheduler/`（独立 git 仓库，tag 见 `git tag`），主管理文档 `../Sample_web_api/docs/task.md`（未被上游仓库跟踪，`docs/task.md` 是同步副本）。

## 2. 现状（一句话版）

- 真机密钥已到位，存 `config/.env`（gitignored；**别再 `cp config/env.example config/.env`，会覆盖成占位密钥 → 401**）。
- 别名 `ntu-dog-00001` 后面目前接的是机器人侧的 **Gazebo 仿真**（RTSP 画面是仿真场景）。它与真狗共用 roscore：**绝不要调 `device/start|stop`**（页面「② 启动设备」「停止设备」也别点），会把仿真速度指令接到真狗上 / 杀真机节点。仿真自带定位，`/position` 直接 200，跳过 ②③④ 直接执行即可。
- 真实地图：`map_20260818_132055`（87 航点）、`map_20260828_230337`。
- mock 上全流程 + 通宵 104 趟稳定。**真机（Gazebo）上 09-05 夜已跑通完整流程**：四点巡检 4 分钟完成，速度 ≈0.8 m/s，检查 3.6 s/点；演练暴露并修了三件事——T22 机器人间歇性不理会停止指令（现已核实 + 告警）、T23 mock→真机切换后事件撞号丢失（schema v6 按网关唯一）、T24 中途被替换的任务云端不发 `task_started`（改按 `total`/路径比对）。通宵每 10 分钟一趟：09-06 早 56 趟零失败（`docs/DEVLOG.md` 第二夜总结）。真狗上线前必测：停止指令忽略率、急停滑行距离、机头零点、速度。

## 3. 怎么跑

```bash
cd /home/leo/agent/scheduler
./start.sh            # 按 config/.env 启动（真机），后台，打开 http://127.0.0.1:8088
./start.sh --mock     # 仿真：本机 mock 网关 + 调度系统
./start.sh --audio    # 叠加：本机播报服务 127.0.0.1:5566
./start.sh --status | --stop | --restart
make test             # 97 用例，约 2.5 分钟；make lint
make smoke            # 真机只读冒烟（scripts/real_smoke.py）
```
日志 `data/logs/app.out`（stdout）与 `data/logs/app.log`（应用日志）；pid `data/run/`。

## 4. 代码地图（改哪儿）

| 想改 | 看 |
|------|----|
| 与云端的调用 / 限速 / 429 | `app/robot/client.py`（SDK 原样在 `app/vendor/certaintyx.py`，不改） |
| 状态灯、定位是否就绪 | `app/robot/status.py`（C9：看 `received_at`，不看 `Location`） |
| 云端事件监听、游标、落库 | `app/robot/events.py` |
| 分段执行状态机（下发/到达/失败/重试/暂停/丢定位/外部任务） | `app/executor/runner.py` |
| 抓图 → 裁切 → VLM → TTS | `app/executor/inspection.py`，`app/media/pano.py`，`app/media/snapshot.py` |
| VLM / TTS 适配器 | `app/vlm/*`，`app/tts/base.py`；扬声器端服务 `audio_server/` |
| REST / SSE | `app/api/*.py`，装配在 `app/context.py`，入口 `app/main.py` |
| 定时计划 | `app/scheduler.py` |
| 前端 | `web/js/views/*.js`（原生 JS，无构建；`?nosse=1` 静态模式供截图） |
| mock 云端网关 | `mock_gateway/`（按文档契约仿真 + 故障注入） |
| 数据库 | `app/schema.sql`（v5）+ `app/db.py` 增量迁移 |

## 5. 必须记住的坑（血泪）

1. 幂等键 10 分钟内全局唯一：换库/重装会撞键 → 云端只回放不执行（键含实例段）；**每次重新下发都要换新键**。
2. 订阅事件队列、取游标都要在下发**之前**；到达判定不能只靠事件，`GET /task` 对账兜底；要识别「没开始 / 停滞 / 掉线 / 被外部任务替换 / 丢定位」。
3. 网关重启后事件 seq 归零 → 游标要重置（已做）。
4. edge-tts 会卡住 → 合成必须带超时（已做）。
5. 进程重启后残留的「进行中」执行要标为中止（已做）；进程退出前先 DELETE /task（已做）。
6. `pkill -f` 会匹配到自己的命令行把 shell 杀掉 → 用 `start.sh` / pid 文件。
7. 测试用例共用一个 mock 机器人：残留执行线程会污染后面的用例（`RunManager.shutdown()`）。
8. 真实网关 idle 时 `/task` 不带 `progress`；mock 已对齐。其他 mock/真机差异随夜测回填到 `mock_gateway/`。

## 6. 未完成 / 待真机确认

见 `docs/TODO.md`（与 `task.md §9` 同步）。此刻最重要：T3 机头校准（要真狗在真实环境）、T6/T7/T8 的真实时序、T10 真 VLM 效果。

## 7. 文档索引

`task.md`（总纲）· `DEVLOG.md`（逐时日志）· `TODO.md` · `ROADMAP.md` · `OPERATIONS.md`（真机手册）· `TEST_PLAN.md`（分步测试）· `SCREENSHOTS.md` · `CHANGELOG.md` · `deploy/`（systemd）。

---
最后更新：2026-09-06 07:30（第二夜收尾）。
