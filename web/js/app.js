import { api, toast, confirmDialog, busy, fmt } from './api.js';
import * as dashboard from './views/dashboard.js';
import * as mapView from './views/map.js';
import * as waypoints from './views/waypoints.js';
import * as tasks from './views/tasks.js';
import * as runs from './views/runs.js';
import * as events from './views/events.js';
import * as settings from './views/settings.js';

// ── 全局状态 ────────────────────────────────────────────────────────────────
export const store = {
  status: null, activeRun: null, mode: null, events: null, statusCodes: null,
  _t: new EventTarget(),
  on(kind, fn) { const w = e => fn(e.detail); this._t.addEventListener(kind, w); return () => this._t.removeEventListener(kind, w); },
  emit(kind, payload) { this._t.dispatchEvent(new CustomEvent(kind, { detail: payload })); },
  get isReal() { return this.mode === 'real'; },
};

// ── 声音（浏览器播报）────────────────────────────────────────────────────────
const sound = { enabled: localStorage.getItem('ps.sound') !== '0', unlocked: false, queue: [] };
function playTts(p) {
  if (!sound.enabled) return;
  const go = () => {
    if (p.audio_url) { const a = new Audio(p.audio_url); a.play().catch(() => speak(p.text)); }
    else speak(p.text);
  };
  const speak = (text) => { if (!('speechSynthesis' in window) || !text) return; const u = new SpeechSynthesisUtterance(text); u.lang = 'zh-CN'; speechSynthesis.speak(u); };
  if (!sound.unlocked) { sound.queue.push(go); toast('浏览器需要一次点击才能播放声音 —— 点击页面任意处', 'warn'); return; }
  go();
}
document.addEventListener('pointerdown', () => { if (!sound.unlocked) { sound.unlocked = true; sound.queue.splice(0).forEach(f => f()); } }, { capture: true });
const btnSound = document.getElementById('btn-sound');
const renderSound = () => { btnSound.textContent = sound.enabled ? '🔊 声音' : '🔇 静音'; };
btnSound.onclick = () => { sound.enabled = !sound.enabled; localStorage.setItem('ps.sound', sound.enabled ? '1' : '0'); renderSound(); };
renderSound();

// ── SSE ─────────────────────────────────────────────────────────────────────
let es = null;
async function connect() {
  if (new URLSearchParams(location.search).has('nosse')) {      // 无头截图/自动化：不开长连接，拉一次状态
    try { const st = await api('/api/robot/status'); store.mode = st.mode; store.events = st.events; store.activeRun = st.active_run; setStatus(st); renderMode(); store.emit('active_run', st.active_run); } catch { /* ignore */ }
    document.getElementById('conn-state').textContent = '静态模式（nosse）';
    return;
  }
  es = new EventSource('/api/stream');
  const conn = document.getElementById('conn-state');
  es.onopen = () => { conn.textContent = '实时连接正常'; };
  es.onerror = () => { conn.textContent = '实时连接中断，重连中…'; };
  es.addEventListener('hello', e => { const p = JSON.parse(e.data).payload; store.mode = p.mode; store.events = p.events; setStatus(p.status); store.activeRun = p.active_run; store.emit('active_run', p.active_run); renderMode(); });
  const kinds = ['robot_status', 'event', 'run', 'leg', 'inspection', 'tts'];
  for (const k of kinds) es.addEventListener(k, e => {
    const p = JSON.parse(e.data).payload;
    if (k === 'robot_status') { setStatus(p); return; }
    if (k === 'tts') playTts(p);
    else if (k === 'run') { if (['completed', 'failed', 'aborted'].includes(p.status)) { store.activeRun = null; store.emit('active_run', null); } else { store.activeRun = { run_id: p.id, status: p.status }; store.emit('active_run', store.activeRun); } }
    store.emit(k, p);
  });
}
function renderMode() {
  const b = document.getElementById('mode-badge');
  b.className = `mode-badge ${store.mode || ''}`;
  b.textContent = store.mode === 'real' ? 'REAL · 真机' : store.mode === 'mock' ? 'MOCK · 仿真' : '…';
}

// ── 顶栏状态灯 ──────────────────────────────────────────────────────────────
function setStatus(s) {
  store.status = s;
  store.emit('robot_status', s);
  const L = document.getElementById('lights');
  const set = (k, cls, text) => { const el = L.querySelector(`[data-k=${k}]`); el.className = `light ${cls}`; el.lastChild.textContent = text; };
  if (!s || s.reachable === false) { set('online', 'bad', '云端不可达'); }
  else set('online', s.online ? 'ok' : 'bad', s.online ? '在线' : '离线');
  const lease = s?.lease;
  if (!lease) set('lease', '', '控制权空闲');
  else set('lease', String(lease.owner).startsWith('api:') ? 'on' : 'warn', String(lease.owner).startsWith('api:') ? '控制权：本系统' : `控制权：${lease.owner}`);
  set('estop', s?.emergency_active ? 'bad' : 'ok', s?.emergency_active ? '急停中' : '无急停');
  const loc = s?.localization || {};
  set('loc', s?.localized ? 'ok' : 'warn', s?.localized ? `定位就绪 ${loc.age ?? ''}s` : '未定位');
  const t = s?.task || {};
  set('task', t.active ? 'on' : (t.status_code === 255 ? 'bad' : ''), t.status_name ? `云端任务 ${t.status_name}` : '云端任务');
  const pos = s?.position;
  document.getElementById('topbar-pos').textContent = pos ? `x=${fmt.num(pos.x)}  y=${fmt.num(pos.y)}  yaw=${fmt.deg(pos.yaw)}   ${s.robot_status_text || ''}   限速计数 ${s.rate_limiter_total ?? ''}` : (s?.errors?.length ? s.errors.join(' | ') : '—');
  document.getElementById('btn-estop').classList.toggle('hidden', !!s?.emergency_active);
  document.getElementById('btn-estop-clear').classList.toggle('hidden', !s?.emergency_active);
}

document.getElementById('btn-estop').onclick = async (e) => {
  const ok = await confirmDialog({ title: '急停', danger: true, okText: '立即急停', body: '将向机器人下发 <b>{"active": true}</b>：机器人端会以 20Hz 持续下发停止指令，直到你显式取消。<br><span class="muted">注意：这不是硬件急停，也不是锁；现场遥控仍可能与之竞争。真正确保不动请按物理急停。</span>' });
  if (!ok) return;
  try { await busy(e.currentTarget, () => api('/api/robot/estop', { method: 'POST', body: { active: true } })); toast('已下发急停', 'warn'); } catch (err) { toast(`急停失败：${err.message}`, 'bad'); }
};
document.getElementById('btn-estop-clear').onclick = async (e) => {
  const ok = await confirmDialog({ title: '取消急停', okText: '取消急停', body: '将下发 {"active": false}，机器人恢复可动。请确认现场安全。' });
  if (!ok) return;
  try { await busy(e.currentTarget, () => api('/api/robot/estop', { method: 'POST', body: { active: false } })); toast('已取消急停', 'ok'); } catch (err) { toast(`失败：${err.message}`, 'bad'); }
};
document.getElementById('btn-stoptask').onclick = async (e) => {
  const ok = await confirmDialog({ title: '停止云端任务', okText: '停止', danger: store.isReal, body: '向云端发 DELETE /task。机器人会停下，但导航等模块仍在跑（不停设备）。如有本系统的执行在进行，建议在「执行监控」里用「中止」。' });
  if (!ok) return;
  try { await busy(e.currentTarget, () => api('/api/robot/task', { method: 'DELETE' })); toast('已请求停止任务', 'warn'); } catch (err) { toast(`失败：${err.message}`, 'bad'); }
};

// ── 路由 ────────────────────────────────────────────────────────────────────
const routes = { dashboard, map: mapView, waypoints, tasks, runs, events, settings };
let current = null;
async function route() {
  const hash = location.hash.replace(/^#\/?/, '') || 'dashboard';
  const [name, ...rest] = hash.split('/');
  const mod = routes[name] || dashboard;
  document.querySelectorAll('.nav a').forEach(a => a.classList.toggle('active', a.dataset.route === (routes[name] ? name : 'dashboard')));
  if (current && current.destroy) { try { current.destroy(); } catch { /* ignore */ } }
  const view = document.getElementById('view');
  view.innerHTML = '';
  current = mod;
  try { await mod.render(view, { store, params: rest, api, toast }); }
  catch (err) { console.error(err); view.innerHTML = `<div class="card"><h2>页面渲染出错</h2><pre class="mono">${err.stack || err}</pre></div>`; }
}
window.addEventListener('hashchange', route);
async function boot() {
  try { const c = await api('/api/robot/status-codes'); store.statusCodes = c; } catch { /* 网关不可达时也能用 */ }
  connect();
  route();
}
boot();
