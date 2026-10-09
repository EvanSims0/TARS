/* Shared helpers for TARS's local pages: API calls, state glyphs, controls, shortcuts. */
"use strict";

const TARS = (() => {
  const TOKEN = document.querySelector('meta[name="tars-token"]')?.content || "";
  const NS = "http://www.w3.org/2000/svg";

  async function api(path, body) {
    const opts = { headers: { "X-Tars-Token": TOKEN }, cache: "no-store" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.error || `Error ${res.status}`), { status: res.status, data });
    return data;
  }

  function el(tag, attrs = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (k === "class") node.className = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
    return node;
  }

  function svg(markup, cls = "glyph", size = 24) {
    const wrap = document.createElementNS(NS, "svg");
    wrap.setAttribute("viewBox", "0 0 24 24");
    wrap.setAttribute("width", size); wrap.setAttribute("height", size);
    wrap.setAttribute("class", cls); wrap.setAttribute("aria-hidden", "true");
    wrap.innerHTML = markup;
    return wrap;
  }

  // The seven states, each a different shape so they read in greyscale and at 16 px.
  const GLYPHS = {
    idle: '<circle class="core" cx="12" cy="12" r="4" fill="#708090"/>',
    listening: '<circle cx="12" cy="12" r="4.2" fill="none" stroke="#fff" stroke-width="2"/>' +
               '<circle class="ring" cx="12" cy="12" r="9.25" fill="none" stroke="#fff" stroke-width="1.5"/>',
    thinking: '<circle cx="12" cy="12" r="7.5" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="2"/>' +
              '<path class="arc" d="M12 4.5a7.5 7.5 0 0 1 7.5 7.5" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>',
    speaking: '<g><rect class="bar" x="3" y="9" width="2" height="6" rx="1" fill="#fff"/>' +
              '<rect class="bar" x="7" y="6" width="2" height="12" rx="1" fill="#fff"/>' +
              '<rect class="bar" x="11" y="3.5" width="2" height="17" rx="1" fill="#fff"/>' +
              '<rect class="bar" x="15" y="6.5" width="2" height="11" rx="1" fill="#fff"/>' +
              '<rect class="bar" x="19" y="9" width="2" height="6" rx="1" fill="#fff"/></g>',
    confirm: '<circle cx="12" cy="12" r="9.25" fill="none" stroke="#fff" stroke-width="1.5"/>' +
             '<path d="M9.5 9.2a2.6 2.6 0 1 1 3.6 2.4c-.7.3-1.1.9-1.1 1.6v.4" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' +
             '<circle cx="12" cy="17" r="1.1" fill="#fff"/>',
    muted: '<rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="#fff" stroke-width="2"/>' +
           '<path d="M6 11a6 6 0 0 0 12 0M12 17v3.5" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' +
           '<path d="M4 4l16 16" stroke="#fff" stroke-width="2" stroke-linecap="round"/>',
    problem: '<path d="M12 4.2l8.6 15.3H3.4z" fill="none" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>' +
             '<path d="M12 10v4" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="17" r="1.1" fill="#fff"/>',
  };
  const STATE_NAMES = { idle: "Idle", listening: "Listening", thinking: "Thinking", speaking: "Speaking",
                        confirm: "Needs confirmation", muted: "Muted", problem: "Problem" };
  const glyph = (state, size = 24) => svg(GLYPHS[state] || GLYPHS.idle, `glyph ${state}`, size);

  const ICONS = {
    grip: '<g fill="#d3d3d3"><rect x="5" y="3" width="2" height="2" rx="1"/><rect x="9" y="3" width="2" height="2" rx="1"/>' +
          '<rect x="5" y="7" width="2" height="2" rx="1"/><rect x="9" y="7" width="2" height="2" rx="1"/>' +
          '<rect x="5" y="11" width="2" height="2" rx="1"/><rect x="9" y="11" width="2" height="2" rx="1"/></g>',
    mail: '<rect x="3" y="5.5" width="18" height="13" rx="2" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
          '<path d="M3.5 7l8.5 6 8.5-6" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="15" rx="2" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
              '<path d="M3.5 9.5h17M8 3v4M16 3v4" stroke="#d3d3d3" stroke-width="1.6" stroke-linecap="round"/>',
    task: '<rect x="4" y="4" width="16" height="16" rx="3" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
          '<path d="M8.5 12.3l2.4 2.4 4.8-5.2" fill="none" stroke="#d3d3d3" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.5" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
           '<path d="M11 18h2" stroke="#d3d3d3" stroke-width="1.6" stroke-linecap="round"/>',
    memory: '<circle cx="12" cy="12" r="3" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
            '<circle cx="5" cy="6" r="2" fill="none" stroke="#d3d3d3" stroke-width="1.6"/><circle cx="19" cy="7" r="2" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
            '<circle cx="12" cy="20" r="2" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
            '<path d="M6.6 7.3l3 2.6M17.3 8.2l-3 2.3M12 15v3" stroke="#d3d3d3" stroke-width="1.6"/>',
    timer: '<circle cx="12" cy="13" r="7.5" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
           '<path d="M12 9v4l2.5 2M10 2.5h4" stroke="#d3d3d3" stroke-width="1.6" stroke-linecap="round"/>',
    dial: '<circle cx="12" cy="12" r="8" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
          '<path d="M12 12l4-4" stroke="#d3d3d3" stroke-width="1.8" stroke-linecap="round"/>',
    search: '<circle cx="10.5" cy="10.5" r="6" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
            '<path d="M15 15l5 5" stroke="#d3d3d3" stroke-width="1.6" stroke-linecap="round"/>',
    check: '<path d="M6 12.5l4 4 8-9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    spinner: '<circle cx="12" cy="12" r="7.5" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="2"/>' +
             '<path class="arc" d="M12 4.5a7.5 7.5 0 0 1 7.5 7.5" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>',
    lock: '<rect x="5" y="10.5" width="14" height="10" rx="2" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>' +
          '<path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" fill="none" stroke="#d3d3d3" stroke-width="1.6"/>',
    min: '<path d="M7 12h10" stroke="#d3d3d3" stroke-width="1.2"/>',
    max: '<rect x="7" y="7" width="10" height="10" fill="none" stroke="#d3d3d3" stroke-width="1.2"/>',
    close: '<path d="M7 7l10 10M17 7L7 17" stroke="#d3d3d3" stroke-width="1.2"/>',
  };
  const icon = (name, size = 18) => svg(ICONS[name] || "", name === "spinner" ? "glyph thinking" : "icon", size);

  const key = (text) => el("span", { class: "key" }, text);

  function toast(text) {
    let t = document.querySelector(".toast");
    if (!t) { t = el("div", { class: "toast", role: "status" }); document.body.append(t); }
    t.textContent = text; t.classList.add("show");
    clearTimeout(t._timer); t._timer = setTimeout(() => t.classList.remove("show"), 2600);
  }

  // Shortcuts named on buttons work everywhere except while typing.
  const shortcuts = new Map();
  function shortcut(combo, fn) { shortcuts.set(combo.toLowerCase(), fn); }
  document.addEventListener("keydown", (e) => {
    const typing = e.target.matches("input, textarea, select");
    const parts = [];
    if (e.ctrlKey) parts.push("ctrl"); if (e.altKey) parts.push("alt"); if (e.shiftKey && e.key.length > 1) parts.push("shift");
    parts.push(e.key.length === 1 ? e.key.toLowerCase() : e.key.toLowerCase());
    const combo = parts.join("+");
    const fn = shortcuts.get(combo);
    if (!fn) return;
    if (typing && !(e.ctrlKey || e.altKey || e.key === "Escape" || e.key === "Enter")) return;
    e.preventDefault(); fn(e);
  });

  function slider({ id, label, value, onInput, onChange, extra }) {
    const valueEl = el("span", { class: "value" });
    const fill = el("i", { class: "fill" }), thumb = el("i", { class: "thumb" });
    const ticks = el("div", { class: "ticks", "aria-hidden": "true" });
    for (let i = 0; i <= 10; i++) ticks.append(el("i", { class: i % 5 === 0 ? "major" : "", style: `left:calc(${i * 10}% - ${i === 10 ? 1 : 0}px)` }));
    const input = el("input", { type: "range", id, min: 0, max: 100, step: 1, value, "aria-label": label });
    const caption = el("div", { class: "small muted" });
    const paint = (v) => {
      valueEl.textContent = String(v).padStart(3, "0") + "%";
      fill.style.width = v + "%"; thumb.style.left = v + "%";
      if (onInput) caption.textContent = onInput(Number(v)) || "";
    };
    input.addEventListener("input", () => paint(input.value));
    input.addEventListener("change", () => onChange && onChange(Number(input.value), input));
    paint(value);
    const root = el("div", { class: "slider" },
      el("div", { class: "slider-head" }, el("span", { class: "h3" }, label), extra || null, el("span", { class: "spacer" }), valueEl),
      el("div", { class: "track" }, input, el("i", { class: "rail" }), fill, thumb, ticks),
      caption);
    root.set = (v) => { input.value = v; paint(v); };
    return root;
  }

  function toggle({ id, label, checked, onChange }) {
    const state = el("span", { class: "state" });
    const btn = el("button", { type: "button", class: "toggle", role: "switch", id, "aria-label": label });
    const paint = (on) => { btn.setAttribute("aria-checked", String(on)); state.textContent = on ? "ON" : "OFF"; state.classList.toggle("on", on); };
    btn.addEventListener("click", async () => {
      const next = btn.getAttribute("aria-checked") !== "true";
      paint(next);
      try { await onChange(next); } catch (e) { paint(!next); toast(e.message); }
    });
    paint(!!checked);
    const wrap = el("div", { class: "toggle-row" }, state, btn);
    wrap.set = paint;
    return wrap;
  }

  // Frameless desktop windows get working controls; in a browser tab they're hidden.
  function titlebar(title) {
    const bar = el("header", { class: "titlebar" }, glyph("idle", 20), el("span", { class: "title" }, title), el("span", { class: "spacer" }));
    const host = window.pywebview && window.pywebview.api;
    if (host) {
      bar.append(el("div", { class: "controls" },
        el("button", { type: "button", "aria-label": "Minimize", onclick: () => host.minimize() }, icon("min", 16)),
        el("button", { type: "button", "aria-label": "Close", class: "close", onclick: () => host.close() }, icon("close", 16))));
    }
    return bar;
  }

  // Open another TARS window: a desktop window when running in the shell, a tab otherwise.
  function openPage(name, hash = "") {
    const host = window.pywebview && window.pywebview.api;
    if (host && host.open) return host.open(name);
    window.open(`/${name}?t=${encodeURIComponent(TOKEN)}${hash ? "#" + hash : ""}`, `tars-${name}`);
  }

  const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const pct = (n) => String(Math.round(n)).padStart(2, "0");

  return { api, el, glyph, icon, key, toast, shortcut, slider, toggle, titlebar, openPage, fmtTime, pct, STATE_NAMES, TOKEN };
})();
