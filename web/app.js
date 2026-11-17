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

  function dialog(message, withInput, value, okLabel, danger) {
    return new Promise((resolve) => {
      const d = $("dlg"), inp = $("dlg-input"), ok = $("dlg-ok");
      $("dlg-msg").textContent = message;
      inp.classList.toggle("hidden", !withInput);
      inp.value = value || "";
      ok.textContent = okLabel || "OK";
      ok.className = danger ? "danger" : "primary";
      d.onclose = () => resolve(d.returnValue === "ok" ? (withInput ? inp.value : true) : null);
      d.showModal();
      if (withInput) { inp.focus(); inp.select(); }
    });
  }

  function wsUrl(path) { return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path; }

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
      c.strokeStyle = "#30363d"; c.lineWidth = 1;
      [0.25, 0.5, 0.75].forEach(f => { c.beginPath(); c.moveTo(0, h * f); c.lineTo(w, h * f); c.stroke(); });
      if (hist.length < 2) return;
      const step = w / 59;
      c.beginPath();
      hist.forEach((v, i) => { const x = w - (hist.length - 1 - i) * step, y = h - v / 100 * h; i ? c.lineTo(x, y) : c.moveTo(x, y); });
      c.strokeStyle = "#58a6ff"; c.lineWidth = 2; c.stroke();
      c.lineTo(w, h); c.lineTo(w - (hist.length - 1) * step, h); c.closePath();
      c.fillStyle = "rgba(88,166,255,.12)"; c.fill();
    }
    async function tick() {
      let s;
      try { s = await api("GET", "/api/stats"); } catch (e) { return; }
      $("hostname").textContent = s.hostname;
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
        c.firstChild.textContent = "Core " + i + " · " + v.toFixed(0) + "%";
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
      $("uptime").textContent = "Up " + fmtUptime(s.uptime);
      $("load").textContent = "Load " + s.load.map(x => x.toFixed(2)).join("  ");
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
        fontFamily: '"JetBrains Mono", "DejaVu Sans Mono", ui-monospace, Menlo, monospace',
        theme: { background: "#000000", foreground: "#e6edf3", cursor: "#58a6ff", selectionBackground: "#264f78" },
      });
      fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open($("terminal"));
      fit.fit();
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

  // ---------------------------------------------------------------- start
  const start = (location.hash || "#overview").slice(1);
  show(tabs[start] ? start : "overview");
})();
