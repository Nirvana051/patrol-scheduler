# 部署

- `patrol-scheduler.service`：systemd 单元（真机常驻）。`sudo cp deploy/patrol-scheduler.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now patrol-scheduler`
- 日志：`journalctl -u patrol-scheduler -f` 与 `data/logs/app.log`
- 清理：`crontab -e` 加一行 `30 3 * * * .venv/bin/python scripts/cleanup_media.py --keep-days 30 --events >> data/logs/cleanup.log 2>&1`
- 局域网访问：不要直接把 `PS_HOST` 改成 0.0.0.0 暴露出去（无鉴权）；用 nginx/caddy 反向代理并加 Basic Auth / SSO。
