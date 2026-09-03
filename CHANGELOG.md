# 变更记录

## [Unreleased] → 0.2.0
- 幂等键加入按 DB 生成的实例段 `ps-{instance}-r{run}-l{leg}-a{attempt}`（mock 测试抓到的跨库撞键 bug）
- 执行器：先订阅再取游标再下发；掉线超时判段失败；中途到达写入段进度；进程停止时中止执行并停机器人任务
- 失败/中止后可从剩余航点重跑（`from_seq`）；`run_legs.item_seq` 列，schema v2 增量迁移
- 设置页「机头校准」；任务编辑器内直接新建任务航点；地图页自动加载点云背景
- 真机准备：`scripts/real_smoke.py` 只读冒烟、`docs/OPERATIONS.md` 上线手册
- 文件日志 `data/logs/app.log`
- 测试：故障场景（掉线/控制权/暂停继续跳过/丢定位）、VLM 适配器（假客户端/假服务）、抓帧管道、监听回退、迁移

## [0.1.0] - 2026-09-04
首个可跑通的版本（全部对着 mock 网关验证）。
- mock 云端网关：复刻文档契约与已知坑（状态词不对称、Location 恒 1、透传只认 ID、幂等重放、限流、急停空体、事件流）
- 后端：SQLite 数据模型；SDK 限速包装；SSE 事件监听落库；状态轮询；导航图最短路；分段执行器；抓图→裁切→VLM→TTS 流水线；VLM（mock/OpenAI 兼容/Anthropic）与 TTS（edge/command/none；browser/local/zmq/webhook）适配器；REST + SSE
- 前端：总览、地图与导航航点、任务航点（全景角度编辑器）、任务规划、执行监控、任务事件、设置
- 测试：单测 + mock 契约 + API + 端到端执行
