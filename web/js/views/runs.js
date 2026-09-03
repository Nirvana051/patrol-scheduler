import { api, h, esc, fmt, toast, busy, badge, confirmDialog, statusBadge, RUN_STATUS, LEG_STATUS, ANSWER_TEXT } from '../api.js';
import { MapCanvas } from '../components/map_canvas.js';
let offs = [], mc = null, cur = null, timer = null;

export async function render(root, { store, params }) {
  root.innerHTML = `<div class="page-head"><h1>执行监控</h1><div class="actions" id="ctl"></div></div>
  <div class="split">
    <div><div class="card"><div class="card-head"><h2>执行记录</h2><button class="btn btn-sm" id="b-reload">↻</button></div><div class="list" id="runs"></div></div></div>
    <div id="detail"><div class="empty">选择一条执行记录</div></div>
  </div>`;
  const list = root.querySelector('#runs');
  const loadList = async () => {
    const { items } = await api('/api/runs?limit=40');
    list.innerHTML = items.length ? items.map(r => `<div class="item ${cur === r.id ? 'active' : ''}" data-id="${r.id}"><div><b>#${r.id}</b> ${esc(r.task_name)}<div class="small muted">${fmt.dt(r.started_at)} · ${r.mode}</div></div>${statusBadge(RUN_STATUS, r.status)}</div>`).join('') : '<div class="muted small">还没有执行记录</div>';
    list.querySelectorAll('.item').forEach(it => it.onclick = () => open(Number(it.dataset.id)));
    return items;
  };
  root.querySelector('#b-reload').onclick = loadList;
  const items = await loadList();
  const want = params && params[0] ? Number(params[0]) : (store.activeRun?.run_id || items[0]?.id);
  if (want) await open(want);

  async function open(id) {
    cur = id; list.querySelectorAll('.item').forEach(i => i.classList.toggle('active', Number(i.dataset.id) === id));
    await paint();
  }
  async function paint() {
    if (!cur) return;
    let r; try { r = await api(`/api/runs/${cur}`); } catch (e) { root.querySelector('#detail').innerHTML = `<div class="card">${esc(e.message)}</div>`; return; }
    const live = ['pending', 'preflight', 'running', 'paused'].includes(r.status);
    const done = r.legs.filter(l => l.status === 'done').length;
    const firstBad = r.legs.find(l => ['failed', 'aborted', 'pending'].includes(l.status) && l.task_waypoint_id);
    const retrySeq = firstBad && firstBad.item_seq;
    root.querySelector('#ctl').innerHTML = live ? `${r.status === 'paused' ? '<button class="btn btn-ok" data-a="resume">▶ 继续</button>' : '<button class="btn" data-a="pause">⏸ 暂停（当前段完成后）</button>'}<button class="btn btn-warn" data-a="skip">⏭ 跳过当前航点</button><button class="btn btn-danger" data-a="abort">■ 中止</button>`
      : (retrySeq && !store.activeRun ? `<button class="btn btn-primary" data-a="retry" data-seq="${retrySeq}">↻ 从「${esc(firstBad.waypoint_name)}」重跑剩余航点</button>` : '');
    root.querySelector('#ctl').querySelectorAll('button').forEach(b => b.onclick = async () => {
      const a = b.dataset.a;
      if (a === 'retry') { if (!(await confirmDialog({ title: '重跑剩余航点', okText: '开始', danger: store.isReal, body: `新建一次执行，从任务的第 ${b.dataset.seq} 个航点开始（前面已完成的不再走）。${store.isReal ? '<br><b>真机会动</b>，确认现场安全。' : ''}` }))) return; try { const nr = await busy(b, () => api(`/api/tasks/${r.task_id}/run?from_seq=${b.dataset.seq}`, { method: 'POST' })); toast(`已开始执行 #${nr.id}`, 'ok'); cur = nr.id; await loadList(); paint(); } catch (e) { toast(e.message, 'bad', 6000); } return; }
      if (a === 'abort' && !(await confirmDialog({ title: '中止执行', danger: true, okText: '中止', body: '会向云端发 DELETE /task 停下机器人，并把本次执行标记为已中止。' }))) return;
      try { await busy(b, () => api(`/api/runs/${cur}/${a}`, { method: 'POST' })); } catch (e) { toast(e.message, 'bad'); }
    });
    const el = root.querySelector('#detail');
    const keepMap = mc && el.querySelector('canvas.map');
    el.innerHTML = `
      <div class="card"><div class="card-head"><div><h2 style="display:inline">#${r.id} ${esc(r.task_name)}</h2> <span class="muted small">${esc(r.map_name)} · ${r.mode} · 开始 ${fmt.dt(r.started_at)} · 用时 ${fmt.dur(r.started_at, r.ended_at)}</span></div>${statusBadge(RUN_STATUS, r.status)}</div>
        <div class="progress"><i style="width:${r.total_legs ? done / r.total_legs * 100 : 0}%"></i></div>
        <div class="small" style="margin-top:6px">${done}/${r.total_legs} 段完成${r.error ? ` · <span class="badge bad">${esc(r.error)}</span>` : ''}${r.summary && r.summary.inspections != null ? ` · 检查 ${r.summary.inspections}（通过 ${r.summary.passed} / 不通过 ${r.summary.failed} / 待复核 ${r.summary.unknown}）` : ''}</div></div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card"><h2>分段进度</h2><div class="legs">${r.legs.map(l => `<div class="leg ${l.status === 'done' ? 'done' : ['failed', 'aborted'].includes(l.status) ? 'failed' : ['dispatched', 'navigating', 'arrived', 'inspecting', 'planning'].includes(l.status) ? 'active' : ''}"><span class="n">${l.seq}</span><div><b>${esc(l.waypoint_name || '')}</b> <span class="small muted mono">${esc(l.from_node ?? '?')} → ${esc(l.to_node)}${l.path && l.path.length > 2 ? `（经 ${l.path.length - 2} 点）` : ''}</span>${l.error ? `<div class="small" style="color:var(--danger)">${esc(l.error)}</div>` : ''}${l.cloud_task && l.cloud_task.total != null && ['dispatched', 'navigating'].includes(l.status) ? `<div class="small" style="color:var(--primary)">已过 ${Number(l.cloud_task.index) + 1}/${l.cloud_task.total} 点（最近到达 ${esc(l.cloud_task.last_reached)}，下一个 ${esc(l.cloud_task.next ?? '—')}）</div>` : ''}<div class="small muted">${l.dispatched_at ? `下发 ${fmt.time(l.dispatched_at)}` : ''}${l.arrived_at ? ` · 到达 ${fmt.time(l.arrived_at)}` : ''}${l.attempt > 1 ? ` · 第 ${l.attempt} 次尝试` : ''}${l.idempotency_key ? ` · <span class="mono">${esc(l.idempotency_key)}</span>` : ''}</div></div>${statusBadge(LEG_STATUS, l.status)}</div>`).join('') || '<div class="muted small">尚未规划</div>'}</div></div>
        <div class="card"><h2>轨迹</h2><div class="mapwrap" style="height:320px"><canvas class="map"></canvas></div></div>
      </div>
      <div class="card" style="margin-top:14px"><h2>检查结果</h2><div id="insps" style="display:grid;gap:10px">${r.inspections.length ? r.inspections.map(inspCard).join('') : '<div class="muted small">尚无检查记录</div>'}</div></div>
      <div class="card" style="margin-top:14px"><h2>本次执行的事件</h2><div id="revents">${r.events.slice(-60).reverse().map(e => `<div class="event-row ${e.level}"><span class="t">${fmt.time(e.ts)}</span><span>${badge(e.source === 'cloud' ? '云端' : '系统', e.source === 'cloud' ? 'info' : '')}</span><span class="mono small">${esc(e.type)}</span><span>${esc(e.message)}</span></div>`).join('') || '<div class="muted small">无</div>'}</div></div>`;
    el.querySelectorAll('[data-replay]').forEach(b => b.onclick = () => { const url = b.dataset.replay; if (url) new Audio(url).play().catch(() => toast('浏览器阻止了自动播放，请再点一次', 'warn')); else if (b.dataset.text && 'speechSynthesis' in window) { const u = new SpeechSynthesisUtterance(b.dataset.text); u.lang = 'zh-CN'; speechSynthesis.speak(u); } });
    // 小地图
    if (mc) mc.destroy();
    mc = new MapCanvas(el.querySelector('canvas.map'));
    try { const d = await api(`/api/maps/${encodeURIComponent(r.map_name)}/waypoints`); mc.setData(d); const twIds = r.legs.filter(l => l.task_waypoint_id).map(l => l.to_node); const tws = d.waypoints.filter(w => twIds.includes(w.node_id)).map(w => ({ ...w, seq: r.legs.find(l => l.to_node === w.node_id)?.seq, name: r.legs.find(l => l.to_node === w.node_id)?.waypoint_name })); mc.setTaskWaypoints(tws); const active = r.legs.find(l => ['dispatched', 'navigating'].includes(l.status)); if (active && active.path) mc.setPath(active.path); if (store.status?.position) mc.setRobot(store.status.position); } catch { /* ignore */ }
  }
  offs.push(store.on('robot_status', s => { if (mc && s?.position) mc.setRobot(s.position); }));
  const refresh = () => { clearTimeout(timer); timer = setTimeout(() => { paint(); loadList(); }, 250); };
  offs.push(store.on('run', refresh)); offs.push(store.on('leg', p => { if (p.run_id === cur) refresh(); })); offs.push(store.on('inspection', p => { if (p.run_id === cur) refresh(); }));
  offs.push(store.on('event', p => { if (p.run_id === cur) refresh(); }));
}
function inspCard(i) {
  return `<div class="insp"><div>${i.annot_url ? `<img src="${i.annot_url}" alt="全景" loading="lazy">` : '<div class="video-box">无图</div>'}${i.crop_url ? `<img class="crop" src="${i.crop_url}" alt="裁切" loading="lazy">` : ''}</div>
    <div><div style="display:flex;justify-content:space-between;align-items:center"><div><b>${esc(i.waypoint_name)}</b> <span class="muted small">${i.angle_from}° → ${i.angle_to}° · ${fmt.time(i.created_at)} · ${i.latency_ms} ms</span></div><div><span class="answer ${i.answer}">${esc(ANSWER_TEXT[i.answer] || i.answer)}</span> ${i.passed === 1 ? badge('通过', 'ok') : i.passed === 0 ? badge('不通过', 'bad') : badge('待复核', 'warn')}</div></div>
      <div class="small" style="margin-top:6px"><span class="muted">问：</span>${esc(i.prompt || '（无 prompt）')}</div>
      <div class="mono small muted" style="margin-top:4px;white-space:pre-wrap">${esc(i.vlm_provider || '')} ⟶ ${esc((i.vlm_raw || '').slice(0, 400))}</div>
      ${i.tts_text ? `<div style="margin-top:8px;display:flex;gap:8px;align-items:center"><span class="muted small">播报：</span><b>${esc(i.tts_text)}</b><button class="btn btn-xs" data-replay="${i.tts_audio_url || ''}" data-text="${esc(i.tts_text)}">▶ 重放</button><span class="small muted">${typeof i.tts_status === 'object' ? Object.entries(i.tts_status || {}).map(([k, v]) => `${k}:${v}`).join(' ') : esc(i.tts_status || '')}</span></div>` : ''}</div></div>`;
}
export function destroy() { offs.forEach(f => f()); offs = []; clearTimeout(timer); if (mc) { mc.destroy(); mc = null; } cur = null; }
