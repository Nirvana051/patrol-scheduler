# -*- coding: utf-8 -*-
"""通宵浸泡测试：反复跑完整巡检，轮着注入各种故障，核对每种故障的**预期结局**，把每轮结果落成 JSONL。

    .venv/bin/python scripts/soak.py --until "2026-09-10T13:00" [--port 8098] [--dir /tmp/soak]
    .venv/bin/python scripts/soak.py --only 掉线 --rounds 4        # 只跑名字含「掉线」的场景

与单元/端到端测试的区别：那些是「一次跑通」，这里是「跑几百次不出意外」——
专门抓那些只在长时间运行后才露头的问题：句柄泄漏、内存增长、事件游标错位、
幂等键复用、状态机在某个时序下卡住。

只对本机 mock 跑：独立数据目录、独立端口、VLM 用 mock（**不会调用任何付费接口**），
绝不碰真机那套，也绝不碰 data/scheduler.db。

调度系统与 mock 网关都跑成**子进程**并走真实 HTTP（不是 in-process），
这样 uvicorn、线程、文件句柄这些都是真的，泄漏才看得出来。
资源指标从 /proc/<pid> 读：VmRSS、线程数、fd 数 —— **GC 之后仍单调上升的 RSS** 才是泄漏。
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_MAP = 'map_demo_20260903_220000'


def now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def http(method: str, url: str, body=None, timeout=30, headers=None):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def proc_stats(pid: int) -> dict:
    """从 /proc 读资源指标。Python 没有 V8 堆，RSS + 线程数 + fd 数才是有意义的三个。"""
    out = {}
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                out['rss_kb'] = int(line.split()[1])
            elif line.startswith('Threads:'):
                out['threads'] = int(line.split()[1])
    except Exception:
        pass
    try:
        out['fds'] = len(os.listdir(f'/proc/{pid}/fd'))
    except Exception:
        pass
    return out


class Soak:
    def __init__(self, args):
        self.dir = Path(args.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.port = args.port
        self.mock_port = args.mock_port
        self.base = f'http://127.0.0.1:{self.port}'
        self.mock = f'http://127.0.0.1:{self.mock_port}'
        self.log = self.dir / 'soak.jsonl'
        self.summary = self.dir / 'summary.txt'
        self.data = self.dir / 'data'
        self.db = self.data / 'soak.db'
        self.app = None
        self.gw = None
        self.restarts = 0

    # ── 环境 ─────────────────────────────────────────────────────────────
    def env(self) -> dict:
        e = dict(os.environ)
        # 关键：清掉 ROS 的 PYTHONPATH，否则 venv 里的包会被顶掉（见 bootstrap.sh 的说明）
        for k in ('PYTHONPATH', 'PYTHONHOME'):
            e.pop(k, None)
        e.update({
            'PYTHONNOUSERSITE': '1',
            'PS_DATA_DIR': str(self.data), 'PS_DB_PATH': str(self.db), 'PS_PORT': str(self.port),
            'CX_HOST': self.mock, 'CX_ROBOT': 'ntu-dog-00001', 'CX_KEY': 'cx_mock0001_' + '0' * 48,
            'SNAPSHOT_SOURCE': 'synthetic',
            'VLM_PROVIDER': 'mock', 'VLM_MOCK_ANSWER': 'alternate',   # ← 不调用 qwen，零 API 消耗
            'TTS_ENGINE': 'none', 'TTS_SINKS': 'browser',
            'RATE_LIMIT_RPS': '20', 'STATUS_POLL_ACTIVE': '1', 'STATUS_POLL_IDLE': '2',
            'PS_LOG_LEVEL': 'warning',
        })
        return e

    def check_ports_free(self):
        """端口上已经有人就直接退出。

        不检查的话：健康检查会打在**别人的实例**上并返回 200，浸泡看起来「起来了」，
        实际每一轮都在对陌生实例下发任务，而自己的 app 因为绑不上端口反复退出（码 3）。
        实测踩过一次 —— 上一次被 timeout 掐掉的残留子进程还占着端口。
        """
        for port, what in ((self.port, '调度系统'), (self.mock_port, 'mock 网关')):
            s, _ = http('GET', f'http://127.0.0.1:{port}/', timeout=2)
            if s != 0:
                raise SystemExit(f'端口 {port} 上已经有人在听（{what} 要用它）。'
                                 f'换个端口：--port / --mock-port，或先把那个进程停掉。')

    def start_all(self):
        self.check_ports_free()
        py = str(ROOT / '.venv/bin/python')
        (self.dir / 'logs').mkdir(exist_ok=True)
        self.gw = subprocess.Popen(
            [py, '-m', 'mock_gateway.server', '--port', str(self.mock_port), '--speed', '8',
             '--prestarted', '--prelocalized'],
            cwd=ROOT, env=self.env(), start_new_session=True,   # 自己的进程组，退出时整组杀
            stdout=open(self.dir / 'logs/gw.log', 'ab'), stderr=subprocess.STDOUT)
        self.wait(f'{self.mock}/healthz', 40, 'mock 网关')
        self.app = subprocess.Popen(
            [py, '-m', 'app.main'], cwd=ROOT, env=self.env(), start_new_session=True,
            stdout=open(self.dir / 'logs/app.log', 'ab'), stderr=subprocess.STDOUT)
        self.wait(f'{self.base}/api/health', 60, '调度系统')

    def wait(self, url: str, tries: int, what: str):
        for _ in range(tries):
            s, _b = http('GET', url, timeout=2)
            if s == 200:
                return
            time.sleep(0.5)
        raise SystemExit(f'{what} 没起来（{url}）')

    def stop_all(self):
        # 整个进程组一起停：子进程自己又起线程/子进程时，单杀 pid 会留下孤儿占着端口
        for p in (self.app, self.gw):
            if p and p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    p.send_signal(signal.SIGTERM)
        for p in (self.app, self.gw):
            if p:
                try:
                    p.wait(timeout=20)
                except Exception:
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except Exception:
                        p.kill()

    def ensure_alive(self) -> bool:
        """app 挂了就重启并记一次 —— 「进程自己死了」本身就是要抓的问题。"""
        if self.app and self.app.poll() is not None:
            self.restarts += 1
            print(f'  ⚠️  调度系统进程退出（码 {self.app.returncode}），重启并记一次异常')
            py = str(ROOT / '.venv/bin/python')
            self.app = subprocess.Popen([py, '-m', 'app.main'], cwd=ROOT, env=self.env(),
                                        start_new_session=True,
                                        stdout=open(self.dir / 'logs/app.log', 'ab'),
                                        stderr=subprocess.STDOUT)
            self.wait(f'{self.base}/api/health', 60, '调度系统（重启）')
            return False
        return True

    # ── 便捷调用 ─────────────────────────────────────────────────────────
    def api(self, method, path, body=None, timeout=30):
        return http(method, self.base + path, body, timeout)

    def inject(self, path, body=None):
        return http('POST', self.mock + path, body or {}, timeout=10)

    def reset_and_wait(self, timeout_s: float = 12.0):
        """复位机器人，并**等到调度系统看见云端任务已空闲**再继续。

        不等的话：前置检查（它会 refresh 一次真实状态）可能还看到上一轮的任务是
        NAVIGATING，于是这一轮直接 aborted —— 实测第 11 轮就是这么误报的。
        """
        st, _b = self.inject('/mock/reset', {'node_id': '1', 'map_name': DEMO_MAP,
                                             'prelocalized': True, 'prestarted': True})
        assert 200 <= st < 300, f'复位 mock 机器人失败 {st}'
        self.inject('/mock/speed', {'speed': 8})
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            s, d = self.api('POST', '/api/robot/status/refresh', {}, timeout=10)
            t = (d or {}).get('task') or {}
            if not t.get('active'):
                return
            time.sleep(0.4)
        raise AssertionError('复位后云端任务一直显示在跑，等超时了')

    def wait_dispatched(self, run_id: int, timeout_s: float = 60.0) -> bool:
        """等到第一段**真的下发出去**（或整趟已结束）。

        中途注入（掉线/502/外部任务）必须等到这一刻之后才有意义：固定 sleep 1 秒的话
        往往还落在前置检查里，把「机器人掉线」变成「前置检查不通过」——
        实测 3 个掉线类场景全是这么误报的。
        """
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            _s, d = self.api('GET', f'/api/runs/{run_id}')
            if isinstance(d, dict):
                if d.get('status') in ('completed', 'failed', 'aborted'):
                    return False
                legs = d.get('legs') or []
                if legs and legs[0].get('status') in ('dispatched', 'navigating'):
                    return True
            time.sleep(0.3)
        return False

    def wait_run(self, run_id: int, timeout_s: float) -> dict:
        t0 = time.time()
        last = {}
        while True:
            s, d = self.api('GET', f'/api/runs/{run_id}')
            if isinstance(d, dict):
                last = d
                if d.get('status') in ('completed', 'failed', 'aborted'):
                    return d
            if time.time() - t0 > timeout_s:
                last = dict(last or {})
                last['status'] = 'TIMEOUT'
                return last
            time.sleep(0.4)

    # ── 一次性准备：同步地图、建任务航点、建任务 ──────────────────────────
    def setup_fixtures(self):
        s, _ = self.api('POST', f'/api/maps/{DEMO_MAP}/sync', {}, timeout=60)
        assert 200 <= s < 300, f'同步地图失败 {s}'
        self.api('POST', '/api/robot/init/device-start', {})
        self.api('POST', '/api/robot/init/localize', {'map_name': DEMO_MAP, 'node_id': '1'})
        s, items = self.api('GET', f'/api/task-waypoints?map_name={DEMO_MAP}')
        have = {t['name']: t for t in (items or {}).get('items', [])}
        self.tw = {}
        for node in ('1', '3', '5', '12', '20', '25', '30', '35', '40', '43'):
            name = f'浸泡点{node}'
            if name in have:
                self.tw[node] = have[name]['id']
                continue
            s, d = self.api('POST', '/api/task-waypoints', {
                'name': name, 'map_name': DEMO_MAP, 'nav_node_id': node,
                'prompt': f'航点 {node} 的门是否关好？', 'angle_from': 150.0, 'angle_to': 210.0,
                'answer_template': {'expected': 'yes', 'on_pass': '{name} 正常',
                                    'on_fail': '{name} 异常', 'on_unknown': '{name} 无法判断'}})
            assert 200 <= s < 300, f'建任务航点 {node} 失败 {s} {d}'   # FastAPI 建资源回 201
            self.tw[node] = d['id']

    def make_task(self, nodes, opts) -> int:
        base = {'settle_seconds': 0.2, 'leg_timeout': 120, 'max_retries': 0,
                'not_started_timeout': 25, 'offline_timeout': 90, 'stall_timeout': 180,
                'start_node': '1', 'require_localization': False}
        base.update(opts or {})
        s, d = self.api('POST', '/api/tasks', {
            'name': f'浸泡任务-{"-".join(nodes)}-{int(time.time() * 1000) % 100000}',
            'map_name': DEMO_MAP, 'waypoint_ids': [self.tw[n] for n in nodes], 'options': base})
        assert 200 <= s < 300, f'建任务失败 {s} {d}'
        return d['id']


# ── 场景表：注入什么、预期结局是什么 ─────────────────────────────────────
def scenarios(k: 'Soak'):
    def fault(kind):
        return lambda: k.inject('/mock/fault', {'kind': kind})

    def offline(on):
        return k.inject('/mock/offline', {'online': on})

    def mid_flap(times=4):
        def f(_):
            for _ in range(times):
                offline(False); time.sleep(1.2); offline(True); time.sleep(1.2)
        return f

    def mid_offline(secs):
        def f(_):
            offline(False); time.sleep(secs); offline(True)
        return f

    def mid_backup(_):
        sys.path.insert(0, str(ROOT))
        from scripts.backup_db import backup
        import sqlite3
        dst = backup(k.db, k.dir / 'backups', 3)
        con = sqlite3.connect(f'file:{dst}?mode=ro', uri=True)
        try:
            got = con.execute('PRAGMA integrity_check').fetchone()[0]
            if got != 'ok':
                raise AssertionError(f'边跑边备份出来的库坏了：{got}')
            if not con.execute('SELECT COUNT(*) FROM runs').fetchone()[0]:
                raise AssertionError('备份里没有任何执行记录')
        finally:
            con.close()

    def mid_settings(_):
        s, _d = k.api('PUT', '/api/settings', {'SETTLE_SECONDS': '2.5', 'STATUS_POLL_ACTIVE': '1'})
        if not 200 <= s < 300:
            raise AssertionError(f'巡检中改设置失败 {s}')
        k.api('PUT', '/api/settings', {'SETTLE_SECONDS': '0.2'})

    def mid_import(_):
        s, ex = k.api('GET', '/api/export', timeout=60)
        if not 200 <= s < 300 or not isinstance(ex, dict) or 'task_waypoints' not in ex:
            raise AssertionError(f'导出失败 {s}')
        s2, imp = k.api('POST', '/api/import', {'data': ex, 'overwrite': False}, timeout=60)
        if not 200 <= s2 < 300:
            raise AssertionError(f'导入失败 {s2} {imp}')
        if imp.get('task_waypoints') != 0:
            raise AssertionError(f'同名航点应当被跳过，实际新建了 {imp.get("task_waypoints")} 个')

    def mid_resync(_):
        s, _d = k.api('POST', f'/api/maps/{DEMO_MAP}/sync', {}, timeout=60)
        if not 200 <= s < 300:
            raise AssertionError(f'巡检中重新同步地图失败 {s}')

    def mid_external(_):
        # 现场有人下发了别的任务 —— 我们必须中止自己那趟，且不去停对方的。
        # 云端接口要鉴权：不带 X-API-Key 的话这一注入直接 401，场景会静默变成「正常完成」
        # 网关有 5 rps 的令牌桶（和真实云端一样），而这一刻执行器正在轮询 —— 撞 429 很正常，退避重试
        st = b = None
        for _try in range(8):
            st, b = http('POST', f'{k.mock}/v1/robots/ntu-dog-00001/task',
                         {'map_name': DEMO_MAP, 'path': ['3', '4', '5']}, timeout=10,
                         headers={'X-API-Key': 'cx_mock0001_' + '0' * 48,
                                  'Idempotency-Key': f'soak-external-{int(time.time() * 1000)}'})
            if st != 429:
                break
            time.sleep(0.6)
        if not 200 <= (st or 0) < 300:
            raise AssertionError(f'注入外部任务失败 {st} {b}')

    return [
        dict(name='正常三段', nodes=['5', '20', '43'], expect=['completed']),
        dict(name='正常单段', nodes=['3'], expect=['completed']),
        dict(name='返回起点', nodes=['5'], opts={'return_to_start': True}, expect=['completed']),
        dict(name='避障失败', nodes=['5', '20'], expect=['failed'], setup=fault('obstacle')),
        dict(name='规划失败后重试', nodes=['5'], opts={'max_retries': 1}, expect=['completed'],
             setup=fault('planning')),
        # 掉线分两种：短暂抖动**应当被容忍**（真机 4G 常见），超过 offline_timeout 才判失败。
        # 掉线判定挂在 5 秒一次的对账上，任一次对账成功就清零，所以注入窗口必须 > 2 个对账周期
        # 才是确定性的 —— 否则本场景会稳定误报。
        dict(name='短暂掉线可容忍', nodes=['20'], opts={'offline_timeout': 8}, expect=['completed'],
             mid=mid_offline(1.5)),
        # 机器人必须走得够久，否则 speed=8 时它先到点、掉线还没判就完成了（实测如此）
        dict(name='长时间掉线判失败', nodes=['20'], opts={'offline_timeout': 3, 'leg_timeout': 90},
             expect=['failed'], setup=lambda: k.inject('/mock/speed', {'speed': 0.6}),
             mid=mid_offline(14)),
        dict(name='持续避障停滞', nodes=['20'], opts={'stall_timeout': 4, 'leg_timeout': 90},
             expect=['failed'], mid=lambda _: k.inject('/mock/obstacle', {'seconds': 30})),
        dict(name='丢定位（continue）', nodes=['20'], opts={'lost_localization_action': 'continue'},
             expect=['completed'], mid=lambda _: k.inject('/mock/lose-localization', {'seconds': 1.0})),
        dict(name='被外部任务替换', nodes=['20'], expect=['aborted'], mid=mid_external),
        dict(name='网关 HTML 502', nodes=['20'], expect=['completed'],
             mid=lambda _: k.inject('/mock/fault', {'kind': 'html502', 'count': 20, 'only_task': True})),
        dict(name='人工中止', nodes=['20'], expect=['aborted'], abort_after=1.2),
        dict(name='机器人忽略停止', nodes=['20'], opts={'stop_wait_seconds': 3}, expect=['aborted'],
             setup=lambda: k.inject('/mock/fault', {'kind': 'ignore_stop', 'on': True}),
             teardown=lambda: k.inject('/mock/fault', {'kind': 'ignore_stop', 'on': False}),
             abort_after=1.2),
        dict(name='暂停后继续', nodes=['3', '5'], expect=['completed'], pause_resume=True),
        dict(name='现场抢控制权', nodes=['5'], expect=['aborted', 'failed'],
             setup=lambda: k.inject('/mock/preempt', {'owner': 'admin', 'seconds': 8})),
        dict(name='事件全丢（靠对账兜住）', nodes=['5'], opts={'leg_timeout': 90}, expect=['completed'],
             setup=lambda: k.inject('/mock/drop-events', {'drop': True}),
             teardown=lambda: k.inject('/mock/drop-events', {'drop': False})),
        dict(name='巡检中在线备份', nodes=['20'], expect=['completed'], mid=mid_backup),
        dict(name='巡检中改设置', nodes=['5', '20'], expect=['completed'], mid=mid_settings),
        dict(name='巡检中导入备份', nodes=['5', '20'], expect=['completed'], mid=mid_import),
        dict(name='巡检中重新同步地图', nodes=['5', '20'], expect=['completed'], mid=mid_resync),
        dict(name='网络反复抖动', nodes=['20'], opts={'offline_timeout': 90}, expect=['completed'],
             mid=mid_flap(4)),
        dict(name='十个航点的长任务', nodes=['3', '5', '12', '20', '25', '30', '35', '40', '43', '1'],
             opts={'leg_timeout': 180}, expect=['completed']),
    ]


def run_round(k: 'Soak', sc: dict, idx: int) -> dict:
    """跑一轮：注入 → 下发 → 中途动作 → 等结局 → 核对预期。"""
    t0 = time.time()
    rec = {'i': idx, 'ts': now(), 'scenario': sc['name'], 'expect': sc['expect']}
    try:
        # 每轮先把机器人复位到航点 1：位置、在线、ROS、租约、以及**所有注入的故障**一起清零。
        # prelocalized/prestarted 必须显式传 —— 不传的话复位会把「已启动设备 + 已定位」也清掉，
        # 于是每一轮都卡在前置检查「定位未就绪」上直接 aborted（实测踩过）。
        k.reset_and_wait()
        if sc.get('setup'):
            sc['setup']()
        task_id = k.make_task(sc['nodes'], sc.get('opts'))

        if sc.get('pause_resume'):
            s, d = k.api('POST', f'/api/tasks/{task_id}/run', {})
            assert 200 <= s < 300, f'下发失败 {s} {d}'
            run_id = d['id']
            time.sleep(1.5)
            k.api('POST', f'/api/runs/{run_id}/pause', {})
            time.sleep(1.5)
            k.api('POST', f'/api/runs/{run_id}/resume', {})
        elif sc.get('via_schedule'):
            s, d = k.api('POST', '/api/schedules', {'task_id': task_id, 'kind': 'interval',
                                                    'every_minutes': 1, 'enabled': True})
            assert 200 <= s < 300, f'建定时失败 {s} {d}'
            sid = d['id']
            s, d = k.api('POST', f'/api/schedules/{sid}/fire', {})
            assert 200 <= s < 300, f'立即执行失败 {s} {d}'
            run_id = (d or {}).get('run_id') or (d or {}).get('id')
            k.api('DELETE', f'/api/schedules/{sid}')
        else:
            s, d = k.api('POST', f'/api/tasks/{task_id}/run', {})
            assert 200 <= s < 300, f'下发失败 {s} {d}'
            run_id = d['id']
        rec['run_id'] = run_id

        if sc.get('mid'):
            # 等段真的下发出去再注入，别落在前置检查里（见 wait_dispatched 的说明）
            k.wait_dispatched(run_id)
            sc['mid']({'run_id': run_id})
        if sc.get('abort_after'):
            k.wait_dispatched(run_id)
            time.sleep(sc['abort_after'])
            k.api('POST', f'/api/runs/{run_id}/abort', {})

        d = k.wait_run(run_id, sc.get('timeout', 240))
        rec['status'] = d.get('status')
        rec['legs'] = len(d.get('legs') or [])
        rec['inspections'] = len(d.get('inspections') or []) if isinstance(d.get('inspections'), list) else None
        rec['ok'] = rec['status'] in sc['expect']
        if not rec['ok']:
            rec['why'] = f'结局 {rec["status"]}，预期 {"/".join(sc["expect"])}'
            rec['error'] = d.get('error')

        # 幂等键唯一性：同一趟里绝不能重复（重复意味着云端会去重、第二段不会真的下发）
        s, ev = k.api('GET', f'/api/events?run_id={run_id}&limit=500')
        keys = []
        for e in ((ev or {}).get('items') or []):
            if e.get('type') == 'leg_dispatched':
                try:
                    keys.append(json.loads(e.get('data') or '{}').get('idempotency_key'))
                except Exception:
                    pass
        keys = [x for x in keys if x]
        if len(keys) != len(set(keys)):
            rec['ok'] = False
            rec['why'] = f'幂等键重复：{keys}'
        rec['keys'] = len(keys)
    except Exception as e:
        rec['ok'] = False
        rec['status'] = 'EXC'
        rec['why'] = f'{type(e).__name__}: {e}'
    finally:
        # 关键：mid/断言抛异常时，那趟执行会被**留在原地继续跑**，
        # 于后面每一轮都撞 409「已有执行在进行」，一次异常毁掉整晚 —— 实测第 10 轮
        # 抛异常后 11~22 轮全废。所以无论怎么退出，都要把在跑的那趟收掉。
        rid = rec.get('run_id')
        if rid:
            try:
                _s, d = k.api('GET', f'/api/runs/{rid}')
                if isinstance(d, dict) and d.get('status') not in ('completed', 'failed', 'aborted'):
                    k.api('POST', f'/api/runs/{rid}/abort', {})
                    end = k.wait_run(rid, 40)
                    rec.setdefault('cleaned', end.get('status'))
            except Exception:
                pass
        if sc.get('teardown'):
            try:
                sc['teardown']()
            except Exception:
                pass
        # 复位所有注入状态，免得污染下一轮
        k.inject('/mock/offline', {'online': True})
        k.inject('/mock/ros', {'available': True})
        k.inject('/mock/drop-events', {'drop': False})
        k.inject('/mock/fault', {'kind': None})
    rec['secs'] = round(time.time() - t0, 1)
    if k.app:
        rec.update(proc_stats(k.app.pid))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description='浸泡测试（只对本机 mock，VLM 用 mock，不消耗任何付费接口）')
    ap.add_argument('--until', default=None, help='跑到这个时刻为止，如 2026-09-10T13:00')
    ap.add_argument('--rounds', type=int, default=0, help='跑够多少轮就停（0 = 不限）')
    ap.add_argument('--only', default=None, help='只跑名字含该子串的场景')
    ap.add_argument('--port', type=int, default=8098)
    ap.add_argument('--mock-port', type=int, default=18488)
    ap.add_argument('--dir', default='/tmp/ps-soak-py')
    a = ap.parse_args()

    until = datetime.fromisoformat(a.until).timestamp() if a.until else time.time() + 3600
    k = Soak(a)
    # timeout(1) / kill 发来的 SIGTERM 也要走收尾，否则子进程会变孤儿继续占着端口
    def _bye(signum, _frame):
        print(f'\n收到信号 {signum}，收尾…')
        k.stop_all()
        raise SystemExit(130)
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGHUP, _bye)
    atexit.register(k.stop_all)
    print(f'浸泡测试：{k.dir}  后端 {k.port}  网关 {k.mock_port}  跑到 {a.until or "一小时后"}')
    print('VLM=mock、TTS=none —— 不会调用 qwen 或任何付费接口')
    k.start_all()
    k.setup_fixtures()
    scs = scenarios(k)
    if a.only:
        scs = [s for s in scs if a.only in s['name']]
        if not scs:
            raise SystemExit(f'没有名字含「{a.only}」的场景')
    print(f'{len(scs)} 类场景轮换\n')

    i = 0
    fails = 0
    first = None
    try:
        while time.time() < until and (not a.rounds or i < a.rounds):
            k.ensure_alive()
            sc = scs[i % len(scs)]
            i += 1
            rec = run_round(k, sc, i)
            if first is None:
                first = rec
            if not rec.get('ok'):
                fails += 1
            with open(k.log, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            mark = '✅' if rec.get('ok') else '❌'
            extra = '' if rec.get('ok') else f'  ← {rec.get("why")}'
            print(f'[{rec["ts"]}] {mark} 第 {i} 轮 {sc["name"]:16} {rec.get("status"):10} '
                  f'{rec["secs"]:6.1f}s  RSS {rec.get("rss_kb", 0) // 1024}M '
                  f'线程 {rec.get("threads")} fd {rec.get("fds")}{extra}', flush=True)
    except KeyboardInterrupt:
        print('\n收到中断，收尾…')
    finally:
        k.stop_all()

    lines = [f'浸泡测试小结（{now()}）',
             f'  轮次 {i}   失败 {fails}   进程异常退出 {k.restarts} 次',
             f'  日志 {k.log}']
    if first and 'rss_kb' in first:
        last = rec if 'rec' in dir() else first
        d_rss = (last.get('rss_kb', 0) - first.get('rss_kb', 0)) / max(1, i)
        lines.append(f'  RSS {first.get("rss_kb", 0) // 1024}M → {last.get("rss_kb", 0) // 1024}M'
                     f'（每轮 {d_rss:+.1f} KB）  线程 {first.get("threads")} → {last.get("threads")}'
                     f'  fd {first.get("fds")} → {last.get("fds")}')
    text = '\n'.join(lines)
    Path(k.summary).write_text(text + '\n', encoding='utf-8')
    print('\n' + text)
    return 1 if fails or k.restarts else 0


if __name__ == '__main__':
    sys.exit(main())
