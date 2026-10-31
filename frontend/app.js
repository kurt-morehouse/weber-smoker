/* Weber smoker dashboard. No build step: plain JavaScript + uPlot.
 *
 * Replay: pick a saved cook, play / pause / speed / scrub (backend /ws/replay).
 * Live:   the cook running on the Pi right now, with cook controls (/ws/live).
 * Both feed the same charts, latest-values table and journal.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const MAX_PROBES = 3;
  const STALE_LIVE_S = 5;
  const BATTERY_WARN = 20;

  // ------------------------------------------------------------------ state
  let mode = "replay";
  let labels = {};
  let data = null;            // { x: [], cols: {name: []} }
  let latest = {};            // col -> {v, t} last non-null value and its row time
  let currentRow = null;
  let currentTime = null;
  let journalStem = null;
  let photoTimes = [];        // epoch seconds of journal photos, drawn on the charts
  let liveStatus = {};
  let liveState = {};
  let editing = false;
  const charts = {};

  // ----------------------------------------------------------------- series
  const ZONE_SERIES = [
    { col: "zone1_temp_f", label: "Zone 1", color: "--z1", width: 2 },
    { col: "zone1_target_f", label: "Zone 1 target", color: "--z1", width: 1, dash: [6, 4] },
    { col: "zone2_temp_f", label: "Zone 2", color: "--z2", width: 2 },
    { col: "zone2_target_f", label: "Zone 2 target", color: "--z2", width: 1, dash: [6, 4] },
  ];
  const DUTY_SERIES = [
    { col: "zone1_relay_state", label: "Zone 1 duty", color: "--z1", width: 1, fill: true },
    { col: "zone2_relay_state", label: "Zone 2 duty", color: "--z2", width: 1, fill: true },
  ];
  const PROBE_COLORS = ["--p1", "--p2", "--p3"];
  const PROBE_SERIES = [];
  for (let n = 1; n <= MAX_PROBES; n++) {
    const color = PROBE_COLORS[n - 1];
    PROBE_SERIES.push({ col: `probe${n}_food_f`, label: `Probe ${n} food`, color, width: 2 });
    PROBE_SERIES.push({ col: `probe${n}_ambient_f`, label: `Probe ${n} ambient`, color, width: 1, dash: [5, 4] });
    for (let i = 1; i <= 4; i++) {
      PROBE_SERIES.push({ col: `probe${n}_t${i}_f`, label: `P${n} t${i}`, color, width: 0.8,
                          dash: [1, 3], internal: true });
    }
  }
  const ALL_COLS = [...ZONE_SERIES, ...DUTY_SERIES, ...PROBE_SERIES].map((s) => s.col);

  function emptyData() {
    const cols = {};
    for (const c of ALL_COLS) cols[c] = [];
    return { x: [], cols };
  }

  // ----------------------------------------------------------------- charts
  const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  function markersPlugin() {
    return {
      hooks: {
        draw: [(u) => {
          if (!photoTimes.length) return;
          const ctx = u.ctx;
          ctx.save();
          ctx.strokeStyle = css("--accent");
          ctx.fillStyle = css("--accent");
          ctx.lineWidth = 1;
          ctx.setLineDash([2, 3]);
          for (const t of photoTimes) {
            const x = u.valToPos(t, "x", true);
            if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
            ctx.beginPath();
            ctx.moveTo(x, u.bbox.top);
            ctx.lineTo(x, u.bbox.top + u.bbox.height);
            ctx.stroke();
            ctx.beginPath();
            ctx.arc(x, u.bbox.top + 5, 4, 0, 2 * Math.PI);
            ctx.fill();
          }
          ctx.restore();
        }],
      },
    };
  }

  function makeChart(el, series, opts) {
    const width = el.clientWidth || 800;
    const ink = css("--muted");
    const grid = css("--line");
    const axis = { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid } };
    return new uPlot({
      width,
      height: opts.height,
      cursor: { sync: { key: "smoker" }, drag: { x: true, y: false } },
      scales: { x: { time: true }, y: opts.yRange ? { range: opts.yRange } : { auto: true } },
      axes: [
        { ...axis, values: (u, ticks) => ticks.map(fmtTime) },
        { ...axis, size: 52, values: (u, ticks) => ticks.map((v) => opts.yFmt ? opts.yFmt(v) : v) },
      ],
      legend: { show: opts.legend !== false },
      plugins: opts.markers ? [markersPlugin()] : [],
      series: [
        { value: (u, v) => (v == null ? "–" : new Date(v * 1000).toLocaleString()) },
        ...series.map((s) => {
          const c = css(s.color);
          return {
            label: s.label,
            stroke: c,
            width: s.width,
            dash: s.dash,
            fill: s.fill ? c + "33" : undefined,
            show: !s.internal || $("show-internal").checked,
            spanGaps: false,
            value: (u, v) => (v == null ? "–" : opts.yFmt ? opts.yFmt(v) : v.toFixed(1)),
          };
        }),
      ],
    }, [[], ...series.map(() => [])], el);
  }

  function buildCharts() {
    for (const k of Object.keys(charts)) { charts[k].destroy(); delete charts[k]; }
    charts.zones = makeChart($("chart-zones"), ZONE_SERIES, { height: 300, markers: true });
    charts.duty = makeChart($("chart-duty"), DUTY_SERIES, {
      height: 110, yRange: [0, 1], yFmt: (v) => `${Math.round(v * 100)}%`, legend: true });
    charts.probes = makeChart($("chart-probes"), PROBE_SERIES, { height: 300, markers: true });
    redraw(true);
  }

  let redrawQueued = false;
  function redraw(now) {
    if (now) { doRedraw(); return; }
    if (redrawQueued) return;
    redrawQueued = true;
    requestAnimationFrame(() => { redrawQueued = false; doRedraw(); });
  }
  function doRedraw() {
    if (!data) data = emptyData();
    const set = (chart, series) => chart && chart.setData([data.x, ...series.map((s) => data.cols[s.col])]);
    set(charts.zones, ZONE_SERIES);
    set(charts.duty, DUTY_SERIES);
    set(charts.probes, PROBE_SERIES);
    renderLatest();
    renderWarnings();
  }

  window.addEventListener("resize", () => {
    for (const [k, el] of [["zones", "chart-zones"], ["duty", "chart-duty"], ["probes", "chart-probes"]]) {
      if (charts[k]) charts[k].setSize({ width: $(el).clientWidth, height: charts[k].height });
    }
  });

  $("show-internal").addEventListener("change", () => {
    const show = $("show-internal").checked;
    PROBE_SERIES.forEach((s, i) => { if (s.internal) charts.probes.setSeries(i + 1, { show }); });
  });

  // ------------------------------------------------------------------- rows
  function resetData() {
    data = emptyData();
    latest = {};
    currentRow = null;
    currentTime = null;
  }

  function addRows(rows) {
    if (!data) data = emptyData();
    for (const row of rows) {
      const t = Date.parse(row.timestamp) / 1000;
      if (!isFinite(t)) continue;
      data.x.push(t);
      for (const c of ALL_COLS) {
        const v = row[c];
        data.cols[c].push(typeof v === "number" ? v : null);
      }
      for (const [k, v] of Object.entries(row)) {
        if (k.startsWith("_")) continue;
        if (v !== null && v !== "" && v !== undefined) latest[k] = { v, t };
      }
      currentRow = row;
      currentTime = t;
    }
    redraw();
  }

  // --------------------------------------------------------- latest table
  function label(col) {
    return (labels.columns && labels.columns[col] && labels.columns[col].label) || col;
  }
  function unit(col) {
    return (labels.columns && labels.columns[col] && labels.columns[col].unit) || "";
  }
  function fmtValue(col, v) {
    if (typeof v !== "number") return String(v);
    if (col.endsWith("_relay_state")) return `${Math.round(v * 100)}%`;
    const u = unit(col);
    return (Number.isInteger(v) ? v : v.toFixed(1)) + (u ? ` ${u}` : "");
  }
  function fmtAge(s) {
    if (s < 90) return `${Math.round(s)} s ago`;
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    return `${(s / 3600).toFixed(1)} h ago`;
  }

  function renderLatest() {
    const body = $("latest").querySelector("tbody");
    const cols = Object.keys(latest).filter((c) => c !== "timestamp");
    const order = (labels.columns ? Object.keys(labels.columns) : []);
    cols.sort((a, b) => {
      const ia = order.indexOf(a), ib = order.indexOf(b);
      return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib) || a.localeCompare(b);
    });
    const html = [];
    if (currentRow) {
      html.push(`<tr><td>Time</td><td>${new Date(currentTime * 1000).toLocaleString()}</td></tr>`);
    }
    for (const c of cols) {
      const cur = currentRow ? currentRow[c] : null;
      const present = cur !== null && cur !== undefined && cur !== "";
      if (present) {
        html.push(`<tr><td>${esc(label(c))}</td><td>${esc(fmtValue(c, cur))}</td></tr>`);
      } else {
        const last = latest[c];
        html.push(`<tr class="missing"><td>${esc(label(c))}</td><td>${esc(fmtValue(c, last.v))}` +
                  ` · ${fmtAge(currentTime - last.t)}</td></tr>`);
      }
    }
    body.innerHTML = html.join("") || `<tr class="missing"><td>No data yet</td><td></td></tr>`;
  }

  function renderWarnings() {
    const low = [];
    if (currentRow) {
      for (let n = 1; n <= MAX_PROBES; n++) {
        const b = currentRow[`probe${n}_battery_pct`];
        if (typeof b === "number" && b <= BATTERY_WARN) low.push(`Probe ${n} battery ${b}%`);
      }
    }
    $("battery-warn").hidden = !low.length;
    $("battery-warn").textContent = low.length ? `Low battery: ${low.join(", ")}` : "";
    const safety = (mode === "live" && liveStatus.safety_latched) ||
                   (currentRow && currentRow.safety) || "";
    $("safety-warn").hidden = !safety;
    $("safety-warn").textContent = safety ? `Safety: ${safety}` : "";
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function showMessage(text, isError) {
    const m = $("message");
    m.hidden = !text;
    m.textContent = text || "";
    m.className = "message" + (isError ? " error" : "");
    clearTimeout(showMessage._t);
    if (text) showMessage._t = setTimeout(() => { m.hidden = true; }, isError ? 10000 : 5000);
  }

  // ------------------------------------------------------------- websockets
  function wsUrl(path) {
    return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;
  }

  function openSocket(path, onMessage, onOpen) {
    let ws = null;
    let closed = false;
    let delay = 500;
    const api = {
      send(msg) { if (ws && ws.readyState === 1) { ws.send(JSON.stringify(msg)); return true; } return false; },
      close() { closed = true; if (ws) ws.close(); },
    };
    function connect() {
      ws = new WebSocket(wsUrl(path));
      ws.onopen = () => { delay = 500; onOpen && onOpen(api); };
      ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch (err) { console.error(err); } };
      ws.onclose = () => { if (!closed) setTimeout(connect, delay = Math.min(delay * 2, 8000)); };
    }
    connect();
    return api;
  }

  // ----------------------------------------------------------------- replay
  let replayWs = null;
  let replayCount = 0;
  let replayPlaying = false;
  let scrubbing = false;

  async function loadCooks(selectFile) {
    const sel = $("cook-select");
    try {
      const cooks = await (await fetch("/api/cooks")).json();
      sel.innerHTML = `<option value="">Pick a cook…</option>` + cooks.map((c) => {
        const when = c.started ? new Date(c.started).toLocaleString() : c.modified.slice(0, 16).replace("T", " ");
        const kind = c.kind === "cook" ? "" : ` [${c.kind}]`;
        const hw = c.hardware === "simulated" ? " (simulated)" : "";
        return `<option value="${esc(c.file)}">${esc(c.name)}${kind}${hw} · ${esc(when)} · ${c.rows} rows</option>`;
      }).join("");
      if (!cooks.length) sel.innerHTML = `<option value="">No cooks in the data folder yet</option>`;
      if (selectFile) sel.value = selectFile;
    } catch (e) {
      sel.innerHTML = `<option value="">Backend not reachable</option>`;
    }
  }

  function onReplayMessage(m) {
    if (mode !== "replay") return;
    switch (m.type) {
      case "loaded":
        replayCount = m.count;
        $("scrub").max = Math.max(0, m.count - 1);
        $("scrub").disabled = m.count === 0;
        $("play-btn").disabled = m.count === 0;
        setJournal(m.file.replace(/\.csv$/, ""));
        break;
      case "history":
        resetData();
        addRows(m.rows);
        break;
      case "rows":
        addRows(m.rows);
        break;
      case "position":
        replayPlaying = m.playing;
        $("play-btn").textContent = m.playing ? "Pause" : "Play";
        if (!scrubbing) $("scrub").value = m.index;
        $("replay-pos").textContent = m.count ? `${m.index + 1} / ${m.count} · ${m.speed}×` : "–";
        break;
      case "end":
        $("play-btn").textContent = "Play";
        break;
      case "error":
        showMessage(m.message, true);
        break;
    }
  }

  $("cook-select").addEventListener("change", () => {
    const f = $("cook-select").value;
    if (!f) return;
    replayWs.send({ type: "load", file: f });
    replayWs.send({ type: "speed", value: Number($("speed-select").value) });
  });
  $("play-btn").addEventListener("click", () => replayWs.send({ type: replayPlaying ? "pause" : "play" }));
  $("speed-select").addEventListener("change", () =>
    replayWs.send({ type: "speed", value: Number($("speed-select").value) }));
  let seekTimer = null;
  $("scrub").addEventListener("input", () => {
    scrubbing = true;
    clearTimeout(seekTimer);
    seekTimer = setTimeout(() => {
      replayWs.send({ type: "seek", index: Number($("scrub").value) });
      scrubbing = false;
    }, 120);
  });

  // ------------------------------------------------------------------- live
  let liveWs = null;
  let cmdSeq = 0;

  function onLiveMessage(m) {
    if (m.type === "state") {
      liveState = m;
      renderLiveState();
      return;
    }
    if (mode !== "live") return;
    switch (m.type) {
      case "history":
        resetData();
        addRows(m.rows);
        break;
      case "row":
        liveStatus = m.status || {};
        addRows([m.row]);
        renderLiveStatus();
        break;
      case "command_result":
        showMessage(m.message, !m.ok);
        break;
    }
  }

  function renderLiveState() {
    const s = liveState;
    const conn = $("live-conn");
    conn.textContent = s.connected ? "Pi connected" : "Pi not reachable";
    conn.className = "pill" + (s.connected ? " ok" : "");
    conn.title = s.error ? `${s.pi}: ${s.error}` : s.pi || "";
    const age = $("live-age");
    if (s.last_row_age_s == null) {
      age.textContent = "no rows yet";
      age.className = "mono muted";
    } else {
      age.textContent = `last row ${s.last_row_age_s.toFixed(0)} s ago`;
      age.className = "mono " + (s.last_row_age_s > STALE_LIVE_S ? "stale" : "muted");
    }
    $("live-file").textContent = s.file ? `recording ${s.file}` : "";
    if (!s.commands) $("live-controls").title = "No command token on the Mac: controls are disabled";
    for (const b of $("live-controls").querySelectorAll("button, input")) b.disabled = !s.commands || !s.connected;
  }

  function renderLiveStatus() {
    const st = liveStatus.state || "idle";
    const pill = $("live-state");
    pill.textContent = st + (liveStatus.hardware === "simulated" ? " (simulated)" : "");
    pill.className = "pill " + (st === "cooking" ? "cooking" : "neutral");
    $("ctl-idle").hidden = st !== "idle";
    $("ctl-running").hidden = st === "idle";
    $("btn-pause").hidden = st !== "cooking";
    $("btn-resume").hidden = st !== "paused";
    const sp = liveStatus.setpoints || {};
    if (!editing) {
      if (sp.zone1 != null) { $("sp1").value = sp.zone1; }
      if (sp.zone2 != null) { $("sp2").value = sp.zone2; }
    }
    const stem = liveStatus.file ? liveStatus.file.replace(/\.csv$/, "") : null;
    if (stem !== journalStem) setJournal(stem);
  }

  for (const id of ["sp1", "sp2"]) {
    $(id).addEventListener("focus", () => { editing = true; });
    $(id).addEventListener("blur", () => { setTimeout(() => { editing = false; }, 300); });
  }

  function command(cmd, args) {
    const ok = liveWs.send({ type: "command", id: `c${++cmdSeq}`, command: cmd, args: args || {} });
    if (!ok) showMessage("Not connected to the backend", true);
  }
  $("btn-start").addEventListener("click", () => command("start", {
    name: $("cook-name").value.trim() || "cook",
    setpoints: { zone1: Number($("sp1-start").value), zone2: Number($("sp2-start").value) },
  }));
  $("btn-setpoints").addEventListener("click", () => {
    const sp = liveStatus.setpoints || {};
    const z1 = Number($("sp1").value), z2 = Number($("sp2").value);
    if (z1 !== sp.zone1) command("set_setpoint", { zone: "zone1", value_f: z1 });
    if (z2 !== sp.zone2) command("set_setpoint", { zone: "zone2", value_f: z2 });
    editing = false;
  });
  $("btn-pause").addEventListener("click", () => {
    const reason = prompt("Reason for pausing (e.g. wrapping, spritzing)?", "");
    if (reason !== null) command("pause", { reason });
  });
  $("btn-resume").addEventListener("click", () => command("resume"));
  $("btn-stop").addEventListener("click", () => {
    if (confirm("Stop the cook? Both relays turn off and the CSV is closed.")) command("stop");
  });

  // ---------------------------------------------------------------- journal
  async function setJournal(stem) {
    journalStem = stem;
    photoTimes = [];
    $("journal-form").hidden = !stem;
    $("journal-for").textContent = stem || (mode === "live" ? "no cook running" : "pick a cook");
    if (!stem) { redraw(); return; }
    try {
      const j = await (await fetch(`/api/journal/${encodeURIComponent(stem)}`)).json();
      if (journalStem !== stem) return;
      $("j-proteins").value = (j.proteins || []).join(", ");
      $("j-setup").value = j.setup || "";
      $("j-outcome").value = j.outcome || "";
      $("j-rating").value = j.rating == null ? "" : String(j.rating);
      $("j-notes").value = j.notes || "";
      $("j-saved").textContent = "";
      renderPhotos(j.photos || []);
    } catch (e) {
      $("journal-for").textContent = `${stem} (journal unavailable)`;
    }
  }

  function renderPhotos(photos) {
    photoTimes = photos.map((p) => p.taken_at && Date.parse(p.taken_at) / 1000).filter((t) => isFinite(t));
    $("j-photos").innerHTML = photos.map((p) => `
      <figure class="photo">
        <a href="/api/journal/${encodeURIComponent(journalStem)}/photos/${encodeURIComponent(p.file)}" target="_blank">
          <img src="/api/journal/${encodeURIComponent(journalStem)}/photos/${encodeURIComponent(p.file)}" alt="${esc(p.caption || "photo")}" loading="lazy"></a>
        <button type="button" data-del="${esc(p.file)}" title="Delete photo">✕</button>
        <figcaption class="cap">${esc(p.caption || "")}${p.taken_at ? " · " + esc(new Date(p.taken_at).toLocaleTimeString()) : ""}</figcaption>
      </figure>`).join("");
    redraw(true);
  }

  $("journal-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!journalStem) return;
    const body = {
      proteins: $("j-proteins").value.split(",").map((s) => s.trim()).filter(Boolean),
      setup: $("j-setup").value,
      outcome: $("j-outcome").value,
      rating: $("j-rating").value === "" ? null : Number($("j-rating").value),
      notes: $("j-notes").value,
    };
    const r = await fetch(`/api/journal/${encodeURIComponent(journalStem)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("j-saved").textContent = r.ok ? `saved ${new Date().toLocaleTimeString()}` : "save failed";
  });

  $("j-photo").addEventListener("change", async () => {
    const file = $("j-photo").files[0];
    if (!file || !journalStem) return;
    const fd = new FormData();
    fd.append("photo", file);
    fd.append("caption", $("j-caption").value);
    const r = await fetch(`/api/journal/${encodeURIComponent(journalStem)}/photos`, { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) { showMessage(j.detail || "upload failed", true); return; }
    $("j-photo").value = "";
    $("j-caption").value = "";
    renderPhotos(j.photos);
  });

  $("j-photos").addEventListener("click", async (e) => {
    const name = e.target.getAttribute && e.target.getAttribute("data-del");
    if (!name || !confirm("Delete this photo?")) return;
    const r = await fetch(`/api/journal/${encodeURIComponent(journalStem)}/photos/${encodeURIComponent(name)}`,
                          { method: "DELETE" });
    if (r.ok) renderPhotos((await r.json()).photos);
  });

  // ------------------------------------------------------------------ modes
  function setMode(m) {
    mode = m;
    try { localStorage.setItem("smoker-mode", m); } catch (e) { /* private window */ }
    $("mode-replay").setAttribute("aria-selected", m === "replay");
    $("mode-live").setAttribute("aria-selected", m === "live");
    $("replay-bar").hidden = m !== "replay";
    $("live-bar").hidden = m !== "live";
    resetData();
    redraw(true);
    if (m === "replay") {
      replayWs.send({ type: "pause" });
      const f = $("cook-select").value;
      setJournal(f ? f.replace(/\.csv$/, "") : null);
      if (f) replayWs.send({ type: "load", file: f });
      loadCooks(f);
    } else {
      replayWs.send({ type: "pause" });
      liveStatus = {};
      setJournal(null);
      if (liveWs) liveWs.close();
      liveWs = openSocket("/ws/live", onLiveMessage);
      renderLiveStatus();
    }
  }
  $("mode-replay").addEventListener("click", () => mode !== "replay" && setMode("replay"));
  $("mode-live").addEventListener("click", () => mode !== "live" && setMode("live"));

  // ------------------------------------------------------------------- boot
  async function boot() {
    try { labels = await (await fetch("/api/labels")).json(); } catch (e) { labels = {}; }
    buildCharts();
    replayWs = openSocket("/ws/replay", onReplayMessage);
    await loadCooks();
    let saved = "replay";
    try { saved = localStorage.getItem("smoker-mode") || "replay"; } catch (e) { /* ignore */ }
    if (saved === "live") setMode("live");
  }
  boot();
})();
