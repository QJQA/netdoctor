#!/usr/bin/env python3
"""
server.py — Network Doctor 本地网页界面

点"开始监测"→ 浏览器里看实时曲线 → 点"停止并生成报告"→ 生成 HTML 报告。
底层复用 netdoctor.py 的探测/判断逻辑，不引入任何第三方依赖。

用法：
  python3 server.py            # 默认端口 7656，自动打开浏览器
  python3 server.py -p 8080
"""
import argparse
import csv
import json
import os
import re
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import netdoctor as nw

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
OUTDIR_RE = re.compile(r"^runs/netdoctor_\d{8}_\d{6}/(report\.html|data\.csv)$")


class Monitor:
    """封装一次监测的生命周期：开始 → 持续记录 → 停止并出报告。"""

    def __init__(self):
        self.lock = threading.Lock()
        self._reset_locked()

    def _reset_locked(self):
        self.running = False
        self.records = []
        self.latest = {}
        self.stop_event = threading.Event()
        self.rec_lock = threading.Lock()
        self.threads = []
        self.targets = {}
        self.names = {}
        self.order = []
        self.interval = 1.0
        self.started = None
        self.outdir = None

    def start(self, gw, wan_list, interval):
        with self.lock:
            if self.running:
                raise RuntimeError("已经在监测中，请先停止当前监测")
            self._reset_locked()

            raw_targets = {"gw": gw or nw.detect_gateway()}
            for i, host in enumerate(dict.fromkeys(wan_list), 1):
                raw_targets[f"wan{i}"] = host

            targets, notes = nw.precheck(raw_targets, 1000)
            if not any(k != "gw" for k in targets):
                raise RuntimeError("所有外网目标都不可用（预检 2 次都不回应），换一个地址试试")

            names = {"gw": "路由器（家里内网）"}
            for k, host in targets.items():
                if k != "gw":
                    names[k] = f"外网 {nw.wan_label(host)}"

            self.targets = targets
            self.names = names
            self.order = list(targets.keys())
            self.interval = interval
            self.started = datetime.now()
            self.outdir = os.path.join(os.getcwd(), "runs", "netdoctor_" + self.started.strftime("%Y%m%d_%H%M%S"))
            os.makedirs(self.outdir, exist_ok=True)
            self.running = True

            self.threads = [threading.Thread(
                target=nw.worker, daemon=True,
                args=(k, targets[k], interval, 2000, self.records, self.latest, self.rec_lock, self.stop_event)
            ) for k in targets]
            for t in self.threads:
                t.start()

            return {"targets": targets, "names": names, "notes": notes, "order": self.order}

    def snapshot(self, since):
        with self.lock:
            running = self.running
            order = list(self.order)
            names = dict(self.names)
            started_ts = self.started.timestamp() if self.started else None
        with self.rec_lock:
            new = self.records[since:]
            total = len(self.records)
            latest = dict(self.latest)
        elapsed = (time.time() - started_ts) if started_ts else 0
        return {
            "running": running,
            "elapsed": elapsed,
            "latest": latest,
            "total": total,
            "new": [[round(t, 2), k, v] for t, k, v in new],
            "order": order,
            "names": names,
        }

    def stop(self):
        with self.lock:
            if not self.running:
                raise RuntimeError("当前没有在监测")
            self.stop_event.set()
            threads, targets, names = self.threads, self.targets, self.names
            interval, started, outdir = self.interval, self.started, self.outdir
        for t in threads:
            t.join(timeout=3)
        with self.rec_lock:
            records = list(self.records)
        with self.lock:
            self.running = False

        if not records:
            raise RuntimeError("没有记录到数据")

        csv_path = os.path.join(outdir, "data.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["时间", "目标", "地址", "延迟ms（空=超时）"])
            for ts, k, v in records:
                w.writerow([datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                            names[k], targets[k], "" if v is None else v])

        report_path = os.path.join(outdir, "report.html")
        stat, verdict = nw.build_report(records, targets, names, interval, started, report_path)
        rel = os.path.relpath(report_path, os.getcwd()).replace(os.sep, "/")
        return {"stat": stat, "verdict": verdict, "report_url": "/" + rel}


monitor = Monitor()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默默认访问日志，避免刷屏

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path, ctype):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_file(INDEX_PATH, "text/html; charset=utf-8")
        elif path == "/icon.svg":
            self._serve_file(os.path.join(BASE_DIR, "icon.svg"), "image/svg+xml")
        elif path == "/api/status":
            since = int(parse_qs(parsed.query).get("since", ["0"])[0])
            self._json(200, monitor.snapshot(since))
        elif path.startswith("/runs/netdoctor_"):
            rel = path.lstrip("/")
            if OUTDIR_RE.match(rel):
                full = os.path.join(os.getcwd(), rel)
                ctype = "text/html; charset=utf-8" if full.endswith(".html") else "text/csv; charset=utf-8"
                self._serve_file(full, ctype)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        if parsed.path == "/api/start":
            try:
                gw = (payload.get("gw") or "").strip() or None
                wan_raw = (payload.get("wan") or "").strip()
                wan_list = [w for w in re.split(r"[,\s]+", wan_raw) if w] or nw.DEFAULT_WAN
                interval = float(payload.get("interval") or 1.0)
                self._json(200, monitor.start(gw, wan_list, interval))
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif parsed.path == "/api/stop":
            try:
                self._json(200, monitor.stop())
            except Exception as e:
                self._json(400, {"error": str(e)})
        else:
            self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description="Network Doctor 本地网页界面")
    ap.add_argument("-p", "--port", type=int, default=7656)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Network Doctor 网页界面已启动：{url}")
    print("按 Ctrl+C 停止服务")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
