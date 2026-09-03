# 变更记录

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
