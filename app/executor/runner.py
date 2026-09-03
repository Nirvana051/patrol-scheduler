# -*- coding: utf-8 -*-
"""执行器：把一个任务拆成「当前航点 → 下一个任务航点」的多段云端任务，逐段下发（机器狗不能在航点暂停）。

每段：取游标 → POST /task → 用事件流等到达（同时每 5s 用 GET /task 对账）→ 稳定 → 检查 → 下一段。
状态判断只用云端附带的 active/terminal/status_code（C5/C6），幂等键 ps-{instance}-r{run}-l{leg}-a{attempt}（C3）。
"""
from __future__ import annotations

import queue
import threading
import time
import traceback

from app.db import dumps, loads, now_iso
from app.executor.inspection import run_inspection
from app.robot.client import RobotError

DEFAULT_OPTIONS = {'settle_seconds': 2.0, 'leg_timeout': 600.0, 'not_started_timeout': 25.0, 'offline_timeout': 90.0, 'stall_timeout': 180.0, 'lease_retry_seconds': 30.0, 'lease_retries': 1, 'max_retries': 1,
                   'return_to_start': False, 'require_localized': True, 'speed': None, 'gait': None,
                   'obs_mode': None, 'nav_mode': None, 'manner': None, 'stop_on_lost_localization': False,
                   'lost_localization_action': 'pause'}   # continue | pause | fail


class RunAbort(Exception):
    """人工中止 / 外部停止 / 前置检查不通过。"""


class LegFailed(Exception):
    pass


class MissionRunner(threading.Thread):
    def __init__(self, ctx, run_id: int, task: dict, items: list[dict], options: dict) -> None:
        super().__init__(name=f'run-{run_id}', daemon=True)
        self.ctx = ctx
        self.run_id = run_id
        self.task = task
        self.items = items                     # [{seq, tw: {...}}]
        self.opt = {**DEFAULT_OPTIONS, **(options or {})}
        self.pause_req = threading.Event()
        self.skip_req = threading.Event()
        self.abort_req = threading.Event()
        self.cur_node: str | None = None
        self.graph = None
        self.status = 'pending'
        self.legs: list[dict] = []
        self.current_leg_id: int | None = None

    # ── 对外控制 ─────────────────────────────────────────────────────────────
    def pause(self) -> None:
        self.pause_req.set()
        self._log('run_pause_requested', '已请求暂停（当前段完成后停下）', level='warn')

    def resume(self) -> None:
        self.pause_req.clear()
        self._log('run_resumed', '继续执行')

    def skip(self) -> None:
        self.skip_req.set()
        self._log('run_skip_requested', '已请求跳过当前航点', level='warn')

    def abort(self) -> None:
        self.abort_req.set()
        self.pause_req.clear()
        self._log('run_abort_requested', '已请求中止', level='warn')

    # ── 辅助 ────────────────────────────────────────────────────────────────
    def _log(self, etype: str, message: str, *, level: str = 'info', leg_id: int | None = None, data=None) -> None:
        self.ctx.log_event(etype, message, level=level, run_id=self.run_id, leg_id=leg_id, data=data)

    def _set_run(self, **fields) -> None:
        sets = ', '.join(f'{k}=?' for k in fields)
        self.ctx.db.execute(f'UPDATE runs SET {sets} WHERE id=?', [*fields.values(), self.run_id])
        if 'status' in fields:
            self.status = fields['status']
        self.ctx.bus.publish('run', self.ctx.runs.get_run(self.run_id))

    def _set_leg(self, leg: dict, **fields) -> None:
        leg.update(fields)
        sets = ', '.join(f'{k}=?' for k in fields)
        self.ctx.db.execute(f'UPDATE run_legs SET {sets} WHERE id=?', [*fields.values(), leg['id']])
        self.ctx.bus.publish('leg', {k: v for k, v in leg.items() if k != 'tw'})

    def _wait_if_paused(self) -> None:
        if self.pause_req.is_set() and not self.abort_req.is_set():
            self._set_run(status='paused')
            self._log('run_paused', '已暂停，等待继续', level='warn')
            while self.pause_req.is_set() and not self.abort_req.is_set():
                time.sleep(0.3)
            if not self.abort_req.is_set():
                self._set_run(status='running')

    def _control_options(self) -> dict:
        out = {}
        for k in ('speed', 'gait', 'obs_mode', 'nav_mode', 'manner'):
            v = self.opt.get(k)
            if v not in (None, ''):
                out[k] = int(v)
        return out

    # ── 主流程 ───────────────────────────────────────────────────────────────
    def run(self) -> None:
        self.ctx.status.hold_active()
        try:
            self._set_run(status='preflight', started_at=now_iso())
            self._preflight()
            self._plan()
            self._set_run(status='running', total_legs=len(self.legs))
            for leg in self.legs:
                if self.abort_req.is_set():
                    raise RunAbort('人工中止')
                self._wait_if_paused()
                if self.abort_req.is_set():
                    raise RunAbort('人工中止')
                self._set_run(current_leg=leg['seq'])
                self.current_leg_id = leg['id']
                self._execute_leg(leg)
            self._finish('completed', None)
        except RunAbort as e:
            if '外部替换' not in str(e):
                self._safe_stop_task()
            self._finish('aborted', str(e))
        except LegFailed as e:
            self._safe_stop_task()
            self._finish('failed', str(e))
        except Exception as e:      # noqa: BLE001 —— 未预期异常：先急停再收尾（C15）
            tb = traceback.format_exc(limit=5)
            self._log('run_exception', f'执行器异常：{e}', level='error', data={'traceback': tb})
            if self.ctx.cfg.get_bool('ESTOP_ON_EXCEPTION'):
                try:
                    self.ctx.gateway.estop()
                    self._log('estop', '因执行器异常已下发急停（需人工在页面上取消）', level='error')
                except RobotError as ee:
                    self._log('estop_failed', f'急停失败：{ee}', level='error')
            self._safe_stop_task()
            self._finish('failed', f'执行器异常：{e}')
        finally:
            self.ctx.status.release_active()
            self.ctx.runs.on_finished(self.run_id)

    def _finish(self, status: str, error: str | None) -> None:
        for leg in self.legs:
            if leg['status'] in ('pending', 'planning', 'dispatched', 'navigating', 'arrived', 'inspecting'):
                self._set_leg(leg, status='aborted' if status != 'completed' else leg['status'], ended_at=now_iso())
        insp = self.ctx.db.query('SELECT answer, passed FROM inspections WHERE run_id=?', (self.run_id,))
        summary = {'legs': len(self.legs), 'legs_done': sum(1 for l in self.legs if l['status'] == 'done'),
                   'inspections': len(insp), 'passed': sum(1 for r in insp if r['passed'] == 1),
                   'failed': sum(1 for r in insp if r['passed'] == 0),
                   'unknown': sum(1 for r in insp if r['passed'] is None)}
        self._set_run(status=status, ended_at=now_iso(), error=error, summary=dumps(summary))
        self._log('run_finished', f"执行结束：{status}" + (f'（{error}）' if error else ''),
                  level='info' if status == 'completed' else 'warn', data=summary)
        if status != 'completed':
            self.ctx.notify('run_' + status, {'run_id': self.run_id, 'task': self.task.get('name'), 'error': error, 'summary': summary})

    def _safe_stop_task(self) -> None:
        try:
            self.ctx.gateway.stop_task()
        except RobotError as e:
            self._log('task_stop_failed', f'停止云端任务失败：{e}', level='warn')

    # ── 前置检查 / 规划 ─────────────────────────────────────────────────────
    def _preflight(self) -> None:
        pf = self.ctx.ops.preflight(require_localized=bool(self.opt.get('require_localized', True)))
        self._log('preflight', '前置检查 ' + ('通过' if pf['ok'] else '未通过'), level='info' if pf['ok'] else 'error',
                  data={'checks': pf['checks']})
        if not pf['ok']:
            bad = '；'.join(c['text'] for c in pf['checks'] if not c['ok'])
            raise RunAbort(f'前置检查未通过：{bad}')
        self.graph = self.ctx.graph(self.task['map_name'])
        if self.graph is None or len(self.graph) == 0:
            raise RunAbort(f"地图 {self.task['map_name']} 的导航航点尚未同步，请先在「地图」页同步")
        pos = pf['status'].get('position')
        if pos and pos.get('x') is not None:
            nid, dist = self.graph.nearest(float(pos['x']), float(pos['y']))
            self.cur_node = nid
            lvl = 'warn' if dist > 3.0 else 'info'
            self._log('start_node', f'当前位置 ({pos["x"]:.2f}, {pos["y"]:.2f}) 最近导航航点 {nid}，距 {dist:.2f} m'
                      + ('（超过 3 m，请确认定位）' if dist > 3.0 else ''), level=lvl)
        else:
            first = self.items[0]['tw'].get('nav_node_id') if self.items else None
            self.cur_node = first
            self._log('start_node', f'读不到位置，假定机器人在首个任务航点 {first}', level='warn')

    def _plan(self) -> None:
        db = self.ctx.db
        seq = 0
        for it in self.items:
            tw = it['tw']
            seq += 1
            target = tw.get('nav_node_id')
            if not target or target not in self.graph:
                nid, d = self.graph.nearest(float(tw['x']), float(tw['y']))
                target = nid
                self._log('plan_note', f"任务航点「{tw['name']}」没有导航航点，取最近的 {nid}（{d:.2f} m）", level='warn')
            leg_id = db.execute('INSERT INTO run_legs(run_id,seq,task_waypoint_id,item_seq,waypoint_name,to_node,status,attempt) '
                                'VALUES(?,?,?,?,?,?,?,0)', (self.run_id, seq, tw['id'], it['seq'], tw['name'], target, 'pending'))
            self.legs.append({'id': leg_id, 'seq': seq, 'task_waypoint_id': tw['id'], 'waypoint_name': tw['name'],
                              'to_node': target, 'status': 'pending', 'attempt': 0, 'tw': tw, 'path': [], 'item_seq': it['seq']})
        if self.opt.get('return_to_start') and self.cur_node and self.legs:
            seq += 1
            leg_id = db.execute('INSERT INTO run_legs(run_id,seq,waypoint_name,to_node,status,attempt) VALUES(?,?,?,?,?,0)',
                                (self.run_id, seq, '返回起点', self.cur_node, 'pending'))
            self.legs.append({'id': leg_id, 'seq': seq, 'task_waypoint_id': None, 'waypoint_name': '返回起点',
                              'to_node': self.cur_node, 'status': 'pending', 'attempt': 0, 'tw': None, 'path': []})
        self._log('planned', f'规划完成：{len(self.legs)} 段，起点航点 {self.cur_node}',
                  data={'legs': [{'seq': l['seq'], 'to': l['to_node'], 'name': l['waypoint_name']} for l in self.legs]})

    # ── 段执行 ───────────────────────────────────────────────────────────────
    def _execute_leg(self, leg: dict) -> None:
        target = leg['to_node']
        max_retries = int(self.opt.get('max_retries') or 0)
        lease_retries = int(self.opt.get('lease_retries') or 0)
        attempt = 0          # 每次下发 +1 —— 幂等键必须换新，否则云端只回放不执行（C3）
        failures = 0         # 计入 max_retries 的失败次数
        lease_tries = 0
        while True:
            attempt += 1
            self.skip_req.clear()
            self._set_leg(leg, status='planning', attempt=attempt, from_node=self.cur_node)
            if self.cur_node == target:
                self._log('leg_no_nav', f"第 {leg['seq']} 段：已在航点 {target}，无需导航", leg_id=leg['id'])
                self._set_leg(leg, status='arrived', path=dumps([target]), arrived_at=now_iso())
                outcome = 'arrived'
            else:
                path = self.graph.shortest_path(self.cur_node, target) if self.cur_node else None
                if not path or len(path) < 2:
                    self._set_leg(leg, status='failed', error=f'{self.cur_node} → {target} 在航点拓扑里不连通', ended_at=now_iso())
                    raise LegFailed(f"第 {leg['seq']} 段：{self.cur_node} → {target} 不连通")
                key = f'ps-{self.ctx.instance_id}-r{self.run_id}-l{leg["seq"]}-a{attempt}'
                q = self.ctx.events.subscribe()                                 # 先订阅、再取游标、再下发（C10）
                cursor = int(self.ctx.gateway.events().get('seq') or 0)
                self._set_leg(leg, path=dumps(path), idempotency_key=key)
                try:
                    self.ctx.gateway.start_patrol(self.task['map_name'], path, idempotency_key=key, **self._control_options())
                except RobotError as e:
                    if e.status == 409 and self._dispatch_actually_landed(path):
                        # 幂等的「诚实失败」：首次请求还在处理 / 响应太大没留存 —— 查 GET /task 发现任务已在跑，继续等
                        self._log('leg_dispatch_409_landed', f"第 {leg['seq']} 段：下发返回 409（{e}），但云端任务已是本段路径，按已下发处理", level='warn', leg_id=leg['id'])
                    else:
                        self.ctx.events.unsubscribe(q)
                        self._set_leg(leg, status='failed', error=f'下发失败：{e}', ended_at=now_iso())
                        wait_s = float(self.opt.get('lease_retry_seconds') or 30)
                        if e.status == 409 and lease_tries < lease_retries:
                            lease_tries += 1
                            self._log('leg_dispatch_409', f"第 {leg['seq']} 段：控制权被占（{e}），{wait_s:.0f} 秒后重试（{lease_tries}/{lease_retries}）", level='warn', leg_id=leg['id'])
                            if self._sleep_unless_abort(wait_s):
                                raise RunAbort('人工中止')
                            continue
                        raise LegFailed(f"第 {leg['seq']} 段下发失败：{e}")
                self._set_leg(leg, status='dispatched', dispatched_at=now_iso())
                self._log('leg_dispatched', f"第 {leg['seq']} 段：{self.cur_node} → {target}，路径 {' → '.join(path)}（{self.graph.path_length(path):.1f} m）",
                          leg_id=leg['id'], data={'path': path, 'idempotency_key': key, 'cursor': cursor, 'attempt': attempt})
                try:
                    outcome = self._wait_arrival(leg, cursor, target, q)
                finally:
                    self.ctx.events.unsubscribe(q)

            if outcome == 'arrived':
                self.cur_node = target
                if leg['status'] != 'arrived':
                    self._set_leg(leg, status='arrived', arrived_at=now_iso())
                self._log('leg_arrived', f"第 {leg['seq']} 段：到达航点 {target}", leg_id=leg['id'])
                if leg.get('tw'):
                    settle = float(self.opt.get('settle_seconds') or 0)
                    if settle > 0:
                        time.sleep(settle)
                    self._set_leg(leg, status='inspecting')
                    run_inspection(self.ctx, self.run_id, leg, leg['tw'])
                self._set_leg(leg, status='done', ended_at=now_iso())
                return
            if outcome == 'lost_localization':
                self._safe_stop_task()
                self._set_leg(leg, status='pending', error='丢定位，等待人工重新定位后继续')
                self._log('run_pause_lost_localization', '定位丢失：已停下云端任务并暂停执行。请在「总览」用机器人当前最近的航点重新定位，然后点「继续」', level='error', leg_id=leg['id'])
                self.pause_req.set()
                self._wait_if_paused()
                if self.abort_req.is_set():
                    raise RunAbort('人工中止')
                self._relocate_current_node()                 # 不计入失败次数，但下一次下发会用新的幂等键
                continue
            if outcome == 'skipped':
                self._safe_stop_task()
                self._set_leg(leg, status='skipped', ended_at=now_iso())
                self._log('leg_skipped', f"第 {leg['seq']} 段已跳过", level='warn', leg_id=leg['id'])
                self._relocate_current_node()
                return
            if outcome in ('aborted', 'stopped', 'external'):
                self._set_leg(leg, status='aborted', ended_at=now_iso(), error=outcome)
                if outcome == 'external':
                    raise RunAbort('云端任务被外部替换（现场下发了别的任务），本次执行中止，不去停对方的任务')
                raise RunAbort('人工中止' if outcome == 'aborted' else '云端任务被外部停止（现场有人接管？）')
            # failed:* / timeout / not_started
            if outcome.startswith('failed:0x'):
                try:
                    st = self.ctx.gateway.task()
                    if st.get('error_hex') == outcome[7:] and st.get('error_name'):
                        outcome = f"{outcome} {st.get('error_name')}（{st.get('error_text')}）"
                except RobotError:
                    pass
            self._safe_stop_task()
            self._relocate_current_node()
            reason = {'timeout': f"超过 {self.opt.get('leg_timeout')} s 未到达",
                      'not_started': '任务下发成功但机器人没有动（设备未启动 / 未定位？见 full-patrol.md ②④）',
                      'failed:offline': f"机器人掉线 / 云端不可达超过 {self.opt.get('offline_timeout')} s",
                      'failed:stall': f"停滞：超过 {self.opt.get('stall_timeout')} s 没有到达新的航点（持续避障 / 卡住？）"}.get(outcome, outcome)
            failures += 1
            if failures <= max_retries and outcome != 'not_started':
                self._log('leg_retry', f"第 {leg['seq']} 段失败（{reason}），重试 {failures}/{max_retries}", level='warn', leg_id=leg['id'])
                continue
            self._set_leg(leg, status='failed', error=reason, ended_at=now_iso())
            raise LegFailed(f"第 {leg['seq']} 段失败：{reason}")

    def _dispatch_actually_landed(self, path: list[str]) -> bool:
        try:
            st = self.ctx.gateway.task()
        except RobotError:
            return False
        return bool(st.get('active')) and [str(x) for x in (st.get('path') or [])] == [str(x) for x in path]

    def _relocate_current_node(self) -> None:
        try:
            pos = self.ctx.gateway.position()
            nid, d = self.graph.nearest(float(pos['x']), float(pos['y']))
            self.cur_node = nid
            self._log('relocated', f'重新定位当前航点：{nid}（距 {d:.2f} m）')
        except RobotError as e:
            self._log('relocate_failed', f'读位置失败，沿用 {self.cur_node}：{e}', level='warn')

    def _sleep_unless_abort(self, seconds: float) -> bool:
        t0 = time.time()
        while time.time() - t0 < seconds:
            if self.abort_req.is_set():
                return True
            time.sleep(0.25)
        return False

    def _wait_arrival(self, leg: dict, cursor: int, target: str, q: queue.Queue) -> str:
        """返回 arrived | failed:<hex> | stopped | aborted | skipped | timeout | not_started | failed:offline"""
        t0 = time.time()
        last_reconcile = 0.0
        progress = False
        leg_timeout = float(self.opt.get('leg_timeout') or 600)
        not_started_timeout = float(self.opt.get('not_started_timeout') or 25)
        offline_timeout = float(self.opt.get('offline_timeout') or 90)
        offline_since: float | None = None
        stall_timeout = float(self.opt.get('stall_timeout') or 180)
        last_progress = t0
        try:
            while True:
                if self.abort_req.is_set():
                    return 'aborted'
                if self.skip_req.is_set():
                    return 'skipped'
                try:
                    ev = q.get(timeout=1.0)
                except queue.Empty:
                    ev = None
                if ev is not None and int(ev.get('seq') or 0) > cursor:
                    t, d = ev.get('type'), ev.get('data') or {}
                    if t == 'waypoint_reached':
                        progress = True
                        last_progress = time.time()
                        prog = {'last_reached': d.get('waypoint'), 'index': d.get('index'), 'total': d.get('total'), 'next': d.get('nextTarget')}
                        if leg['status'] != 'navigating':
                            self._set_leg(leg, status='navigating', cloud_task=dumps(prog))
                        else:
                            self._set_leg(leg, cloud_task=dumps(prog))
                        if str(d.get('waypoint')) == str(target) and not d.get('nextTarget'):
                            return 'arrived'
                    elif t == 'task_started':
                        ev_path = [str(x) for x in (d.get('path') or [])]
                        if ev_path and ev_path != [str(x) for x in loads(leg.get('path'), [])]:
                            self._log('external_task', f"云端开始了另一个任务（路径 {' → '.join(ev_path)}），不是本段的 —— 现场有人下发了任务？", level='error', leg_id=leg['id'])
                            return 'external'
                        progress = True
                        self._set_leg(leg, status='navigating')
                    elif t == 'task_completed':
                        return 'arrived'
                    elif t == 'task_failed':
                        return f"failed:{d.get('errorHex')}"
                    elif t == 'task_stopped':
                        return 'stopped'
                    elif t == 'localization' and not d.get('valid'):
                        action = str(self.opt.get('lost_localization_action') or 'pause')
                        if self.opt.get('stop_on_lost_localization'):
                            action = 'fail'
                        self._log('lost_localization', f'巡检途中丢定位（策略：{action}）', level='warn', leg_id=leg['id'])
                        if action == 'fail':
                            return 'failed:lost_localization'
                        if action == 'pause':
                            return 'lost_localization'
                    elif t == 'emergency' and d.get('active'):
                        self._log('emergency_during_leg', '巡检途中急停指令开始下发', level='error', leg_id=leg['id'])
                now = time.time()
                if now - last_reconcile >= 5.0:
                    last_reconcile = now
                    try:
                        st = self.ctx.gateway.task()
                        offline_since = None
                    except RobotError as e:
                        self._log('reconcile_failed', f'读任务状态失败：{e}', level='warn', leg_id=leg['id'])
                        st = None
                        if e.status in (0, 502):
                            offline_since = offline_since or now
                            if now - offline_since > offline_timeout:
                                return 'failed:offline'
                    if st:
                        self._set_leg(leg, cloud_task=dumps({k: st.get(k) for k in ('status', 'status_code', 'status_name', 'active',
                                                                                   'terminal', 'error_hex', 'error_name', 'current_target', 'visited')}))
                        if st.get('active'):
                            progress = True
                        elif st.get('terminal'):
                            code = st.get('status_code')
                            if code == 4 and str(target) in [str(v) for v in (st.get('visited') or [])]:
                                return 'arrived'
                            if code == 255:
                                return f"failed:{st.get('error_hex')}"
                            if not progress and now - t0 > not_started_timeout:
                                return 'not_started'
                if now - t0 > leg_timeout:
                    return 'timeout'
                if progress and now - last_progress > stall_timeout:
                    return 'failed:stall'
        finally:
            pass


class RunManager:
    """同一时刻只允许一个执行（单机器人）。"""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.lock = threading.Lock()
        self.active: MissionRunner | None = None

    def get_run(self, run_id: int) -> dict | None:
        r = self.ctx.db.query_one('SELECT * FROM runs WHERE id=?', (run_id,))
        if r:
            r['summary'] = loads(r.get('summary'), {})
        return r

    def start(self, task_id: int, from_seq: int | None = None) -> dict:
        """from_seq：从任务里第几个航点开始（跳过前面的），用于失败后重跑剩余航点。"""
        db = self.ctx.db
        task = db.query_one('SELECT * FROM tasks WHERE id=?', (task_id,))
        if not task:
            raise ValueError('任务不存在')
        items = db.query('SELECT ti.seq, tw.* FROM task_items ti JOIN task_waypoints tw ON tw.id=ti.task_waypoint_id '
                         'WHERE ti.task_id=? AND tw.enabled=1 ORDER BY ti.seq', (task_id,))
        if from_seq:
            items = [it for it in items if it['seq'] >= int(from_seq)]
        if not items:
            raise ValueError('任务里没有启用的任务航点' if not from_seq else f'第 {from_seq} 个航点之后没有可执行的任务航点')
        with self.lock:
            if self.active and self.active.is_alive():
                raise RuntimeError(f'已有执行在进行（run #{self.active.run_id}）')
            options = {**loads(task.get('options'), {})}
            name = task['name'] + (f'（从第 {from_seq} 个航点重跑）' if from_seq else '')
            run_id = db.execute('INSERT INTO runs(task_id,task_name,map_name,status,mode,total_legs) VALUES(?,?,?,?,?,?)',
                                (task_id, name, task['map_name'], 'pending', self.ctx.gateway.mode, len(items)))
            runner = MissionRunner(self.ctx, run_id, task, [{'seq': it['seq'], 'tw': dict(it)} for it in items], options)
            self.active = runner
        self.ctx.log_event('run_started', f"开始执行任务「{name}」（{len(items)} 个任务航点，{self.ctx.gateway.mode} 模式）",
                           run_id=run_id, data={'task_id': task_id, 'options': options, 'from_seq': from_seq})
        runner.start()
        return self.get_run(run_id)

    def on_finished(self, run_id: int) -> None:
        with self.lock:
            if self.active and self.active.run_id == run_id:
                self.active = None

    def control(self, run_id: int, action: str) -> dict:
        with self.lock:
            r = self.active
        if not r or r.run_id != run_id or not r.is_alive():
            raise ValueError('该执行不在进行中')
        getattr(r, action)()
        return self.get_run(run_id)

    def shutdown(self, timeout: float = 20.0) -> None:
        """进程退出前：中止进行中的执行（会 DELETE /task 停下机器人）并等它收尾。"""
        with self.lock:
            r = self.active
        if r and r.is_alive():
            r.abort()
            r.join(timeout)

    def current_ref(self) -> tuple[int, int | None] | None:
        with self.lock:
            r = self.active
        if not r or not r.is_alive():
            return None
        return r.run_id, r.current_leg_id

    def active_info(self) -> dict | None:
        with self.lock:
            r = self.active
        if not r or not r.is_alive():
            return None
        return {'run_id': r.run_id, 'status': r.status, 'current_node': r.cur_node,
                'paused': r.pause_req.is_set(), 'legs': [{k: v for k, v in l.items() if k != 'tw'} for l in r.legs]}
