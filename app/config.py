# -*- coding: utf-8 -*-
"""配置：config/.env（凭据等） + settings 表（网页可改的运行参数）。优先级：settings 表 > 环境变量 > 默认值。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / 'config' / '.env'

DEFAULTS: dict[str, str] = {
    # 云端连接（真机改这三项；密钥只在服务端）
    'CX_HOST': 'http://127.0.0.1:18443',
    'CX_ROBOT': 'ntu-dog-00001',
    'CX_KEY': 'cx_mock0001_' + '0' * 48,
    # 本系统
    'PS_HOST': '127.0.0.1',
    'PS_PORT': '8088',
    'PS_DATA_DIR': str(ROOT / 'data'),
    'PS_DB_PATH': '',
    'RATE_LIMIT_RPS': '4',            # 云端限流 5 rps，留 1 rps 余量
    'STATUS_POLL_ACTIVE': '2',        # 有页面/执行时的轮询间隔（秒），不小于 1
    'STATUS_POLL_IDLE': '8',
    # VLM
    'VLM_PROVIDER': 'mock',           # mock | qwen | openai_compat | anthropic
    'VLM_BASE_URL': 'http://127.0.0.1:11434/v1',   # qwen 留空即用 DashScope 兼容模式地址
    'VLM_MODEL': 'qwen2.5vl:7b',
    'VLM_EXTRA_BODY': '',             # JSON 对象，合并进请求体（服务商私有参数，如 {"vl_high_resolution_images": true}）
    'VLM_API_KEY': '',
    'VLM_TIMEOUT': '120',
    'VLM_SEND_FULL_PANO': '0',        # 1 = 裁切图之外再附整张全景
    'VLM_MOCK_ANSWER': 'alternate',   # yes | no | unknown | alternate | random
    'ANTHROPIC_API_KEY': '',
    'ANTHROPIC_MODEL': 'claude-opus-5',
    'VLM_ANTHROPIC_FALLBACKS': '1',
    # TTS
    'TTS_ENGINE': 'edge',             # edge | command | none
    'TTS_VOICE': 'zh-CN-XiaoxiaoNeural',
    'TTS_COMMAND': '',                # command 引擎：如 espeak-ng -v cmn -w {out} "{text}"
    'TTS_SINKS': 'browser',           # browser,local,http,webhook
    'TTS_AUDIO_SERVER_URL': '',       # 本项目 audio_server 的地址，如 http://192.168.0.122:5566
    'TTS_AUDIO_SERVER_TOKEN': '',
    'TTS_WEBHOOK_URL': '',
    'TTS_TIMEOUT': '20',              # 合成超时（秒）：外部服务卡住不能拖住执行
    # 抓图 / 全景
    'SNAPSHOT_SOURCE': 'synthetic',   # rtsp | hls | synthetic | file:<path> | lavfi:<filter> | http(s)://…m3u8
    'FORWARD_DEG': '180',
    'SETTLE_SECONDS': '2',
    'LEG_TIMEOUT': '600',
    'MAX_RETRIES': '1',
    'ESTOP_ON_EXCEPTION': '1',
    'SCHEDULE_TICK_SECONDS': '20',
    'NOTIFY_WEBHOOK_URL': '',          # 检查不通过 / 执行失败中止时 POST JSON
}

# 网页「设置」里可改、存进 settings 表的键（密钥也允许改，但读出来一律掩码）
RUNTIME_KEYS = [k for k in DEFAULTS if not k.startswith('PS_')]
SECRET_KEYS = {'CX_KEY', 'VLM_API_KEY', 'ANTHROPIC_API_KEY', 'TTS_AUDIO_SERVER_TOKEN'}


def load_env_file(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k, v = k.strip(), v.split(' #')[0].strip().strip('"').strip("'")
        if k.startswith('export '):
            k = k[7:].strip()
        os.environ.setdefault(k, v)


def mask(value: str) -> str:
    if not value:
        return ''
    if len(value) <= 12:
        return '*' * len(value)
    return value[:8] + '…' + value[-4:]


class Config:
    def __init__(self, db=None, env_file: Path = ENV_FILE) -> None:
        self.db = db
        load_env_file(env_file)

    def get(self, key: str, default: str | None = None) -> str:
        if self.db is not None and key in RUNTIME_KEYS:
            v = self.db.get_setting(key)
            if v is not None:
                return v
        v = os.environ.get(key)
        if v is not None and v != '':
            return v
        return DEFAULTS.get(key, default if default is not None else '')

    def get_int(self, key: str) -> int:
        try:
            return int(float(self.get(key)))
        except ValueError:
            return int(float(DEFAULTS[key]))

    def get_float(self, key: str) -> float:
        try:
            return float(self.get(key))
        except ValueError:
            return float(DEFAULTS[key])

    def get_bool(self, key: str) -> bool:
        return self.get(key).strip().lower() in ('1', 'true', 'yes', 'on')

    def set(self, key: str, value: str) -> None:
        if key not in RUNTIME_KEYS:
            raise KeyError(key)
        self.db.set_setting(key, value)

    def public(self) -> dict:
        out = {}
        for k in RUNTIME_KEYS:
            v = self.get(k)
            out[k] = mask(v) if k in SECRET_KEYS else v
        out['PS_DATA_DIR'] = str(self.data_dir)
        out['mode'] = self.mode
        return out

    @property
    def mode(self) -> str:
        host = self.get('CX_HOST')
        return 'mock' if ('127.0.0.1' in host or 'localhost' in host) else 'real'

    @property
    def data_dir(self) -> Path:
        return Path(self.get('PS_DATA_DIR'))

    @property
    def media_dir(self) -> Path:
        return self.data_dir / 'media'

    @property
    def db_path(self) -> Path:
        p = self.get('PS_DB_PATH')
        return Path(p) if p else self.data_dir / 'scheduler.db'
