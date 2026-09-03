// 全景角度编辑器：一张 2:1 等距全景 + 两条可拖拽竖线，给出 [from, to] 角度范围（支持跨缝）。
import { h } from '../api.js';

export class PanoEditor {
  constructor(container, { angleFrom = 150, angleTo = 210, forwardDeg = 180, onChange } = {}) {
    this.from = norm(angleFrom); this.to = norm(angleTo); this.forward = forwardDeg; this.onChange = onChange;
    this.el = h(`<div class="pano-editor">
      <div class="pano-stage"><img alt="" draggable="false"><div class="pano-empty">尚无参考图 —— 点「抓一张」或上传</div>
        <div class="pano-shade s1"></div><div class="pano-shade s2"></div>
        <div class="pano-forward"><span>机头</span></div>
        <div class="pano-line from" data-which="from"><span class="tag"></span></div>
        <div class="pano-line to" data-which="to"><span class="tag"></span></div>
      </div>
      <div class="pano-ruler"></div>
      <div class="pano-readout"><span class="range"></span><span class="rel muted small"></span></div></div>`);
    container.appendChild(this.el);
    this.stage = this.el.querySelector('.pano-stage'); this.img = this.el.querySelector('img');
    this.img.style.display = 'none';
    const ruler = this.el.querySelector('.pano-ruler');
    for (let d = 0; d < 360; d += 30) ruler.appendChild(h(`<span style="left:${d / 3.6}%">${d}°</span>`));
    ruler.appendChild(h(`<span style="left:100%">360°</span>`));
    this._drag = null;
    this.stage.addEventListener('pointerdown', e => this._down(e));
    this.stage.addEventListener('pointermove', e => this._move(e));
    this.stage.addEventListener('pointerup', e => this._up(e));
    this.stage.addEventListener('pointercancel', e => this._up(e));
    this.render();
  }
  setImage(url) { if (url) { this.img.src = url; this.img.style.display = ''; this.el.querySelector('.pano-empty').style.display = 'none'; } else { this.img.style.display = 'none'; this.el.querySelector('.pano-empty').style.display = ''; } }
  setRange(from, to, silent = false) { this.from = norm(from); this.to = norm(to); this.render(); if (!silent) this._emit(); }
  setForward(d) { this.forward = d; this.render(); }
  getRange() { return { angle_from: round1(this.from), angle_to: round1(this.to) }; }
  span() { const s = (this.to - this.from + 360) % 360; return s === 0 ? 360 : s; }
  _angleOf(e) { const r = this.stage.getBoundingClientRect(); return norm(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * 360); }
  _down(e) {
    const line = e.target.closest('.pano-line');
    let which = line ? line.dataset.which : null;
    if (!which) { const a = this._angleOf(e); which = circDist(a, this.from) <= circDist(a, this.to) ? 'from' : 'to'; this[which] = a; this.render(); }
    this._drag = which; this.stage.setPointerCapture(e.pointerId); e.preventDefault();
  }
  _move(e) { if (!this._drag) return; this[this._drag] = this._angleOf(e); this.render(); }
  _up() { if (this._drag) { this._drag = null; this._emit(); } }
  _emit() { this.onChange && this.onChange(this.getRange()); }
  render() {
    const pct = a => `${(norm(a) / 360) * 100}%`;
    const L = this.el.querySelector('.from'), R = this.el.querySelector('.to');
    L.style.left = pct(this.from); R.style.left = pct(this.to);
    L.querySelector('.tag').textContent = `${round1(this.from)}°`; R.querySelector('.tag').textContent = `${round1(this.to)}°`;
    const s1 = this.el.querySelector('.s1'), s2 = this.el.querySelector('.s2');
    if (this.from <= this.to) { s1.style.left = '0'; s1.style.width = pct(this.from); s2.style.left = pct(this.to); s2.style.width = `${100 - norm(this.to) / 3.6}%`; }
    else { s1.style.left = pct(this.to); s1.style.width = `${(this.from - this.to) / 3.6}%`; s2.style.width = '0'; }
    const f = this.el.querySelector('.pano-forward'); f.style.left = pct(this.forward);
    const rel = a => { const d = ((a - this.forward + 540) % 360) - 180; return `${d >= 0 ? '+' : ''}${d.toFixed(0)}°`; };
    this.el.querySelector('.range').innerHTML = `<b>${round1(this.from)}° → ${round1(this.to)}°</b>　宽 ${this.span().toFixed(0)}°${this.from > this.to ? '（跨接缝）' : ''}`;
    this.el.querySelector('.rel').textContent = `相对机头 ${rel(this.from)} … ${rel(this.to)}`;
  }
}
function norm(a) { a = Number(a) % 360; return a < 0 ? a + 360 : a; }
function round1(a) { return Math.round(norm(a) * 10) / 10; }
function circDist(a, b) { const d = Math.abs(norm(a) - norm(b)); return Math.min(d, 360 - d); }
