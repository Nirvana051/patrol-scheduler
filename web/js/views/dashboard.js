import { api, h, esc, fmt, toast, busy, badge, statusBadge, RUN_STATUS, ANSWER_TEXT, confirmDialog } from '../api.js';
let offs = [], hls = null, deviceTimer = null;

export async function render(root, { store }) {
  root.innerHTML = `
  <div class="page-head"><h1>总览</h1><div class="actions">
    <button class="btn" id="b-refresh">↻ 刷新状态</button>
    <button class="btn" id="b-snap">📷 抓一张全景</button>
    <a class="btn btn-primary" href="#/tasks">去执行任务 →</a></div></div>
  <div class="grid grid-3">
    <div class="card"><div class="card-head"><h2>机器人</h2><span id="d-online"></span></div><dl class="kv" id="d-robot"></dl></div>
    <div class="card"><div class="card-head"><h2>云端任务</h2><span id="d-tstate"></span></div><dl class="kv" id="d-task"></dl></div>
    <div class="card"><div class="card-head"><h2>当前执行</h2><span id="d-rstate"></span></div><div id="d-run"><div class="muted">没有进行中的执行</div></div></div>
  </div>
  <div class="grid grid-2" style="margin-top:14px">
    <div class="card"><div class="card-head"><h2>机器人初始化（真机必需的 ②③④ 步）</h2><span class="muted small">一次性；不重启、不丢定位就不用重做</span></div>
      <div class="form">
        <div class="form-inline"><button class="btn" id="b-devstart">② 启动设备</button><button class="btn" id="b-devstop">停止设备（收尾 ⑧）</button><span id="dev-msg" class="muted small"></span></div>
        <div class="form-inline"><select id="loc-map" style="max-width:260px"></select><select id="loc-node" style="max-width:140px"></select><button class="btn" id="b-localize">④ 定位</button><span class="muted small">node_id 必须是机器人<b>真实所在</b>的航点</span></div>
        <div id="preflight" class="small"></div>
      </div></div>
    <div class="card"><div class="card-head"><h2>全景画面</h2><span id="video-src" class="muted small"></span></div><div class="video-box" id="video"><span>—</span></div></div>
  </div>
  <div class="card" style="margin-top:14px"><div class="card-head"><h2>最近检查</h2><a href="#/runs" class="small">全部执行 →</a></div><div id="d-insp" class="grid grid-4"></div></div>`;

  const paint = (s) => {
    if (!s) return;
    const info = s.info || {};
    root.querySelector('#d-online').innerHTML = s.reachable === false ? badge('云端不可达', 'bad') : s.online ? badge('在线', 'ok') : badge('离线', 'bad');
    const loc = s.localization || {};
    root.querySelector('#d-robot').innerHTML = `
      <dt>别名 / ID</dt><dd class="mono">${esc(info.alias || s.robot)} / ${esc(info.robotId || '—')}</dd>
      <dt>云端</dt><dd class="mono">${esc(s.host || '')}</dd>
      <dt>位姿</dt><dd class="mono">${s.position ? `x=${fmt.num(s.position.x)} y=${fmt.num(s.position.y)} yaw=${fmt.deg(s.position.yaw)}` : '<span class="muted">未就绪（/position 503）</span>'}</dd>
      <dt>定位</dt><dd>${loc.received ? `${loc.fresh ? badge('新鲜', 'ok') : badge('陈旧', 'warn')} 数据年龄 ${loc.age ?? '—'} s` : badge('从未收到', 'warn')} <span class="muted small">按 received_at 判断，不看 Location</span></dd>
      <dt>急停</dt><dd>${s.emergency_active ? badge('急停指令下发中', 'bad') : badge('无', 'ok')}</dd>
      <dt>ROS</dt><dd>${s.ros_available === false ? badge('不可用（数据陈旧）', 'bad') : badge('可用', 'ok')}</dd>
      <dt>控制权</dt><dd>${s.lease ? `${esc(s.lease.owner)} <span class="muted small">至 ${fmt.time(s.lease.expiresAt)}</span>` : '<span class="muted">空闲</span>'}</dd>
      <dt>感知</dt><dd class="small">${s.perception ? `Location=${s.perception.Location}（${esc(s.perception.location_text || '')}，已知恒为 1） · ${esc(s.perception.obs_text || '')}` : '—'}</dd>
      <dt>事件流</dt><dd class="small">${s.events ? `${s.events.connected ? badge(s.events.transport === 'sse' ? 'SSE 已连' : '轮询中', 'ok') : badge('未连接', 'warn')} 游标 ${s.events.cursor ?? '—'} · 已收 ${s.events.received}` : '—'}</dd>`;
    const t = s.task || {};
    root.querySelector('#d-tstate').innerHTML = t.status_name ? badge(`${t.status_name} · ${t.status_text || ''}`, t.active ? 'info' : t.status_code === 255 ? 'bad' : t.status_code === 4 ? 'ok' : '') : '';
    root.querySelector('#d-task').innerHTML = `
      <dt>status</dt><dd class="mono">${esc(t.status ?? '—')} <span class="muted">(code ${t.status_code ?? '—'}, active=${t.active}, terminal=${t.terminal})</span></dd>
      <dt>地图</dt><dd class="mono">${esc(t.map_name || '—')}</dd>
      <dt>路径</dt><dd class="mono">${(t.path || []).join(' → ') || '—'}</dd>
      <dt>进度</dt><dd>${t.progress ? `${t.progress.visited}/${t.progress.total}` : '—'} · 目标 <b>${esc(t.current_target || '—')}</b></dd>
      <dt>错误码</dt><dd>${t.error_code ? badge(`${t.error_hex} ${t.error_name} ${t.error_text}`, 'bad') : badge('0x0000 无错误', 'ok')}</dd>
      <dt>步态/速度</dt><dd class="small">${esc(t.gait_name || '—')} / ${esc(t.speed_name || '—')} · 避障 ${esc(t.obs_mode_name || '—')}</dd>`;
    root.querySelector('#video-src').textContent = s.snapshot_source || '';
  };
  paint(store.status);
  offs.push(store.on('robot_status', paint));

  const paintRun = async (info) => {
    const box = root.querySelector('#d-run'), st = root.querySelector('#d-rstate');
    if (!info) { box.innerHTML = '<div class="muted">没有进行中的执行</div>'; st.innerHTML = ''; return; }
    try {
      const r = await api(`/api/runs/${info.run_id}`);
      st.innerHTML = statusBadge(RUN_STATUS, r.status);
      const done = r.legs.filter(l => l.status === 'done').length;
      box.innerHTML = `<div><b>${esc(r.task_name)}</b> <span class="muted small">#${r.id} · ${esc(r.map_name)}</span></div>
        <div class="progress" style="margin:8px 0"><i style="width:${r.total_legs ? done / r.total_legs * 100 : 0}%"></i></div>
        <div class="small">第 ${r.current_leg}/${r.total_legs} 段 · 开始 ${fmt.time(r.started_at)} · 用时 ${fmt.dur(r.started_at, r.ended_at)}</div>
        <div style="margin-top:8px"><a class="btn btn-sm btn-primary" href="#/runs/${r.id}">打开执行监控 →</a></div>`;
    } catch (e) { box.innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
  };
  paintRun(store.activeRun);
  offs.push(store.on('active_run', paintRun));
  offs.push(store.on('leg', () => paintRun(store.activeRun)));

  const paintInsp = async () => {
    try {
      const { items } = await api('/api/inspections?limit=4');
      root.querySelector('#d-insp').innerHTML = items.length ? items.map(i => `<div class="card tight">
        ${i.crop_url ? `<img src="${i.crop_url}" style="width:100%;height:90px;object-fit:cover;border-radius:6px">` : ''}
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px"><b>${esc(i.waypoint_name)}</b><span class="answer ${i.answer}" style="font-size:16px">${esc(ANSWER_TEXT[i.answer] || i.answer)}</span></div>
        <div class="small muted">${esc((i.prompt || '').slice(0, 40))}</div>
        <div class="small">${i.passed === 1 ? badge('通过', 'ok') : i.passed === 0 ? badge('不通过', 'bad') : badge('待复核', 'warn')} <span class="muted">${fmt.time(i.created_at)}</span></div>
      </div>`).join('') : '<div class="muted small">还没有检查记录</div>';
    } catch { /* ignore */ }
  };
  paintInsp();
  offs.push(store.on('inspection', paintInsp));

  // 初始化面板
  const mapSel = root.querySelector('#loc-map'), nodeSel = root.querySelector('#loc-node');
  try {
    const { maps } = await api('/api/maps');
    mapSel.innerHTML = maps.map(m => `<option value="${esc(m.name)}" ${m.waypoint_count ? '' : 'disabled'}>${esc(m.name)}${m.waypoint_count ? ` (${m.waypoint_count})` : '（未同步）'}</option>`).join('') || '<option value="">（没有地图）</option>';
    const loadNodes = async () => { if (!mapSel.value) return; try { const d = await api(`/api/maps/${encodeURIComponent(mapSel.value)}/waypoints`); nodeSel.innerHTML = d.waypoints.map(w => `<option value="${esc(w.node_id)}">航点 ${esc(w.node_id)}</option>`).join(''); } catch { nodeSel.innerHTML = '<option value="">先同步地图</option>'; } };
    mapSel.onchange = loadNodes; await loadNodes();
  } catch (e) { mapSel.innerHTML = `<option>${esc(e.message)}</option>`; }

  const devMsg = root.querySelector('#dev-msg');
  const pollDevice = (taskId, starting) => {
    clearInterval(deviceTimer);
    deviceTimer = setInterval(async () => {
      try {
        const st = await api(`/api/robot/init/device-status?task_id=${encodeURIComponent(taskId)}&starting=${starting}`);
        devMsg.textContent = st.completed ? (st.result_success === false ? '脚本跑完但报告失败（看 result_success）' : (starting ? '设备启动完成 ✔' : '设备已停止 ✔')) : `脚本执行中…（${esc(st.message || '')}）`;
        if (st.completed) clearInterval(deviceTimer);
      } catch (e) { devMsg.textContent = e.message; clearInterval(deviceTimer); }
    }, 2000);
  };
  root.querySelector('#b-devstart').onclick = async (e) => {
    if (store.isReal && !(await confirmDialog({ title: '启动设备', okText: '启动', body: '将在机器人上执行启动脚本（拉起导航、定位等模块）。' }))) return;
    try { const r = await busy(e.currentTarget, () => api('/api/robot/init/device-start', { method: 'POST', body: { wait: false } })); devMsg.textContent = `已下发，task_id=${r.task_id}，等待脚本完成…`; pollDevice(r.task_id, true); } catch (err) { toast(err.message, 'bad'); }
  };
  root.querySelector('#b-devstop').onclick = async (e) => {
    if (!(await confirmDialog({ title: '停止设备', okText: '停止设备', danger: true, body: '收尾 ⑧：停设备会关掉导航/定位模块，之后需要重新初始化才能巡检。确定？' }))) return;
    try { const r = await busy(e.currentTarget, () => api('/api/robot/init/device-stop', { method: 'POST', body: { wait: false } })); devMsg.textContent = `已下发停止，task_id=${r.task_id}…`; pollDevice(r.task_id, false); } catch (err) { toast(err.message, 'bad'); }
  };
  root.querySelector('#b-localize').onclick = async (e) => {
    if (!mapSel.value || !nodeSel.value) return toast('先选地图与航点', 'warn');
    if (store.isReal && !(await confirmDialog({ title: '定位', okText: '开始定位', body: `以航点 <b>${esc(nodeSel.value)}</b> 为初值做全局定位。<br><b>请确认机器人此刻确实在这个航点附近</b>：给错了要么偏差超 3m 判失败，要么收敛到错的位置，后面巡检会朝错误方向走。` }))) return;
    devMsg.textContent = '定位中（服务端最长同步等 20s）…';
    try { const r = await busy(e.currentTarget, () => api('/api/robot/init/localize', { method: 'POST', body: { map_name: mapSel.value, node_id: nodeSel.value } })); devMsg.textContent = `定位成功，偏差 ${r.drift} m（阈值 ${r.threshold} m）`; toast('定位成功', 'ok'); }
    catch (err) { devMsg.textContent = ''; toast(`定位失败：${err.message}`, 'bad', 6000); }
  };
  root.querySelector('#b-refresh').onclick = async (e) => { try { const pf = await busy(e.currentTarget, () => api('/api/robot/preflight')); root.querySelector('#preflight').innerHTML = `<b>执行前置检查：</b>${pf.ok ? badge('全部通过', 'ok') : badge('未通过', 'bad')}<ul class="checks">${pf.checks.map(c => `<li>${c.ok ? '✅' : '❌'} ${esc(c.text)}</li>`).join('')}</ul>`; } catch (err) { toast(err.message, 'bad'); } };
  root.querySelector('#b-snap').onclick = async (e) => { try { const r = await busy(e.currentTarget, () => api('/api/robot/snapshot', { method: 'POST' })); showImage(r.url, `${r.width}×${r.height} · ${r.source}`); toast('已抓取一张全景', 'ok'); } catch (err) { toast(`抓帧失败：${err.message}`, 'bad', 6000); } };

  // 视频
  const vbox = root.querySelector('#video');
  const showImage = (url, cap) => { vbox.innerHTML = `<img src="${url}" alt="全景">`; root.querySelector('#video-src').textContent = cap || ''; };
  try {
    const v = await api('/api/robot/video');
    if (v.mode === 'real' && v.hls) {
      vbox.innerHTML = '<video controls muted autoplay playsinline></video>';
      const video = vbox.querySelector('video');
      if (video.canPlayType('application/vnd.apple.mpegurl')) video.src = v.hls;
      else { const { default: Hls } = await import('/vendor/hls.min.js').catch(() => ({ default: window.Hls })); const H = Hls || window.Hls; if (H && H.isSupported()) { hls = new H(); hls.loadSource(v.hls); hls.attachMedia(video); hls.on(H.Events.ERROR, () => { vbox.innerHTML = `<span>HLS 无画面（${esc(v.hls)}）—— 见 video.md 排查顺序</span>`; }); } }
      root.querySelector('#video-src').textContent = v.hls;
    } else {
      vbox.innerHTML = `<span>${v.mode === 'mock' ? 'MOCK 模式没有实时流 —— 点「抓一张全景」看合成画面' : '没有 rtspPath'}</span>`;
    }
  } catch (e) { vbox.innerHTML = `<span>${esc(e.message)}</span>`; }
}

export function destroy() { offs.forEach(f => f()); offs = []; clearInterval(deviceTimer); if (hls) { try { hls.destroy(); } catch { /* ignore */ } hls = null; } }
