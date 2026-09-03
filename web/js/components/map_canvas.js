// 2D 俯视地图画布：导航航点 + 邻接边 + 机器人 + 点云 + 高亮路径 + 任务航点。支持拖拽平移、滚轮缩放、悬停/点击。
export class MapCanvas {
  constructor(canvas, { onHover, onClick } = {}) {
    this.c = canvas; this.ctx = canvas.getContext('2d');
    this.onHover = onHover; this.onClick = onClick;
    this.data = { waypoints: [], edges: [], bounds: null };
    this.byId = new Map();
    this.robot = null; this.cloud = null; this.path = []; this.selected = null; this.hover = null; this.taskWaypoints = [];
    this.trail = [];
    this.scale = 20; this.ox = 0; this.oy = 0;   // 像素/米，世界原点在屏幕上的偏移
    this._drag = null; this._fitted = false;
    this.ro = new ResizeObserver(() => this.resize()); this.ro.observe(canvas.parentElement);
    canvas.addEventListener('pointerdown', e => this._down(e));
    canvas.addEventListener('pointermove', e => this._move(e));
    canvas.addEventListener('pointerup', e => this._up(e));
    canvas.addEventListener('pointerleave', () => { this.hover = null; this.draw(); });
    canvas.addEventListener('wheel', e => this._wheel(e), { passive: false });
    this.resize();
  }
  destroy() { this.ro.disconnect(); }
  resize() {
    const r = this.c.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.w = Math.max(10, r.width); this.hh = Math.max(10, r.height);
    this.c.width = this.w * dpr; this.c.height = this.hh * dpr; this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!this._fitted && this.data.bounds) this.fit();
    this.draw();
  }
  setData(d) { this.data = d; this.byId = new Map((d.waypoints || []).map(w => [String(w.node_id), w])); this._fitted = false; this.fit(); this.draw(); }
  setRobot(p) { this.robot = p; if (p && p.x != null) { const last = this.trail[this.trail.length - 1]; if (!last || Math.hypot(last.x - p.x, last.y - p.y) > 0.05) { this.trail.push({ x: p.x, y: p.y }); if (this.trail.length > 600) this.trail.shift(); } } this.draw(); }
  setPointCloud(pts) { this.cloud = pts; this.draw(); }
  setPath(ids) { this.path = (ids || []).map(String); this.draw(); }
  setSelected(id) { this.selected = id == null ? null : String(id); this.draw(); }
  setTaskWaypoints(list) { this.taskWaypoints = list || []; this.draw(); }
  clearTrail() { this.trail = []; this.draw(); }
  fit() {
    const b = this.data.bounds; if (!b) return;
    const bw = Math.max(1, b.max_x - b.min_x), bh = Math.max(1, b.max_y - b.min_y);
    this.scale = Math.min((this.w - 60) / bw, (this.hh - 60) / bh);
    this.scale = Math.max(2, Math.min(200, this.scale));
    const cx = (b.min_x + b.max_x) / 2, cy = (b.min_y + b.max_y) / 2;
    this.ox = this.w / 2 - cx * this.scale; this.oy = this.hh / 2 + cy * this.scale;
    this._fitted = true;
  }
  toScreen(x, y) { return [this.ox + x * this.scale, this.oy - y * this.scale]; }
  toWorld(sx, sy) { return [(sx - this.ox) / this.scale, (this.oy - sy) / this.scale]; }
  _pos(e) { const r = this.c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
  _hit(sx, sy) {
    let best = null, bd = 12;
    for (const w of this.data.waypoints || []) { const [x, y] = this.toScreen(w.x, w.y); const d = Math.hypot(x - sx, y - sy); if (d < bd) { bd = d; best = w; } }
    return best;
  }
  _down(e) { const [sx, sy] = this._pos(e); this._drag = { sx, sy, ox: this.ox, oy: this.oy, moved: false }; this.c.setPointerCapture(e.pointerId); }
  _move(e) {
    const [sx, sy] = this._pos(e);
    if (this._drag) { const dx = sx - this._drag.sx, dy = sy - this._drag.sy; if (Math.hypot(dx, dy) > 3) this._drag.moved = true; if (this._drag.moved) { this.ox = this._drag.ox + dx; this.oy = this._drag.oy + dy; this.draw(); } return; }
    const hit = this._hit(sx, sy); const id = hit ? String(hit.node_id) : null;
    if (id !== this.hover) { this.hover = id; this.c.style.cursor = hit ? 'pointer' : 'grab'; this.draw(); this.onHover && this.onHover(hit); }
  }
  _up(e) { const [sx, sy] = this._pos(e); const d = this._drag; this._drag = null; if (d && !d.moved) { const hit = this._hit(sx, sy); this.onClick && this.onClick(hit, this.toWorld(sx, sy)); } }
  _wheel(e) { e.preventDefault(); const [sx, sy] = this._pos(e); const [wx, wy] = this.toWorld(sx, sy); const f = e.deltaY < 0 ? 1.15 : 1 / 1.15; this.scale = Math.max(1, Math.min(400, this.scale * f)); this.ox = sx - wx * this.scale; this.oy = sy + wy * this.scale; this.draw(); }
  draw() {
    const g = this.ctx, W = this.w, H = this.hh; if (!g) return;
    const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    g.clearRect(0, 0, W, H);
    g.fillStyle = dark ? '#111823' : '#fafbfd'; g.fillRect(0, 0, W, H);
    // 网格（1m 或 5m）
    const step = this.scale > 40 ? 1 : this.scale > 8 ? 5 : 10;
    g.strokeStyle = dark ? 'rgba(255,255,255,.06)' : 'rgba(0,0,0,.06)'; g.lineWidth = 1;
    const [x0, y0] = this.toWorld(0, H), [x1, y1] = this.toWorld(W, 0);
    for (let x = Math.floor(x0 / step) * step; x <= x1; x += step) { const [sx] = this.toScreen(x, 0); g.beginPath(); g.moveTo(sx, 0); g.lineTo(sx, H); g.stroke(); }
    for (let y = Math.floor(y0 / step) * step; y <= y1; y += step) { const [, sy] = this.toScreen(0, y); g.beginPath(); g.moveTo(0, sy); g.lineTo(W, sy); g.stroke(); }
    // 点云
    if (this.cloud && this.cloud.length) { g.fillStyle = dark ? 'rgba(160,180,210,.35)' : 'rgba(60,80,110,.28)'; for (const p of this.cloud) { const [sx, sy] = this.toScreen(p[0], p[1]); if (sx < -2 || sy < -2 || sx > W + 2 || sy > H + 2) continue; g.fillRect(sx, sy, 1.5, 1.5); } }
    // 边
    g.strokeStyle = dark ? '#3a4a63' : '#b9c4d6'; g.lineWidth = 2;
    for (const [a, b] of this.data.edges || []) { const A = this.byId.get(String(a)), B = this.byId.get(String(b)); if (!A || !B) continue; const [ax, ay] = this.toScreen(A.x, A.y), [bx, by] = this.toScreen(B.x, B.y); g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke(); }
    // 轨迹
    if (this.trail.length > 1) { g.strokeStyle = 'rgba(31,94,255,.45)'; g.lineWidth = 2; g.beginPath(); this.trail.forEach((p, i) => { const [sx, sy] = this.toScreen(p.x, p.y); i ? g.lineTo(sx, sy) : g.moveTo(sx, sy); }); g.stroke(); }
    // 高亮路径
    if (this.path.length > 1) { g.strokeStyle = '#ff9f1a'; g.lineWidth = 5; g.lineCap = 'round'; g.beginPath(); this.path.forEach((id, i) => { const n = this.byId.get(id); if (!n) return; const [sx, sy] = this.toScreen(n.x, n.y); i ? g.lineTo(sx, sy) : g.moveTo(sx, sy); }); g.stroke(); }
    // 节点
    const showLabels = this.scale > 9;
    g.font = '11px ui-monospace, Menlo, monospace'; g.textAlign = 'center'; g.textBaseline = 'middle';
    for (const w of this.data.waypoints || []) {
      const id = String(w.node_id); const [sx, sy] = this.toScreen(w.x, w.y);
      const sel = id === this.selected, hov = id === this.hover, onPath = this.path.includes(id);
      g.beginPath(); g.arc(sx, sy, sel ? 8 : hov ? 7 : 5, 0, Math.PI * 2);
      g.fillStyle = sel ? '#1f5eff' : onPath ? '#ff9f1a' : (dark ? '#8fb0ff' : '#4d7cff'); g.fill();
      g.strokeStyle = dark ? '#0f141c' : '#fff'; g.lineWidth = 1.5; g.stroke();
      // 朝向短线
      const L = 9; g.strokeStyle = sel ? '#1f5eff' : (dark ? '#8fb0ff' : '#4d7cff'); g.beginPath(); g.moveTo(sx, sy); g.lineTo(sx + Math.cos(w.yaw) * L, sy - Math.sin(w.yaw) * L); g.stroke();
      if (showLabels || sel || hov) { g.fillStyle = dark ? '#cfd8e8' : '#334'; g.fillText(id, sx, sy - 12); }
    }
    // 任务航点（菱形）
    for (const t of this.taskWaypoints) {
      const [sx, sy] = this.toScreen(t.x, t.y); g.fillStyle = t.enabled === false ? '#aaa' : '#ffb020'; g.strokeStyle = '#7a4d00'; g.lineWidth = 1.5;
      g.beginPath(); g.moveTo(sx, sy - 10); g.lineTo(sx + 8, sy); g.lineTo(sx, sy + 10); g.lineTo(sx - 8, sy); g.closePath(); g.fill(); g.stroke();
      if (t.seq != null) { g.fillStyle = '#000'; g.font = 'bold 10px sans-serif'; g.fillText(String(t.seq), sx, sy); g.font = '11px ui-monospace, Menlo, monospace'; }
      if (t.name && this.scale > 6) { g.fillStyle = dark ? '#ffd27a' : '#7a4d00'; g.font = '11px sans-serif'; g.fillText(t.name, sx, sy + 18); g.font = '11px ui-monospace, Menlo, monospace'; }
    }
    // 机器人
    if (this.robot && this.robot.x != null) {
      const [sx, sy] = this.toScreen(this.robot.x, this.robot.y); const a = -(this.robot.yaw || 0);
      g.save(); g.translate(sx, sy); g.rotate(a);
      g.fillStyle = 'rgba(31,94,255,.25)'; g.beginPath(); g.arc(0, 0, 14, 0, Math.PI * 2); g.fill();
      g.fillStyle = '#1f5eff'; g.beginPath(); g.moveTo(14, 0); g.lineTo(-8, 8); g.lineTo(-4, 0); g.lineTo(-8, -8); g.closePath(); g.fill();
      g.restore();
    }
    // 比例尺
    const m = step, px = m * this.scale; g.strokeStyle = dark ? '#ccc' : '#333'; g.lineWidth = 2; g.beginPath(); g.moveTo(W - 20 - px, H - 16); g.lineTo(W - 20, H - 16); g.stroke();
    g.fillStyle = dark ? '#ccc' : '#333'; g.textAlign = 'right'; g.font = '11px sans-serif'; g.fillText(`${m} m`, W - 20, H - 26);
  }
}
