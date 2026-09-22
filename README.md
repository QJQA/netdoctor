<p align="center"><b>简体中文</b> · <a href="#english">English</a></p>

# Network Doctor · 宽带故障检测助手

家庭网络故障检测工具。同时 ping 路由器和多个独立外网目标，自动判断问题出在家里 WiFi、光猫之后的外部线路，还是某个设备的周期性后台任务（比如 AirDrop、云同步）。

诞生于一次真实的排查：家里联通线路每隔几分钟掉线一次，用"分层对比 + 找规律 + 出证据"的方法定位到问题，顺手把这套方法写成了工具。

🌐 介绍页：[networkdoctor.pages.dev](https://networkdoctor.pages.dev/)

## 特点

- **零依赖**：只用 Python 标准库，不用装任何包
- **多目标交叉验证**：默认测两个独立公共 DNS，避免"单个目标自己抽风"被误判成你家网络的问题
- **自动识别规律**：区分"固定周期跳高"（多是设备后台任务）和"连续整段掉线"（多是线路掉线重拨）
- **本地网页界面**：开始/停止按钮 + 实时曲线，不用碰命令行
- **断线不丢进度**：网页刷新或关掉标签页，后台继续记录，重新打开自动接上

## 使用

**方式一：双击启动**（macOS）

双击 `启动网络监测.command`，自动打开浏览器界面。

**方式二：命令行**

```bash
# 网页界面（默认端口 7656，自动打开浏览器）
python3 server.py

# 纯命令行版本
python3 netdoctor.py                 # 一直跑，按 Ctrl+C 结束并生成报告
python3 netdoctor.py -m 30           # 跑 30 分钟后自动结束
python3 netdoctor.py --gw 192.168.1.1 --wan 223.5.5.5 119.29.29.29
```

跑完会在 `runs/netdoctor_YYYYMMDD_HHMMSS/` 目录下生成一份带交互曲线图的 HTML 报告，附结论和可以直接拿去找运营商报修的说明。

## 环境要求

macOS / Linux，Python 3.7+（自带的 `python3` 即可，无需 `pip install` 任何东西）。

## 原理

```
你的设备 ──① 路由器（家里内网）── 光猫 ── ② 外网（独立 DNS × 2）
```

同时测两段延迟：哪一段先出问题，卡顿就是卡在哪一段。路由器一直稳定但外网周期性掉线 → 问题在运营商线路；路由器自己也跳高 → 问题在家里 WiFi。

## License

MIT

---

<a id="english"></a>

<p align="center"><a href="#network-doctor--宽带故障检测助手">简体中文</a> · <b>English</b></p>

## Network Doctor

A local tool for tracking down flaky home broadband. It pings your router and several independent internet targets at the same time, and tells you whether the problem is your own WiFi, the line past your modem, or a periodic background task on one of your devices (AirDrop, cloud sync, and the like).

It grew out of a real troubleshooting session: a home internet connection that dropped every few minutes. The method used to pin it down — watch two segments at once, look for a pattern, and get hard evidence — turned into this tool.

🌐 Landing page: [networkdoctor.pages.dev](https://networkdoctor.pages.dev/)

### Features

- **Zero dependencies** — standard-library Python only, nothing to install
- **Cross-validated targets** — tests two independent public DNS servers by default, so a single flaky target can't be mistaken for a problem on your own network
- **Pattern detection** — tells a fixed-period spike (usually a background task) apart from a continuous outage (usually the line dropping and redialing)
- **Local web UI** — start/stop buttons and a live chart, no terminal required
- **Survives interruption** — reload the page or close the tab and recording keeps going in the background; reopening picks it back up automatically

### Usage

**Option 1: double-click launcher** (macOS)

Double-click `启动网络监测.command` to open the web UI in your browser automatically.

**Option 2: command line**

```bash
# Web UI (port 7656 by default, opens your browser automatically)
python3 server.py

# CLI only
python3 netdoctor.py                 # runs until Ctrl+C, then writes a report
python3 netdoctor.py -m 30           # run for 30 minutes and stop automatically
python3 netdoctor.py --gw 192.168.1.1 --wan 223.5.5.5 119.29.29.29
```

Each run writes a self-contained HTML report with an interactive chart to `runs/netdoctor_YYYYMMDD_HHMMSS/`, including a verdict and a summary you can hand to your ISP's support line.

### Requirements

macOS / Linux, Python 3.7+ (the system-provided `python3` is enough — nothing to `pip install`).

### How it works

```
Your device ── ① Router (home LAN) ── Modem ── ② Internet (2 independent DNS targets)
```

Both segments are measured at the same time: whichever one breaks first tells you where the fault is. Router stable but the internet target drops out periodically → the fault is upstream, past your modem. Router itself spikes too → the fault is your own WiFi.

### License

MIT
