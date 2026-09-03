import { api, h, esc, fmt, toast, busy, badge, modal, confirmDialog, ANSWER_TEXT } from '../api.js';
import { PanoEditor } from '../components/pano_editor.js';
let offs = [];
let forwardDeg = 180;

export async function render(root, { store, params }) {
  root.innerHTML = `
  <div class="page-head"><h1>任务航点</h1><div class="actions"><select id="f-map" style="width:260px"><option value="">全部地图</option></select><button class="btn btn-primary" id="b-new">＋ 新建任务航点</button></div></div>
  <div class="help" style="margin-bottom:10px">任务航点 = 导航航点（或手工坐标）+ 检查 prompt + 全景角度范围 + 答案模版。单表存储，可在多个任务里复用。</div>
  <div class="table-wrap"><table><thead><tr><th>#</th><th>名称</th><th>地图</th><th>导航航点</th><th>x / y / yaw</th><th>角度范围</th><th>prompt</th><th>期望</th><th>启用</th><th></th></tr></thead><tbody id="rows"></tbody></table></div>`;
  try { const s = await api('/api/settings'); forwardDeg = Number(s.settings.FORWARD_DEG) || 180; } catch { /* ignore */ }
  const sel = root.querySelector('#f-map');
  try { const { maps } = await api('/api/maps'); sel.innerHTML += maps.map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join(''); } catch { /* ignore */ }
  const load = async () => {
    const q = sel.value ? `?map_name=${encodeURIComponent(sel.value)}` : '';
    const { items } = await api('/api/task-waypoints' + q);
    const tb = root.querySelector('#rows');
    tb.innerHTML = items.length ? items.map(t => `<tr data-id="${t.id}">
      <td class="muted">${t.id}</td><td><b>${esc(t.name)}</b></td><td class="mono small">${esc(t.map_name)}</td><td>${t.nav_node_id ? badge(`航点 ${t.nav_node_id}`, 'info') : badge('手工坐标')}</td>
      <td class="mono small">${fmt.num(t.x)} / ${fmt.num(t.y)} / ${fmt.deg(t.yaw)}</td><td class="mono">${t.angle_from}° → ${t.angle_to}°</td>
      <td class="small" style="max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(t.prompt)}">${esc(t.prompt) || '<span class="muted">（无）</span>'}</td>
      <td>${esc(ANSWER_TEXT[t.answer_template?.expected] || t.answer_template?.expected || '是')}</td><td>${t.enabled ? '✅' : '—'}</td>
      <td class="right" style="white-space:nowrap"><button class="btn btn-xs" data-a="edit">编辑</button> <button class="btn btn-xs btn-danger" data-a="del">删除</button></td></tr>`).join('') : '<tr><td colspan="10" class="empty">还没有任务航点。去「地图」页点一个导航航点 → 添加为任务航点，或点右上角新建。</td></tr>';
    tb.querySelectorAll('tr[data-id]').forEach(tr => {
      const id = Number(tr.dataset.id), t = items.find(x => x.id === id);
      tr.querySelector('[data-a=edit]').onclick = () => openWaypointEditor(t, load);
      tr.querySelector('[data-a=del]').onclick = async () => { if (!(await confirmDialog({ title: '删除任务航点', danger: true, okText: '删除', body: `删除「${esc(t.name)}」？引用它的任务会同时移除该航点。` }))) return; try { await api(`/api/task-waypoints/${id}`, { method: 'DELETE' }); toast('已删除', 'ok'); load(); } catch (e) { toast(e.message, 'bad'); } };
    });
    if (params && params[0]) { const t = items.find(x => String(x.id) === params[0]); if (t) { openWaypointEditor(t, load); location.hash = '#/waypoints'; } }
  };
  sel.onchange = load;
  root.querySelector('#b-new').onclick = () => openWaypointEditor({ map_name: sel.value || '' }, load);
  await load();
}
export function destroy() { offs.forEach(f => f()); offs = []; }

/** 任务航点编辑器（新建/编辑）。init 可只带部分字段。 */
export async function openWaypointEditor(init, onSaved) {
  const t = { name: '', map_name: '', nav_node_id: null, x: null, y: null, z: 0, yaw: null, prompt: '', angle_from: 150, angle_to: 210, enabled: true, answer_template: {}, ...init };
  const tpl = { expected: 'yes', on_pass: '', on_fail: '', on_unknown: '', ...(t.answer_template || {}) };
  let maps = [], nodes = [];
  try { maps = (await api('/api/maps')).maps; } catch { /* ignore */ }
  const body = h(`<div class="split-r" style="grid-template-columns:1fr 1fr">
    <div class="form">
      <div class="form-row"><label>名称</label><input id="w-name" value="${esc(t.name)}" placeholder="如：3 号消防栓"></div>
      <div class="form-row"><label>地图</label><select id="w-map">${maps.map(m => `<option value="${esc(m.name)}" ${m.name === t.map_name ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}</select></div>
      <div class="form-row"><label>导航航点</label><div class="form-inline"><select id="w-node" style="width:160px"><option value="">（手工坐标）</option></select><span class="muted small">选了航点会自动带出 x,y,z,yaw</span></div></div>
      <div class="form-row"><label>x / y / z</label><div class="form-inline"><input id="w-x" type="number" step="0.01" value="${t.x ?? ''}" style="width:110px"><input id="w-y" type="number" step="0.01" value="${t.y ?? ''}" style="width:110px"><input id="w-z" type="number" step="0.01" value="${t.z ?? 0}" style="width:90px"></div></div>
      <div class="form-row"><label>yaw (rad)</label><input id="w-yaw" type="number" step="0.001" value="${t.yaw ?? ''}" style="width:140px"></div>
      <div class="form-row"><label>检查 prompt</label><textarea id="w-prompt" placeholder="例：全景图 190° 到 230° 之间的消防栓柜门是否关好？前面地面方格是否被物品占据？回答只能是「是」或「不是」。">${esc(t.prompt)}</textarea></div>
      <fieldset><legend>答案模版（VLM 只回答 是 / 不是；按期望值决定通过与播报）</legend>
        <div class="form-row"><label>期望回答</label><div class="form-inline"><label><input type="radio" name="w-exp" value="yes" ${tpl.expected !== 'no' ? 'checked' : ''}> 是（yes）= 通过</label><label><input type="radio" name="w-exp" value="no" ${tpl.expected === 'no' ? 'checked' : ''}> 不是（no）= 通过</label></div></div>
        <div class="form-row"><label>通过时播报</label><div class="form-inline" style="flex-wrap:nowrap"><input id="w-pass" value="${esc(tpl.on_pass)}" placeholder="{name}检查通过。"><button class="btn btn-sm" data-tts="w-pass">▶</button></div></div>
        <div class="form-row"><label>不通过播报</label><div class="form-inline" style="flex-wrap:nowrap"><input id="w-fail" value="${esc(tpl.on_fail)}" placeholder="注意，{name}检查未通过，请处理。"><button class="btn btn-sm" data-tts="w-fail">▶</button></div></div>
        <div class="form-row"><label>无法判断播报</label><div class="form-inline" style="flex-wrap:nowrap"><input id="w-unk" value="${esc(tpl.on_unknown)}" placeholder="{name}无法判断，请人工复核。"><button class="btn btn-sm" data-tts="w-unk">▶</button></div></div>
        <div class="help">可用占位符：{name} 航点名、{answer} 模型回答。留空用默认句式。</div></fieldset>
      <div class="form-row"><label>启用</label><label><input type="checkbox" id="w-enabled" ${t.enabled ? 'checked' : ''}> 参与任务执行</label></div>
    </div>
    <div>
      <div class="card-head"><h2>全景角度范围</h2><div class="form-inline"><button class="btn btn-sm" id="w-cap">📷 抓一张作参考图</button><label class="btn btn-sm">上传<input type="file" id="w-up" accept="image/*" hidden></label></div></div>
      <div id="w-pano"></div>
      <div class="form-inline" style="margin-top:8px"><label class="small">from <input id="w-af" type="number" step="0.5" min="0" max="360" value="${t.angle_from}" style="width:90px"></label><label class="small">to <input id="w-at" type="number" step="0.5" min="0" max="360" value="${t.angle_to}" style="width:90px"></label><span class="help">拖动两条黄线或直接填数；宽度 ↔ 360° 全景，中心 180°，机头方向见蓝色虚线（设置里可校准）</span></div>
      <div class="card tight" style="margin-top:12px"><div class="card-head"><h3>试问 VLM</h3><div class="form-inline"><button class="btn btn-sm" id="w-test-ref" ${t.id ? '' : 'disabled title="先保存"'}>用参考图</button><button class="btn btn-sm" id="w-test-cap" ${t.id ? '' : 'disabled title="先保存"'}>现抓一张</button></div></div><div id="w-test" class="small muted">保存后可用当前 prompt 与角度范围试问，结果与将播报的句子会显示在这里。</div></div>
    </div></div>`);
  const foot = h(`<div style="display:flex;gap:8px"><button class="btn" id="w-cancel">取消</button><button class="btn btn-primary" id="w-save">${t.id ? '保存' : '创建'}</button></div>`);
  const m = modal({ title: t.id ? `编辑任务航点 #${t.id}` : '新建任务航点', content: body, footer: foot, wide: true });
  const $ = s => body.querySelector(s);
  const pano = new PanoEditor($('#w-pano'), { angleFrom: t.angle_from, angleTo: t.angle_to, forwardDeg, onChange: r => { $('#w-af').value = r.angle_from; $('#w-at').value = r.angle_to; } });
  if (t.reference_image_url) pano.setImage(t.reference_image_url);
  $('#w-af').onchange = $('#w-at').onchange = () => pano.setRange(Number($('#w-af').value), Number($('#w-at').value), true);

  const loadNodes = async () => {
    const mp = $('#w-map').value; const ns = $('#w-node');
    try { const d = await api(`/api/maps/${encodeURIComponent(mp)}/waypoints`); nodes = d.waypoints; } catch { nodes = []; }
    ns.innerHTML = '<option value="">（手工坐标）</option>' + nodes.map(n => `<option value="${esc(n.node_id)}" ${String(n.node_id) === String(t.nav_node_id) ? 'selected' : ''}>航点 ${esc(n.node_id)}</option>`).join('');
  };
  $('#w-map').onchange = loadNodes; await loadNodes();
  $('#w-node').onchange = () => { const n = nodes.find(x => String(x.node_id) === $('#w-node').value); if (n) { $('#w-x').value = n.x; $('#w-y').value = n.y; $('#w-z').value = n.z; $('#w-yaw').value = n.yaw.toFixed(4); if (!$('#w-name').value) $('#w-name').value = `航点${n.node_id}`; } };

  const collect = () => ({ name: $('#w-name').value.trim(), map_name: $('#w-map').value, nav_node_id: $('#w-node').value || null,
    x: $('#w-x').value === '' ? null : Number($('#w-x').value), y: $('#w-y').value === '' ? null : Number($('#w-y').value), z: Number($('#w-z').value || 0), yaw: $('#w-yaw').value === '' ? null : Number($('#w-yaw').value),
    prompt: $('#w-prompt').value.trim(), angle_from: Number($('#w-af').value), angle_to: Number($('#w-at').value), enabled: $('#w-enabled').checked,
    answer_template: { expected: body.querySelector('input[name=w-exp]:checked').value, on_pass: $('#w-pass').value, on_fail: $('#w-fail').value, on_unknown: $('#w-unk').value }, reference_image: t.reference_image ?? null });
  const save = async (btn) => {
    const d = collect(); if (!d.name) throw new Error('名称必填'); if (!d.map_name) throw new Error('请选择地图');
    const r = t.id ? await api(`/api/task-waypoints/${t.id}`, { method: 'PUT', body: d }) : await api('/api/task-waypoints', { method: 'POST', body: d });
    Object.assign(t, r); return r;
  };
  foot.querySelector('#w-cancel').onclick = () => m.close();
  foot.querySelector('#w-save').onclick = async (e) => { try { await busy(e.currentTarget, () => save()); toast('已保存', 'ok'); m.close(); onSaved && onSaved(t); } catch (err) { toast(err.message, 'bad', 5000); } };
  const ensureSaved = async () => { if (!t.id) { await save(); toast('已先创建该航点', 'ok'); $('#w-test-ref').disabled = $('#w-test-cap').disabled = false; } };
  $('#w-cap').onclick = async (e) => { try { await ensureSaved(); const r = await busy(e.currentTarget, () => api(`/api/task-waypoints/${t.id}/reference-image`, { method: 'POST' })); pano.setImage(r.url + '?t=' + Date.now()); t.reference_image = r.reference_image; toast(`参考图 ${r.width}×${r.height}`, 'ok'); } catch (err) { toast(err.message, 'bad', 6000); } };
  $('#w-up').onchange = async () => { const f = $('#w-up').files[0]; if (!f) return; try { await ensureSaved(); const fd = new FormData(); fd.append('file', f); const r = await api(`/api/task-waypoints/${t.id}/reference-image/upload`, { method: 'POST', body: fd }); pano.setImage(r.url + '?t=' + Date.now()); t.reference_image = r.reference_image; toast('已上传参考图', 'ok'); } catch (err) { toast(err.message, 'bad'); } };
  const test = async (btn, use) => {
    try {
      await ensureSaved(); const d = collect();
      const r = await busy(btn, () => api(`/api/task-waypoints/${t.id}/test-vlm`, { method: 'POST', body: { use, prompt: d.prompt, angle_from: d.angle_from, angle_to: d.angle_to } }));
      $('#w-test').innerHTML = `<div style="display:flex;gap:12px;align-items:flex-start"><img src="${r.crop_url}" style="max-width:260px;max-height:130px;border-radius:6px;object-fit:contain;background:#222">
        <div><div class="answer ${r.vlm.answer}">${esc(ANSWER_TEXT[r.vlm.answer] || r.vlm.answer)}</div><div class="small muted">${esc(r.vlm.provider)} ${esc(r.vlm.model)} · ${r.vlm.latency_ms} ms ${r.vlm.error ? badge(r.vlm.error, 'bad') : ''}</div>
        <div class="mono small" style="margin-top:4px;white-space:pre-wrap">${esc(r.vlm.raw || '')}</div><div style="margin-top:6px">${r.passed === true ? badge('通过', 'ok') : r.passed === false ? badge('不通过', 'bad') : badge('待复核', 'warn')} 将播报：<b>${esc(r.tts_text || '（无）')}</b></div></div></div>`;
      if (use === 'capture') pano.setImage((await api(`/api/task-waypoints/${t.id}`)).reference_image_url + '?t=' + Date.now());
    } catch (err) { $('#w-test').innerHTML = `<span class="badge bad">${esc(err.message)}</span>`; }
  };
  $('#w-test-ref').onclick = e => test(e.currentTarget, 'reference'); $('#w-test-cap').onclick = e => test(e.currentTarget, 'capture');
  body.querySelectorAll('[data-tts]').forEach(b => b.onclick = async () => { const text = ($('#' + b.dataset.tts).value || $('#' + b.dataset.tts).placeholder).replace('{name}', $('#w-name').value || '该点'); try { const r = await busy(b, () => api('/api/tts/test', { method: 'POST', body: { text } })); toast(`已播报：${text}（${Object.entries(r.sinks).map(([k, v]) => k + ':' + v).join(', ')}）`, 'ok'); } catch (err) { toast(err.message, 'bad'); } });
  return m;
}
