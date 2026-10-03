# 用户手册：桌面版安装与配置指南（PDF）

给现场用户的 Windows 安装、配置与使用手册：[巡检调度系统-安装与配置指南-v0.8.1.pdf](巡检调度系统-安装与配置指南-v0.8.1.pdf)（21 页，带书签）。
内容：安装 → 第一次启动 → 仿真模式试用 → 接入真机 → VLM 与播报 → 一次完整巡检 → 托盘 / 退出 / 开机自启 / 数据与备份 → 更新与卸载 → 安全须知 → 常见问题 → 附录。

| 文件 | 是什么 |
|------|--------|
| `guide.html` | 正文源文件（改内容改这里） |
| `img/` | Windows 11 上 0.8 实拍的截图（安装向导、第一次启动、托盘菜单、对话框等）；页面截图复用 `../screenshots/` |
| `print-pdf.js` | 用桌面壳自带的 Electron（Chromium）把 `guide.html` 打成 A4 PDF |
| `fix_outline.py` | 清理书签标题（Chromium 生成的书签在分页处会把标题重复一遍），并设置打开时显示书签栏 |

重新生成（Windows，仓库根目录，先 `cd desktop && npm ci` 装好 Electron）：

```powershell
$env:ELECTRON_RUN_AS_NODE = $null
$pdf = "docs\guide\巡检调度系统-安装与配置指南-v0.8.1.pdf"
desktop\node_modules\electron\dist\electron.exe docs\guide\print-pdf.js docs\guide\guide.html $pdf
python docs\guide\fix_outline.py $pdf          # 需要 pip install pypdf
```

正文字体用微软雅黑（Windows 自带）。不要换成 Noto / 思源黑体：Chromium 会把这类 CFF 字体嵌成 Type3，
而且会把「用」「一」等字映射成康熙部首（U+2F00 段），PDF 里搜索、复制中文会出错。

改了界面或流程要同步改这份手册；截图按 `docs/WINDOWS_TEST.md` 的流程在 Windows 上重截。
