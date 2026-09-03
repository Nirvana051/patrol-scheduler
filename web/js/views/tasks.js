import { api, h, esc, fmt, toast, busy, badge, modal, confirmDialog, statusBadge, RUN_STATUS } from '../api.js';
import { openWaypointEditor } from './waypoints.js';
let offs = [];

export async function render(root, { store }) {
  root.innerHTML = `<div class="page-head"><h1>任务规划</h1><div class="actions"><button class="btn btn-primary" id="b-new">＋ 新建任务</button></div></div>
  <div class="help" style="margin-bottom:10px">任务 = 地图 + 有序的任务航点 + 执行选项。执行时按「当前航点 → 下一个任务航点」分段下发云端任务（机器狗不能在航点暂停），每段到达后做检查。</div>
  <div class="table-wrap"><table><thead><tr><th>#</th><th>任务</th><th>地图</th><th>航点数</th><th>速度/步态/避障</th><th>最近执行</th><th></th></tr></thead><tbody id="rows"></tbody></table></div>`;
  const codes = store.statusCodes;
  const nameOf = (group, v) => { const arr = codes?.control?.[group] || []; const f = arr.find(x => x.code === Number(v)); return f ? f.zh : (v ?? '默认'); };
  const load = async () => {
    const { items } = await api('/api/tasks');
    const tb = root.querySelector('#rows');
    tb.innerHTML = items.length ? items.map(t => `<tr data-id="${t.id}"><td class="muted">${t.id}</td><td><b>${esc(t.name)}</b><div class="small muted">${esc(t.description || '')}</div></td><td class="mono small">${esc(t.map_name)}</td><td>${t.item_count}</td>
      <td class="small">${t.options.speed == null ? '默认' : nameOf('speed', t.options.speed)} / ${t.options.gait == null ? '默认' : nameOf('gait', t.options.gait)} / ${t.options.obs_mode == null ? '默认' : nameOf('obsMode', t.options.obs_mode)}</td>
      <td>${t.last_status ? `${statusBadge(RUN_STATUS, t.last_status)} <span class="small muted">${fmt.dt(t.last_started)}</span>` : '<span class="muted">—</span>'}</td>
      <td class="right" style="white-space:nowrap"><button class="btn btn-xs btn-primary" data-a="run">▶ 执行</button> <button class="btn btn-xs" data-a="plan">路线</button> <button class="btn btn-xs" data-a="edit">编辑</button> <button class="btn btn-xs btn-danger" data-a="del">删除</button></td></tr>`).join('') : '<tr><td colspan="7" class="empty">还没有任务。先在「任务航点」页定义巡检点，再新建任务把它们按顺序加进来。</td></tr>';
    tb.querySelectorAll('tr[data-id]').forEach(tr => {
      const id = Number(tr.dataset.id);
      tr.querySelector('[data-a=edit]').onclick = async () => openTaskEditor(await api(`/api/tasks/${id}`), load);
      tr.querySelector('[data-a=plan]').onclick = () => showPlan(id);
      tr.querySelector('[data-a=run]').onclick = (e) => runTask(id, e.currentTarget, store);
      tr.querySelector('[data-a=del]').onclick = async () => { if (!(await confirmDialog({ title: '删除任务', danger: true, okText: '删除', body: '删除该任务（不会删除任务航点，执行记录保留）？' }))) return; try { await api(`/api/tasks/${id}`, { method: 'DELETE' }); load(); } catch (e) { toast(e.message, 'bad'); } };
    });
  };
  root.querySelector('#b-new').onclick = () => openTaskEditor(null, load);
  offs.push(store.on('run', load));
  await load();
}
export function destroy() { offs.forEach(f => f()); offs = []; }

async function runTask(id, btn, store) {
  let plan; try { plan = await api(`/api/tasks/${id}/plan`); } catch (e) { return toast(`无法规划：${e.message}`, 'bad', 6000); }
  const warn = plan.unreachable.length ? `<div class="badge bad">第 ${plan.unreachable.join(', ')} 段不连通，执行会在该段失败</div>` : '';
  const ok = await confirmDialog({ title: store.isReal ? '⚠ 真机执行确认' : '执行任务', okText: '开始执行', danger: store.isReal,
    body: `${store.isReal ? '<p><b>这会让真实机器人走起来。</b>确认现场没人在路径上、有人能按物理急停。</p>' : ''}<p>起点 ${esc(plan.start_node || '—')}${plan.start_note ? `（${esc(plan.start_note)}）` : ''}，共 ${plan.legs.length} 段，约 ${plan.total_length} m。</p>${warn}<div class="mono small muted">${plan.legs.map(l => `${l.seq}. ${esc(l.name)}: ${l.path ? l.path.join('→') : '不可达'}`).join('<br>')}</div>` });
  if (!ok) return;
  try { const r = await busy(btn, () => api(`/api/tasks/${id}/run`, { method: 'POST' })); toast(`已开始执行 #${r.id}`, 'ok'); location.hash = `#/runs/${r.id}`; } catch (e) { toast(e.message, 'bad', 7000); }
}

async function showPlan(id) {
  try {
    const p = await api(`/api/tasks/${id}/plan`);
    modal({ title: '路线预览', wide: false, content: `<div class="small muted" style="margin-bottom:8px">起点 ${esc(p.start_node || '—')} ${p.start_note ? `· ${esc(p.start_note)}` : ''} · 总长约 <b>${p.total_length} m</b></div>
      <table><thead><tr><th>段</th><th>任务航点</th><th>从 → 到</th><th>路径</th><th>长度</th></tr></thead><tbody>${p.legs.map(l => `<tr><td>${l.seq}</td><td>${esc(l.name)}</td><td class="mono">${esc(l.from_node ?? '?')} → ${esc(l.to_node)}</td><td class="mono small">${l.path ? l.path.join(' → ') : badge('不可达', 'bad')}</td><td>${l.length ?? '—'} m</td></tr>`).join('')}</tbody></table>` });
  } catch (e) { toast(e.message, 'bad'); }
}

export async function openTaskEditor(task, onSaved) {
  const t = task || { name: '', map_name: '', description: '', options: {}, items: [] };
  const o = { settle_seconds: 2, leg_timeout: 600, not_started_timeout: 25, offline_timeout: 90, max_retries: 1, return_to_start: false, require_localized: true, speed: null, gait: null, obs_mode: null, ...t.options };
  let maps = [], tws = [];
  try { maps = (await api('/api/maps')).maps; } catch { /* ignore */ }
  const store = (await import('../app.js')).store; const codes = store.statusCodes?.control || {};
  const opt = (group, cur) => `<option value="" ${cur == null ? 'selected' : ''}>机器人默认</option>` + (codes[group] || []).map(v => `<option value="${v.code}" ${Number(cur) === v.code ? 'selected' : ''}>${esc(v.zh)} (${v.code})</option>`).join('');
  const body = h(`<div class="split-r" style="grid-template-columns:1fr 1fr">
    <div class="form">
      <div class="form-row"><label>名称</label><input id="t-name" value="${esc(t.name)}" placeholder="如：夜间一层巡检"></div>
      <div class="form-row"><label>地图</label><select id="t-map">${maps.map(m => `<option value="${esc(m.name)}" ${m.name === t.map_name ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}</select></div>
      <div class="form-row"><label>说明</label><input id="t-desc" value="${esc(t.description || '')}"></div>
      <fieldset><legend>执行选项</legend><div class="form-grid">
        <label class="small">速度<select id="o-speed">${opt('speed', o.speed)}</select></label><label class="small">步态<select id="o-gait">${opt('gait', o.gait)}</select></label>
        <label class="small">避障<select id="o-obs">${opt('obsMode', o.obs_mode)}</select></label><label class="small">到点稳定 (s)<input id="o-settle" type="number" step="0.5" value="${o.settle_seconds}"></label>
        <label class="small">每段超时 (s)<input id="o-timeout" type="number" value="${o.leg_timeout}"></label><label class="small">段失败重试次数<input id="o-retries" type="number" min="0" value="${o.max_retries}"></label>
        <label class="small" title="下发后机器人一直不动，多久判「任务未执行」">未启动判定 (s)<input id="o-notstarted" type="number" value="${o.not_started_timeout}"></label><label class="small" title="机器人掉线/云端不可达多久判段失败">掉线判定 (s)<input id="o-offline" type="number" value="${o.offline_timeout}"></label>
      </div><div class="form-inline" style="margin-top:8px"><label><input type="checkbox" id="o-return" ${o.return_to_start ? 'checked' : ''}> 结束后返回起点</label><label><input type="checkbox" id="o-reqloc" ${o.require_localized !== false ? 'checked' : ''}> 执行前要求定位就绪（真机务必勾选）</label></div></fieldset>
    </div>
    <div><div class="card-head"><h2>任务航点顺序</h2><div class="form-inline"><select id="t-add" style="width:220px"></select><button class="btn btn-sm" id="b-add">加入</button><button class="btn btn-sm" id="b-new-tw" title="在当前地图上新建一个任务航点并加入本任务">＋ 新建任务航点</button></div></div>
      <ol class="sortable" id="t-items"></ol><div class="help">上下箭头调整顺序。执行时按此顺序逐段前往。</div></div></div>`);
  const foot = h(`<div style="display:flex;gap:8px"><button class="btn" id="t-cancel">取消</button><button class="btn btn-primary" id="t-save">${t.id ? '保存' : '创建'}</button></div>`);
  const m = modal({ title: t.id ? `编辑任务 #${t.id}` : '新建任务', content: body, footer: foot, wide: true });
  const $ = s => body.querySelector(s);
  let items = (t.items || []).map(i => ({ id: i.task_waypoint_id, name: i.name, nav_node_id: i.nav_node_id }));
  const loadTws = async () => { try { tws = (await api(`/api/task-waypoints?map_name=${encodeURIComponent($('#t-map').value)}`)).items; } catch { tws = []; } $('#t-add').innerHTML = tws.length ? tws.map(w => `<option value="${w.id}">${esc(w.name)}（航点 ${esc(w.nav_node_id || '手工')}）</option>`).join('') : '<option value="">该地图还没有任务航点</option>'; };
  const paint = () => { $('#t-items').innerHTML = items.length ? items.map((it, i) => `<li><span class="muted" style="width:22px">${i + 1}.</span><b style="flex:1">${esc(it.name)}</b><span class="small muted">航点 ${esc(it.nav_node_id || '手工')}</span><button class="btn btn-xs" data-i="${i}" data-a="up" ${i === 0 ? 'disabled' : ''}>↑</button><button class="btn btn-xs" data-i="${i}" data-a="down" ${i === items.length - 1 ? 'disabled' : ''}>↓</button><button class="btn btn-xs btn-danger" data-i="${i}" data-a="rm">×</button></li>`).join('') : '<li class="muted">（空）从右上角下拉加入任务航点</li>';
    $('#t-items').querySelectorAll('button').forEach(b => b.onclick = () => { const i = Number(b.dataset.i); if (b.dataset.a === 'rm') items.splice(i, 1); if (b.dataset.a === 'up') [items[i - 1], items[i]] = [items[i], items[i - 1]]; if (b.dataset.a === 'down') [items[i + 1], items[i]] = [items[i], items[i + 1]]; paint(); }); };
  $('#t-map').onchange = loadTws; await loadTws(); paint();
  $('#b-new-tw').onclick = () => openWaypointEditor({ map_name: $('#t-map').value }, async (w) => { await loadTws(); if (w && w.id) { items.push({ id: w.id, name: w.name, nav_node_id: w.nav_node_id }); paint(); } });
  $('#b-add').onclick = () => { const w = tws.find(x => String(x.id) === $('#t-add').value); if (w) { items.push({ id: w.id, name: w.name, nav_node_id: w.nav_node_id }); paint(); } };
  foot.querySelector('#t-cancel').onclick = () => m.close();
  foot.querySelector('#t-save').onclick = async (e) => {
    const d = { name: $('#t-name').value.trim(), map_name: $('#t-map').value, description: $('#t-desc').value, waypoint_ids: items.map(i => i.id),
      options: { speed: $('#o-speed').value === '' ? null : Number($('#o-speed').value), gait: $('#o-gait').value === '' ? null : Number($('#o-gait').value), obs_mode: $('#o-obs').value === '' ? null : Number($('#o-obs').value),
        settle_seconds: Number($('#o-settle').value), leg_timeout: Number($('#o-timeout').value), not_started_timeout: Number($('#o-notstarted').value), offline_timeout: Number($('#o-offline').value), max_retries: Number($('#o-retries').value), return_to_start: $('#o-return').checked, require_localized: $('#o-reqloc').checked } };
    if (!d.name) return toast('名称必填', 'warn'); if (!d.waypoint_ids.length) return toast('至少加入一个任务航点', 'warn');
    try { await busy(e.currentTarget, () => t.id ? api(`/api/tasks/${t.id}`, { method: 'PUT', body: d }) : api('/api/tasks', { method: 'POST', body: d })); toast('已保存', 'ok'); m.close(); onSaved && onSaved(); } catch (err) { toast(err.message, 'bad', 5000); }
  };
}
