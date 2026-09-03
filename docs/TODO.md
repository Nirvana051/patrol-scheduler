# 问题与 TODO

| # | 问题 | 影响 | 状态 / 处置 |
|---|------|------|-----------|
| T1 | 本机没有真机 API 密钥，所有验证在 mock 上完成 | 真机联调前需要一次对照实测 | 待用户提供 `CX_KEY`；先跑 `Sample_web_api/examples/python/04_verify_flow.py` 与本系统「总览 → 刷新状态（前置检查）」 |
| T2 | 云端 API 没有「扬声器」端点 | TTS 无法直接送到机器狗 | 汇出插件化：browser / local / zmq（对接用户 robot-audio 服务，需机器人端加 `play_tts` 动作）/ webhook |
| T3 | 全景图中机头正前方对应的列未知 | 角度范围 ↔ 实际方位需校准 | 设置项 `FORWARD_DEG`（默认 180）；真机首帧对照现场校准 |
| T4 | 云端不暴露地图点云下载 | 地图页点云背景要手工上传 | 保留 `POST /api/maps/{name}/pointcloud` 上传 + 体素下采样接口；`PointCloudProvider.fetch_from_robot` 留桩 |
| T5 | 机器人不能在航点暂停 | 只能分段下发，每段云端任务进入 terminal 后才能检查 | 已按分段设计；段间 settle 时间可配 |
