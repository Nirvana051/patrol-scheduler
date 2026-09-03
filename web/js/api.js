// 与后端交互的小工具 + DOM 助手。不依赖任何框架。
export async function api(path, { method = 'GET', body, headers } = {}) {
  const opts = { method, headers: { ...(headers || {}) } };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    if (data && typeof data === 'object') {
      const d = data.detail ?? data.error;
      if (typeof d === 'string') msg = d;
      else if (Array.isArray(d)) msg = d.map(x => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('；');
      else if (d) msg = JSON.stringify(d);
    }
    const err = new Error(msg); err.status = res.status; err.data = data; throw err;
  }
  return data;
}

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export function h(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export const fmt = {
  time(ts) { if (!ts) return '—'; const d = typeof ts === 'number' ? new Date(ts * (ts < 1e12 ? 1000 : 1)) : new Date(ts); if (isNaN(d)) return String(ts); return d.toLocaleTimeString('zh-CN', { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0').slice(0, 1); },
  dt(ts) { if (!ts) return '—'; const d = typeof ts === 'number' ? new Date(ts * (ts < 1e12 ? 1000 : 1)) : new Date(ts); if (isNaN(d)) return String(ts); return d.toLocaleString('zh-CN', { hour12: false }); },
  num(v, d = 2) { return (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(d); },
  deg(rad) { return (rad === null || rad === undefined) ? '—' : (rad * 180 / Math.PI).toFixed(0) + '°'; },
  dur(a, b) { if (!a) return '—'; const t0 = new Date(a), t1 = b ? new Date(b) : new Date(); const s = Math.max(0, (t1 - t0) / 1000); return s < 60 ? `${s.toFixed(0)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`; },
};

export function toast(msg, kind = 'info', ms = 3500) {
  const box = document.getElementById('toasts');
  const el = h(`<div class="toast ${kind}">${esc(msg)}</div>`);
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, ms);
}

export function modal({ title, content, footer, wide = false, onClose }) {
  const root = document.getElementById('modals');
  const back = h(`<div class="modal-back"><div class="modal ${wide ? 'wide' : ''}">
    <div class="modal-head"><h2>${esc(title)}</h2><button class="close-x" title="关闭">×</button></div>
    <div class="modal-body"></div><div class="modal-foot"></div></div></div>`);
  const body = back.querySelector('.modal-body'), foot = back.querySelector('.modal-foot');
  if (typeof content === 'string') body.innerHTML = content; else if (content) body.appendChild(content);
  if (footer) { if (typeof footer === 'string') foot.innerHTML = footer; else foot.appendChild(footer); } else foot.remove();
  const close = () => { back.remove(); onClose && onClose(); };
  back.querySelector('.close-x').onclick = close;
  back.addEventListener('mousedown', e => { if (e.target === back) close(); });
  root.appendChild(back);
  return { el: back, body, foot, close };
}

export function confirmDialog({ title = '确认', body = '', okText = '确定', danger = false }) {
  return new Promise(resolve => {
    const foot = h(`<div style="display:flex;gap:8px"><button class="btn" data-a="no">取消</button><button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-a="ok">${esc(okText)}</button></div>`);
    const m = modal({ title, content: `<div>${body}</div>`, footer: foot, onClose: () => resolve(false) });
    foot.querySelector('[data-a=no]').onclick = () => { m.close(); };
    foot.querySelector('[data-a=ok]').onclick = () => { m.el.remove(); resolve(true); };
  });
}

export async function busy(btn, fn) {
  if (!btn) return fn();
  btn.classList.add('loading'); btn.disabled = true;
  try { return await fn(); } finally { btn.classList.remove('loading'); btn.disabled = false; }
}

export function badge(text, kind = '') { return `<span class="badge ${kind}">${esc(text)}</span>`; }

export const RUN_STATUS = { pending: ['待开始', ''], preflight: ['前置检查', 'info'], running: ['执行中', 'info'], paused: ['已暂停', 'warn'], completed: ['已完成', 'ok'], failed: ['失败', 'bad'], aborted: ['已中止', 'warn'] };
export const LEG_STATUS = { pending: ['待执行', ''], planning: ['规划中', 'info'], dispatched: ['已下发', 'info'], navigating: ['导航中', 'info'], arrived: ['已到达', 'ok'], inspecting: ['检查中', 'info'], done: ['完成', 'ok'], failed: ['失败', 'bad'], skipped: ['已跳过', 'warn'], aborted: ['已中止', 'warn'] };
export function statusBadge(map, s) { const [t, k] = map[s] || [s, '']; return badge(t, k); }
export const ANSWER_TEXT = { yes: '是', no: '不是', unknown: '无法判断', error: '出错' };
