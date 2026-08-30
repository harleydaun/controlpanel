"""Config store: a single JSON document persisted to /data/config.json.

All writes go through validate() so the controller can trust every field.
"""
import copy
import json
import os
import tempfile
import threading

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")

DEFAULT_CONFIG = {
    "mode": "curve",              # curve | manual | dell
    "manual_percent": 30,
    "poll_interval": 10,          # seconds
    "failsafe_percent": 40,
    "reassert_interval": 60,      # re-send fan command this often even if unchanged
    "temp_source": "cpu_max",     # cpu_max | cpu_avg | all_max
    "temp_api": {                 # optional fast temp source (host exporter);
        "enabled": False,         # iDRAC remains the automatic fallback
        "url": "",                # e.g. http://192.168.1.10:9333/
        "timeout": 2.0,
    },
    "curve": [
        {"temp": 55, "pct": 12},
        {"temp": 65, "pct": 16},
        {"temp": 70, "pct": 20},
        {"temp": 74, "pct": 26},
        {"temp": 77, "pct": 34},
        {"temp": 79, "pct": 45},
    ],
    "smoothing": {
        "alpha_up": 0.50,         # EMA weight while temp is rising
        "alpha_down": 0.15,       # EMA weight while temp is falling
        "deadband_pct": 2,
        "max_step_up": 8,
        "max_step_down": 1,
        "down_hold_polls": 6,
    },
    "emergency": {
        "trigger_temp": 80,       # raw temp >= this -> failsafe + Dell auto
        "clear_temp": 76,         # raw temp <= this -> reclaim manual control
    },
    "pid": {                      # "smart" mode: hold setpoint with minimum fan
        "setpoint": 75,           # target CPU temp (C)
        "kp": 4.0,                # % fan per degree of error
        "ki": 0.02,               # % fan per degree-second (steady-state term)
        "kd": 0.0,                # % fan per (degree/second) of temp slope
        "min_pct": 8,
        "max_pct": 100,
    },
    "history": {"retention_days": 14},
    "drives": {"warn_temp": 45},  # display-only threshold; never drives control
    "profiles": {},               # name -> {curve, smoothing}
}

MODES = ("curve", "pid", "manual", "dell")
TEMP_SOURCES = ("cpu_max", "cpu_avg", "all_max")


def _num(v, lo, hi, name):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"{name} must be a number")
    if not (lo <= v <= hi):
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


def validate_curve(curve):
    if not isinstance(curve, list) or len(curve) < 2:
        raise ValueError("curve needs at least 2 points")
    if len(curve) > 16:
        raise ValueError("curve is limited to 16 points")
    out = []
    for p in curve:
        t = _num(p.get("temp"), 0, 120, "curve temp")
        f = _num(p.get("pct"), 0, 100, "curve pct")
        out.append({"temp": round(float(t), 1), "pct": int(round(f))})
    out.sort(key=lambda p: p["temp"])
    for a, b in zip(out, out[1:]):
        if b["temp"] - a["temp"] < 0.5:
            raise ValueError("curve points must be at least 0.5C apart")
    return out


def validate(cfg):
    """Validate and normalize a full config dict. Raises ValueError."""
    c = copy.deepcopy(cfg)
    if c.get("mode") not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if c.get("temp_source") not in TEMP_SOURCES:
        raise ValueError(f"temp_source must be one of {TEMP_SOURCES}")
    c["manual_percent"] = int(_num(c.get("manual_percent"), 0, 100, "manual_percent"))
    c["poll_interval"] = int(_num(c.get("poll_interval"), 2, 120, "poll_interval"))
    c["failsafe_percent"] = int(_num(c.get("failsafe_percent"), 10, 100, "failsafe_percent"))
    c["reassert_interval"] = int(_num(c.get("reassert_interval"), 10, 3600, "reassert_interval"))
    c["curve"] = validate_curve(c.get("curve"))

    s = c.get("smoothing", {})
    s["alpha_up"] = round(float(_num(s.get("alpha_up"), 0.01, 1.0, "alpha_up")), 3)
    s["alpha_down"] = round(float(_num(s.get("alpha_down"), 0.01, 1.0, "alpha_down")), 3)
    s["deadband_pct"] = int(_num(s.get("deadband_pct"), 0, 20, "deadband_pct"))
    s["max_step_up"] = int(_num(s.get("max_step_up"), 1, 100, "max_step_up"))
    s["max_step_down"] = int(_num(s.get("max_step_down"), 1, 100, "max_step_down"))
    s["down_hold_polls"] = int(_num(s.get("down_hold_polls"), 0, 120, "down_hold_polls"))
    c["smoothing"] = s

    ta = c.get("temp_api", {})
    ta["enabled"] = bool(ta.get("enabled", False))
    url = ta.get("url", "")
    if not isinstance(url, str) or len(url) > 300:
        raise ValueError("temp_api url must be a string under 300 chars")
    if ta["enabled"] and not url.startswith(("http://", "https://")):
        raise ValueError("temp_api url must start with http:// or https://")
    ta["url"] = url.strip()
    ta["timeout"] = round(float(_num(ta.get("timeout", 2.0), 0.5, 10, "temp_api timeout")), 1)
    c["temp_api"] = ta

    p = c.get("pid", {})
    p["setpoint"] = round(float(_num(p.get("setpoint"), 40, 95, "pid setpoint")), 1)
    p["kp"] = round(float(_num(p.get("kp"), 0, 50, "pid kp")), 3)
    p["ki"] = round(float(_num(p.get("ki"), 0, 2, "pid ki")), 4)
    p["kd"] = round(float(_num(p.get("kd"), 0, 100, "pid kd")), 3)
    p["min_pct"] = int(_num(p.get("min_pct"), 0, 100, "pid min_pct"))
    p["max_pct"] = int(_num(p.get("max_pct"), 0, 100, "pid max_pct"))
    if p["max_pct"] <= p["min_pct"]:
        raise ValueError("pid max_pct must be above min_pct")
    c["pid"] = p

    e = c.get("emergency", {})
    e["trigger_temp"] = int(_num(e.get("trigger_temp"), 40, 105, "trigger_temp"))
    e["clear_temp"] = int(_num(e.get("clear_temp"), 30, 100, "clear_temp"))
    if e["clear_temp"] >= e["trigger_temp"]:
        raise ValueError("emergency clear_temp must be below trigger_temp")
    c["emergency"] = e

    h = c.get("history", {})
    h["retention_days"] = int(_num(h.get("retention_days"), 1, 365, "retention_days"))
    c["history"] = h

    d = c.get("drives", {})
    d["warn_temp"] = int(_num(d.get("warn_temp", 45), 25, 70, "drive warn_temp"))
    c["drives"] = d

    profiles = c.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("profiles must be an object")
    for name, prof in profiles.items():
        if not isinstance(name, str) or not (1 <= len(name) <= 40):
            raise ValueError("profile names must be 1-40 characters")
        prof["curve"] = validate_curve(prof.get("curve"))
    c["profiles"] = profiles
    return c


def _deep_merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "profiles":
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class ConfigStore:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._cfg = copy.deepcopy(DEFAULT_CONFIG)
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                loaded = json.load(f)
            self._cfg = validate(_deep_merge(DEFAULT_CONFIG, loaded))
        except FileNotFoundError:
            self._save_locked()
        except (ValueError, json.JSONDecodeError) as e:
            print(f"[config] stored config invalid ({e}); using defaults", flush=True)

    def _save_locked(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
        with os.fdopen(fd, "w") as f:
            json.dump(self._cfg, f, indent=2)
        os.replace(tmp, self.path)

    def get(self):
        with self._lock:
            return copy.deepcopy(self._cfg)

    def update(self, patch):
        """Merge a partial config, validate, persist. Returns the new config."""
        with self._lock:
            merged = validate(_deep_merge(self._cfg, patch))
            self._cfg = merged
            self._save_locked()
            return copy.deepcopy(merged)

    def replace(self, cfg):
        with self._lock:
            self._cfg = validate(cfg)
            self._save_locked()
            return copy.deepcopy(self._cfg)
