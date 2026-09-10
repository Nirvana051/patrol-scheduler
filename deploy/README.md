# 部署

## 0. 先准备 Python 环境

```bash
cd /home/leo/agent/scheduler        # 换成实际路径
./bootstrap.sh                      # 建隔离的 .venv + 按 requirements.lock 装精确版本 + 自检
```

自检里那句「每个包都来自 venv 内部」必须是 ✅。**不要用 `python3 -m venv --system-site-packages`** ——
机器人这类机器的系统 Python 往往就是 ROS / CUDA 环境，`source /opt/ros/*/setup.bash` 设置的
`PYTHONPATH` 还会排在 venv 的 `site-packages` 前面，把 venv 里装的包顶掉（详见 `docs/HANDOFF.md` §5.4 第 21 条）。

## 1. 常驻（systemd）

```bash
sudo cp deploy/patrol-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now patrol-scheduler
```

按实际路径/用户改 `WorkingDirectory` / `User`。单元里已经写了
`UnsetEnvironment=PYTHONPATH PYTHONHOME` 与 `PYTHONNOUSERSITE=1`（systemd 本来给的是干净环境，
这两行是防止有人改成 `--user` 单元或加 `EnvironmentFile` 时把 shell 环境引进来）。

`TimeoutStopSec=40`：停机时应用要先中止进行中的执行、给云端发 `DELETE /task` 停下机器狗，再退线程 —— 别把这个值改小。

- 日志：`journalctl -u patrol-scheduler -f` 与 `data/logs/app.log`

## 2. 定时任务（cron）

`crontab -e`，注意三件事：**cron 的工作目录是家目录**（所以必须 `cd`）、
**cron 的 PATH 很短**（所以用绝对路径），**cron 的环境是干净的**（所以不需要清 `PYTHONPATH`）：

```cron
# 清理过期媒体与事件（只删已结束且过期的执行目录；running/paused 的一律不碰）
30 3 * * * cd /home/leo/agent/scheduler && .venv/bin/python scripts/cleanup_media.py --keep-days 30 --events >> data/logs/cleanup.log 2>&1

# 数据库在线备份（sqlite backup API，WAL 下安全；默认保留 14 份到 data/backups/）
0 4 * * * cd /home/leo/agent/scheduler && .venv/bin/python scripts/backup_db.py >> data/logs/backup.log 2>&1
```

> 原来这两行没有 `cd`，照抄会因为找不到 `.venv/bin/python` 而静默失败（cron 不报错，只是没跑）。
> 加完先手工跑一遍确认，再等它自己触发。
>
> 这两个动作会和夜间巡检撞上（cron 在 03:30 清理、04:00 备份，而巡检可能正在跑）。
> 浸泡测试里有对应的两类场景（「巡检中在线备份」会校验备份库的 `integrity_check`、
> 「巡检中清理媒体」会确认**正在跑的那趟的留档目录不被删**），所以这个组合是验过的。

## 3. 局域网访问

不要直接把 `PS_HOST` 改成 `0.0.0.0` 暴露出去（**没有登录鉴权**）；
用 nginx / caddy 反向代理并加 Basic Auth / SSO。危险设置（如 `TTS_COMMAND` 可在设置页改成任意命令）
在暴露前应从网页上移走（T12）。

## 4. 播报服务（机器狗端）

`patrol-audio.service`：把 `audio_server/` 拷到有喇叭的机器上常驻。
只需 python3 + 一个能出声的播放器（ffplay / mpg123 / aplay 之一），**不需要联网、不需要装 TTS**——
调度系统这边合成好 mp3 直接推过去。`--token` 必须改掉（默认值是占位符）。
