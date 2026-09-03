# -*- coding: utf-8 -*-
"""巡检调度系统入口。  python -m app.main  或  ./run.sh"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import events, maps, robot, runs, settings, stream, task_waypoints, tasks
from app.config import Config
from app.context import AppContext
from app.db import Database
from app.media.snapshot import SnapshotError
from app.robot.client import RobotError
from app.robot.ops import OpsError

WEB_DIR = Path(__file__).resolve().parent.parent / 'web'


def create_app(cfg: Config | None = None, db: Database | None = None) -> FastAPI:
    cfg = cfg or Config()
    db = db or Database(cfg.db_path)
    ctx = AppContext(cfg, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.start()
        ctx.log_event('app_started', f'调度系统启动（{ctx.gateway.mode} 模式，{ctx.gateway.host}，机器人 {ctx.gateway.robot}）')
        try:
            yield
        finally:
            ctx.stop()

    app = FastAPI(title='巡检调度系统', version='0.1.0', lifespan=lifespan, docs_url='/api/docs', openapi_url='/api/openapi.json')
    app.state.ctx = ctx

    @app.exception_handler(RobotError)
    async def _robot_error(request: Request, exc: RobotError):
        status = exc.status if 400 <= (exc.status or 0) < 600 else 502
        return JSONResponse({'detail': str(exc), 'cloud_status': exc.status,
                             'cloud_body': exc.body if isinstance(exc.body, (dict, list)) else None}, status_code=status)

    @app.exception_handler(OpsError)
    async def _ops_error(request: Request, exc: OpsError):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(SnapshotError)
    async def _snap_error(request: Request, exc: SnapshotError):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    for r in (robot.router, maps.router, task_waypoints.router, tasks.router, runs.router, events.router,
              stream.router, settings.router):
        app.include_router(r)

    @app.get('/api/health')
    def health():
        return {'ok': True, 'mode': ctx.gateway.mode, 'robot': ctx.gateway.robot, 'host': ctx.gateway.host}

    app.mount('/media', StaticFiles(directory=str(ctx.media_dir)), name='media')
    if WEB_DIR.exists():
        app.mount('/', StaticFiles(directory=str(WEB_DIR), html=True), name='web')
    return app


def main() -> None:
    import uvicorn
    cfg = Config()
    app = create_app(cfg)
    host, port = cfg.get('PS_HOST'), cfg.get_int('PS_PORT')
    print(f'巡检调度系统: http://{host}:{port}   模式={cfg.mode}  云端={cfg.get("CX_HOST")}  机器人={cfg.get("CX_ROBOT")}')
    uvicorn.run(app, host=host, port=port, log_level=os.environ.get('PS_LOG_LEVEL', 'info'))


if __name__ == '__main__':
    main()
