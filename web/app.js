"use strict";
/* ZeroDesk web UI. All data from the device is inserted with textContent,
   never as HTML. */
(function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };

  // ---------------------------------------------------------------- helpers
  function fmtBytes(n) {
    if (n === null || n === undefined) return "–";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + u[i];
  }
  function fmtUptime(s) {
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return (d ? d + "d " : "") + (d || h ? h + "h " : "") + m + "m";
  }
  function fmtDate(t) {
    const d = new Date(t * 1000);
    return d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" }) + " " +
      d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  let toastTimer;
  function toast(msg, isErr) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), isErr ? 5000 : 2500);
  }
  function setConn(ok) {
    $("conn").className = "dot " + (ok ? "ok" : "bad");
    $("conn").title = ok ? "Connected" : "Can't reach the Cardputer";
  }

  async function api(method, url, body) {
    const opt = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    let r;
    try { r = await fetch(url, opt); } catch (e) { setConn(false); throw new Error("Can't reach the Cardputer"); }
    setConn(true);
    if (r.status === 401) { location.replace("/login"); throw new Error("Signed out"); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ("Error " + r.status));
    return data;
  }

  document.querySelectorAll(".dlg-cancel").forEach(b => b.addEventListener("click", () => b.closest("dialog").close("cancel")));
  function dialog(message, withInput, value, okLabel, danger, secret) {
    return new Promise((resolve) => {
      const d = $("dlg"), inp = $("dlg-input"), ok = $("dlg-ok");
      inp.type = secret ? "password" : "text";
      inp.autocomplete = "off";
      d.returnValue = "";
      $("dlg-msg").textContent = message;
      inp.classList.toggle("hidden", !withInput);
      inp.value = value || "";
      ok.textContent = okLabel || "OK";
      ok.className = danger ? "danger" : "primary";
      d.onclose = () => { const v = inp.value; inp.value = ""; resolve(d.returnValue === "ok" ? (withInput ? v : true) : null); };
      d.showModal();
      if (withInput) { inp.focus(); inp.select(); }
    });
  }

  function wsUrl(path) { return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path; }

  // ---------------------------------------------------------------- admin password + jobs
  function askPassword(wrong) {
    return new Promise((resolve) => {
      const d = $("pw-dlg"), inp = $("pw-input");
      inp.value = "";
      $("pw-err").textContent = typeof wrong === "string" ? wrong : (wrong ? "Wrong password, try again." : "");
      d.onclose = () => { const v = inp.value; inp.value = ""; resolve(d.returnValue === "ok" && v ? v : null); };
      d.returnValue = "";
      d.showModal();
      inp.focus();
    });
  }
  // POST something that may need the admin password; handles the prompt and retry.
  async function adminPost(url, body) {
    let wrong = false;
    for (let attempt = 0; attempt < 4; attempt++) {
      const r = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: body instanceof FormData ? {} : { "Content-Type": "application/json" },
        body: body instanceof FormData ? body : JSON.stringify(body || {}),
      });
      if (r.status === 401) { location.replace("/login"); return null; }
      const data = await r.json().catch(() => ({}));
      if (r.status === 428 && data.need_password) {
        if (data.upload) { url = "/api/packages/install-upload"; body = { upload: data.upload }; }
        const pw = await askPassword(wrong || data.wrong);
        if (!pw) return null;
        wrong = false;
        try { await api("POST", "/api/admin/password", { password: pw }); }
        catch (e) {
          if (e.message === "Wrong password") wrong = true;
          else if (e.message.startsWith("Too many")) wrong = e.message;
          else { toast(e.message, true); return null; }
        }
        continue;
      }
      if (!r.ok) throw new Error(data.error || ("Error " + r.status));
      return data;
    }
    throw new Error("Couldn't get admin access");
  }
  function followJob(jobId, title) {
    return new Promise((resolve) => {
      const d = $("job-dlg"), log = $("job-log"), st = $("job-state");
      $("job-title").textContent = title;
      log.textContent = ""; st.textContent = "Working…"; st.className = "small";
      d.showModal();
      let misses = 0;
      const poll = async () => {
        if (!d.open) { resolve(false); return; }     // closed: stop polling
        let j;
        try { j = await api("GET", "/api/jobs/" + jobId); misses = 0; }
        catch (e) {
          if (++misses > 5) { st.textContent = "Lost track of this job"; st.className = "small bad"; resolve(false); return; }
          setTimeout(poll, 2000); return;
        }
        log.textContent = j.log; log.scrollTop = log.scrollHeight;
        if (j.state === "running") { setTimeout(poll, 800); return; }
        st.textContent = j.state === "done" ? "Done" : "Failed: see the log above";
        st.className = "small " + (j.state === "done" ? "ok" : "bad");
        resolve(j.state === "done");
      };
      poll();
    });
  }
  async function runAdmin(url, body, title) {
    try {
      const r = await adminPost(url, body);
      if (r && r.job) return await followJob(r.job, title);
    } catch (e) { toast(e.message, true); }
    return false;
  }

  // ---------------------------------------------------------------- tabs
  const tabs = {};
  let current = "overview";
  function show(name) {
    current = name;
    document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
    Object.entries(tabs).forEach(([k, t]) => (k === name ? t.enter && t.enter() : t.leave && t.leave()));
    history.replaceState(null, "", "#" + name);
  }
  document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => show(b.dataset.tab)));
  $("logout").addEventListener("click", async () => {
    try { await api("POST", "/api/logout"); } catch (e) { /* ignore */ }
    location.replace("/login");
  });

  // ---------------------------------------------------------------- overview
  tabs.overview = (function () {
    let timer = null;
    const hist = [];
    const canvas = $("cpu-chart");

    function setBar(id, pct) {
      const b = $(id);
      b.style.width = Math.max(0, Math.min(100, pct)) + "%";
      b.className = pct > 90 ? "crit" : pct > 75 ? "warn" : "";
    }
    function drawChart() {
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      const c = canvas.getContext("2d");
      c.scale(dpr, dpr);
      c.clearRect(0, 0, w, h);
      c.strokeStyle = "#1f463e"; c.lineWidth = 1;
      [0.25, 0.5, 0.75].forEach(f => { c.beginPath(); c.moveTo(0, h * f); c.lineTo(w, h * f); c.stroke(); });
      if (hist.length < 2) return;
      const step = w / 59;
      c.beginPath();
      hist.forEach((v, i) => { const x = w - (hist.length - 1 - i) * step, y = h - v / 100 * h; i ? c.lineTo(x, y) : c.moveTo(x, y); });
      c.strokeStyle = "#e8b54d"; c.lineWidth = 2; c.lineJoin = "round"; c.stroke();
      c.lineTo(w, h); c.lineTo(w - (hist.length - 1) * step, h); c.closePath();
      c.fillStyle = "rgba(232,181,77,.13)"; c.fill();
    }
    async function tick() {
      let s;
      try { s = await api("GET", "/api/stats"); } catch (e) { return; }
      $("hostname").textContent = s.hostname; $("host-big").textContent = s.hostname;
      $("cpu-total").textContent = s.cpu.total.toFixed(0) + "%";
      hist.push(s.cpu.total); if (hist.length > 60) hist.shift();
      drawChart();
      const cores = $("cpu-cores");
      if (cores.children.length !== s.cpu.cores.length) {
        cores.replaceChildren(...s.cpu.cores.map((_, i) => {
          const d = el("div", "core"); d.append(el("span", "", "Core " + i)); const b = el("div", "bar"); b.append(el("span")); d.append(b); return d;
        }));
      }
      s.cpu.cores.forEach((v, i) => {
        const c = cores.children[i];
        c.firstChild.textContent = "Core " + i + ": " + v.toFixed(0) + "%";
        const bar = c.querySelector(".bar span"); bar.style.width = v + "%";
      });
      const m = s.memory, mp = m.total ? m.used / m.total * 100 : 0;
      $("mem-pct").textContent = mp.toFixed(0) + "%"; setBar("mem-bar", mp);
      $("mem-text").textContent = fmtBytes(m.used) + " of " + fmtBytes(m.total) + (m.swap_total ? " · swap " + fmtBytes(m.swap_used) : "");
      const dk = s.disk, dp = dk.total ? dk.used / dk.total * 100 : 0;
      $("disk-pct").textContent = dp.toFixed(0) + "%"; setBar("disk-bar", dp);
      $("disk-text").textContent = fmtBytes(dk.used) + " of " + fmtBytes(dk.total);
      if (s.battery) {
        $("bat").textContent = s.battery.percent + "%";
        $("bat-bar").style.width = s.battery.percent + "%";
        $("bat-bar").className = s.battery.percent < 15 ? "crit" : s.battery.percent < 30 ? "warn" : "";
        $("bat-text").textContent = s.battery.status || "";
      } else { $("bat").textContent = "–"; $("bat-text").textContent = "No battery info"; }
      $("temp").textContent = s.temperature !== null ? s.temperature.toFixed(1) + " °C" : "–";
      $("uptime").textContent = "Running for " + fmtUptime(s.uptime);
      $("load").textContent = s.load.map(x => x.toFixed(2)).join("  ");
      const net = $("net");
      net.replaceChildren(...s.network.map(n => {
        const tr = el("tr");
        tr.append(el("td", "", n.name), el("td", "num", "↓ " + fmtBytes(n.rx_rate) + "/s"),
          el("td", "num", "↑ " + fmtBytes(n.tx_rate) + "/s"),
          el("td", "num muted", fmtBytes(n.rx) + " / " + fmtBytes(n.tx)));
        return tr;
      }));
      $("procs").tBodies[0].replaceChildren(...s.processes.map(p => {
        const tr = el("tr");
        tr.append(el("td", "", p.name), el("td", "muted", String(p.pid)),
          el("td", "num", p.cpu.toFixed(1) + "%"), el("td", "num", fmtBytes(p.rss)));
        return tr;
      }));
    }
    return {
      enter() { tick(); clearInterval(timer); timer = setInterval(() => { if (!document.hidden) tick(); }, 2000); },
      leave() { clearInterval(timer); timer = null; },
    };
  })();

  // ---------------------------------------------------------------- screen
  tabs.screen = (function () {
    const canvas = $("screen"), ctx = canvas.getContext("2d"), dev = canvas.parentElement;
    let ws = null, retry = 0, wanted = false, infoTimer = null;
    const down = new Set();

    function connect() {
      if (!wanted || ws) return;
      ws = new WebSocket(wsUrl("/ws/screen"));
      ws.binaryType = "blob";
      ws.onopen = () => { retry = 0; };
      ws.onmessage = async (ev) => {
        if (typeof ev.data === "string") {
          try { const m = JSON.parse(ev.data); if (m.type === "error") toast(m.message, true); } catch (e) { /* */ }
          return;
        }
        try {
          const bmp = await createImageBitmap(ev.data);
          if (canvas.width !== bmp.width || canvas.height !== bmp.height) { canvas.width = bmp.width; canvas.height = bmp.height; }
          ctx.drawImage(bmp, 0, 0);
          bmp.close && bmp.close();
        } catch (e) { /* bad frame */ }
      };
      ws.onclose = (ev) => {
        ws = null;
        if (ev.code === 4401 || ev.code === 1008) { location.replace("/login"); return; }
        if (wanted) setTimeout(connect, Math.min(5000, 500 * 2 ** retry++));
      };
    }
    function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
    function key(code, isDown) {
      if (isDown) down.add(code); else down.delete(code);
      send({ type: "key", code, down: isDown });
    }
    function releaseAll() { down.forEach(c => send({ type: "key", code: c, down: false })); down.clear(); }

    canvas.addEventListener("focus", () => dev.classList.add("focused"));
    canvas.addEventListener("blur", () => { dev.classList.remove("focused"); releaseAll(); });
    canvas.addEventListener("mousedown", () => canvas.focus());
    canvas.addEventListener("keydown", (e) => {
      if (e.metaKey) return;               // leave browser shortcuts alone
      e.preventDefault();
      if (!e.repeat) key(e.code, true);
      else { key(e.code, false); key(e.code, true); }
    });
    canvas.addEventListener("keyup", (e) => { if (e.metaKey) return; e.preventDefault(); key(e.code, false); });
    $("soft-keys").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-key]");
      if (!b) return;
      key(b.dataset.key, true); setTimeout(() => key(b.dataset.key, false), 60);
    });

    // screenshot + recording happen in the browser, from frames it already has
    function bigCanvas() {
      const c = document.createElement("canvas");
      c.width = canvas.width * 3; c.height = canvas.height * 3;
      const g = c.getContext("2d"); g.imageSmoothingEnabled = false;
      g.drawImage(canvas, 0, 0, c.width, c.height);
      return c;
    }
    function saveBlob(blob, name) {
      const a = el("a"); a.href = URL.createObjectURL(blob); a.download = name;
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    }
    const stamp = () => new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
    $("shot").addEventListener("click", () => bigCanvas().toBlob(b => saveBlob(b, "cardputer-" + stamp() + ".png"), "image/png"));
    let rec = null, recTimer = null;
    $("rec").addEventListener("click", () => {
      if (rec) { rec.stop(); return; }
      const out = document.createElement("canvas");
      out.width = canvas.width * 3; out.height = canvas.height * 3;
      const g = out.getContext("2d"); g.imageSmoothingEnabled = false;
      let raf;
      const copy = () => { g.drawImage(canvas, 0, 0, out.width, out.height); raf = requestAnimationFrame(copy); };
      copy();
      const types = ["video/webm;codecs=vp9", "video/webm", "video/mp4"];
      const type = types.find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t));
      if (!type) { cancelAnimationFrame(raf); toast("This browser can't record video", true); return; }
      const chunks = [];
      rec = new MediaRecorder(out.captureStream(20), { mimeType: type, videoBitsPerSecond: 2500000 });
      rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      rec.onstop = () => {
        cancelAnimationFrame(raf); clearInterval(recTimer);
        $("rec").textContent = "Record"; $("rec").classList.remove("recording"); $("rec-time").textContent = "";
        saveBlob(new Blob(chunks, { type }), "cardputer-" + stamp() + (type.includes("mp4") ? ".mp4" : ".webm"));
        rec = null;
      };
      rec.start(1000);
      const t0 = Date.now();
      $("rec").textContent = "Stop recording"; $("rec").classList.add("recording");
      recTimer = setInterval(() => { const s = Math.floor((Date.now() - t0) / 1000); $("rec-time").textContent = "● " + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }, 500);
    });
    $("paste-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const t = $("paste-text").value;
      if (!t) return;
      if (!ws || ws.readyState !== 1) { toast("Not connected to the screen", true); return; }
      send({ type: "text", text: t });
      toast("Typing " + t.length + " characters on the Cardputer…");
      $("paste-text").value = "";
    });

    // apps
    let appsCache = [];
    async function loadApps() {
      let data;
      try { data = await api("GET", "/api/apps"); } catch (e) { toast(e.message, true); return; }
      appsCache = data.apps;
      const box = $("apps");
      box.replaceChildren(...data.apps.map(a => {
        const b = el("button", "app" + (data.running === a.id ? " running" : ""));
        b.title = a.comment || a.name;
        if (a.icon) { const i = el("img"); i.src = "/api/apps/" + encodeURIComponent(a.id) + "/icon"; i.alt = ""; i.loading = "lazy"; b.append(i); }
        else b.append(el("span", "ph", a.name.slice(0, 1).toUpperCase()));
        b.append(el("span", "", a.name));
        b.addEventListener("click", () => startApp(a));
        return b;
      }));
      showRunning(data.running && appsCache.find(a => a.id === data.running));
    }
    function showRunning(app) {
      const r = $("running");
      r.classList.toggle("hidden", !app);
      if (app) r.firstChild.textContent = "Running: " + app.name;
      document.querySelectorAll(".app").forEach((b, i) => b.classList.toggle("running", !!app && appsCache[i] && appsCache[i].id === app.id));
    }
    async function startApp(a) {
      try {
        await api("POST", "/api/apps/" + encodeURIComponent(a.id) + "/start");
        showRunning(a); toast(a.name + " started on the Cardputer");
        canvas.focus();
      } catch (e) { toast(e.message, true); }
    }
    $("app-stop").addEventListener("click", async () => {
      try { await api("POST", "/api/apps/stop"); showRunning(null); } catch (e) { toast(e.message, true); }
    });
    $("apps-refresh").addEventListener("click", loadApps);
    async function pollInfo() {
      try {
        const i = await api("GET", "/api/info");
        const note = $("kb-note");
        note.classList.toggle("hidden", i.keyboard);
        if (!i.keyboard) note.textContent = i.keyboard_error || "Remote typing isn't available.";
        const app = i.running && (appsCache.find(a => a.id === i.running.id) || i.running);
        showRunning(app || null);
      } catch (e) { /* */ }
    }

    return {
      enter() { wanted = true; connect(); loadApps(); pollInfo(); clearInterval(infoTimer); infoTimer = setInterval(pollInfo, 3000); },
      leave() { wanted = false; releaseAll(); clearInterval(infoTimer); if (ws) ws.close(); },
    };
  })();

  // ---------------------------------------------------------------- shell
  tabs.shell = (function () {
    let term = null, fit = null, ws = null;
    const enc = new TextEncoder();
    function status(t) { $("shell-status").textContent = t; }
    function connect() {
      if (ws) ws.close();
      ws = new WebSocket(wsUrl("/ws/shell?cols=" + term.cols + "&rows=" + term.rows));
      ws.binaryType = "arraybuffer";
      ws.onopen = () => { status("Connected"); term.focus(); };
      ws.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          try { const m = JSON.parse(ev.data); if (m.type === "error") term.write("\r\n" + m.message + "\r\n"); } catch (e) { /* */ }
          return;
        }
        term.write(new Uint8Array(ev.data));
      };
      ws.onclose = (ev) => {
        if (ev.code === 4401) { location.replace("/login"); return; }
        status(ev.code === 4429 ? "Too many shells open" : "Disconnected");
        term.write("\r\n\x1b[90m[session closed: press Reconnect]\x1b[0m\r\n");
      };
    }
    function init() {
      term = new window.Terminal({
        cursorBlink: true, fontSize: 13, scrollback: 5000, allowProposedApi: false,
        fontFamily: '"IBM Plex Mono", "DejaVu Sans Mono", ui-monospace, Menlo, monospace',
        theme: { background: "#0b201c", foreground: "#eef3ec", cursor: "#e8b54d", cursorAccent: "#0b201c", selectionBackground: "#2f5e53" },
      });
      fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open($("terminal"));
      fit.fit();
      // re-measure once the bundled font has loaded, so the grid fits it exactly
      document.fonts.load('13px "IBM Plex Mono"').then(() => { term.options.fontFamily = term.options.fontFamily; fit.fit(); }).catch(() => {});
      term.onData((d) => { if (ws && ws.readyState === 1) ws.send(enc.encode(d)); });
      term.onResize(({ cols, rows }) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols, rows })); });
      window.addEventListener("resize", () => { if (current === "shell") fit.fit(); });
      connect();
    }
    $("shell-reconnect").addEventListener("click", () => { if (term) { term.reset(); connect(); } });
    return {
      enter() { if (!term) init(); else { fit.fit(); term.focus(); } },
    };
  })();

  // ---------------------------------------------------------------- files
  tabs.files = (function () {
    let cwd = null, home = null, parent = null, loaded = false, pvPath = null;
    async function open(path) {
      let d;
      try { d = await api("GET", "/api/files/list?path=" + encodeURIComponent(path || "")); }
      catch (e) { toast(e.message, true); return; }
      cwd = d.path; home = d.home; parent = d.parent; loaded = true;
      $("f-up").disabled = !parent;
      const crumbs = $("crumbs");
      const parts = cwd.split("/").filter(Boolean);
      const items = [];
      const root = el("button", "", "/"); root.addEventListener("click", () => open("/")); items.push(root);
      parts.forEach((p, i) => {
        const b = el("button", "", p);
        const target = "/" + parts.slice(0, i + 1).join("/");
        b.addEventListener("click", () => open(target));
        items.push(b);
        if (i < parts.length - 1) items.push(el("span", "", "/"));
      });
      crumbs.replaceChildren(...items);
      $("f-rows").replaceChildren(...d.entries.map(row));
      $("f-empty").classList.toggle("hidden", d.entries.length > 0);
    }
    const join = (dir, name) => (dir === "/" ? "" : dir) + "/" + name;
    function row(e) {
      const tr = el("tr", "row");
      const name = el("td"); const wrap = el("div", "name");
      wrap.append(el("span", "ico", e.dir ? "▸" : e.type === "link" ? "↪" : "·"), el("span", "", e.name));
      name.append(wrap);
      const acts = el("td", "acts");
      const path = join(cwd, e.name);
      if (!e.dir) {
        const dl = el("button", "", "Download");
        dl.addEventListener("click", (ev) => { ev.stopPropagation(); download(path); });
        acts.append(dl);
      }
      const rn = el("button", "", "Rename");
      rn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const nn = await dialog("Rename “" + e.name + "” to:", true, e.name, "Rename");
        if (!nn || nn === e.name) return;
        try { await api("POST", "/api/files/rename", { path, name: nn }); open(cwd); } catch (err) { toast(err.message, true); }
      });
      const del = el("button", "", "Delete");
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const ok = await dialog("Delete “" + e.name + "”" + (e.dir ? " and everything in it" : "") + "? This can't be undone.", false, "", "Delete", true);
        if (!ok) return;
        try { await api("POST", "/api/files/delete", { path }); toast("Deleted"); open(cwd); } catch (err) { toast(err.message, true); }
      });
      acts.append(rn, del);
      tr.append(name, el("td", "num muted", e.dir ? "" : fmtBytes(e.size)), el("td", "muted", fmtDate(e.mtime)), acts);
      tr.addEventListener("click", () => (e.dir ? open(path) : preview(path, e)));
      return tr;
    }
    function download(path) {
      const a = el("a");
      a.href = "/api/files/download?path=" + encodeURIComponent(path);
      a.rel = "noopener";
      document.body.append(a); a.click(); a.remove();
    }
    async function preview(path, e) {
      pvPath = path;
      $("preview").classList.remove("hidden");
      $("pv-name").textContent = e.name;
      const img = $("pv-img"), txt = $("pv-text"), save = $("pv-save"), note = $("pv-note");
      img.classList.add("hidden"); txt.classList.add("hidden"); save.classList.add("hidden"); note.textContent = "";
      if (img.src) { URL.revokeObjectURL(img.src); img.removeAttribute("src"); }
      if (/\.(png|jpe?g|gif|webp|bmp)$/i.test(e.name) && e.size < 20 * 1024 * 1024) {
        try {
          const r = await fetch("/api/files/download?path=" + encodeURIComponent(path), { credentials: "same-origin" });
          if (!r.ok) throw new Error("Couldn't load image");
          img.src = URL.createObjectURL(await r.blob());
          img.classList.remove("hidden");
        } catch (err) { note.textContent = err.message; }
        return;
      }
      if (e.size > 512 * 1024) { note.textContent = "Too large to preview. Use Download."; return; }
      try {
        const d = await api("GET", "/api/files/preview?path=" + encodeURIComponent(path));
        txt.value = d.text; txt.classList.remove("hidden");
        save.classList.remove("hidden");
        if (d.truncated) { note.textContent = "Showing the first 512 KB."; save.classList.add("hidden"); }
      } catch (err) { note.textContent = err.message === "Binary file" ? "Binary file: no preview. Use Download." : err.message; }
    }
    $("pv-close").addEventListener("click", () => { $("preview").classList.add("hidden"); pvPath = null; });
    $("pv-download").addEventListener("click", () => pvPath && download(pvPath));
    $("pv-save").addEventListener("click", async () => {
      try { await api("POST", "/api/files/save", { path: pvPath, text: $("pv-text").value }); toast("Saved"); open(cwd); }
      catch (e) { toast(e.message, true); }
    });
    $("f-up").addEventListener("click", () => parent && open(parent));
    $("f-home").addEventListener("click", () => open(home));
    $("f-mkdir").addEventListener("click", async () => {
      const n = await dialog("New folder name:", true, "", "Create");
      if (!n) return;
      try { await api("POST", "/api/files/mkdir", { path: cwd, name: n }); open(cwd); } catch (e) { toast(e.message, true); }
    });

    function upload(fileList) {
      const filesArr = Array.from(fileList);
      if (!filesArr.length) return;
      const prog = $("upload-progress");
      let i = 0;
      const next = () => {
        if (i >= filesArr.length) { prog.classList.add("hidden"); toast("Uploaded " + filesArr.length + " file" + (filesArr.length > 1 ? "s" : "")); open(cwd); return; }
        const f = filesArr[i++];
        const fd = new FormData(); fd.append("file", f, f.name);
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/files/upload?dir=" + encodeURIComponent(cwd));
        xhr.upload.onprogress = (ev) => {
          prog.classList.remove("hidden");
          prog.textContent = "Uploading " + f.name + " · " + (ev.lengthComputable ? Math.round(ev.loaded / ev.total * 100) + "%" : fmtBytes(ev.loaded));
        };
        xhr.onload = () => {
          if (xhr.status === 401) { location.replace("/login"); return; }
          if (xhr.status !== 200) {
            let m = "Upload failed"; try { m = JSON.parse(xhr.responseText).error || m; } catch (e) { /* */ }
            toast(f.name + ": " + m, true);
          }
          next();
        };
        xhr.onerror = () => { toast("Upload failed: connection lost", true); prog.classList.add("hidden"); };
        xhr.send(fd);
      };
      next();
    }
    $("f-upload").addEventListener("change", (e) => { upload(e.target.files); e.target.value = ""; });
    const dz = $("dropzone");
    ["dragenter", "dragover"].forEach(t => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach(t => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => upload(e.dataTransfer.files));

    return { enter() { if (!loaded) open(""); } };
  })();

  // ---------------------------------------------------------------- apps (install/remove)
  tabs.apps = (function () {
    let store = [], loaded = false;
    function render() {
      const q = $("store-search").value.trim().toLowerCase();
      const list = store.filter(a => !q || (a.title + " " + a.summary + " " + a.code + " " + a.categories.join(" ")).toLowerCase().includes(q));
      $("store-count").textContent = list.length + " apps";
      $("store-list").replaceChildren(...list.map(a => {
        const d = el("div", "store-item");
        const h = el("h3"); h.append(el("span", "", a.title), el("span", "code", a.code)); d.append(h);
        d.append(el("p", "", a.summary));
        const row = el("div", "row");
        const meta = el("span", "meta");
        meta.append(el("span", "", a.author ? "by " + a.author : ""), el("span", "", "Version " + a.version + ", " + fmtBytes(a.size)));
        row.append(meta);
        if (a.installed === a.version) row.append(el("span", "tag", "Installed"));
        else {
          const b = el("button", "primary small", a.installed ? "Update" : "Install");
          b.addEventListener("click", async () => {
            const ok = await runAdmin("/api/store/" + encodeURIComponent(a.code) + "/install", {}, (a.installed ? "Update " : "Install ") + a.title);
            if (ok) { loadStore(); loadInstalled(); }
          });
          row.append(b);
        }
        d.append(row);
        return d;
      }));
    }
    async function loadStore() {
      try { store = (await api("GET", "/api/store")).apps; render(); }
      catch (e) { $("store-list").replaceChildren(el("p", "muted small", e.message)); }
    }
    async function loadInstalled() {
      let d;
      try { d = await api("GET", "/api/packages/installed"); } catch (e) { return; }
      $("installed").tBodies[0].replaceChildren(...d.apps.map(a => {
        const tr = el("tr");
        const act = el("td", "td-act"); const b = el("button", "ghost small", "Remove");
        b.addEventListener("click", async () => {
          if (!await dialog("Remove " + a.name + " (" + a.package + ")?", false, "", "Remove", true)) return;
          if (await runAdmin("/api/packages/" + encodeURIComponent(a.package) + "/remove", {}, "Remove " + a.name)) { loadInstalled(); loadStore(); }
        });
        act.append(b);
        tr.append(el("td", "", a.name), el("td", "sig", a.package), act);
        return tr;
      }));
    }
    $("store-search").addEventListener("input", render);
    $("inst-refresh").addEventListener("click", loadInstalled);
    $("deb-upload").addEventListener("change", async (e) => {
      const f = e.target.files[0]; e.target.value = "";
      if (!f) return;
      const fd = new FormData(); fd.append("file", f, f.name);
      toast("Uploading " + f.name + "…");
      if (await runAdmin("/api/packages/upload", fd, "Install " + f.name)) { loadInstalled(); loadStore(); }
    });
    return { enter() { if (!loaded) { loaded = true; loadStore(); loadInstalled(); } } };
  })();

  // ---------------------------------------------------------------- network
  tabs.network = (function () {
    let loaded = false;
    async function loadConns() {
      let d;
      try { d = await api("GET", "/api/network"); } catch (e) { toast(e.message, true); return; }
      $("conns").tBodies[0].replaceChildren(...d.connections.map(c => {
        const tr = el("tr");
        const act = el("td", "td-act");
        const b = el("button", "ghost small", c.active ? "Disconnect" : "Connect");
        b.addEventListener("click", async () => {
          if (c.active && c.type === "wifi" && !await dialog("Disconnect " + c.name + "? This browser will probably lose contact with the Cardputer.", false, "", "Disconnect", true)) return;
          b.disabled = true;
          try { await api("POST", "/api/network/" + (c.active ? "down" : "up"), { uuid: c.uuid }); toast(c.name + (c.active ? " disconnected" : " connected")); }
          catch (e) { toast(e.message, true); }
          loadConns();
        });
        act.append(b);
        if (c.type === "wifi" && !c.active) {
          const f = el("button", "ghost small", "Forget");
          f.addEventListener("click", async () => {
            if (!await dialog("Forget the Wi-Fi network " + c.name + "?", false, "", "Forget", true)) return;
            if (await runAdmin("/api/network/forget", { uuid: c.uuid }, "Forget " + c.name)) loadConns();
          });
          act.append(f);
        }
        const name = el("td"); name.append(el("span", "dot " + (c.active ? "ok" : "")), document.createTextNode(" " + c.name));
        tr.append(name, el("td", "muted", c.type === "wifi" ? "Wi-Fi" : c.type === "vpn" ? "VPN" : "Ethernet"),
          el("td", "muted", c.device), act);
        return tr;
      }));
    }
    async function scan(rescan) {
      $("wifi").tBodies[0].replaceChildren(el("tr")); $("wifi").tBodies[0].firstChild.append(el("td", "muted small", "Scanning…"));
      let d;
      try { d = await api("GET", "/api/network/scan" + (rescan ? "?rescan=1" : "")); } catch (e) { toast(e.message, true); return; }
      $("wifi").tBodies[0].replaceChildren(...d.networks.map(n => {
        const tr = el("tr");
        const act = el("td", "td-act");
        if (!n.in_use) {
          const b = el("button", "ghost small", "Join");
          b.addEventListener("click", async () => {
            let psk = "";
            if (n.secure) { psk = await dialog("Wi-Fi password for " + n.ssid + ":", true, "", "Join", false, true); if (!psk) return; }
            if (await runAdmin("/api/network/join", { ssid: n.ssid, password: psk }, "Join " + n.ssid)) loadConns();
          });
          act.append(b);
        } else act.append(el("span", "tag", "Connected"));
        tr.append(el("td", "", n.ssid), el("td", "num muted", n.signal + "%"), el("td", "muted", n.secure ? "Secured" : "Open"), act);
        return tr;
      }));
    }
    $("net-refresh").addEventListener("click", loadConns);
    $("wifi-scan").addEventListener("click", () => scan(true));
    return { enter() { if (!loaded) { loaded = true; loadConns(); scan(false); } } };
  })();

  // ---------------------------------------------------------------- start
  const start = (location.hash || "#overview").slice(1);
  show(tabs[start] ? start : "overview");
})();
