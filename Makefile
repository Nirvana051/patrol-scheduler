# 常用命令。需要先建好 .venv（见 README）。
PY := .venv/bin/python

.PHONY: start stop status run mock test lint shots seed smoke clean-media lock backup eval vlm audio-server

start:          ## 一键后台启动（按 config/.env；加 ARGS="--mock --audio" 可叠加）
	./start.sh $(ARGS)

stop:           ## 停掉一键启动的全部进程
	./start.sh --stop

status:         ## 运行状态 + 机器人前置检查
	./start.sh --status

run:            ## 前台启动调度系统（读 config/.env）
	$(PY) -m app.main

mock:           ## 同时起 mock 网关 + 调度系统
	./run.sh --mock

test:           ## 全量测试（约 2 分钟）
	$(PY) -m pytest

lint:           ## 静态检查
	$(PY) -m ruff check .

shots:          ## 无头 Chrome 截图每个视图（需调度系统在跑）
	scripts/ui_screenshots.sh

seed:           ## 灌演示数据并跑一遍（mock）
	$(PY) scripts/seed_demo.py --reset --init --run --mock-speed 6

smoke:          ## 真机只读冒烟
	$(PY) scripts/real_smoke.py

clean-media:    ## 清理 30 天前的执行媒体与事件（先 dry-run 看看）
	$(PY) scripts/cleanup_media.py --keep-days 30 --events --dry-run

backup:         ## 在线备份 SQLite 到 data/backups（保留 14 份）
	$(PY) scripts/backup_db.py

vlm:            ## VLM 连通性探针（接新服务商时先跑这个；可加 ARGS="--provider qwen --key sk-..."）
	$(PY) scripts/vlm_probe.py $(ARGS)

eval:           ## 用人工复核过的检查评测当前 VLM
	$(PY) scripts/eval_vlm.py

audio-server:   ## 本机起一个播报服务（扬声器端）用于联调：http://127.0.0.1:5566
	$(PY) -m audio_server --host 127.0.0.1 --port 5566

lock:           ## 重新生成 requirements.lock
	pip3 --python $(PY) freeze --local | grep -v -E '^(ruff|pip)=' > requirements.lock
