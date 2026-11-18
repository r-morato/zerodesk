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
      ws.onopen = () => { retry = 0; setLed(true); };
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
        ws = null; setLed(false);
        if (ev.code === 4401 || ev.code === 1008) { location.replace("/login"); return; }
        if (wanted) setTimeout(connect, Math.min(5000, 500 * 2 ** retry++));
      };
    }
    function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
    function setLed(on) { const l = $("dev-led"); l.classList.toggle("on", on); l.title = on ? "Live" : "Not connected"; }
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
    // ---- the on-screen keyboard, laid out like the Cardputer's: 4 rows of 11,
    // with Esc and Tab above. Gold labels are the fn layer, green ones sym.
    // K(code, label, shifted, sym character, fn code, fn label, css class)
    const K = (code, label, o) => Object.assign({ code, label, shifted: label.length === 1 ? label.toUpperCase() : label }, o || {});
    const SHIFTED_DIGITS = "!@#$%^&*()";
    const ROWS = [
      [K("Escape", "esc", { cls: "side side-esc" }), K("Tab", "tab", { cls: "side side-tab" })],    // beside the screen
      [...Array.from("1234567890").map((d, i) => K("Digit" + d, d, { shifted: SHIFTED_DIGITS[i], fn: "F" + (i + 1), fnLabel: "F" + (i + 1) })),
       K("Backspace", "del")],
      [K("mod:sym", "sym"), ...["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"].map(c => K("Key" + c.toUpperCase(), c))],
      [K("mod:ShiftLeft", "shift"), ...["a", "s", "d", "f", "g", "h", "j", "k", "l"].map(c => K("Key" + c.toUpperCase(), c)), K("Enter", "ok")],
      [K("mod:fn", "fn"), K("mod:ControlLeft", "ctrl"), K("mod:AltLeft", "alt"),
       ...["z", "x", "c", "v", "b", "n", "m"].map(c => K("Key" + c.toUpperCase(), c)), K("Space", "space")],
    ];
    const at = {}; ROWS.flat().forEach(k => { at[k.code] = k; });
    // fn layer: arrows as an inverted T on F / Z X C, F11 F12, and the navigation keys
    Object.assign(at.KeyF, { fn: "ArrowUp", fnLabel: "↑" });
    Object.assign(at.KeyZ, { fn: "ArrowLeft", fnLabel: "←" });
    Object.assign(at.KeyX, { fn: "ArrowDown", fnLabel: "↓" });
    Object.assign(at.KeyC, { fn: "ArrowRight", fnLabel: "→" });
    Object.assign(at.KeyO, { fn: "F11", fnLabel: "F11" });
    Object.assign(at.KeyP, { fn: "F12", fnLabel: "F12" });
    Object.assign(at.KeyJ, { fn: "Screenshot", fnLabel: "prsc" });
    Object.assign(at.KeyK, { fn: "Home", fnLabel: "home" });
    Object.assign(at.KeyL, { fn: "PageUp", fnLabel: "pgup" });
    Object.assign(at.KeyB, { fn: "Insert", fnLabel: "ins" });
    Object.assign(at.KeyN, { fn: "End", fnLabel: "end" });
    Object.assign(at.KeyM, { fn: "PageDown", fnLabel: "pgdn" });
    Object.assign(at.Backspace, { fn: "Delete", fnLabel: "⌦" });
    // sym layer: the symbols that aren't on the number row
    const SYM = { KeyQ: "~", KeyW: "`", KeyE: "_", KeyR: "-", KeyT: "+", KeyY: "=", KeyU: "[", KeyI: "]",
                  KeyA: ";", KeyS: ":", KeyD: "'", KeyG: '"', KeyH: "<", KeyJ: ">", KeyK: "\\", KeyL: "|",
                  KeyV: ",", KeyB: ".", KeyN: "/", KeyM: "?" };
    Object.entries(SYM).forEach(([c, ch]) => { at[c].sym = ch; });
    // a character -> the key (and whether Shift is needed) on a US layout
    const PLAIN = { "-": "Minus", "=": "Equal", "[": "BracketLeft", "]": "BracketRight", ";": "Semicolon", "'": "Quote",
                    "`": "Backquote", "\\": "Backslash", ",": "Comma", ".": "Period", "/": "Slash" };
    const SHIFTED = { "_": "Minus", "+": "Equal", "{": "BracketLeft", "}": "BracketRight", ":": "Semicolon", '"': "Quote",
                      "~": "Backquote", "|": "Backslash", "<": "Comma", ">": "Period", "?": "Slash" };
    const charKey = (ch) => PLAIN[ch] ? [PLAIN[ch], false] : [SHIFTED[ch], true];

    const kb = $("kb"), device = $("device"), face = $("face"), lcd = $("lcd"), mods = new Set(), keyEls = {};
    ROWS.forEach((row, ri) => row.forEach(k => {
      if (k.code === "gap") { const g = el("span", "key-gap"); g.style.gridColumn = "span " + k.span; kb.append(g); return; }
      const b = el("button", "key" + (k.code.startsWith("mod:") ? " mod" : "") + (k.label.length > 1 ? " word" : "") + (k.cls ? " " + k.cls : ""));
      b.type = "button"; b.tabIndex = -1;
      if (k.fnLabel) b.append(el("span", "fn-label" + (/^[↑↓←→]$/.test(k.fnLabel) ? " arrow" : ""), k.fnLabel));
      if (k.sym) b.append(el("span", "sym-label", k.sym));
      const main = el("span", "main", k.label); b.append(main);
      b.dataset.code = k.code; b._k = k; b._main = main;
      b.setAttribute("aria-label", k.code.replace(/^mod:|Key|Digit/g, ""));
      keyEls[k.code] = b; (ri === 0 ? face : kb).append(b);
    }));
    // Put the screen exactly where it sits on the Cardputer: the key labels the
    // apps draw along its bottom edge (x = 49 ... 273 of 320) land on keys 4 ... 8.
    const MARK_4 = 49, MARK_8 = 273;
    function placeScreen() {
      const k4 = keyEls.Digit4, k8 = keyEls.Digit8, k1 = keyEls.Digit1, k0 = keyEls.Backspace;
      if (!k4.offsetWidth) return;                     // tab hidden: try again when shown
      const fr = face.getBoundingClientRect();
      const mid = (b) => { const r = b.getBoundingClientRect(); return r.left + r.width / 2 - fr.left; };
      const scale = (mid(k8) - mid(k4)) / (MARK_8 - MARK_4);
      const bezel = parseFloat(getComputedStyle(lcd).paddingLeft) || 0;
      const narrow = scale * 320 < 300;                // too small to read: use the full width instead
      face.classList.toggle("narrow", narrow);
      if (narrow) { lcd.style.marginLeft = ""; lcd.style.width = ""; }
      else {
        lcd.style.marginLeft = (mid(k4) - MARK_4 * scale - bezel) + "px";
        lcd.style.width = (320 * scale + 2 * bezel) + "px";
      }
      const kw = k4.getBoundingClientRect().width;
      [[keyEls.Escape, k1], [keyEls.Tab, k0]].forEach(([b, under]) => {
        b.style.width = kw + "px";
        b.style.left = narrow ? "" : (mid(under) - kw / 2) + "px";
      });
    }
    if (window.ResizeObserver) new ResizeObserver(placeScreen).observe(device);
    else window.addEventListener("resize", placeScreen);
    function refreshMods() {
      kb.classList.toggle("fn-on", mods.has("fn"));
      kb.classList.toggle("sym-on", mods.has("sym"));
      kb.querySelectorAll(".key").forEach(b => {
        const k = b._k;
        if (k.code.startsWith("mod:")) b.classList.toggle("on", mods.has(k.code.slice(4)));
        else b._main.textContent = mods.has("ShiftLeft") ? k.shifted : k.label;
      });
    }
    let held = null;      // the key being pressed with the mouse or a finger
    device.addEventListener("pointerdown", (e) => {
      const b = e.target.closest(".key");
      if (!b) return;
      e.preventDefault();
      const k = b._k;
      if (k.code.startsWith("mod:")) {                  // sticky: applies to the next key
        const m = k.code.slice(4);
        if (mods.has(m)) mods.delete(m);
        else { if (m === "fn") mods.delete("sym"); if (m === "sym") mods.delete("fn"); mods.add(m); }
        refreshMods(); return;
      }
      let code = k.code, used = [...mods].filter(m => m !== "fn" && m !== "sym");
      if (mods.has("fn") && k.fn === "Screenshot") {     // fn+J: save a screenshot here
        mods.clear(); refreshMods(); $("shot").click(); return;
      }
      if (mods.has("fn") && k.fn) code = k.fn;
      else if (mods.has("sym") && k.sym) {
        const [c, shift] = charKey(k.sym);
        code = c; used = used.filter(m => m !== "ShiftLeft"); if (shift) used.push("ShiftLeft");
      }
      used.forEach(m => key(m, true));
      key(code, true);
      held = { code, used, b }; b.classList.add("down");
      try { b.setPointerCapture(e.pointerId); } catch (err) { /* */ }
    });
    function letGo() {
      if (!held) return;
      key(held.code, false); held.used.forEach(m => key(m, false));
      held.b.classList.remove("down"); held = null;
      mods.clear(); refreshMods();
    }
    device.addEventListener("pointerup", letGo);
    device.addEventListener("pointercancel", letGo);
    device.addEventListener("lostpointercapture", letGo);
    // light up the on-screen key when typing on a real keyboard
    const physical = { ShiftRight: "mod:ShiftLeft", ControlRight: "mod:ControlLeft", AltRight: "mod:AltLeft",
                       ShiftLeft: "mod:ShiftLeft", ControlLeft: "mod:ControlLeft", AltLeft: "mod:AltLeft", NumpadEnter: "Enter" };
    ROWS.flat().forEach(k => { if (k.fn && !keyEls[k.fn]) physical[k.fn] = k.code; });
    Object.entries(SYM).forEach(([c, ch]) => { const [code] = charKey(ch); if (!keyEls[code] && !physical[code]) physical[code] = c; });
    const lit = (code, on) => { const b = keyEls[code] || keyEls[physical[code]]; if (b) b.classList.toggle("down", on); };
    canvas.addEventListener("keydown", (e) => lit(e.code, true));
    canvas.addEventListener("keyup", (e) => lit(e.code, false));
    canvas.addEventListener("blur", () => kb.querySelectorAll(".key.down").forEach(b => b.classList.remove("down")));

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
    let runningApp = null;
    function showRunning(app) {
      runningApp = app || null;
      const r = $("running");
      r.classList.toggle("hidden", !app);
      if (app) r.firstChild.textContent = "Running: " + app.name;
      document.querySelectorAll(".app").forEach((b, i) => b.classList.toggle("running", !!app && appsCache[i] && appsCache[i].id === app.id));
    }
    async function startApp(a) {
      try {
        const before = runningApp && runningApp.id !== a.id ? runningApp.name : "";
        await api("POST", "/api/apps/" + encodeURIComponent(a.id) + "/start");
        showRunning(a);
        toast(before ? "Closed " + before + " and started " + a.name : a.name + " is running on the Cardputer");
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
        const missing = (i.virtual_only || []).filter(c => !/Right$|^Meta|^Context/.test(c));
        let msg = "";
        if (!i.keyboard) msg = i.keyboard_error || "Remote typing isn't available.";
        else if (i.typing === "virtual") msg = "Typing reaches store apps but not the built-in apps (the Cardputer's keyboard couldn't be opened).";
        else if (missing.length) msg = "Built-in apps don't receive these keys from the browser (store apps do): " +
          missing.map(c => c.replace(/^Key|^Digit|Left$/g, "").replace(/^Arrow/, "")).join(", ") + ".";
        note.classList.toggle("hidden", !msg);
        note.textContent = msg;
        const app = i.running && (appsCache.find(a => a.id === i.running.id) || i.running);
        showRunning(app || null);
      } catch (e) { /* */ }
    }

    return {
      enter() { wanted = true; requestAnimationFrame(placeScreen); connect(); loadApps(); pollInfo(); clearInterval(infoTimer); infoTimer = setInterval(pollInfo, 3000); },
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

  // ---------------------------------------------------------------- apps (install)
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
        tr.append(el("td", "", a.name), el("td", "sig", a.package));
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


  // ---------------------------------------------------------------- start
  const start = (location.hash || "#overview").slice(1);
  show(tabs[start] ? start : "overview");
})();
