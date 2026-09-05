# -*- coding: utf-8 -*-
"""机器狗运动学仿真 + 云端事件缓冲（mock 网关的「机器人」）。

只仿真宪法文档里写明的行为，尤其是那些容易踩坑的点：
* 状态写读不对称：下发写 running，读回 navigating；失败读回 paused（255）
* `visited` 在下发瞬间就包含起点，并立刻产生起点的 waypoint_reached
* 未启动设备 / 未定位时任务照样返回 200，但机器人不动（status 保持 idle）
* /perception 的 Location 恒为 1（机器人端的已知 bug），location_valid 恒为 false
* 事件 500 条环形缓冲，seq 单调递增，since 游标续接
* 定位：先发 relocalization → 等收敛 → 偏差 >3m 判 drift_exceeded；设备没起来判 timeout
"""
from __future__ import annotations

import collections
import json
import math
import threading
import time
import uuid
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def load_status_codes() -> dict:
    return json.loads((FIXTURES / 'status_codes.json').read_text(encoding='utf-8'))['data']


def load_maps(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or FIXTURES / 'map_demo.json').read_text(encoding='utf-8'))['maps']


def _sorted_ids(wps: dict) -> list[str]:
    return sorted(wps, key=lambda k: int(k) if k.isdigit() else 10 ** 9)


class MockRobot:
    TICK = 0.05
    DRIFT_THRESHOLD = 3.0

    def __init__(self, maps: dict | None = None, *, robot_id: str = 'R30_2026_001',
                 alias: str = 'ntu-dog-00001', rtsp_path: str = 'cam-1c697ada870c',
                 speed: float = 1.0, prelocalized: bool = False, prestarted: bool = False,
                 device_delay: float = 3.0, localize_delay: float = 1.5,
                 start_node: str | None = None, preprocess_seconds: float = 0.5) -> None:
        self.maps = maps or load_maps()
        self.codes = load_status_codes()
        self.status_values = {v['code']: v for v in self.codes['status']['values']}
        self.error_values = {v['code']: v for v in self.codes['errorCode']['values']}
        self.active_codes = set(self.codes['status']['activeCodes'])
        self.terminal_codes = set(self.codes['status']['terminalCodes'])
        self.write_aliases = self.codes['status']['writeAliases']

        self.robot_id, self.alias, self.rtsp_path = robot_id, alias, rtsp_path
        self.speed = float(speed)
        self.device_delay = float(device_delay)
        self.localize_delay = float(localize_delay)
        self.preprocess_seconds = float(preprocess_seconds)
        self._preprocess_until = 0.0

        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.t0 = time.time()

        self.online = True
        self.ros_available = True
        self.emergency_active = False
        self.device_started = bool(prestarted)
        self.localized = bool(prelocalized)
        self.loc_received_at = time.time() if self.localized else 0.0
        self.loc_lost_until = 0.0
        self.avoiding_until = 0.0
        self.fault_next_leg: str | None = None
        self.html502_left = 0            # >0 时接下来 n 次机器人端请求返回 nginx 风格的 HTML 502
        self.html502_only_task = False   # True 时只对 GET /task 注入（测执行器对账）
        self.ignore_stop = False         # True 时 DELETE /task 回 200 但机器人继续走（真实 Gazebo 机器人实测如此）
        self.drop_events = False
        self.lease: dict | None = None
        self.device_tasks: dict[str, dict] = {}
        self.current_map = next(iter(self.maps))

        wps = self.maps[self.current_map]
        node = start_node if start_node in wps else _sorted_ids(wps)[0]
        p = wps[node]['pose']['position']
        self.x, self.y, self.z = float(p['x']), float(p['y']), float(p['z'])
        self.yaw = self._yaw_of(wps[node]['pose']['orientation'])
        self.linear = 0.0

        self.task = self._idle_task()
        self.seq = 20                      # 真实网关的 seq 不从 0 开始，别让调用方假设它是 0
        self.events: collections.deque = collections.deque(maxlen=500)

        self._stop = False
        self._thread = threading.Thread(target=self._loop, name='mock-robot', daemon=True)
        self._thread.start()

    # ── 基础 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _yaw_of(q: dict) -> float:
        x, y, z, w = (float(q.get(k, 0.0)) for k in ('x', 'y', 'z', 'w'))
        return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))

    def _idle_task(self) -> dict:
        return {'status_code': 0, 'map_name': '', 'path': [], 'visited': [], 'current_target': '',
                'current_index': -1, 'checkpoint_id': '', 'error_code': 0, 'message': '',
                'gait': 12290, 'speed': 0, 'manner': 0, 'nav_mode': 0, 'obs_mode': 0}

    def ros_time(self) -> float:
        return time.time() - self.t0

    def close(self) -> None:
        self._stop = True

    # ── 事件 ────────────────────────────────────────────────────────────────
    def emit(self, etype: str, data: dict) -> None:
        with self.lock:
            self.seq += 1
            ev = {'seq': self.seq, 'ts': int(time.time() * 1000), 'robotId': self.robot_id,
                  'type': etype, 'data': data}
            if not self.drop_events:
                self.events.append(ev)
            self.cond.notify_all()

    def events_since(self, since: int | None, limit: int = 200) -> dict:
        with self.lock:
            if since is None:
                return {'events': [], 'seq': self.seq, 'nextSince': self.seq}
            evs = [e for e in self.events if e['seq'] > since][:limit]
            return {'events': evs, 'seq': self.seq,
                    'nextSince': evs[-1]['seq'] if evs else since}

    def wait_events(self, since: int, timeout: float) -> list[dict]:
        with self.lock:
            evs = [e for e in self.events if e['seq'] > since]
            if not evs:
                self.cond.wait(timeout)
                evs = [e for e in self.events if e['seq'] > since]
            return evs

    # ── 只读视图 ─────────────────────────────────────────────────────────────
    def overview(self) -> dict:
        with self.lock:
            lease = None
            if self.lease and self.lease['expiresAt'] > time.time() * 1000:
                lease = dict(self.lease)
            return {
                'robotId': self.robot_id, 'name': self.robot_id, 'robotType': 'R30_v1', 'site': '',
                'rtspPath': self.rtsp_path, 'publicIp': '155.69.183.252', 'lanIp': '192.168.0.122',
                'location': 'Singapore, SG', 'agentVersion': '0.1.0-mock', 'online': self.online,
                'lastSeen': int(time.time() * 1000), 'lease': lease, 'alias': self.alias,
                'apiBase': f'/v1/robots/{self.alias}',
            }

    def telemetry(self) -> dict:
        with self.lock:
            now = time.time()
            rt = self.ros_time()
            loc_ok = self.localized and now >= self.loc_lost_until
            status_zh = self.status_values[self.task['status_code']]['zh'] if self.task['status_code'] else '待机中'
            pose = {'x': self.x, 'y': self.y, 'z': self.z, 'yaw': self.yaw}
            return {
                'alarm_active_mode': 0, 'alarm_muted': False,
                'emergency_active': self.emergency_active,
                'ros_available': self.ros_available, 'ros_error': '' if self.ros_available else 'roscore unreachable',
                'telemetry': {
                    'robot_info': {'id': self.robot_id, 'name': 'R30_v1', 'status': status_zh, 'message': '',
                                   'received': True, 'stamp': now},
                    'global_localization': {'received': self.localized, 'received_at': self.loc_received_at if loc_ok else self.loc_received_at,
                                            'stamp': rt, **pose},
                    'odom': {'received': True, 'stamp': rt, 'linear': self.linear, 'angular': 0.0, **pose},
                    'initial_pose': {'received': False, 'stamp': 0, 'x': 0, 'y': 0, 'yaw': 0, 'z': 0},
                    'clicked_point': {'received': False, 'stamp': 0, 'x': 0, 'y': 0},
                },
            }

    def position(self) -> dict | None:
        with self.lock:
            if not self.localized:
                return None
            return {'x': self.x, 'y': self.y, 'z': self.z, 'yaw': self.yaw, 'timestamp': round(self.ros_time(), 3)}

    def perception(self) -> dict:
        with self.lock:
            avoiding = time.time() < self.avoiding_until
            # Location 恒为 1：复刻机器人端拿 Unix 时间减 ROS 时间戳的 bug
            return {'Location': 1, 'ObsState': 1 if avoiding else 0,
                    'location_valid': False, 'avoiding': avoiding,
                    'location_text': '定位无效', 'obs_text': '正在避障' if avoiding else '前方无障碍'}

    def task_view(self) -> dict:
        with self.lock:
            t = dict(self.task)
            code = t.pop('status_code')
            sv = self.status_values[code]
            ev = self.error_values.get(t['error_code'], {'name': 'UNKNOWN', 'zh': '未知错误'})
            ctl = self.codes['control']

            def cname(group, code_):
                for v in ctl[group]:
                    if v['code'] == code_:
                        return v['name']
                return 'UNKNOWN'

            out = {'status': sv['name'].lower(), **t}
            out.update({
                'status_code': code, 'status_name': sv['name'], 'status_text': sv['zh'],
                'active': code in self.active_codes, 'terminal': code in self.terminal_codes,
                'error_name': ev['name'], 'error_text': ev['zh'], 'error_hex': f"0x{t['error_code']:04X}",
                **({'progress': {'visited': len(t['visited']), 'total': len(t['path'])}} if t['path'] else {}),   # 真实网关 idle 时不带 progress
                'gait_name': cname('gait', t['gait']), 'speed_name': cname('speed', t['speed']),
                'manner_name': cname('manner', t['manner']), 'nav_mode_name': cname('navMode', t['nav_mode']),
                'obs_mode_name': cname('obsMode', t['obs_mode']),
            })
            return out

    # ── 写操作 ───────────────────────────────────────────────────────────────
    def start_task(self, map_name: str, path: list[str], opts: dict) -> dict:
        with self.lock:
            wps = self.maps[map_name]
            self.task = self._idle_task()
            self.task.update({'map_name': map_name, 'path': list(path), 'checkpoint_id': opts.get('checkpoint_id') or path[-1]})
            for k in ('gait', 'speed', 'manner', 'nav_mode', 'obs_mode'):
                if k in opts and opts[k] is not None:
                    self.task[k] = int(opts[k])
            self.current_map = map_name
            if not (self.device_started and self.localized):
                # 真机：任务返回 200 但站着不动 —— 导航栈根本没起来
                self.task['message'] = 'nav stack not running'
                return {'accepted': True, 'map_name': map_name, 'path': list(path)}
            # 真机：idle → nav_preprocess(2) → navigating(3)；写入 running 只是别名，读回来先是 nav_preprocess
            self.task['status_code'] = 2 if self.preprocess_seconds > 0 else 3
            self._preprocess_until = time.time() + self.preprocess_seconds
            self.task['visited'] = [path[0]]                     # 下发瞬间就包含起点
            self.task['current_index'] = 1
            self.task['current_target'] = path[1]
            self.emit('waypoint_reached', {'waypoint': path[0], 'map': map_name, 'index': 0,
                                           'total': len(path), 'nextTarget': path[1]})
            self.emit('task_started', {'map': map_name, 'path': list(path), 'statusCode': 3})
            _ = wps
            return {'accepted': True, 'map_name': map_name, 'path': list(path)}

    def stop_task(self) -> dict:
        with self.lock:
            if self.ignore_stop:
                return {'message': '任务已停止'}          # 云端如实转发机器人端的回应，但机器人并没有停
            was_active = self.task['status_code'] in self.active_codes
            m = self.task['map_name']
            self.task['status_code'] = 0
            self.task['current_target'] = ''
            self.task['current_index'] = -1
            self.linear = 0.0
            if was_active:
                self.emit('task_stopped', {'map': m})
            return {'stopped': True}

    def set_estop(self, active: bool) -> dict:
        with self.lock:
            if active != self.emergency_active:
                self.emergency_active = active
                self.emit('emergency', {'active': active})
            return {'active': active}

    def device_start(self) -> str:
        with self.lock:
            tid = uuid.uuid4().hex[:12]
            self.device_tasks[tid] = {'kind': 'start', 'done_at': time.time() + self.device_delay}
            return tid

    def device_stop(self) -> str:
        with self.lock:
            tid = uuid.uuid4().hex[:12]
            self.device_tasks[tid] = {'kind': 'stop', 'done_at': time.time() + self.device_delay}
            return tid

    def device_status(self, tid: str) -> dict | None:
        with self.lock:
            t = self.device_tasks.get(tid)
            if t is None:
                return None
            done = time.time() >= t['done_at']
            if done and not t.get('applied'):
                t['applied'] = True
                if t['kind'] == 'start':
                    self.device_started = True
                else:
                    self.device_started = False
                    self.localized = False           # 导航栈停了，定位也没了
                    self.task = self._idle_task()
            return {'success': True, 'task_id': tid, 'completed': done, 'result_success': True if done else None,
                    'message': ('执行完成' if done else '执行中')}

    def localize(self, map_name: str, node_id: str, pose: dict | None) -> dict:
        wps = self.maps[map_name]
        if pose is None:
            p = wps[node_id]['pose']['position']
            init = {'x': float(p['x']), 'y': float(p['y']), 'yaw': self._yaw_of(wps[node_id]['pose']['orientation'])}
        else:
            init = {'x': float(pose.get('x', 0)), 'y': float(pose.get('y', 0)), 'yaw': float(pose.get('yaw', 0))}
        time.sleep(self.localize_delay)           # 服务端同步等收敛（真机最长 20s）
        with self.lock:
            if not self.device_started or not self.ros_available:
                return {'success': False, 'error': '定位超时：未收到 /global_localization',
                        'data': {'reason': 'timeout', 'init_pose': init}}
            drift = math.hypot(self.x - init['x'], self.y - init['y'])
            result = {'x': self.x, 'y': self.y, 'yaw': self.yaw}
            if drift > self.DRIFT_THRESHOLD:
                return {'success': False, 'error': f'定位偏差过大 {drift:.2f}m',
                        'data': {'reason': 'drift_exceeded', 'drift': round(drift, 3),
                                 'threshold': self.DRIFT_THRESHOLD, 'init_pose': init, 'result_pose': result}}
            self.localized = True
            self.loc_received_at = time.time()
            self.current_map = map_name
            self.emit('localization', {'valid': True})
            return {'success': True, 'data': {'drift': round(drift, 3), 'threshold': self.DRIFT_THRESHOLD,
                                              'pcd_path': f'/home/robot/maps/{map_name}/map.pcd',
                                              'init_pose': init, 'result_pose': result}}

    def topic_ready(self, topic: str) -> bool:
        with self.lock:
            if topic == '/global_localization':
                return self.localized
            return self.device_started

    # ── 控制权 ───────────────────────────────────────────────────────────────
    def acquire_lease(self, owner: str, seconds: float = 30.0) -> tuple[bool, str | None]:
        with self.lock:
            now_ms = time.time() * 1000
            if self.lease and self.lease['expiresAt'] > now_ms and self.lease['owner'] != owner:
                return False, self.lease['owner']
            if not self.lease or self.lease['owner'] != owner or self.lease['expiresAt'] <= now_ms:
                self.lease = {'owner': owner, 'heldSince': int(now_ms), 'expiresAt': int(now_ms + seconds * 1000)}
            else:
                self.lease['expiresAt'] = int(now_ms + seconds * 1000)
            return True, owner

    def preempt(self, owner: str = 'admin', seconds: float = 60.0) -> dict | None:
        """现场登录的人抢走控制权：无条件覆盖（云端语义是「人可抢程序，程序抢不了人」）。seconds<=0 表示放手。"""
        with self.lock:
            if seconds <= 0:
                self.lease = None
            else:
                now_ms = time.time() * 1000
                self.lease = {'owner': owner, 'heldSince': int(now_ms), 'expiresAt': int(now_ms + seconds * 1000)}
            return self.lease

    def release_lease(self, owner: str) -> None:
        with self.lock:
            if self.lease and self.lease['owner'] == owner:
                self.lease = None

    # ── 故障注入 ─────────────────────────────────────────────────────────────
    def set_online(self, online: bool) -> None:
        with self.lock:
            if online != self.online:
                self.online = online
                self.emit('online' if online else 'offline', {'name': self.robot_id, 'robotType': 'R30_v1'})

    def set_ros(self, available: bool) -> None:
        with self.lock:
            self.ros_available = available

    def inject_fault(self, kind: str | None) -> None:
        with self.lock:
            self.fault_next_leg = kind if kind in ('obstacle', 'planning') else None

    def obstacle(self, seconds: float) -> None:
        with self.lock:
            self.avoiding_until = time.time() + seconds
            self.emit('obstacle', {'avoiding': True})

    def lose_localization(self, seconds: float) -> None:
        with self.lock:
            self.loc_lost_until = time.time() + seconds
            self.emit('localization', {'valid': False})

    def teleport(self, node_id: str | None = None, x: float | None = None, y: float | None = None,
                 map_name: str | None = None) -> None:
        with self.lock:
            m = map_name or self.current_map
            if node_id is not None:
                p = self.maps[m][node_id]['pose']['position']
                self.x, self.y = float(p['x']), float(p['y'])
                self.yaw = self._yaw_of(self.maps[m][node_id]['pose']['orientation'])
            else:
                self.x, self.y = float(x), float(y)
            self.current_map = m

    def reset(self, *, prelocalized: bool = False, prestarted: bool = False) -> None:
        with self.lock:
            self.task = self._idle_task()
            self.emergency_active = False
            self.online = True
            self.ros_available = True
            self.device_started = prestarted
            self.localized = prelocalized
            self.loc_received_at = time.time() if prelocalized else 0.0
            self.loc_lost_until = 0.0
            self.avoiding_until = 0.0
            self.fault_next_leg = None
            self.html502_left = 0
            self.html502_only_task = False
            self.ignore_stop = False
            self.drop_events = False
            self.lease = None
            self.linear = 0.0
            self._was_avoiding = False          # 否则复位后第一拍会补发一条「避障结束」
            self._loc_lost = False

    def snapshot_state(self) -> dict:
        with self.lock:
            return {'x': self.x, 'y': self.y, 'yaw': self.yaw, 'online': self.online,
                    'ros_available': self.ros_available, 'emergency_active': self.emergency_active,
                    'device_started': self.device_started, 'localized': self.localized,
                    'current_map': self.current_map, 'task': self.task_view(), 'seq': self.seq,
                    'lease': self.lease, 'fault_next_leg': self.fault_next_leg,
                    'drop_events': self.drop_events, 'speed': self.speed}

    # ── 运动学主循环 ─────────────────────────────────────────────────────────
    def _loop(self) -> None:
        last = time.time()
        while not self._stop:
            time.sleep(self.TICK)
            now = time.time()
            dt, last = now - last, now
            with self.lock:
                try:
                    self._step(dt, now)
                except Exception:          # noqa: BLE001 — 仿真线程不能死
                    pass

    def _step(self, dt: float, now: float) -> None:
        if self.localized and now >= self.loc_lost_until:
            self.loc_received_at = now
        avoiding_before = self.linear >= 0 and getattr(self, '_was_avoiding', False)
        avoiding = now < self.avoiding_until
        if avoiding_before and not avoiding:
            self.emit('obstacle', {'avoiding': False})
        if getattr(self, '_loc_lost', False) and now >= self.loc_lost_until:
            self._loc_lost = False
            self.emit('localization', {'valid': True})
        if now < self.loc_lost_until:
            self._loc_lost = True
        self._was_avoiding = avoiding

        t = self.task
        if t['status_code'] == 2:
            if now >= self._preprocess_until:
                t['status_code'] = 3
            self.linear = 0.0
            return
        if t['status_code'] != 3:
            self.linear = 0.0
            return
        if not (self.device_started and self.localized and self.ros_available) or self.emergency_active or avoiding:
            self.linear = 0.0
            return
        if self.fault_next_leg:
            code = 9035 if self.fault_next_leg == 'obstacle' else 9036
            self.fault_next_leg = None
            t['status_code'] = 255
            t['error_code'] = code
            self.linear = 0.0
            self.emit('task_failed', {'errorCode': code, 'errorHex': f'0x{code:04X}',
                                      'visited': list(t['visited']), 'total': len(t['path'])})
            return

        wps = self.maps[t['map_name']]
        target = t['current_target']
        p = wps[target]['pose']['position']
        tx, ty = float(p['x']), float(p['y'])
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        step = self.speed * dt
        if dist > 1e-6:
            self.yaw = math.atan2(dy, dx)
        if dist <= step + 1e-6:
            self.x, self.y = tx, ty
            t['visited'].append(target)
            idx = t['current_index']
            nxt = t['path'][idx + 1] if idx + 1 < len(t['path']) else None
            self.emit('waypoint_reached', {'waypoint': target, 'map': t['map_name'], 'index': idx,
                                           'total': len(t['path']), 'nextTarget': nxt})
            if nxt is None:
                t['status_code'] = 4
                t['current_target'] = ''
                self.linear = 0.0
                self.emit('task_completed', {'map': t['map_name'], 'visited': list(t['visited']),
                                             'total': len(t['path'])})
            else:
                t['current_index'] = idx + 1
                t['current_target'] = nxt
        else:
            self.x += dx / dist * step
            self.y += dy / dist * step
            self.linear = self.speed
