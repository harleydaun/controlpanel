"""ipmitool wrapper.

Default interface is lanplus (talks to the iDRAC over the network) because the
container is expected to run inside a VM with no access to the host's
/dev/ipmi0. Set IPMI_INTERFACE=open and map /dev/ipmi0 into the container to
use the local interface instead.

Set MOCK_IPMI=true to run against a simulated server (UI preview / testing).
"""
import asyncio
import math
import os
import re
import time


class IpmiError(Exception):
    pass


class Ipmi:
    def __init__(self):
        self.interface = os.environ.get("IPMI_INTERFACE", "lanplus")
        self.host = os.environ.get("IDRAC_HOST", "")
        self.user = os.environ.get("IDRAC_USER", "root")
        password = os.environ.get("IDRAC_PASSWORD", "")
        pw_file = os.environ.get("IDRAC_PASSWORD_FILE", "")
        if pw_file and os.path.exists(pw_file):
            with open(pw_file) as f:
                password = f.read().strip()
        self.password = password
        self._lock = asyncio.Lock()
        if self.interface == "lanplus" and not self.host:
            raise IpmiError("IDRAC_HOST must be set when IPMI_INTERFACE=lanplus")

    def _base_args(self):
        if self.interface == "open":
            return ["ipmitool"]
        return [
            "ipmitool", "-I", self.interface,
            "-H", self.host, "-U", self.user, "-P", self.password,
        ]

    async def _run(self, *args, timeout=20.0):
        async with self._lock:  # ipmitool sessions don't like concurrency
            proc = await asyncio.create_subprocess_exec(
                *self._base_args(), *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise IpmiError(f"ipmitool timed out ({' '.join(args)})")
        text = out.decode(errors="replace").strip()
        if proc.returncode != 0:
            raise IpmiError(f"ipmitool failed ({' '.join(args)}): {text[:300]}")
        return text

    async def check(self):
        await self._run("mc", "info", timeout=15.0)

    async def read_temps(self):
        """Returns dict of sensor name -> degrees C.

        The R730xd reports both CPU sensors as bare 'Temp'; they are renamed
        CPU1 Temp / CPU2 Temp in the order they appear.
        """
        out = await self._run("sdr", "type", "temperature")
        temps = {}
        cpu_n = 0
        for line in out.splitlines():
            m = re.match(r"^(.*?)\s*\|.*?\|\s*(\S+)\s*\|.*?\|\s*(-?\d+)\s*degrees C", line)
            if not m:
                continue
            name, status, val = m.group(1).strip(), m.group(2), int(m.group(3))
            if status in ("ns", "na", "disabled"):
                continue
            if name == "Temp":
                cpu_n += 1
                name = f"CPU{cpu_n} Temp"
            temps[name] = val
        if not temps:
            raise IpmiError("no temperature readings parsed")
        return temps

    async def read_fans(self):
        out = await self._run("sdr", "type", "fan")
        fans = {}
        for line in out.splitlines():
            m = re.match(r"^(.*?)\s*\|.*?\|\s*(\S+)\s*\|.*?\|\s*(\d+)\s*RPM", line)
            if m and m.group(2) not in ("ns", "na"):
                fans[m.group(1).strip().replace(" RPM", "")] = int(m.group(3))
        return fans

    async def read_power(self):
        try:
            out = await self._run("sdr", "type", "current")
        except IpmiError:
            return None
        m = re.search(r"(\d+)\s*Watts", out)
        return int(m.group(1)) if m else None

    async def set_manual_control(self):
        await self._run("raw", "0x30", "0x30", "0x01", "0x00")

    async def set_auto_control(self):
        await self._run("raw", "0x30", "0x30", "0x01", "0x01")

    async def set_fan_percent(self, pct):
        pct = max(0, min(100, int(pct)))
        await self.set_manual_control()
        await self._run("raw", "0x30", "0x30", "0x02", "0xff", f"0x{pct:02x}")

    async def get_third_party_response(self):
        """True = Dell's third-party-PCIe cooling response is DISABLED."""
        try:
            out = await self._run("raw", "0x30", "0xce", "0x01", "0x16",
                                  "0x05", "0x00", "0x00", "0x00")
        except IpmiError:
            return None
        tokens = out.split()
        # response: 16 05 00 00 00 05 00 <flag> 00 00 ; flag 01 = disabled
        if len(tokens) >= 8 and tokens[0] == "16":
            return tokens[7] == "01"
        return None

    async def set_third_party_response(self, disabled):
        flag = "0x01" if disabled else "0x00"
        await self._run("raw", "0x30", "0xce", "0x00", "0x16", "0x05",
                        "0x00", "0x00", "0x00", "0x05", "0x00", flag,
                        "0x00", "0x00")


class MockIpmi(Ipmi):
    """Simulated R730xd for UI development / preview. Enable with MOCK_IPMI=true."""

    def __init__(self):  # skip Ipmi env checks
        self._t0 = time.time()
        self._pct = 40
        self._manual = False
        self._third_party_disabled = False
        self._cpu = 55.0
        self._last = time.time()

    def _wave(self, base, amp, period, phase=0.0):
        return base + amp * math.sin((time.time() - self._t0) / period + phase)

    async def check(self):
        return None

    async def read_temps(self):
        # First-order thermal model that responds to fan speed, so closed-loop
        # control (PID) actually converges in mock mode.
        now = time.time()
        dt = min(now - self._last, 60.0)
        self._last = now
        load = max(0.05, self._wave(0.55, 0.45, 300))          # 0.05 .. 1.0
        equilibrium = 32 + load * 62 - 0.32 * self._pct
        self._cpu += (equilibrium - self._cpu) * (1 - math.exp(-dt / 45))
        return {
            "Inlet Temp": round(self._wave(22, 1, 900)),
            "Exhaust Temp": round(self._cpu * 0.5 + 12),
            "CPU1 Temp": round(self._cpu + self._wave(0, 1.2, 37)),
            "CPU2 Temp": round(self._cpu - 3 + self._wave(0, 1.2, 53, 1.0)),
        }

    async def read_fans(self):
        rpm = 2000 + self._pct * 140
        return {f"Fan{i}": int(rpm + self._wave(0, 60, 20, i)) for i in range(1, 7)}

    async def read_power(self):
        return round(self._wave(160, 40, 300))

    async def set_manual_control(self):
        self._manual = True

    async def set_auto_control(self):
        self._manual = False

    async def set_fan_percent(self, pct):
        self._manual = True
        self._pct = max(0, min(100, int(pct)))

    async def get_third_party_response(self):
        return self._third_party_disabled

    async def set_third_party_response(self, disabled):
        self._third_party_disabled = bool(disabled)


def make_ipmi():
    if os.environ.get("MOCK_IPMI", "").lower() in ("1", "true", "yes"):
        return MockIpmi()
    return Ipmi()
