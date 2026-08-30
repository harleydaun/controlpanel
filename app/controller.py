"""Fan control loop.

Quiet-first proportional control, ported from the original bash controller:
- Control temp defaults to max(CPU1, CPU2).
- Continuous curve with linear interpolation.
- Asymmetric EMA smoothing: rises track fast (safety), falls track slow (quiet).
- Deadband + slew limiting: rise up to max_step_up %/poll, decay
  max_step_down %/poll and only after down_hold_polls consecutive low polls.
- Emergency handoff to Dell auto control triggers on the RAW temperature so
  smoothing can never delay the safety response.

Additions over the bash version:
- Modes: curve / manual (fixed %, emergency guard still active) / dell.
- Degraded mode: repeated sensor-read failures hand control back to Dell auto
  instead of pinning fans at failsafe forever.
- Periodic reassertion of the fan command so an iDRAC reset can't silently
  revert to auto while we think we're in control.
"""
import asyncio
import time

from ipmi import IpmiError

READ_FAILURES_BEFORE_DELL = 3


def curve_target(curve, temp):
    """Linear interpolation over sorted curve points; clamps at both ends."""
    if temp <= curve[0]["temp"]:
        return curve[0]["pct"]
    if temp >= curve[-1]["temp"]:
        return curve[-1]["pct"]
    for a, b in zip(curve, curve[1:]):
        if temp <= b["temp"]:
            frac = (temp - a["temp"]) / (b["temp"] - a["temp"])
            return int(round(a["pct"] + frac * (b["pct"] - a["pct"])))
    return curve[-1]["pct"]


class Controller:
    def __init__(self, store, ipmi, history):
        self.store = store
        self.ipmi = ipmi
        self.history = history
        self.wake = asyncio.Event()
        self._stop = False
        self._started = time.time()

        # control state
        self.current_pct = None       # what we last commanded (None = not ours)
        self.smooth = None            # EMA of control temp
        self.down_count = 0
        self.pid_i = 0.0              # PID integral term (in fan %)
        self.pid_prev = None          # previous smoothed temp, for the D term
        self.pid_ready = False        # False -> re-initialize bumplessly
        self.emergency = False
        self.degraded = False         # sensor reads failing -> Dell has control
        self.fail_count = 0
        self.last_mode = None
        self.last_assert = 0.0

        # last readings for the API
        self.temps = {}
        self.fans = {}
        self.power = None
        self.control_temp = None
        self.target_pct = None
        self.idrac_ok = False
        self.last_update = None
        self.last_error = None
        self.third_party_disabled = None
        self._last_prune = 0.0

    # ---------------------------------------------------------------- logging
    def log(self, level, msg):
        print(f"[{level}] {msg}", flush=True)
        try:
            self.history.add_event(level, msg)
        except Exception:
            pass

    # ----------------------------------------------------------------- status
    def status(self):
        return {
            "mode": self.store.get()["mode"],
            "emergency": self.emergency,
            "degraded": self.degraded,
            "idrac_ok": self.idrac_ok,
            "temps": self.temps,
            "fans": self.fans,
            "power_w": self.power,
            "control_temp": self.control_temp,
            "smooth_temp": round(self.smooth, 1) if self.smooth is not None else None,
            "fan_pct": self.current_pct,
            "target_pct": self.target_pct,
            "pid_integral": round(self.pid_i, 1) if self.pid_ready else None,
            "third_party_disabled": self.third_party_disabled,
            "last_update": self.last_update,
            "last_error": self.last_error,
            "uptime": int(time.time() - self._started),
        }

    # ------------------------------------------------------------ ipmi helpers
    async def _set_pct(self, pct, why=""):
        pct = max(0, min(100, int(pct)))
        await self.ipmi.set_fan_percent(pct)
        self.current_pct = pct
        self.last_assert = time.time()
        if why:
            self.log("info", f"Fan -> {pct}% ({why})")

    async def _hand_to_dell(self, why):
        try:
            await self.ipmi.set_fan_percent(self.store.get()["failsafe_percent"])
            await asyncio.sleep(1)
            await self.ipmi.set_auto_control()
            self.log("warn", f"Handed control to Dell auto ({why})")
        except IpmiError as e:
            self.log("error", f"Failed handing to Dell auto: {e}")
        self.current_pct = None
        self.smooth = None
        self.down_count = 0

    # ------------------------------------------------------------------- loop
    async def run(self):
        self.log("info", "Controller starting")
        while not self._stop:
            try:
                await self.ipmi.check()
                self.idrac_ok = True
                self.log("info", "iDRAC connection OK")
                break
            except IpmiError as e:
                self.idrac_ok = False
                self.last_error = str(e)
                self.log("error", f"Cannot reach iDRAC, retrying in 10s: {e}")
                await asyncio.sleep(10)

        try:
            self.third_party_disabled = await self.ipmi.get_third_party_response()
        except Exception:
            pass

        while not self._stop:
            cfg = self.store.get()
            try:
                await self._tick(cfg)
                self.last_error = None
            except IpmiError as e:
                self.idrac_ok = False
                self.last_error = str(e)
                self.log("error", f"IPMI error: {e}")
            except Exception as e:  # never let the loop die
                self.last_error = repr(e)
                self.log("error", f"Controller error: {e!r}")

            now = time.time()
            if now - self._last_prune > 3600:
                self._last_prune = now
                try:
                    self.history.prune(cfg["history"]["retention_days"])
                except Exception:
                    pass

            try:
                await asyncio.wait_for(self.wake.wait(), timeout=cfg["poll_interval"])
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def _tick(self, cfg):
        mode = cfg["mode"]

        # ---- mode transitions ----
        if self.last_mode is not None and mode != self.last_mode:
            self.log("info", f"Mode changed: {self.last_mode} -> {mode}")
            self.emergency = False
            self.down_count = 0
            self.pid_ready = False
            if mode == "dell":
                await self.ipmi.set_auto_control()
                self.current_pct = None
            else:
                await self._set_pct(cfg["failsafe_percent"], "mode change baseline")
        self.last_mode = mode

        # ---- read sensors ----
        try:
            temps = await self.ipmi.read_temps()
            self.idrac_ok = True
            if self.degraded:
                self.degraded = False
                self.pid_ready = False
                self.log("info", "Sensor reads recovered; resuming control")
                if mode != "dell":
                    await self._set_pct(cfg["failsafe_percent"], "recovery baseline")
            self.fail_count = 0
        except IpmiError as e:
            self.fail_count += 1
            self.log("warn", f"Temp read failed ({self.fail_count}): {e}")
            if mode != "dell" and not self.degraded:
                if self.fail_count >= READ_FAILURES_BEFORE_DELL:
                    self.degraded = True
                    await self._hand_to_dell("repeated sensor read failures")
                else:
                    try:
                        await self._set_pct(cfg["failsafe_percent"], "sensor read failed")
                        self.smooth = None
                        self.down_count = 0
                    except IpmiError as e2:
                        self.log("error", f"Failsafe set failed: {e2}")
            return

        self.temps = temps
        cpu = [v for k, v in temps.items() if k.startswith("CPU")]
        source = cfg["temp_source"]
        if source == "cpu_avg" and cpu:
            raw = sum(cpu) / len(cpu)
        elif source == "all_max":
            raw = max(temps.values())
        else:
            raw = max(cpu) if cpu else max(temps.values())
        self.control_temp = round(raw, 1)
        raw_cpu_max = max(cpu) if cpu else raw  # emergency always watches CPUs

        # fans/power are best-effort telemetry
        try:
            self.fans = await self.ipmi.read_fans()
        except IpmiError:
            pass
        try:
            self.power = await self.ipmi.read_power()
        except IpmiError:
            pass

        emerg = cfg["emergency"]
        if mode == "dell" or self.degraded:
            self.target_pct = None
        else:
            # ---- emergency: always on RAW temp, never smoothed ----
            if self.emergency:
                if raw_cpu_max <= emerg["clear_temp"]:
                    self.emergency = False
                    self.log("info",
                             f"Raw temp {raw_cpu_max}C cleared emergency; reclaiming control")
                    self.smooth = raw
                    self.pid_ready = False
                    if mode == "manual":
                        pct = cfg["manual_percent"]
                    elif mode == "pid":
                        pct = cfg["failsafe_percent"]
                    else:
                        pct = curve_target(cfg["curve"], raw)
                    await self._set_pct(pct, "post-emergency")
            elif raw_cpu_max >= emerg["trigger_temp"]:
                self.emergency = True
                self.log("error",
                         f"EMERGENCY: raw temp {raw_cpu_max}C >= {emerg['trigger_temp']}C")
                await self._hand_to_dell("emergency temperature")
            elif mode == "manual":
                self.target_pct = cfg["manual_percent"]
                if self.current_pct != cfg["manual_percent"]:
                    await self._set_pct(cfg["manual_percent"], "manual setting")
            elif mode == "pid":
                await self._pid_step(cfg, raw)
            else:
                await self._curve_step(cfg, raw)

            # reassert the command periodically in case the iDRAC was reset
            if (not self.emergency and self.current_pct is not None
                    and time.time() - self.last_assert >= cfg["reassert_interval"]):
                await self._set_pct(self.current_pct)

        self.last_update = int(time.time())
        rpms = list(self.fans.values())
        self.history.add_sample(
            control=self.control_temp,
            cpu1=temps.get("CPU1 Temp"), cpu2=temps.get("CPU2 Temp"),
            inlet=temps.get("Inlet Temp"), exhaust=temps.get("Exhaust Temp"),
            fan_pct=self.current_pct, target_pct=self.target_pct,
            rpm=sum(rpms) / len(rpms) if rpms else None,
            power=self.power, mode=mode,
            emergency=self.emergency or self.degraded)

    async def _pid_step(self, cfg, raw):
        """Hold pid.setpoint using the least fan possible.

        PI(D) on the smoothed temperature. The integral term is what finds the
        quietest sustainable fan level: while the CPU sits below the setpoint
        it slowly drains, letting the fans creep down until the temperature
        rises to meet the target. Anti-windup by back-calculation keeps the
        integral honest at the output clamps. Output changes are slew-limited
        with the same up/down rates as curve mode so decay stays silent.
        """
        p, s = cfg["pid"], cfg["smoothing"]
        if self.smooth is None:
            self.smooth = float(raw)
        else:
            alpha = s["alpha_up"] if raw > self.smooth else s["alpha_down"]
            self.smooth += (raw - self.smooth) * alpha

        dt = max(1, cfg["poll_interval"])
        err = self.smooth - p["setpoint"]
        d = 0.0
        if self.pid_prev is not None and self.pid_ready:
            d = p["kd"] * (self.smooth - self.pid_prev) / dt
        self.pid_prev = self.smooth

        if not self.pid_ready:
            # bumpless start: pick the integral so output == current fan level
            base = self.current_pct if self.current_pct is not None else cfg["failsafe_percent"]
            self.pid_i = float(base) - p["kp"] * err
            self.pid_ready = True
            self.log("info", f"Smart mode engaged: target {p['setpoint']}C, "
                             f"starting from {base}%")

        self.pid_i += p["ki"] * err * dt
        out = p["kp"] * err + self.pid_i + d
        if out > p["max_pct"]:
            self.pid_i -= out - p["max_pct"]
            out = p["max_pct"]
        elif out < p["min_pct"]:
            self.pid_i += p["min_pct"] - out
            out = p["min_pct"]

        target = int(round(out))
        self.target_pct = target
        if self.current_pct is None:
            await self._set_pct(target, "initial smart target")
            return

        step = target - self.current_pct
        step = min(step, s["max_step_up"]) if step > 0 else max(step, -s["max_step_down"])
        new_pct = self.current_pct + step
        if new_pct != self.current_pct:
            direction = "UP" if step > 0 else "DOWN"
            old = self.current_pct
            await self._set_pct(new_pct)
            self.log("info",
                     f"temp={raw}C smooth={self.smooth:.1f}C err={err:+.1f}C "
                     f"pid={target}% {direction}: {old}% -> {new_pct}%")

    async def _curve_step(self, cfg, raw):
        s = cfg["smoothing"]
        if self.smooth is None:
            self.smooth = float(raw)
        else:
            alpha = s["alpha_up"] if raw > self.smooth else s["alpha_down"]
            self.smooth += (raw - self.smooth) * alpha

        target = curve_target(cfg["curve"], self.smooth)
        self.target_pct = target

        if self.current_pct is None:
            await self._set_pct(target, "initial curve target")
            self.down_count = 0
            return

        diff = target - self.current_pct
        new_pct = self.current_pct
        if diff >= s["deadband_pct"]:
            new_pct = self.current_pct + min(diff, s["max_step_up"])
            self.down_count = 0
        elif diff <= -s["deadband_pct"]:
            self.down_count += 1
            if self.down_count >= s["down_hold_polls"]:
                new_pct = self.current_pct - s["max_step_down"]
        else:
            self.down_count = 0

        if new_pct != self.current_pct:
            direction = "UP" if new_pct > self.current_pct else "DOWN"
            old = self.current_pct
            await self._set_pct(new_pct)
            self.log("info",
                     f"temp={raw}C smooth={self.smooth:.1f}C target={target}% "
                     f"{direction}: {old}% -> {new_pct}%")

    # --------------------------------------------------------------- shutdown
    async def shutdown(self):
        self._stop = True
        self.wake.set()
        cfg = self.store.get()
        self.log("info", "Shutting down: failsafe then Dell auto control")
        try:
            await self.ipmi.set_fan_percent(cfg["failsafe_percent"])
            await asyncio.sleep(1)
            await self.ipmi.set_auto_control()
            self.log("info", "Dell auto control restored")
        except Exception as e:
            self.log("error", f"Cleanup failed: {e!r}")
