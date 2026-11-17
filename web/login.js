"use strict";
(function () {
  const form = document.getElementById("pair-form");
  const input = document.getElementById("code");
  const err = document.getElementById("pair-error");

  input.addEventListener("input", () => {
    const digits = input.value.replace(/\D/g, "").slice(0, 6);
    input.value = digits.length > 3 ? digits.slice(0, 3) + " " + digits.slice(3) : digits;
    err.textContent = "";
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const code = input.value.replace(/\D/g, "");
    if (code.length !== 6) { err.textContent = "The code has 6 digits."; return; }
    const btn = form.querySelector("button");
    btn.disabled = true;
    try {
      const r = await fetch("/api/pair", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }), credentials: "same-origin",
      });
      if (r.ok) { location.replace("/"); return; }
      const body = await r.json().catch(() => ({}));
      err.textContent = body.error || "Couldn't pair.";
      input.value = "";
      input.focus();
    } catch (e) {
      err.textContent = "Can't reach the Cardputer.";
    } finally {
      btn.disabled = false;
    }
  });
})();
