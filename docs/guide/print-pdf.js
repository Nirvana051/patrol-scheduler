// 用 Electron（Chromium）把 guide.html 打成 A4 PDF：electron print-pdf.js <in.html> <out.pdf>
'use strict';
const { app, BrowserWindow } = require('electron');
const fs = require('fs');
const path = require('path');

const [src, out] = process.argv.slice(-2).map((p) => path.resolve(p));

app.whenReady().then(async () => {
  try {
    const win = new BrowserWindow({ show: false, width: 1100, height: 1500 });
    await win.loadFile(src);
    await win.webContents.executeJavaScript(`Promise.all([document.fonts.ready,
      ...[...document.images].map((i) => i.complete ? 1 : new Promise((r) => { i.onload = i.onerror = r; }))]).then(() => true)`);
    const broken = await win.webContents.executeJavaScript('[...document.images].filter((i) => !i.naturalWidth).map((i) => i.getAttribute("src"))');
    if (broken.length) throw new Error('图片缺失：' + broken.join(', '));
    const footer = `<div style="font-family:'Microsoft YaHei',sans-serif;font-size:7.5px;color:#9ca3af;width:100%;padding:0 16mm;display:flex;justify-content:space-between">
      <span>巡检调度系统 · 安装与配置指南 · v0.8.1</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`;
    const pdf = await win.webContents.printToPDF({
      printBackground: true, preferCSSPageSize: true, displayHeaderFooter: true,
      headerTemplate: '<div></div>', footerTemplate: footer, generateDocumentOutline: true, generateTaggedPDF: true,
    });
    fs.writeFileSync(out, pdf);
    console.log(JSON.stringify({ out, bytes: pdf.length }));
    app.exit(0);
  } catch (e) {
    console.error('PDF 失败：', e.message);
    app.exit(1);
  }
});
