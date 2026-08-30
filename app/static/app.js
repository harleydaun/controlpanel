/* R730xd fan control dashboard */
"use strict";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const COLORS = {
  cpu: css("--series-cpu"), inlet: css("--series-inlet"), exhaust: css("--series-exhaust"),
  fan: css("--series-fan"), power: css("--series-power"),
  ink2: css("--ink-2"), muted: css("--muted"), grid: css("--grid"), baseline: css("--baseline"),
};

const PRESETS = {
  silent: [{temp:50,pct:8},{temp:65,pct:12},{temp:72,pct:18},{temp:77,pct:28},{temp:79,pct:40}],
  balanced: [{temp:55,pct:12},{temp:65,pct:16},{temp:70,pct:20},{temp:74,pct:26},{temp:77,pct:34},{temp:79,pct:45}],
  cool: [{temp:45,pct:20},{temp:55,pct:25},{temp:65,pct:35},{temp:72,pct:45},{temp:78,pct:60}],
};

let config = null;          // last config from server
let status = null;          // last status from server
let historyRange = 3600;

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
}

/* ============================== curve editor ============================== */
const curve = (() => {
  const W = 760, H = 320, M = { l: 46, r: 18, t: 16, b: 32 };
  const X0 = 30, X1 = 90; // temperature domain
  let points = [];        // working copy (sorted by temp)
  let saved = "[]";       // JSON of last-applied points
  let selected = -1;
  let dragging = -1;
  let svg = null;

  const sx = (t) => M.l + (t - X0) / (X1 - X0) * (W - M.l - M.r);
  const sy = (p) => H - M.b - p / 100 * (H - M.t - M.b);
  const ix = (px) => X0 + (px - M.l) / (W - M.l - M.r) * (X1 - X0);
  const iy = (py) => (H - M.b - py) / (H - M.t - M.b) * 100;

  function svgPoint(evt) {
    const pt = new DOMPoint(evt.clientX, evt.clientY);
    return pt.matrixTransform(svg.getScreenCTM().inverse());
  }

  const dirty = () => JSON.stringify(points) !== saved;

  function render() {
    const el = $("curve-editor");
    const grid = [];
    for (let t = X0; t <= X1; t += 10)
      grid.push(`<line x1="${sx(t)}" y1="${M.t}" x2="${sx(t)}" y2="${H - M.b}" stroke="${COLORS.grid}"/>`,
        `<text x="${sx(t)}" y="${H - M.b + 18}" fill="${COLORS.muted}" font-size="11" text-anchor="middle">${t}°</text>`);
    for (let p = 0; p <= 100; p += 20)
      grid.push(`<line x1="${M.l}" y1="${sy(p)}" x2="${W - M.r}" y2="${sy(p)}" stroke="${COLORS.grid}"/>`,
        `<text x="${M.l - 8}" y="${sy(p) + 4}" fill="${COLORS.muted}" font-size="11" text-anchor="end">${p}%</text>`);

    let path = "";
    if (points.length) {
      const cl = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
      const pts = [
        { temp: X0, pct: points[0].pct },
        ...points.map(p => ({ temp: cl(p.temp, X0, X1), pct: p.pct })),
        { temp: X1, pct: points[points.length - 1].pct },
      ];
      path = "M " + pts.map(p => `${sx(p.temp)} ${sy(p.pct)}`).join(" L ");
    }

    const dots = points.map((p, i) => `
      <g class="pt" data-i="${i}" style="cursor:grab">
        <circle cx="${sx(p.temp)}" cy="${sy(p.pct)}" r="14" fill="transparent"/>
        <circle cx="${sx(p.temp)}" cy="${sy(p.pct)}" r="${i === selected ? 8 : 6}"
          fill="${COLORS.cpu}" stroke="#1a1a19" stroke-width="2"/>
      </g>`).join("");

    // live operating marker: smoothed temp vs actual commanded fan %
    let marker = "";
    if (status && status.smooth_temp != null && status.fan_pct != null) {
      const mxr = Math.max(X0, Math.min(X1, status.smooth_temp));
      marker = `
        <line x1="${sx(mxr)}" y1="${M.t}" x2="${sx(mxr)}" y2="${H - M.b}"
          stroke="${COLORS.inlet}" stroke-dasharray="3 4" opacity="0.7"/>
        <circle cx="${sx(mxr)}" cy="${sy(status.fan_pct)}" r="6" fill="${COLORS.inlet}"
          stroke="#1a1a19" stroke-width="2"/>
        <text x="${sx(mxr) + 8}" y="${M.t + 12}" fill="${COLORS.inlet}" font-size="11">
          now ${status.smooth_temp}°C · ${status.fan_pct}%</text>`;
    }

    el.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
        ${grid.join("")}
        <line x1="${M.l}" y1="${H - M.b}" x2="${W - M.r}" y2="${H - M.b}" stroke="${COLORS.baseline}"/>
        <path d="${path}" fill="none" stroke="${COLORS.cpu}" stroke-width="2.5"/>
        ${marker}${dots}
      </svg>`;
    svg = el.querySelector("svg");
    bindSvg();
    syncButtons();
    syncPointEdit();
  }

  function bindSvg() {
    svg.addEventListener("pointerdown", (e) => {
      const g = e.target.closest("g.pt");
      if (g) {
        selected = dragging = +g.dataset.i;
        svg.setPointerCapture(e.pointerId);
        render();
      }
    });
    svg.addEventListener("pointermove", (e) => {
      if (dragging < 0) return;
      const p = svgPoint(e);
      const t = Math.max(X0, Math.min(X1, Math.round(ix(p.x) * 2) / 2));
      const f = Math.max(0, Math.min(100, Math.round(iy(p.y))));
      const lo = dragging > 0 ? points[dragging - 1].temp + 0.5 : X0;
      const hi = dragging < points.length - 1 ? points[dragging + 1].temp - 0.5 : X1;
      points[dragging] = { temp: Math.max(lo, Math.min(hi, t)), pct: f };
      render();
    });
    const stop = () => { if (dragging >= 0) { dragging = -1; render(); } };
    svg.addEventListener("pointerup", stop);
    svg.addEventListener("pointercancel", stop);
    svg.addEventListener("dblclick", (e) => {
      if (e.target.closest("g.pt")) return;
      if (points.length >= 16) return toast("Curve limit is 16 points", true);
      const p = svgPoint(e);
      const t = Math.round(ix(p.x) * 2) / 2, f = Math.max(0, Math.min(100, Math.round(iy(p.y))));
      if (t < X0 || t > X1) return;
      points.push({ temp: t, pct: f });
      points.sort((a, b) => a.temp - b.temp);
      selected = points.findIndex(q => q.temp === t && q.pct === f);
      render();
    });
  }

  function syncButtons() {
    $("curve-apply").disabled = !dirty();
    $("curve-revert").disabled = !dirty();
  }

  function syncPointEdit() {
    const box = $("point-edit");
    if (selected < 0 || !points[selected]) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    if (document.activeElement !== $("pt-temp")) $("pt-temp").value = points[selected].temp;
    if (document.activeElement !== $("pt-pct")) $("pt-pct").value = points[selected].pct;
  }

  function load(pts) {
    points = pts.map(p => ({ ...p })).sort((a, b) => a.temp - b.temp);
    saved = JSON.stringify(points);
    selected = -1;
    render();
  }

  $("pt-temp").addEventListener("change", () => {
    if (selected < 0) return;
    points[selected].temp = Math.max(0, Math.min(120, +$("pt-temp").value || 0));
    points.sort((a, b) => a.temp - b.temp);
    render();
  });
  $("pt-pct").addEventListener("change", () => {
    if (selected < 0) return;
    points[selected].pct = Math.max(0, Math.min(100, Math.round(+$("pt-pct").value || 0)));
    render();
  });
  $("pt-delete").addEventListener("click", () => {
    if (selected < 0) return;
    if (points.length <= 2) return toast("Curve needs at least 2 points", true);
    points.splice(selected, 1);
    selected = -1;
    render();
  });
  $("curve-revert").addEventListener("click", () => load(JSON.parse(saved)));
  $("curve-apply").addEventListener("click", async () => {
    try {
      config = await api("/api/config", { method: "PUT", body: JSON.stringify({ curve: points }) });
      saved = JSON.stringify(points);
      syncButtons();
      toast("Fan curve applied");
    } catch (e) { toast(`Curve rejected: ${e.message}`, true); }
  });
  $("preset-select").addEventListener("change", (e) => {
    const pts = PRESETS[e.target.value];
    e.target.value = "";
    if (!pts) return;
    points = pts.map(p => ({ ...p }));
    selected = -1;
    render();
  });

  return { load, render, dirty };
})();

/* ================================ charts ================================= */
Chart.defaults.color = COLORS.muted;
Chart.defaults.borderColor = COLORS.grid;
Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';

const fmtTime = (ts) => {
  const d = new Date(ts * 1000);
  return historyRange >= 172800
    ? d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

function lineChart(canvasId, datasets, { yMin, yMax, unit, legend }) {
  return new Chart($(canvasId), {
    type: "line",
    data: { datasets },
    options: {
      animation: false,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      elements: { point: { radius: 0, hoverRadius: 4 }, line: { borderWidth: 2, tension: 0 } },
      scales: {
        x: { type: "linear", ticks: { callback: (v) => fmtTime(v), maxTicksLimit: 8, maxRotation: 0 }, grid: { color: COLORS.grid } },
        y: { min: yMin, max: yMax, grid: { color: COLORS.grid }, ticks: { maxTicksLimit: 6 } },
      },
      plugins: {
        legend: { display: legend, labels: { color: COLORS.ink2, boxWidth: 14, boxHeight: 3 } },
        tooltip: {
          callbacks: {
            title: (items) => items.length ? fmtTime(items[0].parsed.x) : "",
            label: (item) => ` ${item.dataset.label}: ${item.parsed.y == null ? "–" : Math.round(item.parsed.y * 10) / 10}${unit}`,
          },
        },
      },
    },
  });
}

const chTemps = lineChart("chart-temps", [
  { label: "CPU (max)", borderColor: COLORS.cpu, data: [] },
  { label: "Inlet", borderColor: COLORS.inlet, data: [] },
  { label: "Exhaust", borderColor: COLORS.exhaust, data: [] },
], { unit: "°C", legend: true });

const chFan = lineChart("chart-fan", [
  { label: "Fan output", borderColor: COLORS.fan, data: [] },
  { label: "Curve target", borderColor: COLORS.muted, borderDash: [5, 4], data: [] },
], { yMin: 0, yMax: 100, unit: "%", legend: true });

const chPower = lineChart("chart-power", [
  { label: "Power", borderColor: COLORS.power, data: [] },
], { yMin: 0, unit: " W", legend: false });

async function loadHistory() {
  try {
    const rows = await api(`/api/history?seconds=${historyRange}&points=400`);
    const pick = (k) => rows.map(r => ({ x: r.ts, y: r[k] }));
    chTemps.data.datasets[0].data = pick("control");
    chTemps.data.datasets[1].data = pick("inlet");
    chTemps.data.datasets[2].data = pick("exhaust");
    chFan.data.datasets[0].data = pick("fan_pct");
    chFan.data.datasets[1].data = pick("target_pct");
    chPower.data.datasets[0].data = pick("power");
    const now = Math.floor(Date.now() / 1000);
    [chTemps, chFan, chPower].forEach(c => {
      c.options.scales.x.min = now - historyRange;
      c.options.scales.x.max = now;
      c.update("none");
    });
  } catch (e) { /* transient */ }
}

$("range-switch").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-sec]");
  if (!btn) return;
  historyRange = +btn.dataset.sec;
  document.querySelectorAll("#range-switch button").forEach(b => b.classList.toggle("active", b === btn));
  loadHistory();
});

/* ============================ status rendering =========================== */
function tempClass(t) {
  if (t == null || !config) return "";
  if (t >= config.emergency.trigger_temp - 2) return "hot";
  if (t >= config.curve[config.curve.length - 1].temp) return "warm";
  return "";
}

function renderStatus() {
  if (!status) return;
  const badge = $("conn-badge");
  badge.className = "badge " + (status.idrac_ok ? "ok" : "bad");
  badge.textContent = status.idrac_ok ? "iDRAC connected" : "iDRAC unreachable";

  const banner = $("alert-banner");
  if (status.emergency) {
    banner.className = "alert critical";
    banner.textContent = "EMERGENCY — temperature exceeded the trigger; Dell auto control has the fans until it clears.";
  } else if (status.degraded) {
    banner.className = "alert warn";
    banner.textContent = "Sensor reads are failing — control was handed to Dell auto until readings recover.";
  } else if (status.last_error) {
    banner.className = "alert warn";
    banner.textContent = `IPMI error: ${status.last_error}`;
  } else {
    banner.className = "alert hidden";
  }

  const temps = status.temps || {};
  const cpus = Object.entries(temps).filter(([k]) => k.startsWith("CPU"));
  const cpuMax = cpus.length ? Math.max(...cpus.map(([, v]) => v)) : null;
  const set = (id, val, unit, cls = "") => {
    $(id).innerHTML = val == null ? "–" : `${val}<span class="unit">${unit}</span>`;
    $(id).className = "tile-value " + cls;
  };
  set("t-cpu", cpuMax, "°C", tempClass(cpuMax));
  $("t-cpu-sub").textContent = cpus.map(([k, v]) => `${k.replace(" Temp", "")} ${v}°`).join(" · ");
  set("t-inlet", temps["Inlet Temp"], "°C");
  set("t-exhaust", temps["Exhaust Temp"], "°C");

  const mode = status.mode;
  set("t-fan", status.fan_pct, "%");
  $("t-fan-sub").textContent =
    mode === "dell" || status.degraded ? "Dell auto control" :
    status.emergency ? "emergency — Dell auto" :
    mode === "manual" ? "manual" :
    status.target_pct != null ? `curve target ${status.target_pct}%` : "curve";
  if (status.fan_pct == null) $("t-fan").textContent = mode === "dell" ? "auto" : "–";

  const rpms = Object.values(status.fans || {});
  set("t-rpm", rpms.length ? Math.round(rpms.reduce((a, b) => a + b) / rpms.length) : null, " rpm");
  $("t-rpm-sub").textContent = rpms.length ? `${Math.min(...rpms)}–${Math.max(...rpms)} across ${rpms.length} fans` : "";
  set("t-power", status.power_w, " W");

  document.querySelectorAll("#mode-switch button").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === mode));
  $("manual-card").hidden = mode !== "manual";

  const tbody = $("fans-table").querySelector("tbody");
  tbody.innerHTML = Object.entries(status.fans || {})
    .map(([k, v]) => `<tr><td>${k}</td><td>${v} rpm</td></tr>`).join("") ||
    "<tr><td>No fan data</td></tr>";

  if ($("s-thirdparty").dataset.userTouched !== "1" && status.third_party_disabled != null)
    $("s-thirdparty").checked = status.third_party_disabled;

  curve.render(); // refresh live marker
}

/* =============================== mode + manual =========================== */
$("mode-switch").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-mode]");
  if (!btn) return;
  try {
    config = await api("/api/mode", { method: "POST", body: JSON.stringify({ mode: btn.dataset.mode }) });
    toast(`Mode: ${btn.dataset.mode}`);
    await refreshStatus();
  } catch (err) { toast(err.message, true); }
});

$("manual-slider").addEventListener("input", () => {
  $("manual-value").textContent = `${$("manual-slider").value}%`;
});
$("manual-apply").addEventListener("click", async () => {
  try {
    config = await api("/api/mode", {
      method: "POST",
      body: JSON.stringify({ mode: "manual", manual_percent: +$("manual-slider").value }),
    });
    toast(`Manual fan set to ${config.manual_percent}%`);
  } catch (err) { toast(err.message, true); }
});

/* ================================ settings =============================== */
const S = {
  "s-poll": ["poll_interval"], "s-failsafe": ["failsafe_percent"],
  "s-reassert": ["reassert_interval"],
  "s-aup": ["smoothing", "alpha_up"], "s-adown": ["smoothing", "alpha_down"],
  "s-deadband": ["smoothing", "deadband_pct"], "s-stepup": ["smoothing", "max_step_up"],
  "s-stepdown": ["smoothing", "max_step_down"], "s-hold": ["smoothing", "down_hold_polls"],
  "s-etrig": ["emergency", "trigger_temp"], "s-eclear": ["emergency", "clear_temp"],
  "s-retention": ["history", "retention_days"],
};

function fillSettings() {
  for (const [id, path] of Object.entries(S)) {
    let v = config;
    path.forEach(k => v = v[k]);
    $(id).value = v;
  }
  $("s-source").value = config.temp_source;
  $("manual-slider").value = config.manual_percent;
  $("manual-value").textContent = `${config.manual_percent}%`;
}

$("settings-apply").addEventListener("click", async () => {
  const patch = { temp_source: $("s-source").value, smoothing: {}, emergency: {}, history: {} };
  for (const [id, path] of Object.entries(S)) {
    const v = +$(id).value;
    if (path.length === 1) patch[path[0]] = v;
    else patch[path[0]][path[1]] = v;
  }
  try {
    config = await api("/api/config", { method: "PUT", body: JSON.stringify(patch) });
    fillSettings();
    toast("Settings applied");
  } catch (e) { toast(`Rejected: ${e.message}`, true); }
});

$("s-thirdparty").addEventListener("change", async (e) => {
  e.target.dataset.userTouched = "1";
  const disabled = e.target.checked;
  try {
    const r = await api("/api/thirdparty", { method: "POST", body: JSON.stringify({ disabled }) });
    toast(`Third-party PCIe cooling response ${r.disabled ? "disabled" : "enabled"}`);
  } catch (err) {
    e.target.checked = !disabled;
    toast(err.message, true);
  }
});

$("test-conn").addEventListener("click", async () => {
  try {
    const r = await api("/api/test", { method: "POST" });
    toast(r.ok ? "iDRAC connection OK" : `Failed: ${r.error}`, !r.ok);
  } catch (e) { toast(e.message, true); }
});

/* ================================ profiles =============================== */
function fillProfiles() {
  const sel = $("profile-select");
  const names = Object.keys(config.profiles || {});
  sel.innerHTML = names.length
    ? names.map(n => `<option>${n.replace(/</g, "&lt;")}</option>`).join("")
    : "<option value=''>(no saved profiles)</option>";
}

$("profile-save").addEventListener("click", async () => {
  const name = $("profile-name").value.trim();
  if (!name) return toast("Enter a profile name", true);
  if (curve.dirty()) return toast("Apply the curve first, then save it as a profile", true);
  try {
    config = await api("/api/profiles", { method: "POST", body: JSON.stringify({ name }) });
    $("profile-name").value = "";
    fillProfiles();
    toast(`Profile “${name}” saved`);
  } catch (e) { toast(e.message, true); }
});

$("profile-load").addEventListener("click", async () => {
  const name = $("profile-select").value;
  if (!name) return;
  try {
    config = await api(`/api/profiles/${encodeURIComponent(name)}/apply`, { method: "POST" });
    curve.load(config.curve);
    fillSettings();
    toast(`Profile “${name}” applied`);
  } catch (e) { toast(e.message, true); }
});

$("profile-delete").addEventListener("click", async () => {
  const name = $("profile-select").value;
  if (!name) return;
  try {
    config = await api(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
    fillProfiles();
    toast(`Profile “${name}” deleted`);
  } catch (e) { toast(e.message, true); }
});

/* ================================= events ================================ */
async function loadEvents() {
  try {
    const rows = await api("/api/events?limit=200");
    $("events-body").innerHTML = rows.map(r => `
      <tr>
        <td class="ts">${new Date(r.ts * 1000).toLocaleString()}</td>
        <td class="level ${r.level}">${r.level}</td>
        <td>${r.message.replace(/</g, "&lt;")}</td>
      </tr>`).join("");
  } catch { /* transient */ }
}

/* ================================== boot ================================= */
async function refreshStatus() {
  try {
    status = await api("/api/status");
  } catch {
    status = null;
    $("conn-badge").className = "badge bad";
    $("conn-badge").textContent = "backend unreachable";
    return;
  }
  renderStatus();
}

(async function boot() {
  try {
    config = await api("/api/config");
  } catch (e) {
    toast("Failed to load config", true);
    return;
  }
  curve.load(config.curve);
  fillSettings();
  fillProfiles();
  await refreshStatus();
  await loadHistory();
  await loadEvents();
  setInterval(refreshStatus, 3000);
  setInterval(loadHistory, 30000);
  setInterval(loadEvents, 15000);
})();
