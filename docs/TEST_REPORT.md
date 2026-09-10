# 测试报告 —— 2026-09-09 夜（回滚到 73b721b + .venv 隔离改造）

**范围**：`main` 回滚到 `73b721b` 之后，重建隔离的 Python 环境、修掉过程中暴露的缺陷，
并做一轮压力测试。全程 **`VLM_PROVIDER=mock` + `TTS_ENGINE=none`，未调用任何付费接口
（qwen3.5 消耗为 0）**，也未触碰生产库 `data/scheduler.db`（前后 md5 一致）。

> 长跑（浸泡 + 用例连跑 + 环境守卫）**没有跑到预定的 2026-09-10 13:00**：
> 会话结束时后台进程被一并收掉，日志目录（会话级临时目录）也随之清空。
> 已观测到的部分见「§6 长跑结果」，缺失的部分**不做推断**。

---

## 1. 回滚

| 项 | 结果 |
|---|---|
| 目标 | `73b721b4c223173df946e86f76bc4711f878cdcf`（09-08 19:55，即已推送到 origin 的那个版本）|
| HEAD | ✅ 精确落在该提交 |
| 被回退的内容 | 一段跨平台重写（**135 个提交**），从未推送过远端 |
| 后续决定 | 那段重写**已确认放弃**，本仓库只保留这一条 Python 线；相关分支与标签已删除 |
| 生产库 | ✅ `data/scheduler.db` md5 前后一致（`0b26f1c5…`），95 MB 未变 |
| 真实密钥 | ✅ `config/.env` md5 未变，权限仍 `600`（`.gitignore` 里，`reset --hard` 不触及）|

## 2. 为什么 Ubuntu 上「不稳定」——查出来的根因

原 `.venv` 是 `include-system-site-packages = true` 建的，而这台机器的系统 Python
就是机器人的 **ROS 2 + CUDA + torch** 环境。更要紧的是 `~/.bashrc` 里
`source /opt/ros/humble/setup.bash` 与本机的 ROS 工作区，`PYTHONPATH` 带着 **11 个目录**，
并且排在 venv 的 `site-packages` **前面**：

```
/home/<另一个用户>/<ros-ws>/build/<pkg>                   ← 别的用户的家目录，且不存在
/home/<本机用户>/<ros-ws>/install/<pkg>/...              （共 9 个工作区目录）
/opt/ros/humble/lib/python3.10/site-packages
/opt/ros/humble/local/lib/python3.10/dist-packages
```

后果（实测）：

| | 改造前 | 改造后 |
|---|---|---|
| venv 里可见的包 | **445 个**（含 `rclpy` `ros2cli` `tf2-*` `moveit-*` 若干内部 ROS 包、`torch+cu13x` `ultralytics` `PyQt5` …）| **55 个** |
| `sys.path` 里的外来目录 | 11 个 | **0 个** |
| 同时存在两个版本的包 | numpy(1.21.5/1.26.0)、pytest(6.2.5/8.4.2)、scipy、matplotlib、PyYAML、sympy… | 无 |
| 关键包的实际来源 | `requests`/`PIL` 来自 **apt**（`/usr/lib/python3/dist-packages`）、`numpy` 来自 **`~/.local`** | 全部来自 venv 内部 |
| venv 里有 pip 吗 | **没有**（装不了、修不了任何东西），而系统 python 的 `ensurepip` 也被删了 | 有（pip 26.2.1）|

也就是说：**`apt upgrade` 动一下 ROS 或系统 python，巡检系统 import 到的东西就会悄悄变**，
而文档里教的 `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
在这台机器上根本跑不通。

## 3. 做了什么

- **新增 `bootstrap.sh`**：找 3.10+ 解释器（优先 `uv`，退回 `python -m venv`）→ 建**隔离** venv
  → 按 `requirements.lock` 装**精确版本** → 自检「每个包都来自 venv 内部、`sys.path` 无外来目录」。
  `ensurepip` 与 `uv` 都没有时给出两条明确出路，而不是含糊报错。
- **环境净化**：`start.sh` / `run.sh` / `bootstrap.sh` 启动前 `unset PYTHONPATH PYTHONHOME`
  并设 `PYTHONNOUSERSITE=1`；`Makefile` 的 **11 处** python 调用统一走 `PYRUN`；
  systemd 单元加 `UnsetEnvironment=PYTHONPATH PYTHONHOME`。没有 ROS 的机器上这些是无害空操作。
- **`requirements.lock` 重新生成为 52 个精确版本**：FastAPI 栈钉在生产已验证的版本
  （fastapi 0.141.1 / uvicorn 0.52.4 / starlette 1.6.0 / pydantic 2.13.5）；
  `requests`、`Pillow` 原来来自 apt（2.25.1 / 9.0.1，都是 2021–2022 的老版本），
  改成 venv 内的当前稳定版（2.34.2 / 12.3.0）并**靠全量用例证明**。
- **新增 `scripts/soak.py`**（Python 版浸泡测试，22 类场景）。
- **文档**：USAGE §1 重写为 `bootstrap.sh`，并写明 `PYTHONPATH` 这个坑与手工跑时的规避写法。

## 4. 顺带查出的真缺陷

| # | 缺陷 | 怎么发现的 |
|---|---|---|
| 1 | `app/media/pano.py` 合成图里**门扇矩形坐标是反的**（小图上 `door_w*2-12` 变负 → `x1 < x0`）。Pillow 9 静默吞掉、Pillow 12 直接 `ValueError` —— 也就是说这个门框在小图上**一直没画出来过**，只是以前没人知道 | 换到隔离环境（Pillow 12）后 `tests/test_pano.py::test_annotate_and_synth` 报错。修法：画不出来就不画，不是去钉住老 Pillow |
| 2 | 文档里的安装命令在这台机器上跑不通（`ensurepip` 缺失）| 照文档走一遍 |
| 3 | `Makefile` 的 `make test` 会带着 ROS 的 `PYTHONPATH` 跑（用到 ROS 环境里的 numpy）| 查净化覆盖面时发现 |

## 5. 测试结果（截至 2026-09-09 23:50）

| 项目 | 结果 |
|---|---|
| **全量用例（隔离 venv）** | ✅ **118 / 118 通过**，246 秒（Pillow 12 + requests 2.34.2 + numpy 1.26.0）|
| **语法检查** `ruff` | ✅ 全通过 |
| **端到端（mock）** | ✅ 4 段完成、3 次检查全过（`scripts/seed_demo.py --init --run`，独立数据目录）|
| **浸泡测试 22 类场景** | ✅ **22 / 22 全绿，0 失败**；线程 33 → 33、fd 29 → 29 稳定 |
| **干净 clone → 能跑** | ✅ `git clone`（无 `.venv`/`data`/`config/.env`）→ `./bootstrap.sh --dev` **9.2 秒**（uv 缓存）→ 隔离自检 9 项全过 → **`pytest` 118/118 通过**（245.7 秒）；脚本行尾都是 LF。这就是「换一台 Ubuntu 机器能跑起来」的完整凭据 |
| **环境隔离守卫** | ✅ 55 包 / 0 外来路径 / 0 个 venv 外的包 |
| **qwen3.5 API 消耗** | ✅ **0 次调用**（全程 `VLM_PROVIDER=mock`）|
| **生产库与密钥** | ✅ 未触碰（md5 一致）|

### 浸泡测试覆盖的 22 类场景

正常路径（4）：单段 / 三段 / 返回起点 / 十个航点的长任务
故障注入（9）：避障失败、规划失败后重试、短暂掉线可容忍、长时间掉线判失败、持续避障停滞、
丢定位、被外部任务替换、网关 HTML 502、事件全丢（靠 5 秒对账兜住）
人工操作（4）：人工中止、机器人忽略停止指令、暂停后继续、现场抢控制权
运维动作（4）：巡检中在线备份（并校验备份库 `integrity_check`）、巡检中改设置、
巡检中导入备份、巡检中重新同步地图
网络（1）：反复抖动（4 次断连重连）

每轮都核对**预期结局**与**幂等键唯一性**，并记 RSS / 线程数 / fd 数。

### 写浸泡脚本时修掉的四个脚手架缺陷（否则跑一夜也是白跑）

1. **异常时不收掉在跑的那趟** → 后面每一轮都撞 `409 已有执行在进行`，
   一次异常毁掉整晚（实测第 10 轮抛异常后 11–22 轮全废）。**最严重的一条。**
2. `mid` 固定 `sleep 1` 会落在前置检查里，把「机器人掉线」变成「前置检查不通过」——
   3 个掉线类场景全是这么误报的。改成等段真的下发之后再注入。
3. 复位漏传 `prelocalized` / `prestarted` → 每轮都卡在「定位未就绪」直接 aborted。
4. 缺端口预检 + 子进程不成组 → 上一次被 `timeout` 掐掉的残留占着端口，
   而健康检查却打在**陌生实例**上，看起来「起来了」。

## 6. 长跑结果（只跑了 8 分钟，被中断）

三条并行任务于 2026-09-09 **23:46 启动**，计划跑到 09-10 13:00，全部 mock、零 API 消耗：

| 任务 | 计划 | 实际观测到的（截至 23:54） |
|---|---|---|
| 浸泡测试 | 22 类场景轮换到 13:00 | **39 轮，0 失败**；RSS 78–86 MB 区间波动、线程 33、fd 29 全程不变 |
| 全量用例连跑 | `pytest` 反复跑 | **第 1 次 118/118 通过**（242 秒）|
| 环境隔离守卫 | 每 20 分钟检查 | **1 次，✅**（55 包 / 0 外来路径 / 0 个 venv 外的包）|

**为什么只有 8 分钟**：这三条是会话内的后台进程，会话结束时被一并收掉；
日志与浸泡数据目录在会话级临时目录下，也随之清空。**23:54 之后没有任何数据，不做推断。**

要补这一段，重跑即可（三条命令都在仓库里，互不依赖）：

```bash
# ① 浸泡（22 类场景，跑到指定时刻；不消耗任何付费接口）
env -u PYTHONPATH -u PYTHONHOME PYTHONNOUSERSITE=1 \
  nohup .venv/bin/python -u scripts/soak.py --until "2026-09-11T13:00" \
  --port 39301 --mock-port 18531 --dir ~/ps-soak > ~/ps-soak.log 2>&1 &

# ② 全量用例（单次约 4 分）
make test

# ③ 环境隔离自检（确认 venv 没被 PYTHONPATH 污染）
./bootstrap.sh
```

想让它真的跑一夜而不依赖会话，用 `setsid` 或 systemd user 单元起浸泡那条
（`systemd-run --user --unit=ps-soak …`），否则会话一结束进程就没了 —— 这次就是这么丢的。
