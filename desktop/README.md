# desktop/ —— 桌面壳（Electron）

只做四件事：拉起随包携带的 Python 后端、给它一个窗口与托盘、优雅退出（先停机器人）、自动更新。
不含任何业务逻辑，后端就是仓库里的 `app/`。架构、构建、发布与排障全部写在 [`../docs/DESKTOP.md`](../docs/DESKTOP.md)。

```bash
python3 scripts/build_runtime.py      # 先组装 runtime/（Python 3.12 + 依赖 + ffmpeg + 字体）
cd desktop && npm ci
npm run smoke                         # 自检：拉起后端（仿真模式）→ 健康检查 → 优雅退出，不开窗口
npm run mock                          # 开发：带窗口与托盘，仿真模式
python3 scripts/build_desktop.py      # 出本平台的安装包到 desktop/dist/
```
