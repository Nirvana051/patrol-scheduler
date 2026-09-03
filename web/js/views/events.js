import { api, h, esc, fmt, badge, toast } from '../api.js';
let offs = [], filters = { source: '', type: '', level: '', run_id: '', q: '' }, oldest = null;

export async function render(root, { store }) {
  root.innerHTML = `<div class="page-head"><h1>任务事件</h1><div class="actions form-inline">
    <select id="f-source" style="width:110px"><option value="">全部来源</option><option value="cloud">云端</option><option value="system">系统</option></select>
    <select id="f-type" style="width:180px"><option value="">全部类型</option></select>
    <select id="f-level" style="width:110px"><option value="">全部级别</option><option value="info">info</option><option value="warn">warn</option><option value="error">error</option></select>
    <input id="f-run" placeholder="执行 #" style="width:90px"><input id="f-q" placeholder="搜索…" style="width:160px"><button class="btn" id="b-apply">筛选</button></div></div>
  <div class="help" style="margin-bottom:8px">云端事件（到达航点/任务完成/失败/避障/丢定位/急停/上下线）与系统事件（下发、抓图、VLM、TTS、设置变更…）统一时间线，实时追加；云端只留 500 条内存，这里才是持久记录。</div>
  <div class="card tight"><div id="rows"></div><div style="text-align:center;padding:8px"><button class="btn btn-sm" id="b-more">加载更早的</button></div></div>`;
  const rows = root.querySelector('#rows');
  const row = e => `<div class="event-row ${e.level}" data-id="${e.id}"><span class="t">${fmt.dt(e.ts)}</span><span>${badge(e.source === 'cloud' ? '云端' : '系统', e.source === 'cloud' ? 'info' : '')}</span><span class="mono small">${esc(e.type)}${e.cloud_seq ? ` <span class="muted">#${e.cloud_seq}</span>` : ''}${e.run_id ? ` <a href="#/runs/${e.run_id}" class="small">run ${e.run_id}</a>` : ''}</span><span title="${esc(JSON.stringify(e.data))}">${esc(e.message)}</span></div>`;
  const qs = (extra = {}) => Object.entries({ ...filters, ...extra }).filter(([, v]) => v !== '' && v != null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');
  const load = async (more = false) => {
    const d = await api(`/api/events?limit=100&${qs(more && oldest ? { before_id: oldest } : {})}`);
    if (!more) { rows.innerHTML = ''; const ts = root.querySelector('#f-type'); const cur = ts.value; ts.innerHTML = '<option value="">全部类型</option>' + d.types.map(t => `<option value="${esc(t)}" ${t === cur ? 'selected' : ''}>${esc(t)}</option>`).join(''); }
    rows.insertAdjacentHTML('beforeend', d.items.map(row).join('') || (more ? '' : '<div class="empty">没有事件</div>'));
    if (d.items.length) oldest = d.items[d.items.length - 1].id;
    root.querySelector('#b-more').disabled = d.items.length < 100;
  };
  root.querySelector('#b-apply').onclick = () => { filters = { source: root.querySelector('#f-source').value, type: root.querySelector('#f-type').value, level: root.querySelector('#f-level').value, run_id: root.querySelector('#f-run').value, q: root.querySelector('#f-q').value }; oldest = null; load(); };
  root.querySelector('#b-more').onclick = () => load(true);
  offs.push(store.on('event', e => { if (filters.source && e.source !== filters.source) return; if (filters.type && e.type !== filters.type) return; if (filters.level && e.level !== filters.level) return; if (filters.run_id && String(e.run_id) !== filters.run_id) return; rows.insertAdjacentHTML('afterbegin', row(e)); }));
  await load();
}
export function destroy() { offs.forEach(f => f()); offs = []; oldest = null; }
