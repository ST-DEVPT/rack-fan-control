// $, $$, fmt, clamp, esc and api come from util.js
const T_MIN = 20, T_MAX = 95;

let overview = null;                 // /api/overview: every server, the drivers, the build
let server = null, draft = null, dirty = false, range = 3600, drag = null;
let route = { view: "overview" };

const time = (t, sec) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(sec && { second: "2-digit" }) });
const unit = (v, u, d = 0) => v == null ? "—" : `${fmt(v, d)}<small>${u}</small>`;
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : "";

function toast(msg, error) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast show" + (error ? " error" : "");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.className = "toast", 3200);
}
// whatever a button's handler did not catch (a request that never got an answer, say) is said, not swallowed
addEventListener("unhandledrejection", e => toast(`Error: ${e.reason?.message || e.reason}`, true));

function curveSpeed(curve, temp) {
  const p = [...curve].sort((a, b) => a[0] - b[0]);
  if (temp <= p[0][0]) return p[0][1];
  for (let i = 1; i < p.length; i++) if (temp <= p[i][0]) {
    const [t0, s0] = p[i - 1], [t1, s1] = p[i];
    return t1 > t0 ? Math.round(s0 + (s1 - s0) * (temp - t0) / (t1 - t0)) : s1;
  }
  return p[p.length - 1][1];
}

// What a server is doing, in a few words and a colour, for cards, the sidebar and headers.
// one rule for "hot" everywhere: within 5 °C of the server's own failsafe
const hot = (cpu, failsafe) => cpu != null && failsafe != null && cpu >= failsafe - 5;

function status(x) {
  if (x.error) return { cls: "bad", text: "Unreachable" };
  if (!x.updated) return { cls: "", text: "Connecting…" };
  if (x.failsafe) return { cls: "warn", text: "Failsafe · automatic" };
  if (x.effective === "monitor") return { cls: "ok", text: "Monitoring" };
  if (x.effective === "auto") return { cls: "ok", text: "Automatic" };
  return { cls: x.dry_run ? "warn" : "ok", text: `${x.dry_run ? "Dry run · " : ""}${cap(x.mode)} · ${x.applied_speed}%` };
}

// ---------------------------------------------------------------- router
function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  if (parts[0] === "server" && parts[1]) return { view: "server", id: parts[1] };
  if (parts[0] === "add") return { view: "edit", driver: parts[1] || "" };
  if (parts[0] === "edit" && parts[1]) return { view: "edit", id: parts[1] };
  if (parts[0] === "alerts") return { view: "alerts" };
  if (["prometheus", "grafana", "homarr", "automation", "backup"].includes(parts[0])) return { view: parts[0] };
  return { view: "overview" };
}

// Pages with unsaved changes register here; leaving one asks first, and so does closing the tab.
const unsaved = { server: () => dirty, alerts: () => typeof aDirty !== "undefined" && aDirty };
const discard = { server: () => { dirty = false; }, alerts: () => { aDirty = false; } };

function go() {
  const next = parseRoute();
  const leaving = next.view !== route.view || next.id !== route.id;
  if (leaving && unsaved[route.view]?.()) {
    if (!confirm(translate("Discard unsaved changes?"))) { history.back(); return; }
    discard[route.view]();
  }
  if (next.view === "server" && next.id !== route.id) { server = null; draft = null; $("#changes").open = $("#sel").open = false; }
  route = next;
  $$(".view").forEach(v => v.hidden = v.id !== "v-" + route.view);
  $$(".side-nav a").forEach(a => a.setAttribute("aria-current", a.dataset.route === route.view));
  renderSide();
  if (route.view === "server") pollServer();
  if (route.view === "edit" && overview?.role === "viewer") { location.hash = "#/"; return toast("This account can only look", true); }
  if (route.view === "edit") openEditor();
  if (route.view === "overview") renderOverview();
  if (["prometheus", "grafana", "homarr", "automation"].includes(route.view)) renderIntegration(route.view);
  const title = { overview: "Overview", alerts: "Alerts", prometheus: "Prometheus", grafana: "Grafana", homarr: "Homarr",
                  automation: "Automation", backup: "Backup",
                  edit: route.id ? "Edit server" : "Add a server" }[route.view];
  if (title) document.title = `${translate(title)} · Fan Control`;
  scrollTo(0, 0);
}
addEventListener("hashchange", go);

// ---------------------------------------------------------------- data
async function pollOverview() {
  try {
    const r = await api("/api/overview");
    if (r.status === 401) return location.replace("/login");
    overview = await r.json();
    $("#signout").hidden = !overview.auth;
    document.body.classList.toggle("viewer", overview.role === "viewer");
    $("#version").textContent = /^\d/.test(overview.version) ? "v" + overview.version : overview.version;
    $("#update").hidden = !overview.update;
    if (overview.update) {
      $("#update").href = overview.update.url;
      $("#update").textContent = `v${overview.update.version} available`;
    }
    renderSide();
    if (route.view === "overview") renderOverview();
    offline(false);
  } catch {
    offline(true);
  }
}

// When the dashboard cannot be reached, everything on screen is the past: say so, and dim it.
let lastContact = Date.now();
function offline(down) {
  if (!down) lastContact = Date.now();
  document.body.classList.toggle("offline", down);
  $("#offline").hidden = !down;
  if (down) $("#offline").textContent = `Can't reach Fan Control. Readings below are from ${time(lastContact / 1000)}.`;
}

async function pollServer() {
  if (route.view !== "server") return;
  const id = route.id;
  try {
    const r = await api("/api/state?server=" + encodeURIComponent(id));
    if (r.status === 401) return location.replace("/login");
    if (r.status === 404) { toast("That server no longer exists", true); location.hash = "#/"; return; }
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    if (route.view !== "server" || route.id !== id) return;
    server = s;
    if (!dirty) draft = structuredClone(server.settings);
    renderServer();
    offline(false);
  } catch {
    $("#dot-link").className = "dot bad";
    $("#link").textContent = "Dashboard offline";
    offline(true);
  }
}

// ---------------------------------------------------------------- sidebar
function renderSide() {
  if (!overview) return;
  $("#side-servers").innerHTML = overview.servers.map(x => {
    const st = status(x);
    return `<a href="#/server/${encodeURIComponent(x.id)}" aria-current="${route.view === "server" && route.id === x.id}">
      <span class="dot ${st.cls}" aria-hidden="true"></span><span class="nm">${esc(x.name)}</span><span class="sr">, ${esc(translate(st.text))}</span>
      <span class="t">${x.cpu_temp == null ? "" : fmt(tv(x.cpu_temp)) + "°"}</span></a>`;
  }).join("") || '<p class="side-empty">No servers yet</p>';
}

// ---------------------------------------------------------------- overview
function renderOverview() {
  if (!overview) return;
  const list = overview.servers;
  $("#welcome").hidden = list.length > 0;
  $("#fleet").hidden = !list.length;
  if (!list.length) {
    $("#ov-lede").textContent = "Nothing to watch yet.";
    $("#welcome-tiles").innerHTML = tiles(overview.drivers, null, true);
    return;
  }
  const temps = list.map(x => x.cpu_temp).filter(v => v != null);
  const watts = list.map(x => x.watts).filter(v => v != null);
  const issues = list.filter(x => x.error || x.failsafe).length;
  $("#ov-lede").textContent = [
    `${list.length} server${list.length > 1 ? "s" : ""}`,
    temps.length ? `hottest CPU ${fmt(tv(Math.max(...temps)))} ${tu()}` : "",
    watts.length ? `${fmt(watts.reduce((a, b) => a + b))} W in total` : "",
    issues ? `${issues} need${issues > 1 ? "" : "s"} attention` : "all fine",
  ].filter(Boolean).map(translate).join(" · ");
  $("#fleet").innerHTML = list.map(card).join("") +
    `<a class="card add" href="#/add"><span class="plus">+</span><span>Add a server</span></a>`;
}

function card(x) {
  const st = status(x);
  const fans = x.effective === "manual" ? `${x.applied_speed}%` : x.fan_pct != null ? `${x.fan_pct}%` :
    x.fan_rpm != null ? `${(x.fan_rpm / 1000).toFixed(1)}k rpm` : "—";
  const badge = x.control ? (x.experimental ? "Fan control · experimental" : "Fan control") : "Monitoring";
  return `<a class="card${x.error ? " is-bad" : x.failsafe ? " is-warn" : ""}" href="#/server/${encodeURIComponent(x.id)}">
    <div class="card-top"><span class="dot ${st.cls}"></span><b>${esc(x.name)}</b><span class="vendor">${esc(x.vendor)}</span></div>
    <div class="card-sub">${esc(x.model || x.driver_label)}${x.host ? " · " + esc(x.host) : ""}</div>
    <div class="card-main">
      <div class="big${hot(x.cpu_temp, x.failsafe_temp) ? " hot" : ""}">${unit(tv(x.cpu_temp), tu())}</div>
      <dl><div><dt>Fans</dt><dd>${fans}</dd></div><div><dt>Power</dt><dd>${x.watts == null ? "—" : fmt(x.watts) + " W"}</dd></div>
        <div><dt>Inlet</dt><dd>${x.inlet == null ? "—" : fmt(tv(x.inlet)) + " " + tu()}</dd></div></dl>
    </div>
    ${cardSpark(x.spark)}
    <div class="card-foot"><span class="state ${st.cls}">${esc(x.error ? "Unreachable: " + x.error : st.text)}</span><span class="badge">${badge}</span></div>
  </a>`;
}

function cardSpark(pts) {
  const W = 300, H = 44, temps = pts.map(p => p[1]).filter(v => v != null);
  if (temps.length < 2) return `<svg class="card-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="0" x2="${W}" y1="${H - 1}" y2="${H - 1}"/></svg>`;
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0] || t0 + 1;
  const lo = Math.min(...temps) - 2, hi = Math.max(Math.max(...temps), lo + 10) + 2;
  const x = t => (t - t0) / Math.max(1, t1 - t0) * W, y = v => H - 3 - (v - lo) / (hi - lo) * (H - 8);
  const d = pts.filter(p => p[1] != null).map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  return `<svg class="card-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <path class="area" d="${d}L${W},${H}L0,${H}Z"/><path class="ln" d="${d}"/></svg>`;
}

function tiles(drivers, selected, links) {
  return drivers.map(d => {
    const tags = [d.control ? "Fan control" : "Monitoring", d.experimental ? "Experimental" : ""].filter(Boolean);
    const inner = `<b>${esc(d.label)}</b><span>${esc(d.description)}</span>
      <span class="tags">${tags.map(t => `<i class="${t === "Experimental" ? "exp" : t === "Monitoring" ? "mon" : "ctl"}">${t}</i>`).join("")}</span>`;
    return links
      ? `<a class="tile" href="#/add/${d.kind}">${inner}</a>`
      : `<button type="button" class="tile" role="radio" data-kind="${d.kind}" aria-checked="${d.kind === selected}">${inner}</button>`;
  }).join("");
}

// ---------------------------------------------------------------- one server
function renderServer() {
  const s = server, sens = s.sensors || { temps: [], fans: [] };
  const auto = s.effective === "auto", monitor = s.effective === "monitor" || !s.control;
  const rpms = sens.fans.map(f => f.rpm).filter(v => v != null);
  const pcts = sens.fans.map(f => f.pct).filter(v => v != null);
  const avgRpm = rpms.length ? rpms.reduce((a, b) => a + b) / rpms.length : null;
  const avgPct = pcts.length ? pcts.reduce((a, b) => a + b) / pcts.length : null;

  document.title = `${s.name} · Fan Control`;
  $("#s-name").textContent = s.name;
  $("#s-vendor").textContent = s.driver_label;
  $("#s-model").textContent = s.model || s.driver_label;
  $("#host").textContent = s.driver === "demo" ? "simulated data" : s.host === "local" ? "local BMC" : s.host;
  $("#s-edit").hidden = s.source === "environment";
  $("#s-edit").href = "#/edit/" + encodeURIComponent(s.id);
  $("#s-diag").href = `/api/servers/${encodeURIComponent(s.id)}/diagnostics`;
  const stale = !s.updated || Date.now() / 1000 - s.updated > s.interval * 3;
  $("#dot-link").className = "dot " + (s.error ? "bad" : stale ? "warn" : "ok");
  $("#link").textContent = s.error ? "BMC error" : stale ? "Waiting for readings" : `Read ${Math.max(0, Math.round(Date.now() / 1000 - s.updated))} s ago`;
  $("#dot-power").className = "dot " + (s.power === "on" ? "ok" : s.power === "off" ? "warn" : "");
  $("#power").textContent = s.power === "on" ? "Powered on" : s.power === "off" ? "Powered off" : "Power —";
  $("#mode-now").textContent = (s.dry_run ? "Dry run · " : "") +
    (monitor ? "Monitoring" : auto ? "Automatic" : `${cap(s.settings.mode)} · ${s.applied_speed} %`);

  const alert = s.error ? ["BMC", s.error] : s.failsafe ? ["Failsafe", `${cap(s.reason)}. The BMC is controlling the fans.`]
    : s.following === false ? ["Fans", "Their RPM does not match the speed set: the BMC seems to have taken them back. Fan Control keeps taking them again."]
    : null;
  $("#alert").classList.toggle("show", !!alert);
  // written only when it changes: the box is a live region, read out again on every write
  if (alert && $("#alert-msg").dataset.en !== alert[1]) {
    $("#alert-title").textContent = alert[0]; $("#alert-msg").textContent = alert[1]; $("#alert-msg").dataset.en = alert[1];
  }
  if (!alert) $("#alert-msg").dataset.en = "";
  $("#accept-cert").hidden = !(s.error && s.error.includes("certificate changed") && overview?.role === "admin");

  const cpus = sens.temps.filter(t => t.cpu).sort((a, b) => b.value - a.value);
  const fs = draft.failsafe_temp;
  $("#v-cpu").innerHTML = unit(tv(s.cpu_temp), tu());
  $("#v-cpu").classList.toggle("hot", s.cpu_temp != null && s.cpu_temp >= fs - 5);
  $("#cpu-name").textContent = cpus.length > 1 ? cpus.map(c => `${c.name.replace("CPU ", "#")} ${fmt(tv(c.value))}°`).join(" · ") : "";
  $("#s-cpu").textContent = s.cpu_temp == null ? "" : monitor ? (cpus[0]?.name || "") : `${fmt(td(fs - s.cpu_temp))} ${tu()} below failsafe`;
  $("#v-speed").innerHTML = monitor ? unit(avgPct, "%") : auto ? "Auto" : unit(s.applied_speed, "%");
  $("#s-speed").textContent = monitor ? "set by the BMC" : cap(translate(s.reason));
  $("#lbl-fanavg").textContent = rpms.length ? "Average speed" : "Fans reporting";
  $("#v-rpm").innerHTML = avgRpm != null ? `${(avgRpm / 1000).toFixed(1)}<small>k rpm</small>` : unit(sens.fans.length || null, "fans");
  $("#s-rpm").textContent = rpms.length ? `${Math.min(...rpms).toLocaleString()}–${Math.max(...rpms).toLocaleString()} across ${rpms.length} fans`
    : pcts.length ? `${Math.min(...pcts)}–${Math.max(...pcts)} %` : "";
  $("#v-inlet").innerHTML = unit(tv(sens.inlet), tu());
  $("#v-exhaust").innerHTML = unit(tv(sens.exhaust), tu());
  $("#s-exhaust").textContent = sens.inlet != null && sens.exhaust != null ? `+${fmt(td(sens.exhaust - sens.inlet))} ${tu()} over inlet` : "";
  $("#v-watts").innerHTML = unit(sens.watts, "W");
  const w = s.history.filter(p => p.watts != null && p.t > Date.now() / 1000 - 3600).map(p => p.watts);
  $("#s-watts").textContent = w.length > 1 ? `1 h average: ${fmt(w.reduce((a, b) => a + b) / w.length)} W` : "";

  const hour = s.history.filter(p => p.t > Date.now() / 1000 - 3600);
  spark("#sp-cpu", hour, "cpu", "var(--cool)", 10);
  spark("#sp-speed", hour, monitor ? "fanpct" : "speed", "var(--accent)", 30);
  spark("#sp-rpm", hour, rpms.length ? "rpm" : "fanpct", "var(--ink-2)", rpms.length ? 2000 : 30);
  spark("#sp-inlet", hour, "inlet", "var(--ink-2)", 8);
  spark("#sp-exhaust", hour, "exhaust", "var(--ink-2)", 10);
  spark("#sp-watts", hour, "watts", "var(--ink-2)", 40);

  // bars relative to the fastest the fans went in the stored history, so 30 % looks like 30 %
  const maxRpm = Math.max(...rpms, ...s.history.map(p => p.rpm || 0), 1) * 1.1;
  $("#fan-count").textContent = sens.fans.length ? `${sens.fans.length} fans` : "";
  $("#fan-unit").textContent = rpms.length ? "RPM" : "%";
  $("#fans").innerHTML = sens.fans.map(f => {
    const pct = f.rpm != null ? f.rpm / maxRpm * 100 : f.pct;
    return `<tr><td>${esc(f.name)}${f.ok ? "" : ' <span class="muted">· alert</span>'}</td>
    <td class="bar"><div class="meter"><i style="width:${clamp(pct ?? 0, 0, 100)}%"></i></div></td>
    <td class="r">${f.rpm != null ? f.rpm.toLocaleString() : f.pct}${f.floor ? `<small class="muted" title="${translate("BMC minimum")}"> / ≥${Math.round(f.floor)}</small>` : ""}</td></tr>`;
  }).join("") || '<tr class="empty"><td colspan="3">No readings</td></tr>';
  renderCalib();
  const handed = s.handed_until > Date.now() / 1000;
  $("#ho-text").textContent = handed ? `The BMC has the fans until ${time(s.handed_until)}.` : "Hand the fans to the BMC for";
  $("#ho-btns").hidden = handed;
  $("#ho-end").hidden = !handed;
  $("#csv").href = `/api/history.csv?server=${encodeURIComponent(s.id)}&seconds=${range}`;
  $("#temp-count").textContent = sens.temps.length ? `${sens.temps.length} sensors` : "";
  const pos = v => clamp((v - T_MIN) / (T_MAX - T_MIN) * 100, 2, 100);
  $("#temps").innerHTML = sens.temps.map(t => {
    const near = t.warn && t.value >= t.warn - draft.threshold_margin;
    return `<tr><td>${esc(t.name)} <span class="mono muted">${esc(t.entity)}</span></td>
    <td class="bar"><div class="meter${t.warn ? " has-warn" : ""}"><i class="${near || (t.cpu && t.value >= fs - 5) ? "hot" : "c"}" style="width:${pos(t.value)}%"></i>
      ${t.warn ? `<b class="warn-tick" style="left:${pos(t.warn)}%" title="BMC warning at ${fmt(tv(t.warn))} ${tu()}"></b>` : ""}</div></td>
    <td class="r">${fmt(tv(t.value))}${t.warn ? `<small class="muted"> / ${fmt(tv(t.warn))}</small>` : ""}</td></tr>`;
  }).join("") || '<tr class="empty"><td colspan="3">No readings</td></tr>';
  $("#updated").textContent = `every ${s.interval} s`;
  $("#log").innerHTML = s.events.map(e => `<tr><td>${time(e.t, true)}</td><td class="${e.level}">${esc(e.msg)}</td></tr>`).join("")
    || '<tr class="empty"><td>Nothing yet</td></tr>';

  // control panel, or why there is none
  $("#control-body").hidden = monitor;
  $("#monitor-note").hidden = !monitor;
  if (monitor) {
    $("#applied").textContent = "monitoring";
    $("#monitor-note").innerHTML = `<b>Monitoring only.</b> ${esc(cap(s.monitor_reason))}, so the BMC keeps its own fan curve.
      Temperatures, history, Discord alerts and metrics work as usual.`;
  } else {
    $("#applied").textContent = auto ? "BMC in control" : `applying ${s.applied_speed} %`;
    $("#pcie-row").hidden = !s.pcie;
    renderControls();
  }
  $("#legend-auto").hidden = monitor;
  $("#legend-target").hidden = !s.history.some(p => p.target != null);
  renderChart();
}

function spark(sel, pts, key, color, minSpan) {
  const svg = $(sel), W = svg.clientWidth || 160, H = 30;
  const vals = pts.filter(p => p[key] != null);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (vals.length < 2) { svg.innerHTML = `<line x1="0" x2="${W}" y1="${H - 1}" y2="${H - 1}" stroke="var(--rule)"/>`; return; }
  // scale to the data, but never zoom in so far that sensor jitter looks like a trend
  let lo = Math.min(...vals.map(p => p[key])), hi = Math.max(...vals.map(p => p[key]));
  const mid = (lo + hi) / 2, half = Math.max(hi - lo, minSpan) / 2 * 1.15;
  lo = mid - half; hi = mid + half;
  const now = Date.now() / 1000, x = t => (1 - (now - t) / 3600) * W, y = v => H - 2 - (v - lo) / (hi - lo) * (H - 4);
  let d = "";
  pts.forEach((p, i) => { if (p[key] == null) return; d += (d && pts[i - 1]?.[key] != null ? "L" : "M") + x(p.t).toFixed(1) + "," + y(p[key]).toFixed(1); });
  const last = vals[vals.length - 1];
  svg.innerHTML = `<line x1="0" x2="${W}" y1="${H - 1}" y2="${H - 1}" stroke="var(--rule)"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/>
    <circle cx="${x(last.t)}" cy="${y(last[key])}" r="2.5" fill="${color}"/>`;
}

// ---------------------------------------------------------------- controls
function renderControls() {
  $$(".seg button").forEach(b => b.setAttribute("aria-pressed", b.dataset.mode === draft.mode));
  $$(".panel").forEach(p => p.classList.toggle("on", p.dataset.panel === draft.mode));
  if (document.activeElement !== $("#fixed")) $("#fixed").value = draft.fixed_speed;
  $("#smart-target").min = Math.round(tv(40)); $("#smart-target").max = Math.round(tv(85));
  if (document.activeElement !== $("#smart-target")) $("#smart-target").value = Math.round(tv(draft.smart_target));
  $("#smart-out").innerHTML = `${fmt(tv(draft.smart_target))}<small> ${tu()}</small>`;
  $$(".tu").forEach(el => el.textContent = tu());
  renderSmart();
  $("#fixed-out").innerHTML = `${draft.fixed_speed}<small> %</small>`;
  $("#failsafe").min = Math.round(tv(40)); $("#failsafe").max = Math.round(tv(90));
  $("#exhaust-limit").min = Math.round(tv(30)); $("#exhaust-limit").max = Math.round(tv(90));
  if (document.activeElement !== $("#failsafe")) $("#failsafe").value = Math.round(tv(draft.failsafe_temp));
  if (document.activeElement !== $("#ramp")) $("#ramp").value = draft.ramp_down_seconds;
  if (document.activeElement !== $("#min-speed")) $("#min-speed").value = draft.min_speed;
  const warn = minSpeedWarning();
  $("#min-warn").hidden = !warn;
  $("#min-warn").textContent = warn;
  if (document.activeElement !== $("#exhaust-limit")) $("#exhaust-limit").value = draft.exhaust_limit == null ? "" : Math.round(tv(draft.exhaust_limit));
  $("#margin-text").textContent = fmt(td(draft.threshold_margin));
  $$("#bmc-thr button").forEach(b => b.setAttribute("aria-pressed", b.dataset.thr === String(draft.bmc_thresholds)));
  $$("#dry button").forEach(b => b.setAttribute("aria-pressed", b.dataset.dry === String(draft.dry_run)));
  $$("#quiet-on button").forEach(b => b.setAttribute("aria-pressed", b.dataset.quiet === String(draft.quiet.enabled)));
  $("#quiet-fields").classList.toggle("off", !draft.quiet.enabled);
  for (const sel of ["#quiet-start", "#quiet-end", "#quiet-max"]) $(sel).disabled = !draft.quiet.enabled;
  for (const [sel, key] of [["#quiet-start", "start"], ["#quiet-end", "end"], ["#quiet-max", "max_speed"]])
    if (document.activeElement !== $(sel)) $(sel).value = draft.quiet[key];
  $$("[data-pcie]").forEach(b => b.setAttribute("aria-pressed", b.dataset.pcie === String(draft.pcie_cooling)));
  $("#save").disabled = $("#discard").disabled = !dirty;
  $("#save-state").textContent = dirty ? "Unsaved changes" : "No changes";
  $("#save-state").className = "state" + (dirty ? " dirty" : "");
  renderSchedule();
  if (drag == null) renderCurve();
}

// ---------------------------------------------------------------- schedule
const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
function renderSchedule() {
  const box = $("#sched");
  if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // don't redraw under the cursor
  box.innerHTML = draft.schedule.map((p, i) => `<div class="prof" data-i="${i}">
      <input class="p-name" data-k="name" value="${esc(p.name)}" maxlength="30" aria-label="Profile name">
      <span class="days">${DAY_NAMES.map((d, n) => `<button type="button" data-day="${n}" aria-pressed="${p.days.includes(n)}" title="${translate(d)}">${translate(d)[0]}</button>`).join("")}</span>
      <input type="time" data-k="start" value="${p.start}" aria-label="Start"> –
      <input type="time" data-k="end" value="${p.end}" aria-label="End">
      <span class="num">max <input type="number" data-k="max_speed" min="0" max="100" value="${p.max_speed ?? ""}" placeholder="—" aria-label="Maximum speed"><span class="muted">%</span></span>
      <span class="num">${translate("target")} <input type="number" data-k="smart_target" value="${p.smart_target == null ? "" : Math.round(tv(p.smart_target))}" placeholder="—" aria-label="Smart target"><span class="muted">${tu()}</span></span>
      <button type="button" class="inline-btn" data-del aria-label="Remove profile">×</button>
    </div>`).join("") + (draft.schedule.length < 8 ? `<button type="button" class="inline-btn" id="sched-add">${translate("Add a profile")}</button>` : "");
}
$("#sched").addEventListener("click", e => {
  if (e.target.id === "sched-add") {
    draft.schedule = [...draft.schedule, { name: translate("Weekend"), days: [5, 6], start: "00:00", end: "00:00", max_speed: 30, smart_target: null }];
    return touch();
  }
  const row = e.target.closest(".prof");
  if (!row) return;
  const i = +row.dataset.i, p = { ...draft.schedule[i] };
  if (e.target.dataset.day != null) {
    const d = +e.target.dataset.day;
    p.days = p.days.includes(d) ? p.days.filter(x => x !== d) : [...p.days, d].sort();
  } else if (e.target.dataset.del != null) {
    draft.schedule = draft.schedule.filter((_, n) => n !== i);
    return touch();
  } else return;
  draft.schedule = draft.schedule.map((q, n) => n === i ? p : q);
  touch();
});
$("#sched").addEventListener("input", e => {
  const row = e.target.closest(".prof"), k = e.target.dataset.k;
  if (!row || !k) return;
  const i = +row.dataset.i, raw = e.target.value.trim();
  const v = k === "name" ? e.target.value : k === "start" || k === "end" ? raw
    : raw === "" ? null : k === "smart_target" ? fromT(+raw) : +raw;
  if (v === "" && (k === "start" || k === "end")) return;
  draft.schedule = draft.schedule.map((q, n) => n === i ? { ...q, [k]: v } : q);
  dirty = true;
  $("#save").disabled = $("#discard").disabled = false;
  $("#save-state").textContent = "Unsaved changes";
  $("#save-state").className = "state dirty";
});
function touch() { dirty = true; renderControls(); }

// smart mode: what it is doing now, and the map it has learned of this server
// How smart mode is doing, from the readings of the last hours: is it holding the target, and calmly?
function smartReport() {
  const pts = (server?.history || []).filter(p => p.target != null && p.cpu != null);
  const hours = pts.length ? (pts.at(-1).t - pts[0].t) / 3600 : 0;
  if (pts.length < 20 || hours < 0.25) return "";
  const within = pts.filter(p => Math.abs(p.cpu - p.target) <= 2).length / pts.length;
  const speeds = pts.map(p => p.speed).filter(v => v != null);
  let turns = 0, dir = 0;
  for (let i = 1; i < speeds.length; i++) {
    const d = Math.sign(speeds[i] - speeds[i - 1]);
    if (d && d !== dir) { if (dir) turns++; dir = d; }
  }
  const avg = speeds.length ? speeds.reduce((a, b) => a + b) / speeds.length : 0;
  return `Last ${fmt(hours, 1)} h in smart mode: CPU within ${fmt(td(2))} ${tu()} of the target ${Math.round(within * 100)} % ` +
    `of the time, fans at ${Math.round(avg)} % on average, ${fmt(turns / hours, 1)} changes of direction an hour.`;
}

function renderSmart() {
  $("#smart-report").textContent = smartReport();
  const sm = server?.smart, active = sm && server.effective === "manual";
  $("#smart-live").textContent = active ? `Now ${server.applied_speed} % · ${server.reason.replace(/^smart: /, "")}` : "";
  const trend = sm ? `Trend ${sm.trend >= 0 ? "+" : ""}${fmt(td(sm.trend), 1)} ${tu()}/min · ` : "";
  $("#smart-detail").textContent = !active ? "" : sm.learned != null ? `${trend}learned ${sm.learned} % for this load`
    : `${trend}still learning what this load needs`;
  const pts = server?.smart_map || [];
  $("#smart-map").hidden = pts.length < 2;
  if (pts.length < 2) return;
  const W = 400, H = 150, pad = { l: 34, r: 12, t: 10, b: 24 };
  const watts = server.sensors?.watts;
  let lo = Math.min(...pts.map(p => p[0]), watts ?? Infinity), hi = Math.max(...pts.map(p => p[0]), watts ?? -Infinity);
  const span = Math.max(20, hi - lo);
  lo -= span * .08; hi += span * .08;
  const x = w => pad.l + (w - lo) / (hi - lo) * (W - pad.l - pad.r), y = v => pad.t + (1 - v / 100) * (H - pad.t - pad.b);
  let g = "";
  for (const v of [0, 50, 100]) g += `<line class="g" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${pad.l - 6}" y="${y(v) + 3.5}" text-anchor="end">${v}</text>`;
  const step = Math.max(10, Math.ceil((hi - lo) / 4 / 10) * 10);
  for (let w = Math.ceil(lo / step) * step; w <= hi; w += step) g += `<text x="${x(w)}" y="${H - 7}" text-anchor="middle">${w} W</text>`;
  const line = pts.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  g += `<polyline class="line" points="${line}"/>` + pts.map(p => `<circle class="dot-pt" cx="${x(p[0]).toFixed(1)}" cy="${y(p[1]).toFixed(1)}" r="2.5"/>`).join("");
  if (active && watts != null) g += `<circle class="now" cx="${x(watts).toFixed(1)}" cy="${y(server.applied_speed).toFixed(1)}" r="4"/>`;
  $("#smart-svg").innerHTML = g;
}
$("#smart-forget").onclick = async () => {
  if (!confirm(translate("Forget what smart mode learned about this server? It learns again as it runs."))) return;
  const r = await api("/api/smart/forget?server=" + encodeURIComponent(server.id), {});
  if (!r.ok) return toast(await failure(r), true);
  server.smart_map = [];
  renderSmart();
  toast("Smart mode starts learning afresh");
};

// The minimum speed against each fan's BMC minimum, from what the calibration measured: under it the
// BMC logs an event, lights the amber LED and may run every fan at full speed.
function minSpeedWarning() {
  const c = server?.calibration, p = draft.min_speed;
  const res = (c?.results || []).slice().sort((a, b) => a.pct - b.pct);
  for (const f of (server?.sensors?.fans || []).filter(f => f.floor)) {
    const pts = res.filter(r => r.fans?.[f.name] != null).map(r => [r.pct, r.fans[f.name]]);
    if (pts.length < 2) continue;
    if (p < pts[0][0]) {
      if (/BMC minimum/.test(c.reason || "")) return `Below ${pts[0][0]} % ${f.name} nears its BMC minimum of ${Math.round(f.floor)} RPM.`;
      continue;
    }
    const i = Math.max(1, pts.findIndex(q => q[0] >= p));
    const [[p0, r0], [p1, r1]] = [pts[i - 1], pts[i]];
    const rpm = Math.round(p1 === p0 ? r1 : r0 + (r1 - r0) * (p - p0) / (p1 - p0));
    if (rpm < f.floor) return `At ${p} % ${f.name} would turn about ${rpm} RPM, under its BMC minimum of ${Math.round(f.floor)} RPM: the BMC may raise an alarm and run every fan at full speed.`;
  }
  return "";
}

// Earlier settings: what changed each time, by whom, and a way back (into the draft, applied as usual)
const CHANGE_LABELS = { mode: "mode", fixed_speed: "fixed speed", curve: "curve", curve_source: "curve sensor",
  smart_target: "smart target", failsafe_temp: "CPU failsafe", min_speed: "minimum speed", exhaust_limit: "exhaust limit",
  bmc_thresholds: "BMC thresholds", threshold_margin: "threshold margin", ramp_down_seconds: "ramp-down delay",
  quiet: "quiet hours", schedule: "schedule", dry_run: "dry run", pcie_cooling: "PCIe cooling" };
let changes = [];
async function loadChanges() {
  const box = $("#changes-list");
  const r = await api("/api/settings/history?server=" + encodeURIComponent(server.id)).catch(() => null);
  if (!r?.ok) { box.innerHTML = `<p class="hint">${esc(r ? await failure(r) : translate("Fan Control did not answer"))}</p>`; return; }
  changes = (await r.json()).changes;
  box.innerHTML = !changes.length ? `<p class="hint">${translate("Nothing saved yet.")}</p>` : `<table><tbody>${changes.map((c, i) => {
    const before = changes[i + 1]?.settings;
    const what = before ? Object.keys(CHANGE_LABELS).filter(k => JSON.stringify(c.settings[k]) !== JSON.stringify(before[k]))
      .map(k => translate(CHANGE_LABELS[k])) : [translate("the oldest kept")];
    return `<tr><td class="mono">${new Date(c.t * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}</td>
      <td>${esc(what.join(", ") || translate("nothing changed"))}<br><small class="muted">${esc(c.who || "")}</small></td>
      <td class="r"><button class="inline-btn" data-load="${i}">${translate("Load")}</button></td></tr>`;
  }).join("")}</tbody></table>`;
}
$("#changes").addEventListener("toggle", e => { if (e.target.open) loadChanges(); });
$("#changes-list").addEventListener("click", e => {
  const b = e.target.closest("[data-load]");
  if (!b) return;
  draft = { ...structuredClone(draft), ...structuredClone(changes[+b.dataset.load].settings) };
  touch();
  toast("Earlier settings loaded. Apply to use them");
});

// The BMC's System Event Log, read when opened: a full one stops recording, and low fan speeds land in it
async function loadSel() {
  const box = $("#sel-body");
  box.innerHTML = `<p class="hint">${translate("Reading the BMC…")}</p>`;
  const r = await api("/api/sel?server=" + encodeURIComponent(server.id)).catch(() => null);
  if (!r?.ok) { box.innerHTML = `<p class="hint">${esc(r ? await failure(r) : translate("Fan Control did not answer"))}</p>`; return; }
  const d = await r.json(), full = d.used_pct >= 75;
  box.innerHTML = `<p class="hint${full ? " full" : ""}">${d.used_pct == null ? `${d.entries ?? 0} entries` : `${d.entries} entries, ${d.used_pct} % full`}</p>` +
    (full ? `<p class="hint full">${translate("A full log stops recording: clear it once you have read it.")}</p>` : "") +
    (d.recent.length ? `<table><tbody>${d.recent.map(e => `<tr><td class="mono">${esc(e[1] || "")} ${esc(e[2] || "")}</td>
      <td>${esc(e[3] || "")} · ${esc(e[4] || "")}${e[5] ? ` <span class="muted">${esc(e[5])}</span>` : ""}</td></tr>`).join("")}</tbody></table>` : "") +
    `<div class="sel-actions"><button class="inline-btn" id="sel-again">${translate("Read again")}</button>
     <button class="inline-btn" id="sel-clear">${translate("Clear the log")}</button></div>`;
  $("#sel-again").onclick = loadSel;
  $("#sel-clear").onclick = async () => {
    if (!confirm(translate("Clear the BMC's event log? Its entries are gone for good; read them first."))) return;
    const r = await api("/api/sel/clear?server=" + encodeURIComponent(server.id), {});
    if (!r.ok) return toast(await failure(r), true);
    toast("Event log cleared");
    loadSel();
  };
}
$("#sel").addEventListener("toggle", e => { if (e.target.open) loadSel(); });

// What the fans themselves draw, from the calibration: the server's power at a fan RPM over its power at the slowest
function fanWatts(c) {
  const pts = (c?.results || []).filter(r => r.watts != null).map(r => [r.rpm, r.watts]).sort((a, b) => a[0] - b[0]);
  if (pts.length < 2 || pts.at(-1)[1] - pts[0][1] < 5) return null;
  return rpm => {
    if (rpm <= pts[0][0]) return 0;
    for (let i = 1; i < pts.length; i++) if (rpm <= pts[i][0]) {
      const [r0, w0] = pts[i - 1], [r1, w1] = pts[i];
      return Math.max(0, w0 - pts[0][1] + (w1 - w0) * (rpm - r0) / (r1 - r0));
    }
    return pts.at(-1)[1] - pts[0][1];
  };
}
let week = { key: "", at: 0, pts: [] };  // 7 days of 5-minute averages, for the fan power comparison
async function fanPower(c) {
  const w = fanWatts(c), now = (server.history || []).filter(p => p.rpm != null);
  if (!w || now.length < 20) return "";
  const avg = now.reduce((a, p) => a + w(p.rpm), 0) / now.length;
  let text = `Over the last ${fmt((now.at(-1).t - now[0].t) / 3600, 1)} h the fans drew about ${Math.round(avg)} W, ` +
    `${fmt(avg * 24 * 30 / 1000, 1)} kWh a month at that rate.`;
  if (week.key !== server.id || Date.now() - week.at > 600000) {
    const r = await api(`/api/history?server=${encodeURIComponent(server.id)}&seconds=604800`).catch(() => null);
    week = { key: server.id, at: Date.now(), pts: r?.ok ? (await r.json()).points : [] };
  }
  const mean = pts => pts.reduce((a, p) => a + w(p.rpm), 0) / pts.length;
  const ours = week.pts.filter(p => p.rpm != null && p.speed != null), bmc = week.pts.filter(p => p.rpm != null && p.speed == null);
  if (ours.length >= 12 && bmc.length >= 12)
    text += ` ${translate(`In the last 7 days: about ${Math.round(mean(ours))} W while Fan Control had the fans, ${Math.round(mean(bmc))} W while the BMC had them.`)}`;
  return text;
}

// fan calibration: start it, follow it, and use what it found
const CALIB_HELP = "Steps the fans from 100 % down to 0 % and measures each speed, to find the lowest minimum worth setting. About 6 minutes, loud at first; every protection stays on.";
function renderCalib() {
  const c = server.calibration, res = c?.results || [], running = c?.status === "running";
  $("#calib").hidden = !server.control || !(server.sensors?.fans || []).some(f => f.rpm != null);
  $("#calib-start").hidden = running;
  $("#calib-stop").hidden = !running;
  const use = !running && c?.suggested != null && c.suggested !== draft.min_speed;
  $("#calib-use").hidden = !use;
  if (use) $("#calib-use").textContent = `Use ${c.suggested} % as minimum speed`;
  const when = c?.ended && new Date(c.ended * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
  $("#calib-text").textContent = !c ? CALIB_HELP
    : running ? `Calibrating: step ${c.i + 1} of ${c.steps.length}, fans at ${c.steps[c.i]} %.`
    : c.status === "done" ? (c.suggested == null ? `Calibrated on ${when}. No speed kept every fan turning.`
      : `Calibrated on ${when}. ${c.floor > res.at(-1).pct ? `Below ${c.floor} % the fans turn no slower.` : "The fans slow down at every step."} Suggested minimum speed: ${c.suggested} %.`)
    : `Stopped at ${c.at} %: ${c.reason}.` + (c.suggested != null ? ` Every fan kept turning down to ${c.suggested} %.` : "");
  $("#calib-note").hidden = !(c?.status === "done" && c.reason);
  if (c?.status === "done" && c.reason) $("#calib-note").textContent = `The calibration went no slower: ${c.reason}.`;
  const power = !running && c?.fan_watts >= 5;  // under that, the readings' own noise
  $("#calib-power").hidden = !power;
  if (power) $("#calib-power").textContent = `At 100 % the fans draw about ${c.fan_watts} W more than at ${res.at(-1).pct} %.`;
  if (power) fanPower(c).then(t => { $("#calib-use-power").hidden = !t; $("#calib-use-power").textContent = t; }).catch(() => {});
  else $("#calib-use-power").hidden = true;
  $("#calib-chart").hidden = res.length < 2;
  if (res.length < 2) return;
  // RPM by speed, measured; a hollow dot where a fan stood still
  const W = 400, H = 140, pad = { l: 42, r: 12, t: 10, b: 22 };
  const top = Math.ceil(Math.max(...res.map(r => r.rpm)) * 1.1 / 1000) * 1000;
  const x = p => pad.l + p / 100 * (W - pad.l - pad.r), y = v => pad.t + (1 - v / top) * (H - pad.t - pad.b);
  let g = "";
  for (const v of [0, top / 2, top]) g += `<line class="g" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${pad.l - 6}" y="${y(v) + 3.5}" text-anchor="end">${v}</text>`;
  for (const p of [0, 25, 50, 75, 100]) g += `<text x="${x(p)}" y="${H - 6}" text-anchor="middle">${p}</text>`;
  const pts = [...res].sort((a, b) => a.pct - b.pct);
  g += `<polyline class="line" points="${pts.map(r => `${x(r.pct).toFixed(1)},${y(r.rpm).toFixed(1)}`).join(" ")}"/>`;
  g += pts.map(r => `<circle class="dot-pt${r.ok ? "" : " bad"}" cx="${x(r.pct).toFixed(1)}" cy="${y(r.rpm).toFixed(1)}" r="3"><title>${r.pct} %: ${r.rpm} RPM${r.watts != null ? `, ${r.watts} W` : ""}</title></circle>`).join("");
  if (c.suggested != null) g += `<line class="fs" x1="${x(c.suggested)}" x2="${x(c.suggested)}" y1="${pad.t}" y2="${H - pad.b}"/>`;
  $("#calib-svg").innerHTML = g;
}
$("#calib-start").onclick = async () => {
  if (!confirm(translate("Calibrate the fans? They run from 100 % down to 0 % for about 6 minutes, loud at first."))) return;
  const r = await api("/api/calibrate?server=" + encodeURIComponent(server.id), {});
  if (!r.ok) return toast("Could not start: " + await failure(r), true);
  toast("Calibration started");
  setTimeout(pollServer, 800);
};
$("#calib-stop").onclick = async () => {
  const r = await api("/api/calibrate?server=" + encodeURIComponent(server.id), { action: "stop" });
  if (!r.ok) return toast(await failure(r), true);
  setTimeout(pollServer, 800);
};
// maintenance, a stress test: the BMC keeps the fans for a while, then control comes back on its own
async function handOver(minutes) {
  const r = await api("/api/handover?server=" + encodeURIComponent(server.id), { minutes });
  if (!r.ok) return toast(await failure(r), true);
  toast(minutes ? "Fans handed to the BMC" : "Fans taken back");
  setTimeout(pollServer, 800);
}
$$("[data-ho]").forEach(b => b.onclick = () => handOver(+b.dataset.ho));
$("#ho-end").onclick = () => handOver(0);
$("#calib-use").onclick = () => {
  draft.min_speed = server.calibration.suggested;
  touch();
  toast(`Minimum speed set to ${draft.min_speed} %. Apply to use it`);
};

$$(".seg button").forEach(b => b.onclick = () => { draft.mode = b.dataset.mode; touch(); });
$("#fixed").oninput = e => { draft.fixed_speed = +e.target.value; touch(); };
$("#smart-target").oninput = e => { draft.smart_target = fromT(+e.target.value); touch(); };
$$("[data-preset]").forEach(b => b.onclick = () => { draft.fixed_speed = +b.dataset.preset; $("#fixed").value = draft.fixed_speed; touch(); });
$("#failsafe").oninput = e => { const v = fromT(+e.target.value); if (v >= 40 && v <= 90) { draft.failsafe_temp = v; touch(); } };
$("#ramp").oninput = e => { const v = +e.target.value; if (Number.isInteger(v) && v >= 0 && v <= 600) { draft.ramp_down_seconds = v; touch(); } };
$$("[data-pcie]").forEach(b => b.onclick = () => { draft.pcie_cooling = JSON.parse(b.dataset.pcie); touch(); });
$$("[data-thr]").forEach(b => b.onclick = () => { draft.bmc_thresholds = b.dataset.thr === "true"; touch(); });
$$("[data-dry]").forEach(b => b.onclick = () => { draft.dry_run = b.dataset.dry === "true"; touch(); });
$$("[data-quiet]").forEach(b => b.onclick = () => { draft.quiet = { ...draft.quiet, enabled: b.dataset.quiet === "true" }; touch(); });
$("#quiet-start").onchange = e => { if (e.target.value) { draft.quiet = { ...draft.quiet, start: e.target.value }; touch(); } };
$("#quiet-end").onchange = e => { if (e.target.value) { draft.quiet = { ...draft.quiet, end: e.target.value }; touch(); } };
$("#quiet-max").oninput = e => { const v = +e.target.value; if (Number.isInteger(v) && v >= 0 && v <= 100) { draft.quiet = { ...draft.quiet, max_speed: v }; touch(); } };
$("#min-speed").oninput = e => { const v = +e.target.value; if (Number.isInteger(v) && v >= 0 && v <= 60) { draft.min_speed = v; touch(); } };
$("#exhaust-limit").oninput = e => {
  const raw = e.target.value.trim(), v = fromT(+raw);
  if (raw === "") { draft.exhaust_limit = null; touch(); } else if (v >= 30 && v <= 90) { draft.exhaust_limit = v; touch(); }
};
$("#discard").onclick = () => { dirty = false; draft = structuredClone(server.settings); renderServer(); };
// Changes that leave the server with less cooling than before are spelled out and confirmed.
function coolingCuts(before, after) {
  const cuts = [], t = v => `${fmt(tv(v))} ${tu()}`;
  const at65 = s => s.mode === "fixed" ? s.fixed_speed : s.mode === "curve" ? curveSpeed(s.curve, 65) : null;
  if (after.min_speed < before.min_speed && after.min_speed < 20)
    cuts.push(`${translate("Minimum speed")} ${before.min_speed} % → ${after.min_speed} %`);
  if (after.failsafe_temp >= before.failsafe_temp + 3)
    cuts.push(`${translate("CPU failsafe")} ${t(before.failsafe_temp)} → ${t(after.failsafe_temp)}`);
  if (before.exhaust_limit != null && after.exhaust_limit == null) cuts.push(translate("Exhaust air limit turned off"));
  if (before.bmc_thresholds && !after.bmc_thresholds) cuts.push(translate("BMC warning thresholds turned off"));
  if (before.pcie_cooling !== false && after.pcie_cooling === false) cuts.push(translate("Dell's cooling for third-party PCIe cards turned off"));
  const was = at65(before), now = at65(after);
  if (now != null && now < 30 && (was == null || now < was - 5)) cuts.push(`${translate("Fans at 65 °C")}: ${now} %`);
  return cuts;
}

$("#save").onclick = async () => {
  const cuts = coolingCuts(server.settings, draft);
  if (cuts.length && !confirm(`${translate("These changes reduce cooling:")}\n\n• ${cuts.join("\n• ")}\n\n${translate("Apply them anyway?")}`)) return;
  $("#save").disabled = true;
  try {
    const r = await api("/api/settings?server=" + encodeURIComponent(server.id), draft);
    if (!r.ok) throw new Error(await failure(r));
    const body = await r.json();
    dirty = false; draft = body;
    toast("Settings applied");
    if ($("#changes").open) loadChanges();
    renderControls();
    setTimeout(pollServer, 1200);
  } catch (e) {
    toast("Could not apply: " + e.message, true);
    $("#save").disabled = false;
  }
};

// A number outside its range is never dropped in silence: the field says what it accepts.
$(".fields").addEventListener("input", e => {
  const el = e.target;
  if (el.type !== "number") return;
  const bad = el.value !== "" && !el.checkValidity();
  el.setAttribute("aria-invalid", bad);
  const f = el.closest(".f, .prof");
  let msg = f.querySelector(".field-error");
  if (!msg) {
    msg = Object.assign(document.createElement("p"), { className: "field-error" });
    msg.setAttribute("role", "status");
    f.append(msg);
  }
  msg.textContent = bad ? (el.validity.stepMismatch ? translate("Whole numbers only")
    : `Between ${el.min} and ${el.max}`) : "";
});

// ---------------------------------------------------------------- curve presets
const CURVES = {
  quiet: [[35, 12], [45, 15], [55, 22], [62, 35], [68, 55]],
  balanced: [[30, 15], [45, 20], [55, 30], [65, 50], [72, 75]],
  cool: [[30, 20], [45, 30], [55, 45], [65, 70], [70, 90]],
  storage: [[30, 25], [45, 30], [55, 40], [65, 60], [72, 85]],
};
$$("[data-curve]").forEach(b => b.onclick = () => {
  draft.curve = CURVES[b.dataset.curve].map(p => [...p]);
  draft.mode = "curve";
  touch();
  toast(`${b.textContent} curve loaded. Apply to use it`);
});
$("#curve-copy").addEventListener("focus", () => {
  const others = (overview?.servers || []).filter(x => x.id !== server?.id && x.control);
  $("#curve-copy").innerHTML = '<option value="">Copy from…</option>' +
    others.map(x => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("");
});
$("#curve-copy").onchange = async e => {
  const id = e.target.value;
  e.target.value = "";
  if (!id) return;
  const r = await api("/api/state?server=" + encodeURIComponent(id));
  if (!r.ok) return toast("Could not read that server", true);
  const other = await r.json();
  draft.curve = other.settings.curve.map(p => [...p]);
  draft.mode = "curve";
  touch();
  toast(`Curve copied from ${other.name}. Apply to use it`);
};

// ---------------------------------------------------------------- curve editor
const CW = 400, CH = 230, P = { l: 34, r: 10, t: 12, b: 26 };
const cx = t => P.l + (t - T_MIN) / (T_MAX - T_MIN) * (CW - P.l - P.r);
const cy = v => P.t + (1 - v / 100) * (CH - P.t - P.b);
const ct = x => T_MIN + (x - P.l) / (CW - P.l - P.r) * (T_MAX - T_MIN);
const cs = y => 100 * (1 - (y - P.t) / (CH - P.t - P.b));

// What the curve follows: the hottest CPU, the exhaust air or any temperature sensor (a GPU, a disk bay)
function curveNow() {
  const src = draft.curve_source, sens = server?.sensors;
  if (!src) return server?.cpu_temp;
  if (src === "exhaust") return sens?.exhaust;
  const v = (sens?.temps || []).filter(t => t.name === src).map(t => t.value);
  return v.length ? Math.max(...v) : null;
}
function renderCurveSource() {
  const sel = $("#curve-src");
  if (document.activeElement === sel) return;
  const names = [...new Set((server?.sensors?.temps || []).filter(t => !t.cpu).map(t => t.name))];
  const src = draft.curve_source;
  if (src && src !== "exhaust" && !names.includes(src)) names.push(src);
  sel.innerHTML = `<option value="">${translate("Hottest CPU")}</option><option value="exhaust">${translate("Exhaust air")}</option>` +
    names.map(n => `<option value="${esc(n)}">${esc(n)}${curveNow() == null && n === src ? " " + translate("(no reading)") : ""}</option>`).join("");
  sel.value = src || "";
}
$("#curve-src").onchange = e => { draft.curve_source = e.target.value || null; touch(); };

function renderCurve() {
  renderCurveSource();
  const pts = [...draft.curve].sort((a, b) => a[0] - b[0]), fs = draft.failsafe_temp;
  let g = `<defs><pattern id="hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line y2="5" stroke="var(--accent)" stroke-opacity=".25"/></pattern></defs>`;
  for (let v = 0; v <= 100; v += 25) g += `<line class="g" x1="${P.l}" x2="${CW - P.r}" y1="${cy(v)}" y2="${cy(v)}"/><text x="${P.l - 6}" y="${cy(v) + 3.5}" text-anchor="end">${v}</text>`;
  for (let t = 30; t <= 90; t += 10) g += `<text x="${cx(t)}" y="${CH - 8}" text-anchor="middle">${fmt(tv(t))}°</text>`;
  if (!draft.curve_source) g += `<rect x="${cx(fs)}" y="${P.t}" width="${Math.max(0, CW - P.r - cx(fs))}" height="${CH - P.t - P.b}" fill="url(#hatch)"/>
    <line class="fs" x1="${cx(fs)}" x2="${cx(fs)}" y1="${P.t}" y2="${CH - P.b}"/>
    <text class="fs-text" x="${cx(fs) - 4}" y="${P.t + 10}" text-anchor="end">Auto ≥ ${fmt(tv(fs))}°</text>`;
  const line = [[T_MIN, pts[0][1]], ...pts, [T_MAX, pts[pts.length - 1][1]]].map(([t, v]) => `${cx(t).toFixed(1)},${cy(v).toFixed(1)}`);
  g += `<polygon class="area" points="${cx(T_MIN)},${cy(0)} ${line.join(" ")} ${cx(T_MAX)},${cy(0)}"/><polyline class="line" points="${line.join(" ")}"/>`;
  const now = curveNow();
  if (now != null) {
    const sp = curveSpeed(draft.curve, now);
    g += `<line class="now-l" x1="${cx(now)}" x2="${cx(now)}" y1="${cy(sp)}" y2="${CH - P.b}"/>
      <circle class="now" cx="${cx(now)}" cy="${cy(sp)}" r="4"/>
      <text x="${cx(now) + 7}" y="${cy(sp) + 14}" style="fill:var(--accent)">${fmt(tv(now))}° → ${sp}%</text>`;
  }
  draft.curve.forEach((p, i) => g += `<g class="pt" data-i="${i}" tabindex="0" role="slider" aria-valuenow="${p[1]}"
      aria-valuetext="${fmt(tv(p[0]))} ${tu()}, ${p[1]} %" aria-label="${translate("Curve point")} ${i + 1}">
      <circle cx="${cx(p[0])}" cy="${cy(p[1])}" r="13" fill="transparent"/><circle cx="${cx(p[0])}" cy="${cy(p[1])}" r="4.5"/></g>`);
  // The panel redraws every 5 s: a point that has the keyboard focus keeps it. Which one is decided
  // before the redraw, since current browsers fire focusout while innerHTML removes the old point.
  const active = document.activeElement?.closest?.("#curve .pt");
  const keep = focusPoint ?? (active ? +active.dataset.i : null);
  $("#curve").innerHTML = g;
  if (keep != null && (active || document.activeElement === document.body)) {
    focusPoint = keep;
    $(`#curve .pt[data-i="${keep}"]`)?.focus();
  }
  renderCurvePoints();
}

// The same points as plain fields: what touch screens and keyboards use, and exact values for anyone.
function renderCurvePoints() {
  const box = $("#curve-points");
  if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  box.innerHTML = draft.curve.map((p, i) => `<div class="cp" data-i="${i}">
      <span class="num"><input type="number" data-k="0" value="${Math.round(tv(p[0]))}" min="${Math.round(tv(T_MIN))}" max="${Math.round(tv(T_MAX))}" step="1"
        aria-label="${translate("Point")} ${i + 1}: ${translate("temperature")}"><span class="muted">${tu()}</span></span>
      <span class="arrow" aria-hidden="true">→</span>
      <span class="num"><input type="number" data-k="1" value="${p[1]}" min="0" max="100" step="1"
        aria-label="${translate("Point")} ${i + 1}: ${translate("fan speed")}"><span class="muted">%</span></span>
      <button type="button" class="inline-btn" data-del ${draft.curve.length <= 2 ? "disabled" : ""} aria-label="${translate("Remove point")} ${i + 1}">×</button>
    </div>`).join("") +
    (draft.curve.length < 10 ? `<button type="button" class="inline-btn" id="cp-add">${translate("Add a point")}</button>` : "");
}
let focusPoint = null;
$("#curve-points").addEventListener("change", e => {
  const row = e.target.closest(".cp"), k = e.target.dataset.k;
  if (!row || k == null || !e.target.checkValidity() || e.target.value === "") return;
  const i = +row.dataset.i, v = +e.target.value;
  draft.curve[i] = k === "0" ? [Math.round(fromT(v)), draft.curve[i][1]] : [draft.curve[i][0], Math.round(v)];
  draft.curve.sort((a, b) => a[0] - b[0]);
  touch();
});
$("#curve-points").addEventListener("click", e => {
  if (e.target.id === "cp-add") {
    // halfway along the widest gap, at the speed the curve already gives there
    const pts = [...draft.curve].sort((a, b) => a[0] - b[0]);
    let best = [pts[pts.length - 1][0], Math.min(T_MAX, pts[pts.length - 1][0] + 10)];
    for (let n = 1; n < pts.length; n++) if (pts[n][0] - pts[n - 1][0] > best[1] - best[0]) best = [pts[n - 1][0], pts[n][0]];
    const t = Math.round((best[0] + best[1]) / 2);
    draft.curve = [...pts, [t, curveSpeed(pts, t)]].sort((a, b) => a[0] - b[0]);
    return touch();
  }
  const del = e.target.closest("[data-del]");
  if (del && draft.curve.length > 2) {
    draft.curve.splice(+del.closest(".cp").dataset.i, 1);
    touch();
  }
});
$("#curve").addEventListener("keydown", e => {
  const g = e.target.closest(".pt");
  if (!g) return;
  const i = +g.dataset.i, step = e.shiftKey ? 5 : 1;
  const [t, s] = draft.curve[i];
  const moves = { ArrowUp: [t, s + step], ArrowDown: [t, s - step], ArrowRight: [t + step, s], ArrowLeft: [t - step, s] };
  if (moves[e.key]) {
    e.preventDefault();
    draft.curve[i] = [clamp(moves[e.key][0], T_MIN, T_MAX), clamp(moves[e.key][1], 0, 100)];
  } else if ((e.key === "Delete" || e.key === "Backspace") && draft.curve.length > 2) {
    e.preventDefault();
    draft.curve.splice(i, 1);
    focusPoint = null;
    return touch();
  } else return;
  const moved = draft.curve[i];
  draft.curve.sort((a, b) => a[0] - b[0]);
  focusPoint = draft.curve.indexOf(moved);
  touch();
});
$("#curve").addEventListener("focusout", e => { if (!e.relatedTarget?.closest?.("#curve .pt")) focusPoint = null; });

function svgPoint(e) {
  const r = $("#curve").getBoundingClientRect();
  return { x: (e.clientX - r.left) / r.width * CW, y: (e.clientY - r.top) / r.height * CH, r };
}
$("#curve").addEventListener("pointerdown", e => {
  const g = e.target.closest(".pt");
  if (!g) return;
  drag = +g.dataset.i;
  $("#curve").setPointerCapture(e.pointerId);
});
$("#curve").addEventListener("pointermove", e => {
  if (drag == null) return;
  const { x, y, r } = svgPoint(e);
  const p = draft.curve[drag] = [Math.round(clamp(ct(x), T_MIN, T_MAX)), Math.round(clamp(cs(y), 0, 100))];
  dirty = true;
  const tip = $("#curve-tip");
  tip.style.display = "block";
  tip.style.left = cx(p[0]) / CW * r.width + "px";
  tip.style.top = cy(p[1]) / CH * r.height + "px";
  tip.textContent = `${fmt(tv(p[0]))} ${tu()} → ${p[1]} %`;
  renderCurve();
});
const endDrag = () => {
  if (drag == null) return;
  drag = null; $("#curve-tip").style.display = "none";
  draft.curve.sort((a, b) => a[0] - b[0]);
  renderControls();
};
$("#curve").addEventListener("pointerup", endDrag);
$("#curve").addEventListener("pointercancel", endDrag);
$("#curve").addEventListener("dblclick", e => {
  const g = e.target.closest(".pt");
  if (g) {
    if (draft.curve.length <= 2) return toast("The curve needs at least 2 points", true);
    draft.curve.splice(+g.dataset.i, 1);
  } else {
    if (draft.curve.length >= 10) return toast("10 points at most", true);
    const { x, y } = svgPoint(e);
    draft.curve.push([Math.round(clamp(ct(x), T_MIN, T_MAX)), Math.round(clamp(cs(y), 0, 100))]);
    draft.curve.sort((a, b) => a[0] - b[0]);
  }
  touch();
});

// ---------------------------------------------------------------- history chart
$$(".tabs button").forEach(b => b.onclick = () => {
  range = +b.dataset.range;
  $$(".tabs button").forEach(x => x.setAttribute("aria-pressed", x === b));
  $("#csv").href = `/api/history.csv?server=${encodeURIComponent(server.id)}&seconds=${range}`;
  renderChart();
});

let chart = null;  // geometry of the last render, for the hover tooltip
let long = { key: "", at: 0, pts: [] };  // 5-minute averages for the 24 h and 7 d views

async function loadLong() {
  const key = server.id + ":" + range;
  if (long.key === key && Date.now() - long.at < 300000) return;
  const r = await api(`/api/history?server=${encodeURIComponent(server.id)}&seconds=${range}`);
  if (!r.ok) return;
  long = { key, at: Date.now(), pts: (await r.json()).points };
  renderChart();
}

function renderChart() {
  if (!server) return;
  const isLong = range > 10800;
  if (isLong) loadLong().catch(() => {});  // cached, re-fetched every 5 minutes; offline, the banner says so
  const monitor = server.effective === "monitor" || !server.control, fanKey = monitor ? "fanpct" : "speed";
  const svg = $("#chart"), W = svg.clientWidth || 800, H = svg.clientHeight || 320;
  const pl = 36, pr = 40, pt = 10, pb = 26;
  const end = Date.now() / 1000, start = end - range;
  const h = (isLong ? (long.key === server.id + ":" + range ? long.pts : []) : server.history).filter(p => p.t >= start);
  const x = t => pl + (t - start) / range * (W - pl - pr);
  const yT = v => pt + (1 - (v - T_MIN) / (T_MAX - T_MIN)) * (H - pt - pb);
  const yS = v => pt + (1 - v / 100) * (H - pt - pb);
  const step = isLong ? 450 : (server.interval || 15) * 1.5;
  const label = t => range > 86400
    ? new Date(t * 1000).toLocaleDateString([], { weekday: "short", day: "numeric" })
    : time(t);
  let g = `<defs><pattern id="autoband" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line y2="6" stroke="var(--rule-2)"/></pattern></defs>`;
  for (let v = 0; v <= 100; v += 25) g += `<line class="grid-l" x1="${pl}" x2="${W - pr}" y1="${yS(v)}" y2="${yS(v)}"/><text x="${W - pr + 8}" y="${yS(v) + 4}">${v}%</text>`;
  for (let v = 20; v <= 95; v += 15) g += `<text x="${pl - 8}" y="${yT(v) + 4}" text-anchor="end">${fmt(tv(v))}°</text>`;
  const n = W < 520 ? 3 : 6;
  for (let i = 0; i <= n; i++) {
    const t = start + range * i / n;
    g += `<text x="${x(t)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === n ? "end" : "middle"}">${label(t)}</text>`;
  }
  if (!monitor) {  // stretches where the BMC had control, as hatched bands
    let band = null;
    const flush = t => { if (band != null) { g += `<rect x="${x(band)}" y="${pt}" width="${Math.max(2, x(t) - x(band))}" height="${H - pt - pb}" fill="url(#autoband)"/>`; band = null; } };
    h.forEach(p => { if (p.speed == null) { if (band == null) band = p.t - step / 3; } else flush(p.t); });
    flush(h.length ? h[h.length - 1].t + step / 3 : end);
  }
  const path = (key, y) => {
    let d = "", pen = false;
    h.forEach((p, i) => {
      if (p[key] == null || (i && p.t - h[i - 1].t > step * 2)) pen = false;
      if (p[key] == null) return;
      d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`; pen = true;
    });
    return d;
  };
  g += `<line class="axis" x1="${pl}" x2="${W - pr}" y1="${H - pb}" y2="${H - pb}"/>
    <path d="${path("exhaust", yT)}" fill="none" stroke="var(--ink-3)" stroke-width="1.25" stroke-dasharray="4 3"/>
    <path d="${path(fanKey, yS)}" fill="none" stroke="var(--accent)" stroke-width="1.75" stroke-linejoin="round"/>
    <path d="${path("cpu", yT)}" fill="none" stroke="var(--cool)" stroke-width="1.75" stroke-linejoin="round"/>
    <path d="${path("target", yT)}" fill="none" stroke="var(--cool)" stroke-width="1" stroke-dasharray="2 4"/>
    <g id="cross" style="display:none"><line class="axis" y1="${pt}" y2="${H - pb}"/><circle r="3.5" fill="var(--cool)"/><circle r="3.5" fill="var(--accent)"/></g>`;
  if (!h.length) g += `<text x="${(W + pl - pr) / 2}" y="${H / 2}" text-anchor="middle">Collecting readings…</text>`;
  // what happened, as ticks along the top (changes, failsafes, errors; not every speed step); hovering lists them
  const marks = (server.events || []).filter(e => e.t >= start && !/^(Fans → |Dry run: )/.test(e.msg));
  g += marks.map(e => `<line class="ev ${e.level}" x1="${x(e.t).toFixed(1)}" x2="${x(e.t).toFixed(1)}" y1="${pt}" y2="${pt + 9}"/>`).join("");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = g;
  chart = { h, x, yT, yS, W, pl, pr, fanKey, marks };
  if (hoverX != null) showPoint(hoverX);  // a redraw every 5 s must not take the crosshair from under the pointer
}

let hoverX = null;
$("#chart").addEventListener("pointermove", e => {
  hoverX = e.clientX - $("#chart").getBoundingClientRect().left;
  showPoint(hoverX);
});
function showPoint(mx) {
  if (!chart || !chart.h.length) return;
  const r = $("#chart").getBoundingClientRect();
  const p = chart.h.reduce((a, b) => Math.abs(chart.x(b.t) - mx) < Math.abs(chart.x(a.t) - mx) ? b : a);
  const X = chart.x(p.t), cross = $("#cross"), [line, c1, c2] = cross.children, fan = p[chart.fanKey];
  cross.style.display = "";
  line.setAttribute("x1", X); line.setAttribute("x2", X);
  c1.setAttribute("cx", X); c1.setAttribute("cy", p.cpu != null ? chart.yT(p.cpu) : -10);
  c2.setAttribute("cx", X); c2.setAttribute("cy", fan != null ? chart.yS(fan) : -10);
  const tip = $("#tip");
  tip.innerHTML = `<div class="t">${range > 86400 ? new Date(p.t * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : time(p.t, true)}${range > 10800 ? " · 5 min average" : ""}</div>
    <div><span>CPU</span><span>${fmt(tv(p.cpu))} ${tu()}</span></div>
    <div><span>Exhaust</span><span>${fmt(tv(p.exhaust))} ${tu()}</span></div>
    <div><span>Fans</span><span>${fan == null ? "Automatic" : fan + " %"}</span></div>
    ${p.rpm != null ? `<div><span>Fan speed</span><span>${p.rpm.toLocaleString()} rpm</span></div>` : ""}
    <div><span>Power</span><span>${fmt(p.watts)} W</span></div>
    ${chart.marks.filter(e => Math.abs(chart.x(e.t) - mx) <= 8).slice(0, 3)
      .map(e => `<div class="ev-line ${e.level}"><span>${time(e.t)}</span><span>${esc(e.msg)}</span></div>`).join("")}`;
  tip.style.display = "block";
  tip.style.left = (X > r.width / 2 ? X - tip.offsetWidth - 14 : X + 14) + "px";
}
$("#chart").addEventListener("pointerleave", () => {
  hoverX = null;
  $("#tip").style.display = "none";
  const c = $("#cross");
  if (c) c.style.display = "none";
});

// ---------------------------------------------------------------- shell
$$(".tu").forEach(el => el.textContent = tu());  // static unit labels; the unit only changes with a reload
$("#pref-lang").value = PREFS.lang;
$("#pref-unit").value = PREFS.unit;
$("#pref-lang").onchange = e => { PREFS.lang = e.target.value; savePrefs(); location.reload(); };
$("#pref-unit").onchange = e => { PREFS.unit = e.target.value; savePrefs(); location.reload(); };

$("#signout").onclick = async () => {
  await api("/api/logout", {});
  location.replace("/login");
};

$("#accept-cert").onclick = async () => {
  if (!confirm(translate("Only accept it if you replaced the BMC's certificate yourself. Otherwise someone may be intercepting the connection to it."))) return;
  const r = await api(`/api/servers/${encodeURIComponent(server.id)}/accept-certificate`, {});
  toast(r.ok ? "The new certificate will be remembered from the next reading" : "Could not accept it", !r.ok);
};

let resizing;
addEventListener("resize", () => {  // once the window settles, not on every pixel of a drag
  clearTimeout(resizing);
  resizing = setTimeout(() => { if (route.view === "server" && server) renderServer(); }, 150);
});
addEventListener("beforeunload", e => { if (Object.values(unsaved).some(f => f())) e.preventDefault(); });

route = parseRoute();
// after every script has run: the first route may be a page that integrations.js draws
addEventListener("DOMContentLoaded", () => pollOverview().then(go));
// Polling: never two requests of a kind at once (a slow BMC must not pile them up), and nothing at
// all while the tab is hidden; coming back refreshes at once.
function every(ms, fn) {
  let busy = false;
  const tick = async () => {
    if (!document.hidden && !busy) {
      busy = true;
      try { await fn(); } catch (e) { console.error(e); } finally { busy = false; }  // one failure never stops it
    }
    setTimeout(tick, ms);
  };
  setTimeout(tick, ms);
}
every(5000, pollOverview);
every(5000, pollServer);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollOverview(); pollServer(); } });
