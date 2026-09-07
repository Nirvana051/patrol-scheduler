# 文档索引

九篇文档，按「你现在要干什么」挑一篇读，别从头读到尾。

| 你要做的事 | 读这篇 |
|-----------|--------|
| **第一次接触，想跑起来看看** | [USAGE.md](USAGE.md) §1–§2（装 + 仿真里五分钟跑一遍，不需要任何凭据） |
| **日常使用**：建任务航点、圈角度范围、写 prompt、跑巡检、看结果 | [USAGE.md](USAGE.md)（使用说明，日常操作入口） |
| **接真机上线**：凭据、初始化 ②③④、安全红线、故障速查 | [OPERATIONS.md](OPERATIONS.md) |
| **接 VLM**（通义千问 / Ollama / OpenAI / Anthropic）或**接播报** | [USAGE.md §5](USAGE.md#5-接-vlm-与播报) |
| **验收 / 逐步测一遍**（含真机实测参考值与故障演练结果） | [TEST_PLAN.md](TEST_PLAN.md) |
| **接手这套代码**：现状、改哪儿、有哪些坑不能再踩 | [HANDOFF.md](HANDOFF.md) |
| **搞清设计**：宪法约束、术语、架构、数据模型、业务流程、接口清单 | [task.md](task.md)（总纲） |
| **还剩什么没做 / 未来做什么** | [TODO.md](TODO.md) / [ROADMAP.md](ROADMAP.md)（都由 task.md §9/§10 同步生成） |
| **某个 bug 当时是怎么查出来的** | [DEVLOG.md](DEVLOG.md)（逐时日志，现象 → 定位 → 修法） |
| **界面长什么样** | [SCREENSHOTS.md](SCREENSHOTS.md) |
| **版本里改了什么** | [../CHANGELOG.md](../CHANGELOG.md) |
| **常驻部署**（systemd、cron 备份、反向代理） | [../deploy/README.md](../deploy/README.md) |

## 几条贯穿全部文档的硬约束

写代码或上真机之前至少扫一眼这几条（完整 16 条在 [task.md §1](task.md)）：

1. **机器狗不能在航点暂停** → 每个巡检点单独下发一段。
2. **只下发任务机器人不会动**：真机要先启动设备 + 定位（但共用 roscore 的仿真环境里**不要**碰启动/停止设备）。
3. **判断定位就绪只认 `telemetry.global_localization.received_at` 的新鲜度**：`/perception.Location` 不可信，`/position` 200 也不代表新鲜（会回放缓存值）。
4. **状态词写读不对称**：写 `running` 读回 `navigating`，失败读回 `paused` → 只用 `active`/`terminal`/`status_code`。
5. **幂等键 10 分钟内全局唯一**，重试复用、重新下发换新。
6. **急停不是硬件急停**：有 5–7 秒滑行、不取消任务、现场遥控会与之竞争。
7. **停任务可能不生效**（实测约 1/3）：系统会核实并告警。
8. **密钥只放 `config/.env`**，不进仓库、不进前端。

## 文档之间的关系

```
docs/README.md ← 你在这里
  ├─ USAGE.md        日常操作（页面怎么用、一次巡检怎么做完）
  ├─ OPERATIONS.md   真机上线与安全、故障速查表
  ├─ TEST_PLAN.md    分步验收 + 真机实测数字
  ├─ HANDOFF.md      交接：现状 / 代码地图 / 坑清单 / 推送状态
  ├─ task.md         总纲（宪法、架构、数据模型、接口、进度、问题、规划）
  │    ├─ TODO.md      ← 由 task.md §9 生成
  │    └─ ROADMAP.md   ← 由 task.md §10 生成
  ├─ DEVLOG.md       逐时开发日志
  └─ SCREENSHOTS.md  界面截图与真机全景样张
```

`task.md` 的**源文件**在 `../../Sample_web_api/docs/task.md`（用户指定的位置，那边未被上游仓库跟踪），本目录里的是同步副本：改完源文件跑 `scripts/sync_task_doc.sh`，`TODO.md` / `ROADMAP.md` 也按 §9/§10 重新生成。
