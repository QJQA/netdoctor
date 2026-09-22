#!/usr/bin/env python3
"""
netdoctor.py — Mac 家庭网络持续监测

同时 ping【家里路由器】和【多个独立外网目标】，每秒一次，实时显示延迟，
结束后生成一份带对比曲线的 HTML 报告，并给出判断：
卡顿出在家里 WiFi/内网、外部线路，还是某个设备的周期性后台任务。

用法（只需要 Mac 自带的 python3，无需安装任何库）：
  python3 netdoctor.py                          # 一直跑，按 Ctrl+C 结束并生成报告
  python3 netdoctor.py -m 60                    # 跑 60 分钟后自动结束
  python3 netdoctor.py --gw 192.168.1.1 --wan 223.5.5.5 119.29.29.29  # 手动指定地址
"""
import argparse
import csv
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

RTT_RE = re.compile(r"time[=<]([\d.]+)\s*ms")

WAN_SPIKE_MS = 100   # 外网延迟超过这个值算一次"卡顿"
GW_SPIKE_MS = 50     # 路由器延迟超过这个值算内网异常（正常 WiFi 一般 < 10ms）

DEFAULT_WAN = ["223.5.5.5", "119.29.29.29"]  # 阿里 DNS、腾讯 DNS：两家独立目标便于交叉验证

KNOWN_HOSTS = {
    "223.5.5.5": "阿里DNS",
    "119.29.29.29": "腾讯DNS",
    "114.114.114.114": "114DNS",
    "8.8.8.8": "Google DNS",
    "1.1.1.1": "Cloudflare DNS",
}

WAN_COLORS = ["--wan", "--wan2", "--wan3", "--wan4"]


def wan_label(host):
    return KNOWN_HOSTS.get(host, host)


def detect_gateway():
    """自动找到当前网络的路由器地址。"""
    try:
        out = subprocess.run(["route", "-n", "get", "default"],
                             capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"gateway:\s*(\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "192.168.1.1"


def ping_once(host, timeout_ms):
    """发一个 ping 包，返回延迟毫秒数；超时/失败返回 None。"""
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", str(timeout_ms), host],
                           capture_output=True, text=True,
                           timeout=timeout_ms / 1000 + 2)
        m = RTT_RE.search(r.stdout)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def precheck(targets, timeout_ms):
    """跑之前粗测一遍每个目标，剔除完全不回应 ping 的外网目标（避免整轮测试作废）。"""
    ok_targets = {}
    notes = []
    for key, host in targets.items():
        results = [ping_once(host, timeout_ms) for _ in range(2)]
        reachable = any(r is not None for r in results)
        if reachable or key == "gw":
            ok_targets[key] = host
            if not reachable:
                notes.append(f"⚠ 路由器 {host} 预检未回应 ping，仍会继续监测（可能是防火墙屏蔽了 ping）。")
        else:
            notes.append(f"⚠ 已跳过 {wan_label(host)}（{host}）：预检 2 次都不回应，该服务器可能本身屏蔽 ping。")
    return ok_targets, notes


def worker(key, host, interval, timeout_ms, records, latest, lock, stop):
    next_t = time.time()
    while not stop.is_set():
        ts = time.time()
        rtt = ping_once(host, timeout_ms)
        with lock:
            records.append((ts, key, rtt))
            latest[key] = rtt
        next_t = max(next_t + interval, time.time())
        stop.wait(max(0.0, next_t - time.time()))


# ---------------------------------------------------------------- 统计与判断

def stats(values, spike_ms):
    n = len(values)
    ok = sorted(v for v in values if v is not None)
    lost = n - len(ok)

    def pct(p):
        if not ok:
            return None
        return ok[min(len(ok) - 1, int(round(p / 100 * (len(ok) - 1))))]

    return {
        "n": n,
        "lost": lost,
        "loss": (lost / n * 100) if n else 0.0,
        "avg": (sum(ok) / len(ok)) if ok else None,
        "p50": pct(50),
        "p95": pct(95),
        "max": ok[-1] if ok else None,
        "spikes": sum(1 for v in values if v is None or v > spike_ms),
    }


def spike_events(series, spike_ms):
    """series: [(ts, v), ...]，返回超时/超阈值的时间戳（升序）。"""
    return sorted(t for t, v in series if v is None or v > spike_ms)


def group_blocks(events_ts, interval, gap_tol=1.5):
    """把相邻（间隔 <= interval*gap_tol）的事件合并成一段。"""
    if not events_ts:
        return []
    blocks = [[events_ts[0]]]
    for t in events_ts[1:]:
        if t - blocks[-1][-1] <= interval * gap_tol:
            blocks[-1].append(t)
        else:
            blocks.append([t])
    return blocks


def classify_pattern(events_ts, interval):
    """区分三种规律：
    - periodic  固定周期反复出现（多是某个后台任务，比如 AirDrop、云同步）
    - continuous 连续多个采样点掉线/超标（多是线路整体掉线重拨）
    - irregular 零散，没有明显规律
    """
    if len(events_ts) < 2:
        return "none", {}

    blocks = group_blocks(events_ts, interval)
    block_durs = [(b[-1] - b[0]) + interval for b in blocks]
    long_blocks = [d for d in block_durs if d >= interval * 3]

    starts = [b[0] for b in blocks]
    if len(starts) >= 4:
        diffs = sorted(b - a for a, b in zip(starts, starts[1:]))
        mid = diffs[len(diffs) // 2]
        if mid > 0:
            close = [d for d in diffs if abs(d - mid) <= max(1.0, mid * 0.25)]
            if len(close) / len(diffs) >= 0.6 and not long_blocks:
                return "periodic", {"period": mid, "count": len(starts)}

    if long_blocks:
        return "continuous", {
            "count": len(long_blocks),
            "avg_dur": sum(long_blocks) / len(long_blocks),
            "max_dur": max(long_blocks),
        }

    return "irregular", {"count": len(blocks)}


def diagnose(records, targets, interval):
    keys = list(targets.keys())
    wan_keys = [k for k in keys if k != "gw"]
    series = {k: [(t, v) for t, kk, v in records if kk == k] for k in keys}
    stat = {k: stats([v for _, v in series[k]],
                      GW_SPIKE_MS if k == "gw" else WAN_SPIKE_MS)
            for k in keys}
    meta = {}

    enough_wan = any(stat[k]["n"] >= 30 for k in wan_keys)
    if "gw" not in stat or stat["gw"]["n"] == 0 or not enough_wan:
        verdict = ("数据太少", "记录时间太短，建议至少跑 10 分钟，最好在平时会卡的时段跑 30–60 分钟。")
        return stat, verdict, meta

    # 1) 先看内网/路由器
    if stat["gw"]["loss"] >= 1 or stat["gw"]["spikes"] >= 3:
        gw_events = spike_events(series["gw"], GW_SPIKE_MS)
        pattern, detail = classify_pattern(gw_events, interval)
        meta = {"scope": "gw", "pattern": pattern, "detail": detail}
        if pattern == "periodic":
            verdict = ("问题很可能是某个后台任务，而不是网络线路",
                       f"路由器延迟每约 {detail['period']:.0f} 秒规律性跳高（共 {detail['count']} 次）。"
                       "这种精准的固定周期通常是设备后台在发起流量（例如 AirDrop、云同步、自动备份），"
                       "可以逐个关掉可疑的后台功能后重跑一次确认。")
        else:
            verdict = ("问题在家里 WiFi / 内网",
                       f"连家里路由器都出现了延迟飙高或丢包（异常 {stat['gw']['spikes']} 次）。"
                       "优先处理 WiFi：改连 5G 频段、靠近路由器、插网线对比，或考虑更换路由器/组 Mesh。")
        return stat, verdict, meta

    # 2) 路由器正常，看外网——多目标交叉验证，避免误判成"某个服务器自己不回应"
    wan_event_sets = {k: spike_events(series[k], WAN_SPIKE_MS)
                       for k in wan_keys if stat[k]["n"] >= 30}

    if len(wan_event_sets) >= 2:
        base_key = min(wan_event_sets, key=lambda k: len(wan_event_sets[k]))
        confirmed = [t for t in wan_event_sets[base_key]
                     if all(any(abs(t - t2) <= 1.5 for t2 in evs)
                            for k2, evs in wan_event_sets.items() if k2 != base_key)]
        confidence = "高（多个独立外网目标同时复现）"
    elif len(wan_event_sets) == 1:
        confirmed = list(wan_event_sets.values())[0]
        confidence = "中（只有一个外网目标存活，建议加第二个目标交叉验证）"
    else:
        confirmed, confidence = [], "—"

    if len(confirmed) >= 3:
        confirmed.sort()
        pattern, detail = classify_pattern(confirmed, interval)
        meta = {"scope": "wan", "pattern": pattern, "detail": detail, "confidence": confidence}
        if pattern == "continuous":
            verdict = ("问题在光猫之后的外部线路",
                       f"外网连续出现 {detail['count']} 段整体超时（平均约 {detail['avg_dur']:.0f} 秒，"
                       f"最长 {detail['max_dur']:.0f} 秒），期间路由器段始终正常。置信度：{confidence}。"
                       "这种整段掉线多是光猫拨号反复重连或运营商线路波动，可以把这份报告拿去找运营商报修。")
        elif pattern == "periodic":
            verdict = ("外网延迟呈规律性波动",
                       f"外网每约 {detail['period']:.0f} 秒出现一次卡顿（共 {detail['count']} 次）。置信度：{confidence}。"
                       "较少见，可能是路由器 QoS 策略、定时任务或运营商限速触发，建议结合具体时间点排查。")
        else:
            verdict = ("问题在光猫之后的外部线路",
                       f"路由器一直稳定，但外网出现 {len(confirmed)} 次卡顿，暂未看出固定规律。置信度：{confidence}。"
                       "如果集中在有人下载/看视频的时段，开路由器 QoS；如果反复出现且与家里用网无关，可拿这份报告找运营商报修。")
        return stat, verdict, meta

    verdict = ("这段时间网络稳定",
               "记录期间没有抓到明显卡顿（或未能在多个外网目标间交叉确认）。卡顿是偶发的，建议在玩游戏、刷视频卡的时段再跑一次。")
    return stat, verdict, meta


# ---------------------------------------------------------------- 报告

def fmt(v, suffix=" ms"):
    return "—" if v is None else f"{v:.1f}{suffix}"


def build_report(records, targets, names, interval, started, path):
    stat, verdict, meta = diagnose(records, targets, interval)
    order = [k for k in ("gw", *sorted(k for k in targets if k != "gw")) if k in targets]

    color_of = {"gw": "--gw"}
    for i, k in enumerate(k for k in order if k != "gw"):
        color_of[k] = WAN_COLORS[i % len(WAN_COLORS)]

    series_js = {}
    for k in order:
        pts = [[round(t, 2), None if v is None else round(v, 1)]
               for t, kk, v in records if kk == k]
        series_js[k] = {"name": names[k], "color": color_of[k], "points": pts}

    all_ok = sorted(v for _, _, v in records if v is not None)
    p99 = all_ok[int(0.99 * (len(all_ok) - 1))] if all_ok else 100
    ymax = max(150, min(600, p99 * 1.3))
    t0 = min(t for t, _, _ in records)
    t1 = max(t for t, _, _ in records)
    data = {"series": series_js, "order": order, "t0": t0, "t1": t1,
            "ymax": ymax, "spike": WAN_SPIKE_MS}

    dur_min = (t1 - t0) / 60

    def row(key):
        s = stat[key]
        return (f"<tr><td>{html.escape(names[key])}</td><td>{s['n']}</td>"
                f"<td class='{ 'bad' if s['loss'] >= 1 else '' }'>{s['loss']:.1f}%</td>"
                f"<td>{fmt(s['avg'])}</td><td>{fmt(s['p50'])}</td><td>{fmt(s['p95'])}</td>"
                f"<td>{fmt(s['max'])}</td><td>{s['spikes']}</td></tr>")

    table = "".join(row(k) for k in order)

    legend = "".join(
        f'<span><i style="background:var({color_of[k]})"></i>{html.escape(names[k])}</span>'
        for k in order
    )

    n_wan_events = sum(1 for t, k, v in records
                       if k != "gw" and (v is None or v > WAN_SPIKE_MS))
    events_note = f"外网卡顿样本 {n_wan_events} 次"
    if meta.get("scope") == "wan" and meta.get("pattern") in ("continuous", "periodic", "irregular"):
        events_note += f"，判定用到 {meta['detail'].get('count', '-')} 个确认事件"

    page = (TEMPLATE
            .replace("__START__", started.strftime("%Y-%m-%d %H:%M"))
            .replace("__DUR__", f"{dur_min:.1f}")
            .replace("__VERDICT_T__", html.escape(verdict[0]))
            .replace("__VERDICT_D__", html.escape(verdict[1]))
            .replace("__TABLE__", table)
            .replace("__LEGEND__", legend)
            .replace("__GWSPIKE__", str(GW_SPIKE_MS))
            .replace("__WANSPIKE__", str(WAN_SPIKE_MS))
            .replace("__EVENTS__", events_note)
            .replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return stat, verdict


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网络监测报告</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#e3e1dc;
--gw:#2f6fde;--wan:#e8742a;--wan2:#8e44ad;--wan3:#16a085;--wan4:#b8860b;
--bad:#d93a3a;--ok:#1f9d55}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1e1e1f;--fg:#ececec;--mute:#9a9a9f;
--line:#333;--gw:#6b9cff;--wan:#ff9a52;--wan2:#c58af9;--wan3:#4cd9c0;--wan4:#e0b34d;
--bad:#ff6b6b;--ok:#4cc38a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mute);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}
.verdict h2{margin:0 0 6px;font-size:18px}
.verdict p{margin:0;color:var(--mute)}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px;min-width:640px}
th,td{text-align:right;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--mute);font-weight:500}
td.bad{color:var(--bad);font-weight:600}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--mute);margin-bottom:8px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
#box{position:relative}
canvas{width:100%;height:380px;display:block}
#tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:6px 10px;font-size:12px;display:none;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.note{font-size:13px;color:var(--mute);margin-top:10px}
</style></head><body><main>
<h1>家庭网络监测报告</h1>
<div class="sub">开始于 __START__ · 共记录 __DUR__ 分钟 · __EVENTS__</div>

<div class="card verdict"><h2>__VERDICT_T__</h2><p>__VERDICT_D__</p></div>

<div class="card">
<div class="legend">
__LEGEND__
<span><i style="background:var(--bad)"></i>顶部红点 = 超时丢包</span>
<span>虚线 = __WANSPIKE__ms 卡顿线</span>
</div>
<div id="box"><canvas id="c"></canvas><div id="tip"></div></div>
<div class="note">怎么看：所有线都同时跳高 → 问题在家里 WiFi/内网；只有外网线跳高、路由器线平稳 → 问题在光猫之后的外部线路；只有一个外网目标单独跳高 → 可能是那个目标自己不稳定，不代表你家线路有问题。</div>
</div>

<div class="card wrap">
<table><thead><tr><th>目标</th><th>包数</th><th>丢包率</th><th>平均</th><th>中位数</th><th>95%</th><th>最高</th><th>异常次数</th></tr></thead>
<tbody>__TABLE__</tbody></table>
<div class="note">异常次数：路由器 &gt; __GWSPIKE__ms 或超时；外网 &gt; __WANSPIKE__ms 或超时。“95%”表示 95% 的包都比这个值快，最能反映平时体验。</div>
</div>
</main>
<script>
const D = __DATA__;
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
const tip = document.getElementById('tip'), box = document.getElementById('box');
const pad = {l:46, r:14, t:18, b:28};
let hoverT = null;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const hhmm = t => { const d = new Date(t*1000); return d.toTimeString().slice(0,8); };

function geom(){
  const W = cv.clientWidth, H = cv.clientHeight;
  const span = (D.t1 - D.t0) || 1;
  return {W, H,
    X: t => pad.l + (t - D.t0) / span * (W - pad.l - pad.r),
    T: x => D.t0 + (x - pad.l) / (W - pad.l - pad.r) * span,
    Y: v => pad.t + (1 - Math.min(v, D.ymax) / D.ymax) * (H - pad.t - pad.b)};
}

function draw(){
  const dpr = window.devicePixelRatio || 1;
  const {W, H, X, Y} = geom();
  cv.width = W*dpr; cv.height = H*dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  ctx.font = '11px -apple-system,sans-serif'; ctx.fillStyle = css('--mute');
  ctx.strokeStyle = css('--line'); ctx.lineWidth = 1;
  for (let i=0;i<=5;i++){
    const v = D.ymax*i/5, y = Y(v);
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(W-pad.r,y); ctx.stroke();
    ctx.textAlign='right'; ctx.fillText(Math.round(v)+'ms', pad.l-6, y+4);
  }
  ctx.textAlign='center';
  for (let i=0;i<=5;i++){
    const t = D.t0 + (D.t1-D.t0)*i/5;
    ctx.fillText(hhmm(t).slice(0,5), X(t), H-8);
  }
  // 卡顿线
  ctx.setLineDash([5,4]); ctx.strokeStyle = css('--bad'); ctx.globalAlpha=.5;
  ctx.beginPath(); ctx.moveTo(pad.l,Y(D.spike)); ctx.lineTo(W-pad.r,Y(D.spike)); ctx.stroke();
  ctx.setLineDash([]); ctx.globalAlpha=1;
  // 各条曲线
  D.order.forEach((k, idx) => {
    const s = D.series[k];
    ctx.strokeStyle = css(s.color); ctx.lineWidth = 1.4; ctx.beginPath();
    let pen = false;
    for (const [t,v] of s.points){
      if (v === null){ pen = false; continue; }
      const x = X(t), y = Y(v);
      pen ? ctx.lineTo(x,y) : ctx.moveTo(x,y); pen = true;
    }
    ctx.stroke();
    ctx.fillStyle = css('--bad');
    for (const [t,v] of s.points) if (v === null){
      ctx.beginPath(); ctx.arc(X(t), pad.t - 3 - idx*5, 2.5, 0, 7); ctx.fill();
    }
  });
  if (hoverT !== null){
    ctx.strokeStyle = css('--mute'); ctx.globalAlpha=.6;
    ctx.beginPath(); ctx.moveTo(X(hoverT),pad.t); ctx.lineTo(X(hoverT),H-pad.b); ctx.stroke();
    ctx.globalAlpha=1;
  }
}

function nearest(arr, t){
  if (!arr.length) return null;
  let lo=0, hi=arr.length-1;
  while (hi-lo>1){ const m=(lo+hi)>>1; arr[m][0] < t ? lo=m : hi=m; }
  return Math.abs(arr[lo][0]-t) < Math.abs(arr[hi][0]-t) ? arr[lo] : arr[hi];
}

cv.addEventListener('mousemove', e => {
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left;
  const {W, T} = geom();
  if (x < pad.l || x > W - pad.r){ tip.style.display='none'; hoverT=null; draw(); return; }
  hoverT = T(x);
  const f = p => !p ? '—' : (p[1]===null ? '<b style="color:var(--bad)">超时</b>' : p[1]+' ms');
  let lines = `<b>${hhmm(hoverT)}</b><br>`;
  for (const k of D.order){
    lines += `${D.series[k].name}：${f(nearest(D.series[k].points, hoverT))}<br>`;
  }
  tip.innerHTML = lines;
  tip.style.display='block';
  const left = Math.min(x + 12, W - tip.offsetWidth - 4);
  tip.style.left = left+'px'; tip.style.top = (e.clientY - r.top + 12)+'px';
  draw();
});
cv.addEventListener('mouseleave', () => { tip.style.display='none'; hoverT=null; draw(); });
window.addEventListener('resize', draw);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', draw);
draw();
</script></body></html>
"""


# ---------------------------------------------------------------- 主程序

def color(v, spike):
    if v is None:
        return "\033[31m 超时  \033[0m"
    s = f"{v:6.1f}ms"
    return f"\033[31m{s}\033[0m" if v > spike else s


def main():
    ap = argparse.ArgumentParser(description="同时监测路由器和多个外网目标的延迟，生成对比报告")
    ap.add_argument("--gw", help="路由器地址（默认自动检测）")
    ap.add_argument("--wan", nargs="+", default=DEFAULT_WAN,
                     help=f"外网目标，可给多个用空格分开，默认 {' '.join(DEFAULT_WAN)}（用两家独立 DNS 便于交叉验证）")
    ap.add_argument("-m", "--minutes", type=float, default=0, help="运行分钟数，0 = 一直跑到 Ctrl+C")
    ap.add_argument("-i", "--interval", type=float, default=1.0, help="ping 间隔秒数（默认 1）")
    ap.add_argument("--timeout", type=int, default=2000, help="单包超时毫秒（默认 2000）")
    args = ap.parse_args()

    raw_targets = {"gw": args.gw or detect_gateway()}
    for i, host in enumerate(dict.fromkeys(args.wan), 1):  # 去重但保序
        raw_targets[f"wan{i}"] = host

    print(f"预检目标（每个测 2 次）...")
    targets, notes = precheck(raw_targets, min(args.timeout, 1000))
    for n in notes:
        print(n)
    if not any(k != "gw" for k in targets):
        print("所有外网目标都不可用，无法继续监测。换个 --wan 地址试试。")
        sys.exit(1)

    names = {"gw": "路由器（家里内网）"}
    for k, host in targets.items():
        if k != "gw":
            names[k] = f"外网 {wan_label(host)}"

    started = datetime.now()
    outdir = os.path.join(os.getcwd(), "runs", "netdoctor_" + started.strftime("%Y%m%d_%H%M"))
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "data.csv")
    report_path = os.path.join(outdir, "report.html")

    print(f"开始监测  {' | '.join(f'{names[k]}: {targets[k]}' for k in targets)}")
    print(f"数据保存到：{outdir}")
    print("按 Ctrl+C 结束并生成报告" + (f"（或 {args.minutes:g} 分钟后自动结束）" if args.minutes else ""))
    print("-" * 64)

    records, latest, lock, stop = [], {}, threading.Lock(), threading.Event()
    threads = [threading.Thread(target=worker, daemon=True,
                                args=(k, targets[k], args.interval, args.timeout, records, latest, lock, stop))
               for k in targets]
    for t in threads:
        t.start()

    written = 0
    t_start = time.time()
    f = open(csv_path, "w", newline="", encoding="utf-8")
    # 显式指定 \n 行尾，避免默认 \r\n 导致命令行按行筛选（grep 等）漏掉超时记录
    w = csv.writer(f, lineterminator="\n")
    w.writerow(["时间", "目标", "地址", "延迟ms（空=超时）"])
    order = list(targets.keys())
    try:
        while True:
            time.sleep(1)
            with lock:
                new = records[written:]
                written = len(records)
                cur = {k: latest.get(k) for k in order}
                wan_bad = sum(1 for _, k, v in records
                             if k != "gw" and (v is None or v > WAN_SPIKE_MS))
            for ts, k, v in new:
                w.writerow([datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                            names[k], targets[k], "" if v is None else v])
            f.flush()
            elapsed = time.time() - t_start
            status = "  ".join(
                f"{names[k].replace('外网 ', '')} {color(cur[k], GW_SPIKE_MS if k == 'gw' else WAN_SPIKE_MS)}"
                for k in order
            )
            sys.stdout.write(f"\r{datetime.now():%H:%M:%S}  {status}   "
                             f"已记录 {int(elapsed // 60)}分{int(elapsed % 60):02d}秒   "
                             f"外网卡顿 {wan_bad} 次   ")
            sys.stdout.flush()
            if args.minutes and elapsed >= args.minutes * 60:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=args.timeout / 1000 + 3)
        with lock:
            for ts, k, v in records[written:]:
                w.writerow([datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                            names[k], targets[k], "" if v is None else v])
        f.close()

    print("\n" + "-" * 64)
    if not records:
        print("没有记录到数据。")
        return
    stat, verdict = build_report(records, targets, names, args.interval, started, report_path)
    for k in order:
        s = stat[k]
        print(f"{names[k]}：丢包 {s['loss']:.1f}%  平均 {fmt(s['avg'])}  最高 {fmt(s['max'])}")
    print(f"判断：{verdict[0]}")
    print(f"报告：{report_path}")
    try:
        subprocess.run(["open", report_path], check=False)
    except Exception:
        pass


if __name__ == "__main__":
    main()
