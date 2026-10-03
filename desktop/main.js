// 巡检调度系统桌面壳。只做四件事：拉起随包携带的 Python 后端、给它一个窗口与托盘、优雅退出（先停机器人）、自动更新。
// 不含任何业务逻辑 —— 后端就是仓库里的 app/（python -m app.main），壳只通过 HTTP 和它说话。
// 详细说明：docs/DESKTOP.md。
'use strict';
const { app, BrowserWindow, Tray, Menu, dialog, shell, nativeImage, Notification } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const net = require('net');
const path = require('path');
const log = require('electron-log');

const ARGS = new Set(process.argv.slice(1));
const SMOKE = ARGS.has('--smoke');              // 自检模式：拉起后端（仿真）→ 健康检查 → 优雅退出，不开窗口，CI 用
const MOCK_FLAG = ARGS.has('--mock');
const SHOT = [...ARGS].find((a) => a.startsWith('--screenshot='));   // --screenshot=/path/x.png：开窗口、页面加载完截一张就优雅退出（CI 产物 / 文档截图）
const OPEN = ([...ARGS].find((a) => a.startsWith('--open=')) || '').slice('--open='.length);   // --open=#/settings：启动时打开哪个视图
const SCROLL_TO = ([...ARGS].find((a) => a.startsWith('--scroll-to=')) || '').slice('--scroll-to='.length);   // 截图前把某个元素滚进视口
const APP_ID = 'cn.patrolscheduler.desktop';
const SHUTDOWN_GRACE_MS = 40000;                // 与 deploy/patrol-scheduler.service 的 TimeoutStopSec=40 一致：退出前要中止执行并 DELETE /task
const MOCK_KEY = 'cx_mock0001_' + '0'.repeat(48);

let L = null;          // 路径布局
let py = null;         // 解释器
let backend = null;    // 后端进程管理
let win = null;
let tray = null;
let quitRequested = false;   // 已进入退出流程（再点退出 / Cmd+Q 不再重复进）
let readyToExit = false;     // 后端已停好，before-quit 放行
let mockMode = false;
let firstRun = false;
let trayHinted = false;
let settings = { mock: false };
let autoUpdater = null;
let manualCheck = false;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── 路径 ──────────────────────────────────────────────────────────────────────
function layout() {
  // 打包后：resources/backend（app、web、mock_gateway…）与 resources/runtime 并列；开发时：仓库根目录与 runtime/
  const res = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..');
  const userData = app.getPath('userData');
  return {
    backend: app.isPackaged ? path.join(res, 'backend') : res,
    runtime: path.join(res, 'runtime'),
    userData,
    logs: path.join(userData, 'logs'),
    settingsFile: path.join(userData, 'desktop.json'),
  };
}

function findPython(L) {
  const man = path.join(L.runtime, 'manifest.json');
  if (fs.existsSync(man)) {
    const m = JSON.parse(fs.readFileSync(man, 'utf8'));
    const exe = path.join(L.runtime, ...m.python.exe.split('/'));
    if (fs.existsSync(exe)) return { exe, runtime: L.runtime, manifest: m };
  }
  if (!app.isPackaged) {   // 开发机没组装 runtime 时退到 .venv（行为与 start.sh 一致）
    const venv = process.platform === 'win32'
      ? path.join(L.backend, '.venv', 'Scripts', 'python.exe')
      : path.join(L.backend, '.venv', 'bin', 'python');
    if (fs.existsSync(venv)) return { exe: venv, runtime: null, manifest: null };
  }
  return null;
}

function loadSettings() {
  try { return { mock: false, ...JSON.parse(fs.readFileSync(L.settingsFile, 'utf8')) }; } catch { return { mock: false }; }
}
function saveSettings() {
  try { fs.mkdirSync(L.userData, { recursive: true }); fs.writeFileSync(L.settingsFile, JSON.stringify(settings, null, 2)); } catch (e) { log.warn('保存设置失败', e.message); }
}

// ── 小工具 ────────────────────────────────────────────────────────────────────
function freePort(start) {
  return new Promise((resolve, reject) => {
    const tryPort = (p) => {
      if (p > start + 50) return reject(new Error(`从 ${start} 起 50 个端口都被占用`));
      const srv = net.createServer();
      srv.once('error', () => tryPort(p + 1));
      srv.listen(p, '127.0.0.1', () => srv.close(() => resolve(p)));
    };
    tryPort(start);
  });
}

async function http(method, url, { headers = {}, body, timeoutMs = 5000 } = {}) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(url, { method, headers, body, signal: ac.signal });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* 不是 JSON */ }
    return { status: r.status, json, text };
  } finally { clearTimeout(t); }
}

// ── 后端进程 ──────────────────────────────────────────────────────────────────
class Backend {
  constructor(L, py) {
    this.L = L; this.py = py;
    this.proc = null; this.mockProc = null;
    this.port = null; this.token = null; this.mock = false;
    this.stopping = false; this.onExit = null;
  }

  env(extra) {
    const e = { ...process.env, ...extra };
    // 与 start.sh 相同的净化：别让机器上的 ROS 之类通过 PYTHONPATH 把 runtime 里的包顶掉
    for (const k of Object.keys(e)) if (/^python(path|home)$/i.test(k)) delete e[k];   // Windows 的环境变量名不分大小写
    e.PYTHONNOUSERSITE = '1'; e.PYTHONUNBUFFERED = '1'; e.PYTHONIOENCODING = 'utf-8';
    e.PYTHONDONTWRITEBYTECODE = '1';      // 安装目录可能只读（Program Files / 已签名的 .app），而且往 .app 里写 pyc 会破坏签名
    if (this.py.runtime) e.PS_RUNTIME_DIR = this.py.runtime;
    return e;
  }

  dataDirFor(mock) { return path.join(this.L.userData, mock ? 'data-mock' : 'data'); }
  dataDir() { return this.dataDirFor(this.mock); }
  logFd(name) {
    fs.mkdirSync(this.L.logs, { recursive: true });
    const fd = fs.openSync(path.join(this.L.logs, name), 'a');
    (this._fds = this._fds || []).push(fd);
    return fd;
  }

  closeFds() { for (const fd of this._fds || []) { try { fs.closeSync(fd); } catch { /* 已关 */ } } this._fds = []; }

  async start({ mock }) {
    this.mock = !!mock; this.stopping = false; this._stopPromise = null;
    if (this.mockProc && !this.mockProc.exited) { this.mockProc.kill(); }   // 崩溃重启时别把上一只 mock 网关漏在后台
    this.mockProc = null;
    this.port = await freePort(Number(process.env.PS_PORT) || 8088);
    this.token = crypto.randomBytes(16).toString('hex');
    const dataDir = this.dataDir();
    fs.mkdirSync(dataDir, { recursive: true });
    fs.mkdirSync(path.join(this.L.userData, 'config'), { recursive: true });
    const extra = {
      PS_HOST: '127.0.0.1', PS_PORT: String(this.port), PS_DATA_DIR: dataDir, PS_DB_PATH: '',
      PS_ENV_FILE: path.join(this.L.userData, 'config', '.env'),
      PS_SHUTDOWN_TOKEN: this.token, PS_LOG_LEVEL: process.env.PS_LOG_LEVEL || 'warning',
    };
    if (this.mock) {
      // 仿真模式：先起内置 mock 网关，再把后端指过去；数据目录独立（data-mock），与真机的库互不影响
      const mp = await freePort(18443);
      const out = this.logFd('mock.out');
      this.mockProc = spawn(this.py.exe, ['-m', 'mock_gateway.server', '--host', '127.0.0.1', '--port', String(mp),
        '--speed', '2', '--prestarted', '--prelocalized'],
      { cwd: this.L.backend, env: this.env({}), stdio: ['ignore', out, out], windowsHide: true });
      const mp_ = this.mockProc;
      mp_.exited = false;
      mp_.on('exit', (c) => { mp_.exited = true; log.info('mock 网关退出', c); });
      await this.waitHttp(`http://127.0.0.1:${mp}/healthz`, 30000, 'mock 网关', () => mp_.exited);
      Object.assign(extra, { CX_HOST: `http://127.0.0.1:${mp}`, CX_ROBOT: 'ntu-dog-00001', CX_KEY: MOCK_KEY, SNAPSHOT_SOURCE: 'synthetic' });
    }
    const out = this.logFd('backend.out');
    const proc = spawn(this.py.exe, ['-m', 'app.main'], { cwd: this.L.backend, env: this.env(extra), stdio: ['ignore', out, out], windowsHide: true });
    proc.exited = false;
    this.proc = proc;
    log.info(`后端启动 pid=${proc.pid} port=${this.port} mock=${this.mock} data=${dataDir}`);
    proc.on('exit', (code, signal) => {
      proc.exited = true;
      log.info(`后端退出 code=${code} signal=${signal}`);
      if (this.proc === proc) { this.proc = null; if (this.onExit) this.onExit(code, signal, this.stopping); }
    });
    await this.waitHttp(`http://127.0.0.1:${this.port}/api/health`, 60000, '调度系统', () => proc.exited);
    return this.port;
  }

  async waitHttp(url, timeoutMs, what, died) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      if (died && died()) throw new Error(`${what}进程启动后就退出了，看日志：${this.L.logs}`);
      try { const r = await http('GET', url, { timeoutMs: 2000 }); if (r.status === 200) return; } catch { /* 还没起来 */ }
      await sleep(400);
    }
    throw new Error(`${what} ${timeoutMs / 1000} 秒内没起来（${url}），看日志：${this.L.logs}`);
  }

  url(hash = '') { return `http://127.0.0.1:${this.port}/${hash}`; }

  async health() {
    try { const r = await http('GET', this.url('api/health'), { timeoutMs: 5000 }); return r.status === 200 ? r.json : null; } catch { return null; }
  }

  stop() {
    // 幂等：退出流程、切模式、崩溃重启可能并发调用，只跑一次真正的停止
    if (!this._stopPromise) this._stopPromise = this._stop();
    return this._stopPromise;
  }

  async _stop() {
    this.stopping = true;
    const proc = this.proc;
    if (proc && !proc.exited) {
      // 走 HTTP 而不是信号：Windows 没有 SIGTERM，Node 也发不出 Ctrl+Break。接口会让 uvicorn 优雅退出 →
      // lifespan 收尾 → ctx.stop() 中止执行并 DELETE /task 停机器人（与 systemd 停服务完全同一条路）。
      try {
        const r = await http('POST', this.url('api/shutdown'), { headers: { 'X-Shutdown-Token': this.token }, timeoutMs: 5000 });
        if (r.status !== 200) throw new Error(`HTTP ${r.status} ${r.text.slice(0, 120)}`);
      } catch (e) {
        log.warn('shutdown 接口没响应，改为直接结束进程：' + e.message);
        proc.kill();
      }
      const t0 = Date.now();
      while (!proc.exited && Date.now() - t0 < SHUTDOWN_GRACE_MS) await sleep(200);
      if (!proc.exited) { log.warn(`后端 ${SHUTDOWN_GRACE_MS / 1000} 秒内没退出，强制结束`); proc.kill('SIGKILL'); await sleep(500); }
    }
    this.proc = null;
    if (this.mockProc && !this.mockProc.exited) this.mockProc.kill();
    this.mockProc = null;
    this.closeFds();
  }
}

// ── 窗口与托盘 ────────────────────────────────────────────────────────────────
function iconImage() { return nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon.png')); }
function trayImage() {
  const name = process.platform === 'darwin' ? 'trayTemplate.png' : 'tray.png';
  const img = nativeImage.createFromPath(path.join(__dirname, 'assets', name));
  if (process.platform === 'darwin') img.setTemplateImage(true);
  return img;
}

function createWindow(hash = '') {
  win = new BrowserWindow({
    width: 1440, height: 920, minWidth: 960, minHeight: 600, title: '巡检调度系统', icon: iconImage(),
    autoHideMenuBar: true, show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.once('ready-to-show', () => win.show());
  if (SHOT) {
    win.webContents.once('did-finish-load', async () => {
      await sleep(2500);                                   // 等 SSE hello 与状态卡片画完
      try {
        if (SCROLL_TO) {
          await win.webContents.executeJavaScript(`(() => { const el = document.querySelector(${JSON.stringify(SCROLL_TO)}); if (el) el.scrollIntoView({ block: 'start' }); return !!el; })()`);
          await sleep(400);
        }
        const img = await win.webContents.capturePage();
        fs.writeFileSync(SHOT.slice('--screenshot='.length), img.toPNG());
        console.log(JSON.stringify({ screenshot: SHOT.slice('--screenshot='.length), size: img.getSize() }));
      } catch (e) { console.error('截图失败', e.message); }
      app.quit();
    });
  }
  win.loadURL(backend.url(hash));
  win.on('close', (e) => { if (!quitRequested) { e.preventDefault(); win.hide(); hintTray(); } });
  win.on('closed', () => { win = null; });
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
}

function showWindow(hash) {
  if (!win) createWindow(hash || '');
  else { if (hash) win.loadURL(backend.url(hash)); win.show(); win.focus(); }
}

function hintTray() {
  if (trayHinted) return;
  trayHinted = true;
  if (Notification.isSupported()) new Notification({ title: '巡检调度系统还在运行', body: '已收到托盘；巡检与定时计划继续执行。要彻底退出请用托盘菜单里的「退出」。' }).show();
}

function createTray() {
  tray = new Tray(trayImage());
  tray.setToolTip('巡检调度系统');
  tray.on('click', () => showWindow());
  rebuildTrayMenu();
}

function rebuildTrayMenu() {
  if (!tray) return;
  const menu = Menu.buildFromTemplate([
    { label: '打开页面', click: () => showWindow() },
    { label: '打开设置', click: () => showWindow('#/settings') },
    { type: 'separator' },
    { label: mockMode ? '当前：仿真模式（内置 mock 网关）' : '当前：真机模式（按设置里的云端地址）', enabled: false },
    { label: '仿真模式', type: 'checkbox', checked: mockMode, click: (item) => switchMode(item.checked) },
    { label: '状态…', click: showStatus },
    { type: 'separator' },
    { label: '打开数据目录', click: () => shell.openPath(backend.dataDir()) },
    { label: '打开日志目录', click: () => shell.openPath(L.logs) },
    { label: '开机自启', type: 'checkbox', checked: app.getLoginItemSettings().openAtLogin,
      click: (item) => app.setLoginItemSettings({ openAtLogin: item.checked }) },
    ...(autoUpdater ? [{ label: '检查更新…', click: () => { manualCheck = true; autoUpdater.checkForUpdates().catch((e) => notify('检查更新失败', e.message)); } }] : []),
    { type: 'separator' },
    { label: '退出（会先停下机器人）', click: () => app.quit() },
  ]);
  tray.setContextMenu(menu);
}

function notify(title, body) {
  log.info(`[notify] ${title}: ${body}`);
  if (Notification.isSupported()) new Notification({ title, body }).show();
}

async function showStatus() {
  const h = await backend.health();
  if (!h) return dialog.showMessageBox({ type: 'error', title: '状态', message: '后端没有响应', detail: `日志：${L.logs}` });
  const p = h.platform || {};
  const lines = [
    `模式 ${h.mode}   云端 ${h.host}   机器人 ${h.robot}`,
    `版本 ${h.version}   已运行 ${Math.round(h.uptime_s / 60)} 分钟   端口 ${backend.port}`,
    `线程：状态轮询 ${h.threads.status_poller ? '✓' : '✗'}  事件监听 ${h.threads.events_listener ? '✓' : '✗'}（${h.events && h.events.connected ? '已连' : '未连'}）  定时 ${h.threads.scheduler ? '✓' : '✗'}`,
    `执行中：${h.active_run ? JSON.stringify(h.active_run) : '无'}`,
    `Python ${p.python}   ffmpeg ${p.ffmpeg || '没找到'}`,
    `runtime ${p.runtime_dir || '（未用 runtime，开发模式）'}`,
    `字体 ${p.font || '没找到（标注里的中文会是方块）'}`,
    `数据目录 ${backend.dataDir()}`,
  ];
  await dialog.showMessageBox({ type: 'info', title: '状态', message: '巡检调度系统', detail: lines.join('\n'), buttons: ['好'] });
}

async function switchMode(mock) {
  const h = await backend.health();
  if (h && h.active_run) {
    const r = await dialog.showMessageBox({ type: 'warning', title: '切换模式', message: '有巡检正在执行。', detail: '切换模式会中止这趟执行并让机器人停下。', buttons: ['中止并切换', '取消'], defaultId: 1, cancelId: 1 });
    if (r.response !== 0) { rebuildTrayMenu(); return; }
  }
  tray && tray.setToolTip('正在切换模式…');
  try {
    await backend.stop();
    mockMode = mock; settings.mock = mock; saveSettings();
    await backend.start({ mock });
    if (win) win.loadURL(backend.url());
    notify(mock ? '已切到仿真模式' : '已切到真机模式', mock ? '内置 mock 网关已启动，数据目录 data-mock' : '按设置里的云端地址连接');
  } catch (e) {
    fatal(e);
  } finally {
    tray && tray.setToolTip('巡检调度系统');
    rebuildTrayMenu();
  }
}

// ── 退出：先停机器人 ──────────────────────────────────────────────────────────
async function quitFlow() {
  if (quitRequested) return;                                      // 第二次点退出 / Cmd+Q / 系统关机：已经在退了
  quitRequested = true;
  if (!backend) { readyToExit = true; app.exit(0); return; }      // 后端还没建起来（启动失败路径上用户按了 Cmd+Q）
  const h = await backend.health();
  if (h && h.active_run) {
    const r = await dialog.showMessageBox({
      type: 'warning', title: '退出', message: '有巡检正在执行。',
      detail: '退出会中止这趟执行并让机器人停下（最多等 40 秒）。', buttons: ['中止并退出', '取消'], defaultId: 1, cancelId: 1,
    });
    if (r.response !== 0) { quitRequested = false; return; }
  }
  if (tray) tray.setToolTip('正在停止（先停机器人）…');
  if (win) { win.destroy(); win = null; }                // 先断掉页面那条 SSE，后端的优雅退出才不会等它
  try { await backend.stop(); } catch (e) { log.error('停止后端出错', e); }
  readyToExit = true;
  app.exit(0);
}

const crashTimes = [];
async function onBackendExit(code, signal, stopping) {
  if (stopping || quitRequested) return;
  const now = Date.now();
  crashTimes.push(now);
  while (crashTimes.length && now - crashTimes[0] > 10 * 60 * 1000) crashTimes.shift();
  if (crashTimes.length > 5) {
    dialog.showErrorBox('后端反复退出', `10 分钟内退出了 ${crashTimes.length} 次，不再自动拉起。\n看日志：${L.logs}`);
    return;
  }
  notify('后端意外退出，正在重新拉起', `退出码 ${code}${signal ? ' 信号 ' + signal : ''}，日志在 ${L.logs}`);
  await sleep(2000);
  try {
    await backend.start({ mock: mockMode });
    if (win) win.loadURL(backend.url());
  } catch (e) { fatal(e); }
}

function fatal(e) {
  log.error(e);
  if (SMOKE) { console.error('FATAL:', e.message); app.exit(1); return; }
  dialog.showErrorBox('巡检调度系统启动失败', `${e.message}\n\n日志目录：${L ? L.logs : '?'}`);
  app.exit(1);
}

// ── 自动更新（只在打包后）─────────────────────────────────────────────────────
function setupAutoUpdate() {
  try { ({ autoUpdater } = require('electron-updater')); } catch (e) { log.warn('没有 electron-updater', e.message); return; }
  autoUpdater.logger = log;
  if (process.platform === 'darwin') autoUpdater.channel = `latest-${process.arch}`;   // 与 scripts/build_desktop.py 的 publish.channel 对应
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.on('update-available', (info) => { if (manualCheck) notify('发现新版本', `${info.version} 正在后台下载…`); });
  autoUpdater.on('update-not-available', () => { if (manualCheck) dialog.showMessageBox({ type: 'info', title: '检查更新', message: `已是最新版本（${app.getVersion()}）` }); manualCheck = false; });
  autoUpdater.on('error', (e) => { if (manualCheck) dialog.showMessageBox({ type: 'warning', title: '检查更新', message: '检查更新失败', detail: String(e && e.message || e) }); manualCheck = false; });
  autoUpdater.on('update-downloaded', async (info) => {
    manualCheck = false;
    const r = await dialog.showMessageBox({ type: 'info', title: '有新版本', message: `新版本 ${info.version} 已下载`, detail: '现在重启安装？若有巡检在执行会先停下机器人。', buttons: ['现在重启安装', '稍后'], defaultId: 0, cancelId: 1 });
    if (r.response !== 0) return;
    const h = await backend.health();
    if (h && h.active_run) {
      const r2 = await dialog.showMessageBox({ type: 'warning', title: '安装更新', message: '有巡检正在执行。', detail: '继续会中止这趟执行并让机器人停下。', buttons: ['中止并安装', '取消'], defaultId: 1, cancelId: 1 });
      if (r2.response !== 0) return;
    }
    quitRequested = true;
    if (win) { win.destroy(); win = null; }
    try { await backend.stop(); } catch (e) { log.error(e); }
    readyToExit = true;
    autoUpdater.quitAndInstall(false, true);
  });
  autoUpdater.checkForUpdates().catch((e) => log.warn('检查更新失败', e.message));
  setInterval(() => autoUpdater.checkForUpdates().catch(() => {}), 6 * 3600 * 1000);
}

// ── 自检模式（CI）────────────────────────────────────────────────────────────
async function smoke() {
  const t0 = Date.now();
  try {
    await backend.start({ mock: true });
    const h = await backend.health();
    if (!h || !h.ok) throw new Error('/api/health 不 ok');
    const expect = py.manifest ? py.manifest.python.version : null;
    if (expect && h.platform.python !== expect) throw new Error(`后端 Python ${h.platform.python} ≠ runtime ${expect}`);
    if (py.runtime && !h.platform.runtime_dir) throw new Error('后端没识别到 runtime 目录');
    if (py.runtime && !h.platform.ffmpeg) throw new Error('后端没找到 ffmpeg');
    if (py.runtime && !(h.platform.font || '').includes('NotoSansCJK')) throw new Error('后端没用上 runtime 里的字体');
    const st = await http('POST', backend.url('api/robot/status/refresh'), { timeoutMs: 15000 });
    if (st.status !== 200 || !st.json.reachable) throw new Error('内置 mock 网关不可达：' + st.text.slice(0, 200));
    const snap = await http('POST', backend.url('api/robot/snapshot'), { timeoutMs: 30000 });
    if (snap.status !== 200 || !/^\/media\/[^\\]+$/.test(snap.json.url)) throw new Error('抓图接口异常：' + snap.text.slice(0, 200));
    const t1 = Date.now();
    await backend.stop();
    if (backend.proc) throw new Error('后端没有退出');
    console.log(JSON.stringify({ smoke: 'ok', start_ms: t1 - t0, stop_ms: Date.now() - t1, python: h.platform.python,
      ffmpeg: h.platform.ffmpeg, runtime: h.platform.runtime_dir, font: h.platform.font, mode: h.mode, version: h.version }));
    app.exit(0);
  } catch (e) {
    console.error('SMOKE FAILED:', e.message);
    try { await backend.stop(); } catch { /* 尽力 */ }
    app.exit(1);
  }
}

// ── 入口 ──────────────────────────────────────────────────────────────────────
async function main() {
  L = layout();
  fs.mkdirSync(L.logs, { recursive: true });
  log.transports.file.resolvePathFn = () => path.join(L.logs, 'desktop.log');
  log.info(`启动 ${app.getName()} ${app.getVersion()} packaged=${app.isPackaged} platform=${process.platform}`);
  py = findPython(L);
  if (!py) return fatal(new Error(`找不到 Python 运行时：${L.runtime} 里没有 manifest.json。开发机先跑 python3 scripts/build_runtime.py`));
  log.info(`Python ${py.exe}${py.runtime ? '' : '（.venv，开发模式）'}`);
  backend = new Backend(L, py);
  backend.onExit = onBackendExit;
  if (SMOKE) return smoke();

  settings = loadSettings();
  mockMode = MOCK_FLAG || !!settings.mock;
  firstRun = !mockMode && !fs.existsSync(path.join(backend.dataDirFor(false), 'scheduler.db'));
  try { await backend.start({ mock: mockMode }); } catch (e) { return fatal(e); }
  if (process.platform === 'darwin') {
    Menu.setApplicationMenu(Menu.buildFromTemplate([{ role: 'appMenu' }, { role: 'editMenu' }, { role: 'windowMenu' }]));
  } else {
    Menu.setApplicationMenu(null);
  }
  if (app.isPackaged) setupAutoUpdate();
  createTray();
  createWindow(OPEN || (firstRun ? '#/settings' : ''));
  if (firstRun) {
    dialog.showMessageBox(win, {
      type: 'info', title: '第一次启动', message: '请先在「设置」里填云端地址、机器人别名与密钥。',
      detail: '想先看看效果：托盘菜单里勾上「仿真模式」，会用内置的 mock 网关跑一只仿真机器狗，数据与真机互不影响。\n关掉窗口不会退出，程序留在托盘继续跑；要彻底退出用托盘菜单里的「退出」。',
      buttons: ['知道了'],
    });
  }
}

app.setAppUserModelId(APP_ID);
if (!SMOKE && !app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => showWindow());
  app.on('activate', () => { if (backend && backend.port) showWindow(); });
  app.on('window-all-closed', () => { /* 留在托盘，不退出 */ });
  app.on('before-quit', (e) => { if (!readyToExit) { e.preventDefault(); quitFlow(); } });
  app.whenReady().then(main).catch(fatal);
}
