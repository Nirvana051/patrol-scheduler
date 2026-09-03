import { api, h, esc, toast, busy, badge } from '../api.js';

const GROUPS = [
  { title: '云端连接（改动会重建连接）', keys: [['CX_HOST', '云端地址', 'text', 'https://certaintyx.sg:8443 真机；http://127.0.0.1:18443 mock'], ['CX_ROBOT', '机器人别名', 'text', '用别名不用机器人 ID'], ['CX_KEY', 'API 密钥', 'password', '只存服务端；显示为掩码；operator + auto 模式'], ['RATE_LIMIT_RPS', '本地限速 (rps)', 'number', '云端 5 rps，留余量'], ['STATUS_POLL_ACTIVE', '活跃轮询间隔 (s)', 'number', '≥1'], ['STATUS_POLL_IDLE', '空闲轮询间隔 (s)', 'number', '']] },
  { title: 'VLM 视觉判读', keys: [['VLM_PROVIDER', '提供方', 'select:mock,openai_compat,anthropic', 'mock 用于无模型环境'], ['VLM_BASE_URL', 'OpenAI 兼容地址', 'text', 'Ollama: http://127.0.0.1:11434/v1'], ['VLM_MODEL', '模型', 'text', '如 qwen2.5vl:7b / gpt-4o'], ['VLM_API_KEY', 'API Key', 'password', ''], ['ANTHROPIC_API_KEY', 'Anthropic API Key', 'password', ''], ['ANTHROPIC_MODEL', 'Anthropic 模型', 'text', '默认 claude-opus-5'], ['VLM_ANTHROPIC_FALLBACKS', '启用服务端 fallbacks', 'select:1,0', ''], ['VLM_SEND_FULL_PANO', '附整张全景', 'select:0,1', '除裁切外再附整图'], ['VLM_TIMEOUT', '超时 (s)', 'number', ''], ['VLM_MOCK_ANSWER', 'mock 回答', 'select:alternate,yes,no,unknown,random,keyword', '']] },
  { title: 'TTS 播报', keys: [['TTS_ENGINE', '引擎', 'select:edge,command,none', 'edge 需联网；none 只推文本让浏览器念'], ['TTS_VOICE', 'edge 声音', 'text', 'zh-CN-XiaoxiaoNeural / zh-CN-YunxiNeural'], ['TTS_COMMAND', 'command 模板', 'text', '{text} {out}，如 espeak-ng -v cmn -w {out} {text}'], ['TTS_SINKS', '汇出', 'text', '逗号分隔：browser,local,zmq,webhook'], ['TTS_ZMQ_ENDPOINT', 'robot-audio ZMQ', 'text', 'tcp://<robot-ip>:5555（需机器人端支持 play_tts）'], ['TTS_WEBHOOK_URL', 'Webhook', 'text', '']] },
  { title: '抓图与执行', keys: [['SNAPSHOT_SOURCE', '抓图源', 'text', 'rtsp（首选，TCP）| hls（8554 被挡时的备选）| synthetic | file:<路径> | lavfi:<filter>'], ['FORWARD_DEG', '机头对应角度 (°)', 'number', '全景图上机器人正前方的列，默认 180 = 图像中心；真机首帧校准'], ['SETTLE_SECONDS', '到点稳定 (s)', 'number', ''], ['LEG_TIMEOUT', '每段超时 (s)', 'number', ''], ['MAX_RETRIES', '段失败重试', 'number', ''], ['ESTOP_ON_EXCEPTION', '执行器异常时急停', 'select:1,0', '示例脚本的做法：出异常先急停再收尾']] },
];

export async function render(root, { store }) {
  const s = await api('/api/settings');
  root.innerHTML = `<div class="page-head"><h1>设置</h1><div class="actions"><span class="muted small">优先级：此处设置 &gt; config/.env &gt; 默认值</span><button class="btn btn-primary" id="b-save">保存</button></div></div>
  <div class="grid grid-2">
    <div id="groups"></div>
    <div>
      <div class="card"><h2>当前适配器</h2><dl class="kv"><dt>模式</dt><dd>${s.mode === 'real' ? badge('REAL 真机', 'warn') : badge('MOCK 仿真', 'ok')}</dd><dt>VLM</dt><dd class="mono">${esc(s.adapters.vlm)}</dd><dt>TTS</dt><dd class="mono">${esc(s.adapters.tts.engine)} → ${esc(s.adapters.tts.sinks.join(', '))}</dd><dt>抓图源</dt><dd class="mono">${esc(s.adapters.snapshot)}</dd><dt>事件监听</dt><dd>${s.events.connected ? badge(s.events.transport, 'ok') : badge('未连接', 'warn')} 游标 ${s.events.cursor ?? '—'}，已收 ${s.events.received}${s.events.last_error ? `<div class="small muted">${esc(s.events.last_error)}</div>` : ''}</dd></dl>
        <div class="form-inline" style="margin-top:10px"><input id="tts-text" value="消防栓门已关闭，检查通过。" style="width:260px"><button class="btn btn-sm" id="b-tts">▶ 试听 TTS</button></div><div id="tts-out" class="small muted" style="margin-top:4px"></div></div>
      <div class="card" style="margin-top:14px"><div class="card-head"><h2>机头校准（FORWARD_DEG）</h2><button class="btn btn-sm" id="cal-snap">📷 抓一张全景</button></div>
        <div class="help">真机第一次抓图后，把蓝线拖到画面里机器人<b>正前方</b>所在的位置，保存后任务航点编辑器里的「相对机头」读数才准确。当前 ${esc(s.settings.FORWARD_DEG)}°。</div>
        <div class="pano-stage" id="cal-stage" style="margin-top:8px;aspect-ratio:2/1"><img id="cal-img" alt="" draggable="false" style="display:none"><div class="pano-empty" id="cal-empty">先抓一张全景</div><div class="pano-forward" id="cal-line" style="left:${Number(s.settings.FORWARD_DEG) / 3.6}%;border-left-width:3px;pointer-events:none"><span>机头</span></div></div>
        <div class="form-inline" style="margin-top:8px"><label class="small">机头角度 <input id="cal-deg" type="number" step="0.5" min="0" max="360" value="${esc(s.settings.FORWARD_DEG)}" style="width:100px"></label><button class="btn btn-sm btn-primary" id="cal-save">保存为 FORWARD_DEG</button></div></div>
      ${s.mode === 'mock' ? `<div class="card" style="margin-top:14px"><h2>演示场景（合成全景）</h2><label><input type="checkbox" id="scene-door" ${s.scene.door_open ? 'checked' : ''}> 消防栓柜门打开（让 VLM/TTS 走「不通过」分支的素材）</label><div class="help">只影响 synthetic 抓图源画出来的图。</div></div>` : ''}
      <div class="card" style="margin-top:14px"><div class="card-head"><h2>系统自检</h2><button class="btn btn-sm" id="b-health">↻</button></div><dl class="kv" id="health"></dl></div>
      <div class="card" style="margin-top:14px"><h2>说明</h2><ul class="small" style="padding-left:18px;margin:0"><li>密钥永不回传前端，只显示掩码；不改就原样提交即可。</li><li>改 CX_* 会重建云端连接与监听线程（当前执行请先中止）。</li><li>状态码表来自 <span class="mono">GET /v1/status-codes</span>，不在本地维护。</li></ul></div>
    </div></div>`;
  const g = root.querySelector('#groups');
  for (const grp of GROUPS) {
    g.appendChild(h(`<div class="card" style="margin-bottom:14px"><h2>${esc(grp.title)}</h2><div class="form">${grp.keys.map(([k, label, type, help]) => {
      const v = s.settings[k] ?? '';
      let input;
      if (type.startsWith('select:')) input = `<select data-k="${k}">${type.slice(7).split(',').map(o => `<option value="${o}" ${String(v) === o ? 'selected' : ''}>${o}</option>`).join('')}</select>`;
      else input = `<input data-k="${k}" type="${type === 'password' ? 'text' : type}" value="${esc(v)}" ${type === 'number' ? 'step="any"' : ''}>`;
      return `<div class="form-row"><label title="${k}">${esc(label)}<div class="mono" style="font-size:10px;opacity:.7">${k}</div></label><div>${input}${help ? `<div class="help">${esc(help)}</div>` : ''}</div></div>`; }).join('')}</div></div>`));
  }
  root.querySelector('#b-save').onclick = async (e) => {
    const body = {}; root.querySelectorAll('[data-k]').forEach(el => { body[el.dataset.k] = el.value; });
    try { const r = await busy(e.currentTarget, () => api('/api/settings', { method: 'PUT', body })); toast(r.changed.length ? `已更新：${r.changed.join(', ')}${r.reconnected ? '（已重建连接）' : ''}` : '没有变化', 'ok'); if (r.changed.length) render(root, { store }); } catch (err) { toast(err.message, 'bad', 6000); }
  };
  root.querySelector('#b-tts').onclick = async (e) => { try { const r = await busy(e.currentTarget, () => api('/api/tts/test', { method: 'POST', body: { text: root.querySelector('#tts-text').value } })); root.querySelector('#tts-out').textContent = `引擎 ${r.engine}${r.audio_url ? ' · 已生成音频' : ' · 无音频（浏览器朗读）'} · ` + Object.entries(r.sinks).map(([k, v]) => `${k}: ${v}`).join('，') + (r.error ? ` · ${r.error}` : ''); } catch (err) { toast(err.message, 'bad'); } };
  const paintHealth = async () => { try { const hh = await api('/api/health'); root.querySelector('#health').innerHTML = `<dt>版本</dt><dd>${esc(hh.version)} · 实例 ${esc(hh.instance_id)} · 运行 ${Math.round(hh.uptime_s / 60)} min</dd><dt>数据库</dt><dd class="mono small">${esc(hh.db_path)}（schema v${hh.db_version}）</dd><dt>线程</dt><dd>${hh.threads.status_poller ? badge('状态轮询', 'ok') : badge('状态轮询停止', 'bad')} ${hh.threads.events_listener ? badge('事件监听', 'ok') : badge('事件监听停止', 'bad')} <span class="small muted">状态快照 ${hh.status_age_s ?? '—'} s 前</span></dd><dt>媒体占用</dt><dd>${(hh.media_bytes / 1048576).toFixed(1)} MB</dd><dt>云端请求计数</dt><dd>${hh.rate_limiter_total}</dd>`; } catch (e) { root.querySelector('#health').innerHTML = `<dd>${esc(e.message)}</dd>`; } };
  root.querySelector('#b-health').onclick = paintHealth; paintHealth();
  // 机头校准
  const stage = root.querySelector('#cal-stage'), calLine = root.querySelector('#cal-line'), calDeg = root.querySelector('#cal-deg');
  const setCal = (deg) => { deg = ((Number(deg) % 360) + 360) % 360; calLine.style.left = `${deg / 3.6}%`; calDeg.value = Math.round(deg * 10) / 10; };
  let calDrag = false;
  const calAngle = (e) => { const r = stage.getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * 360; };
  stage.addEventListener('pointerdown', e => { calDrag = true; stage.setPointerCapture(e.pointerId); setCal(calAngle(e)); });
  stage.addEventListener('pointermove', e => { if (calDrag) setCal(calAngle(e)); });
  stage.addEventListener('pointerup', () => { calDrag = false; });
  calDeg.onchange = () => setCal(calDeg.value);
  root.querySelector('#cal-snap').onclick = async (e) => { try { const r = await busy(e.currentTarget, () => api('/api/robot/snapshot', { method: 'POST' })); const img = root.querySelector('#cal-img'); img.src = r.url; img.style.display = ''; root.querySelector('#cal-empty').style.display = 'none'; } catch (err) { toast(err.message, 'bad', 6000); } };
  root.querySelector('#cal-save').onclick = async (e) => { try { await busy(e.currentTarget, () => api('/api/settings', { method: 'PUT', body: { FORWARD_DEG: String(calDeg.value) } })); toast(`机头角度已保存：${calDeg.value}°`, 'ok'); } catch (err) { toast(err.message, 'bad'); } };
  const door = root.querySelector('#scene-door'); if (door) door.onchange = async () => { try { await api('/api/demo/scene', { method: 'POST', body: { door_open: door.checked } }); toast('场景已更新', 'ok'); } catch (err) { toast(err.message, 'bad'); } };
}
export function destroy() {}
