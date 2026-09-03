# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import HTTPException, Request

from app.context import AppContext


def ctx_of(request: Request) -> AppContext:
    return request.app.state.ctx


def bad_request(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg)


def not_found(msg: str = '不存在') -> HTTPException:
    return HTTPException(status_code=404, detail=msg)
