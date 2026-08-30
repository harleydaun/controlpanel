#!/usr/bin/env python3
"""Tiny CPU temperature exporter for the R730xd fan controller.

Runs on the Proxmox host, reads the Intel coretemp package sensors from
/sys/class/hwmon, and serves them as JSON on :9333. Stdlib only, no deps.

    GET / -> {"temps": {"CPU1 Temp": 62.0, "CPU2 Temp": 58.5}, "ts": ...}

Note: these are DTS package temps, which typically read a few degrees C
higher than the iDRAC's socket sensors — retune the fan target accordingly
when switching sources.
"""
import glob
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9333


def read_temps():
    temps = {}
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(hw + "/name") as f:
                if f.read().strip() != "coretemp":
                    continue
        except OSError:
            continue
        for tf in sorted(glob.glob(hw + "/temp*_input")):
            base = tf[: -len("_input")]
            try:
                with open(base + "_label") as f:
                    label = f.read().strip()
            except OSError:
                continue
            m = re.match(r"Package id (\d+)", label)
            if not m:
                continue
            try:
                with open(tf) as f:
                    val = int(f.read()) / 1000.0
            except (OSError, ValueError):
                continue
            temps[f"CPU{int(m.group(1)) + 1} Temp"] = round(val, 1)
    return temps


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"temps": read_temps(), "ts": time.time()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the journal quiet
        pass


if __name__ == "__main__":
    print(f"temp-exporter listening on :{PORT}, sensors: {read_temps()}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
