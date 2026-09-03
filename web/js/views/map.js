import { api, h, esc, fmt, toast, busy, badge } from '../api.js';
import { MapCanvas } from '../components/map_canvas.js';
import { openWaypointEditor } from './waypoints.js';
let offs = [], mc = null, state = { map: null, data: null, selected: null, tws: [] };

export async function render(root, { store }) {
  root.innerHTML = `
  <div class="page-head"><h1>地图与导航航点</h1><div class="actions"><span class="muted small">导航航点来自云端 GET /maps/{name}/waypoints，同步进本地缓存；任务航点在其上定义</span></div></div>
  <div class="split">
    <div>
      <div class="card"><div class="card-head"><h2>地图</h2><button class="btn btn-sm" id="b-reload">↻</button></div><div class="list" id="maps"></div></div>
      <div class="card" style="margin-top:14px"><h2>选中的导航航点</h2><div id="sel" class="muted small">在地图上点一个航点</div></div>
      <div class="card" style="margin-top:14px"><h2>路线预览</h2><div class="form-inline"><input id="r-from" placeholder="从" style="width:80px"><span>→</span><input id="r-to" placeholder="到" style="width:80px"><button class="btn btn-sm" id="b-route">算路</button></div><div id="route-out" class="small" style="margin-top:6px"></div></div>
      <div class="card" style="margin-top:14px"><h2>点云背景（下采样接口）</h2><div class="form-inline"><label class="small">体素 <input id="voxel" type="number" step="0.05" value="0.3" style="width:80px"> m</label><button class="btn btn-sm" id="b-cloud">加载</button><button class="btn btn-sm" id="b-cloud-off">隐藏</button></div>
        <div class="form-inline" style="margin-top:6px"><input type="file" id="pcd" accept=".pcd,.ply" style="width:auto"><button class="btn btn-sm" id="b-upload">上传</button></div><div id="cloud-out" class="help">云端暂不提供点云下载，先手工上传 .pcd/.ply（TODO T4）</div></div>
    </div>
    <div class="mapwrap" style="height:calc(100vh - 140px);min-height:520px"><canvas class="map"></canvas>
      <div class="map-toolbar"><button class="btn btn-sm" id="b-fit">适应视图</button><button class="btn btn-sm" id="b-trail">清轨迹</button></div>
      <div class="map-legend"><span>● 导航航点</span><span style="color:#ff9f1a">━ 路径</span><span style="color:#c98700">◆ 任务航点</span><span style="color:#1f5eff">➤ 机器人</span></div></div>
  </div>`;
  const canvas = root.querySelector('canvas.map');
  mc = new MapCanvas(canvas, { onHover: () => {}, onClick: (w) => { if (w) select(w); } });
  root.querySelector('#b-fit').onclick = () => { mc.fit(); mc.draw(); };
  root.querySelector('#b-trail').onclick = () => mc.clearTrail();
  offs.push(store.on('robot_status', s => { if (s && s.position) mc.setRobot(s.position); }));
  if (store.status?.position) mc.setRobot(store.status.position);

  const listEl = root.querySelector('#maps');
  const loadMaps = async () => {
    try {
      const { maps, cloud_error } = await api('/api/maps');
      listEl.innerHTML = maps.map(m => `<div class="item ${m.name === state.map ? 'active' : ''}" data-m="${esc(m.name)}"><div><div class="mono">${esc(m.name)}</div><div class="small muted">${m.waypoint_count ? `${m.waypoint_count} 个航点 · ${fmt.dt(m.synced_at)}` : '未同步'}${m.on_cloud ? '' : ' · 云端已无此图'}${m.has_pointcloud ? ' · 有点云' : ''}</div></div><button class="btn btn-xs" data-sync="${esc(m.name)}" ${m.on_cloud ? '' : 'disabled'}>同步</button></div>`).join('') + (cloud_error ? `<div class="small muted">云端列表读取失败：${esc(cloud_error)}</div>` : '');
      listEl.querySelectorAll('[data-sync]').forEach(b => b.onclick = async (e) => { e.stopPropagation(); try { const r = await busy(b, () => api(`/api/maps/${encodeURIComponent(b.dataset.sync)}/sync`, { method: 'POST' })); toast(`已同步 ${r.count} 个导航航点`, 'ok'); await loadMaps(); await openMap(b.dataset.sync); } catch (err) { toast(err.message, 'bad'); } });
      listEl.querySelectorAll('.item').forEach(it => it.onclick = () => openMap(it.dataset.m));
      if (!state.map) { const first = maps.find(m => m.waypoint_count) || maps[0]; if (first) openMap(first.name); }
    } catch (e) { listEl.innerHTML = `<div class="muted small">${esc(e.message)}</div>`; }
  };
  root.querySelector('#b-reload').onclick = loadMaps;

  const openMap = async (name) => {
    state.map = name; listEl.querySelectorAll('.item').forEach(i => i.classList.toggle('active', i.dataset.m === name));
    try {
      state.data = await api(`/api/maps/${encodeURIComponent(name)}/waypoints`);
      mc.setData(state.data);
      const { items } = await api(`/api/task-waypoints?map_name=${encodeURIComponent(name)}`); state.tws = items; mc.setTaskWaypoints(items);
      const mrow = [...listEl.querySelectorAll('.item')].find(i => i.dataset.m === name);
      if (mrow && mrow.textContent.includes('有点云')) root.querySelector('#b-cloud').click(); else mc.setPointCloud(null);
      root.querySelector('#sel').innerHTML = `<div class="small">${state.data.waypoints.length} 个航点，${state.data.edges.length} 条边，${state.data.components} 个连通块 ${state.data.components > 1 ? badge('拓扑不连通！', 'warn') : ''}</div><div class="muted small">点一个航点查看/添加为任务航点</div>`;
    } catch (e) { mc.setData({ waypoints: [], edges: [], bounds: null }); root.querySelector('#sel').innerHTML = `<span class="muted">${esc(e.message)}</span>`; }
  };
  const select = (w) => {
    state.selected = w; mc.setSelected(w.node_id);
    const used = state.tws.filter(t => t.nav_node_id === w.node_id);
    root.querySelector('#sel').innerHTML = `<dl class="kv"><dt>航点</dt><dd><b>${esc(w.node_id)}</b></dd><dt>x / y / z</dt><dd class="mono">${fmt.num(w.x)} / ${fmt.num(w.y)} / ${fmt.num(w.z)}</dd><dt>yaw</dt><dd class="mono">${fmt.deg(w.yaw)}（${fmt.num(w.yaw, 3)} rad）</dd><dt>邻居</dt><dd class="mono">${(w.neighbors || []).join(', ')}</dd><dt>任务航点</dt><dd>${used.length ? used.map(t => `<a href="#/waypoints/${t.id}">${esc(t.name)}</a>`).join('、') : '<span class="muted">无</span>'}</dd></dl>
      <div class="form-inline" style="margin-top:8px"><button class="btn btn-primary btn-sm" id="b-add">⚑ 添加为任务航点</button><button class="btn btn-sm" id="b-locate">设为定位初值</button><button class="btn btn-sm" id="b-rfrom">路线起点</button><button class="btn btn-sm" id="b-rto">路线终点</button></div>`;
    root.querySelector('#b-add').onclick = () => openWaypointEditor({ map_name: state.map, nav_node_id: w.node_id, x: w.x, y: w.y, z: w.z, yaw: w.yaw, name: `航点${w.node_id}` }, async () => { const { items } = await api(`/api/task-waypoints?map_name=${encodeURIComponent(state.map)}`); state.tws = items; mc.setTaskWaypoints(items); select(w); });
    root.querySelector('#b-locate').onclick = async (e) => { if (!confirm(`以航点 ${w.node_id} 为初值做定位？请确认机器人真实在此航点附近。`)) return; try { const r = await busy(e.currentTarget, () => api('/api/robot/init/localize', { method: 'POST', body: { map_name: state.map, node_id: w.node_id } })); toast(`定位成功，偏差 ${r.drift} m`, 'ok'); } catch (err) { toast(`定位失败：${err.message}`, 'bad', 6000); } };
    root.querySelector('#b-rfrom').onclick = () => { root.querySelector('#r-from').value = w.node_id; };
    root.querySelector('#b-rto').onclick = () => { root.querySelector('#r-to').value = w.node_id; root.querySelector('#b-route').click(); };
  };
  root.querySelector('#b-route').onclick = async () => {
    const a = root.querySelector('#r-from').value.trim(), b = root.querySelector('#r-to').value.trim(); if (!a || !b || !state.map) return;
    try { const r = await api(`/api/maps/${encodeURIComponent(state.map)}/route?from_node=${encodeURIComponent(a)}&to_node=${encodeURIComponent(b)}`); if (!r.reachable) { root.querySelector('#route-out').innerHTML = badge('不连通', 'bad'); mc.setPath([]); } else { mc.setPath(r.path); root.querySelector('#route-out').innerHTML = `${r.path.length} 个点 · ${r.length} m<div class="mono muted">${r.path.join(' → ')}</div>`; } } catch (e) { toast(e.message, 'bad'); }
  };
  root.querySelector('#b-cloud').onclick = async (e) => { if (!state.map) return; try { const d = await busy(e.currentTarget, () => api(`/api/maps/${encodeURIComponent(state.map)}/pointcloud?voxel=${root.querySelector('#voxel').value}`)); mc.setPointCloud(d.points); root.querySelector('#cloud-out').textContent = `${d.source_count} → ${d.count} 点（体素 ${d.voxel.toFixed(2)} m）`; } catch (err) { root.querySelector('#cloud-out').textContent = err.message; } };
  root.querySelector('#b-cloud-off').onclick = () => mc.setPointCloud(null);
  root.querySelector('#b-upload').onclick = async (e) => { const f = root.querySelector('#pcd').files[0]; if (!f || !state.map) return toast('先选文件和地图', 'warn'); const fd = new FormData(); fd.append('file', f); try { const r = await busy(e.currentTarget, () => api(`/api/maps/${encodeURIComponent(state.map)}/pointcloud`, { method: 'POST', body: fd })); toast(`上传成功，${r.points} 点`, 'ok'); root.querySelector('#b-cloud').click(); } catch (err) { toast(err.message, 'bad'); } };
  await loadMaps();
}
export function destroy() { offs.forEach(f => f()); offs = []; if (mc) { mc.destroy(); mc = null; } }
