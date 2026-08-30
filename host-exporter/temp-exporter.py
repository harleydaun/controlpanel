#!/usr/bin/env python3
"""Tiny CPU + drive temperature exporter for the R730xd fan controller.

Runs on the Proxmox host, serves JSON on :9333. Stdlib only, no deps.

    GET / -> {"temps":  {"CPU1 Temp": 62.0, "CPU2 Temp": 58.5},
              "drives": {"sda": 36.0, "sdb": 35.0, ...},
              "ts": ...}

CPU temps come from the coretemp package sensors in /sys/class/hwmon and are
read per request (they're free). Drive temps come from smartctl and are
refreshed by a background thread every DRIVE_REFRESH seconds (default 60) —
HDD temps move slowly and SMART polling shouldn't be hammered. Requires
smartmontools and root (smartctl needs raw device access); if smartctl is
missing or unprivileged, "drives" is simply empty.

Note: DTS package temps typically read a few degrees C higher than the
iDRAC's socket sensors — retune the fan target accordingly when switching.
"""
import glob
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9333
DRIVE_REFRESH = int(os.environ.get("DRIVE_REFRESH", "60"))

_drives = {}
_drives_lock = threading.Lock()

SAS_TEMP_RE = re.compile(r"Current Drive Temperature:\s*(\d+)\s*C")


def read_cpu_temps():
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


def _smartctl(args, timeout=20):
    try:
        return subprocess.run(["smartctl"] + args, capture_output=True,
                              text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def scan_drives():
    devs = []
    for line in _smartctl(["--scan"]).splitlines():
        parts = line.split("#")[0].split()
        if not parts:
            continue
        dtype = parts[parts.index("-d") + 1] if "-d" in parts else None
        devs.append((parts[0], dtype))
    return devs


def read_drive_temp(dev, dtype):
    out = _smartctl(["-A", dev] + (["-d", dtype] if dtype else []))
    m = SAS_TEMP_RE.search(out)          # SAS/SCSI drives
    if m:
        return float(m.group(1))
    for line in out.splitlines():        # ATA attribute table
        fields = line.split()
        if len(fields) >= 10 and fields[1] in ("Temperature_Celsius",
                                               "Airflow_Temperature_Cel"):
            try:
                return float(fields[9])
            except ValueError:
                pass
    return None


def drive_name(dev, dtype):
    # Controller-addressed entries like "/dev/bus/0 -d megaraid,4" have a
    # useless basename ("0") — name them by their controller slot instead.
    if dtype and "," in dtype:
        kind, idx = dtype.split(",", 1)
        return f"{kind}-{idx}"
    return os.path.basename(dev)


def drive_loop():
    global _drives
    while True:
        found = {}
        for dev, dtype in scan_drives():
            t = read_drive_temp(dev, dtype)
            if t is not None:
                found[drive_name(dev, dtype)] = t
        with _drives_lock:
            _drives = found
        time.sleep(DRIVE_REFRESH)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _drives_lock:
            drives = dict(_drives)
        body = json.dumps({"temps": read_cpu_temps(), "drives": drives,
                           "ts": time.time()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the journal quiet
        pass


if __name__ == "__main__":
    threading.Thread(target=drive_loop, daemon=True).start()
    print(f"temp-exporter listening on :{PORT}, cpu: {read_cpu_temps()}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
