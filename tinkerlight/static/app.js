"use strict";
// Tinkerlight admin / programming UI. Plain JS, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const to16 = v8 => Math.max(0, Math.min(65535, Math.round(v8 * 257)));
const to8 = v16 => Math.max(0, Math.min(255, Math.round(v16 / 257)));

let show = null;          // fixtures, presets, schedules, settings
let profiles = {};
let state = null;         // polled live state
let selected = new Set(); // fixture ids in the programmer
let dragging = null;      // attr currently being dragged (don't overwrite from poll)
let editProfile = null;   // working copy in the profile editor
let patchDraft = null;

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), err ? 5000 : 2000);
}

async function api(method, path, body) {
  const opt = { method, headers: { "X-Tinkerlight": "1" } };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    toast(data.error || ("HTTP " + r.status), true);
    throw new Error(data.error || r.status);
  }
  return data;
}

// ------------------------------------------------------------------ tabs ---
$$("nav button").forEach(b => b.addEventListener("click", () => {
  $$("nav button").forEach(x => x.classList.toggle("active", x === b));
  $$(".tab").forEach(s => s.classList.toggle("hidden", s.id !== "tab-" + b.dataset.tab));
  location.hash = b.dataset.tab;
}));

// --------------------------------------------------------------- loading ---
async function loadShow() {
  [show, profiles] = await Promise.all([api("GET", "/api/show"), api("GET", "/api/profiles")]);
  const ids = show.fixtures.map(f => f.id);
  selected = new Set([...selected].filter(id => ids.includes(id)));
  if (!selected.size) ids.forEach(id => selected.add(id));
  patchDraft = null;
  renderAll();
}

function renderAll() {
  renderPresetGrid();
  renderChips();
  renderProgrammer();
  renderRecord();
  renderPresetTable();
  renderSchedules();
  renderAdmin();
}

function profileOf(fid) {
  const f = show.fixtures.find(x => x.id === fid);
  return f && profiles[f.profile];
}

function firstSelected() {
  return show.fixtures.find(f => selected.has(f.id));
}

// ------------------------------------------------------------------ live ---
function renderPresetGrid() {
  const g = $("#preset-grid");
  g.replaceChildren(...show.presets.map(p => el("button", {
    class: "preset", "data-id": p.id,
    onclick: () => goPreset(p.id),
  }, el("span", { class: "sw", style: "background:" + (p.color || "#888") }), p.name)));
  if (!show.presets.length) g.append(el("p", { class: "muted", text: "No presets yet." }));
}

async function goPreset(id) {
  const f = $("#go-fade").value;
  await api("POST", `/api/presets/${id}/go`, f === "" ? {} : { fade: parseFloat(f) });
  poll();
}

function renderChips() {
  const c = $("#fixture-chips");
  c.replaceChildren(...show.fixtures.map(f => el("span", {
    class: "chip" + (selected.has(f.id) ? " on" : ""),
    onclick: () => {
      selected.has(f.id) ? selected.delete(f.id) : selected.add(f.id);
      renderChips(); renderProgrammer(); syncFromState();
    },
  }, `${f.name} @${f.address}`)));
}

function fmt(attr, v16) {
  return `${Math.round(v16 / 655.35)}% · ${to8(v16)}`;
}

// Batch slider moves and send them at most every 50 ms.
const pending = {};
let flushTimer = null;
function sendLive(values) {
  Object.assign(pending, values);
  if (!flushTimer) flushTimer = setTimeout(flushLive, 50);
}
async function flushLive() {
  flushTimer = null;
  const values = Object.assign({}, pending);
  for (const k in pending) delete pending[k];
  if (!selected.size || !Object.keys(values).length) return;
  await api("POST", "/api/live", {
    fixtures: [...selected], values, fade: parseFloat($("#live-fade").value) || 0,
  }).catch(() => {});
}

function renderProgrammer() {
  const box = $("#attr-groups");
  const f = firstSelected();
  const p = f && profiles[f.profile];
  box.replaceChildren();
  $("#cmd-buttons").replaceChildren();
  if (!p) { box.append(el("p", { class: "muted", text: "Select a fixture." })); return; }
  const inMode = new Set(p.modes[f.mode].channels.map(c => c.attr));
  const groups = {};
  for (const [name, spec] of Object.entries(p.attributes)) {
    if (!inMode.has(name)) continue;
    (groups[spec.group || "Other"] ||= []).push([name, spec]);
  }
  for (const [g, attrs] of Object.entries(groups)) {
    const card = el("div", { class: "card" }, el("h3", { text: g }));
    for (const [name, spec] of attrs) {
      const val = el("span", { class: "val", title: "Click to type a DMX value" });
      const slider = el("input", { type: "range", min: 0, max: 65535, step: 1, "data-attr": name });
      slider.addEventListener("pointerdown", () => dragging = name);
      slider.addEventListener("pointerup", () => dragging = null);
      slider.addEventListener("input", () => {
        val.textContent = fmt(name, +slider.value);
        sendLive({ [name]: +slider.value });
      });
      val.addEventListener("click", () => {
        const v = prompt(`${spec.label}: DMX value 0-255`, to8(+slider.value));
        if (v !== null && v !== "" && !isNaN(v)) {
          slider.value = to16(+v); val.textContent = fmt(name, +slider.value);
          sendLive({ [name]: +slider.value });
        }
      });
      const row = el("div", { class: "attr" }, el("label", { text: spec.label }), val, slider);
      if (spec.ranges && spec.ranges.length) {
        const sel = el("select", {}, el("option", { value: "", text: "- choose -" }),
          spec.ranges.map((r, i) => el("option", { value: i, text: `${r.label} (${r.from}-${r.to})` })));
        sel.addEventListener("change", () => {
          if (sel.value === "") return;
          const r = spec.ranges[+sel.value];
          const v = to16(Math.round((r.from + r.to) / 2));
          slider.value = v; val.textContent = fmt(name, v);
          sendLive({ [name]: v });
          sel.value = "";
        });
        row.append(sel);
      }
      card.append(row);
    }
    box.append(card);
  }
  (p.commands || []).forEach((c, i) => $("#cmd-buttons").append(el("button", {
    onclick: async () => {
      if (!confirm(`Send "${c.label}" to the selected fixtures?`)) return;
      await api("POST", "/api/command", { fixtures: [...selected], index: i });
      toast(c.label + " sent");
    },
  }, c.label)));
}

function syncFromState() {
  if (!state || !show) return;
  const f = firstSelected();
  const vals = f && state.values[f.id];
  if (!vals) return;
  for (const s of $$("#attr-groups input[type=range]")) {
    const a = s.dataset.attr;
    if (a === dragging || !(a in vals)) continue;
    s.value = vals[a];
    s.parentElement.querySelector(".val").textContent = fmt(a, vals[a]);
  }
  if (dragging !== "xy" && "pan" in vals && "tilt" in vals) {
    $("#xy-dot").style.left = (vals.pan / 655.35) + "%";
    $("#xy-dot").style.top = (vals.tilt / 655.35) + "%";
  }
}

// Pan / tilt pad
(() => {
  const pad = $("#xy");
  const move = e => {
    const r = pad.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const y = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
    $("#xy-dot").style.left = x * 100 + "%";
    $("#xy-dot").style.top = y * 100 + "%";
    sendLive({ pan: Math.round(x * 65535), tilt: Math.round(y * 65535) });
  };
  pad.addEventListener("pointerdown", e => { dragging = "xy"; pad.setPointerCapture(e.pointerId); move(e); });
  pad.addEventListener("pointermove", e => { if (dragging === "xy") move(e); });
  pad.addEventListener("pointerup", () => dragging = null);
})();

$("#colorpick").addEventListener("input", e => {
  const h = e.target.value;
  const [r, g, b] = [1, 3, 5].map(i => parseInt(h.substr(i, 2), 16));
  sendLive({ cyan: to16(255 - r), magenta: to16(255 - g), yellow: to16(255 - b) });
});

$("#btn-blackout").addEventListener("click", async () => {
  await api("POST", "/api/blackout", { on: !(state && state.status.blackout) });
  poll();
});
$("#btn-home").addEventListener("click", async () => {
  await api("POST", "/api/home", { fade: parseFloat($("#live-fade").value) || 0 });
  poll();
});
let gmTimer = null;
$("#gm").addEventListener("input", e => {
  $("#gm-val").textContent = e.target.value + "%";
  clearTimeout(gmTimer);
  gmTimer = setTimeout(() => api("POST", "/api/master", { level: e.target.value / 100 }), 80);
});

function renderRecord() {
  const p = profileOf(firstSelected() && firstSelected().id);
  const groups = p ? [...new Set(Object.values(p.attributes).map(a => a.group || "Other"))] : [];
  $("#rec-groups").replaceChildren(el("span", { class: "muted small", text: "Include:" }),
    ...groups.map(g => el("label", { class: "inline" },
      el("input", { type: "checkbox", value: g, checked: g !== "Control" }), " " + g)));
  const over = $("#rec-over");
  over.replaceChildren(el("option", { value: "", text: "- new preset -" }),
    ...show.presets.map(p => el("option", { value: p.id, text: p.name })));
}
$("#rec-over").addEventListener("change", e => {
  const p = show.presets.find(x => x.id === e.target.value);
  if (p) { $("#rec-name").value = p.name; $("#rec-fade").value = p.fade; $("#rec-color").value = p.color || "#888888"; }
});
$("#btn-record").addEventListener("click", async () => {
  const name = $("#rec-name").value.trim();
  if (!name) return toast("Give the preset a name", true);
  await flushLive();
  const groups = $$("#rec-groups input:checked").map(i => i.value);
  await api("POST", "/api/presets/record", {
    id: $("#rec-over").value || undefined, name, groups,
    fixtures: [...selected], fade: parseFloat($("#rec-fade").value) || 0, color: $("#rec-color").value,
  });
  toast(`Recorded "${name}"`);
  $("#rec-name").value = ""; $("#rec-over").value = "";
  loadShow();
});

// --------------------------------------------------------------- presets ---
function renderPresetTable() {
  const t = $("#preset-table");
  t.replaceChildren(el("tr", {}, ["", "Name", "Fade (s)", "Contents", ""].map(h => el("th", { text: h }))));
  show.presets.forEach((p, i) => {
    const color = el("input", { type: "color", value: p.color || "#888888" });
    const name = el("input", { value: p.name });
    const fade = el("input", { type: "number", min: 0, step: 0.5, value: p.fade, class: "num" });
    const contents = Object.entries(p.values).map(([fid, v]) => {
      const f = show.fixtures.find(x => x.id === fid);
      return `${fid === "*" ? "all" : (f ? f.name : "(unpatched)")}: ${Object.keys(v).length} attrs`;
    }).join(", ");
    const move = async d => {
      const ids = show.presets.map(x => x.id);
      const j = i + d;
      if (j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      await api("POST", "/api/presets/order", { ids });
      loadShow();
    };
    t.append(el("tr", {},
      el("td", {}, color), el("td", {}, name), el("td", {}, fade),
      el("td", { class: "muted small", text: contents }),
      el("td", {},
        el("button", { onclick: () => goPreset(p.id) }, "Go"), " ",
        el("button", { onclick: async () => {
          await api("PUT", `/api/presets/${p.id}`, { name: name.value, fade: +fade.value, color: color.value });
          toast("Saved"); loadShow();
        } }, "Save"), " ",
        el("button", { onclick: () => move(-1), title: "Move up" }, "↑"),
        el("button", { onclick: () => move(1), title: "Move down" }, "↓"), " ",
        el("button", { class: "danger", onclick: async () => {
          if (!confirm(`Delete preset "${p.name}"?`)) return;
          await api("DELETE", `/api/presets/${p.id}`); loadShow();
        } }, "Delete"))));
  });
}

// -------------------------------------------------------------- schedule ---
function describeWhen(w) {
  if (!w || w.type === "time") return w ? w.time : "";
  const o = +w.offset || 0;
  return w.type + (o ? (o > 0 ? " +" : " ") + o + " min" : "");
}
function describeAction(a) {
  const presetName = id => (show.presets.find(p => p.id === id) || { name: "(deleted preset)" }).name;
  switch (a.type) {
    case "preset": return `Preset "${presetName(a.preset)}"` + (a.fade != null && a.fade !== "" ? `, ${a.fade}s` : "");
    case "blackout": return a.on ? "Blackout on" : "Blackout off";
    case "home": return "Home";
    case "command": return "Command #" + (a.command + 1);
  }
  return a.type;
}

function renderSchedules() {
  const t = $("#sched-table");
  t.replaceChildren(el("tr", {}, ["On", "Name", "When", "Days", "Action", ""].map(h => el("th", { text: h }))));
  for (const s of show.schedules) {
    const on = el("input", { type: "checkbox", checked: s.enabled });
    on.addEventListener("change", async () => {
      await api("POST", "/api/schedules", Object.assign({}, s, { enabled: on.checked }));
      loadShow(); poll();
    });
    const days = s.days && s.days.length ? s.days.map(d => DAYS[d]).join(" ") : "every day";
    const dates = s.date_from || s.date_to ? ` (${s.date_from || "…"} → ${s.date_to || "…"})` : "";
    t.append(el("tr", {},
      el("td", {}, on), el("td", { text: s.name }), el("td", { text: describeWhen(s.when) }),
      el("td", { class: "small", text: days + dates }), el("td", { text: describeAction(s.action) }),
      el("td", {},
        el("button", { onclick: async () => { await api("POST", `/api/schedules/${s.id}/run`); poll(); } }, "Run now"), " ",
        el("button", { onclick: () => openSchedule(s) }, "Edit"), " ",
        el("button", { class: "danger", onclick: async () => {
          if (!confirm(`Delete "${s.name}"?`)) return;
          await api("DELETE", `/api/schedules/${s.id}`); loadShow(); poll();
        } }, "Delete"))));
  }
}

function openSchedule(s) {
  s = s || { name: "", enabled: true, days: [], when: { type: "time", time: "18:00" },
             action: { type: "preset", preset: show.presets[0] && show.presets[0].id } };
  const f = $("#sched-form");
  f.id.value = s.id || "";
  f.name.value = s.name;
  f.enabled.checked = s.enabled;
  f.when_type.value = s.when.type;
  f.time.value = s.when.time || "18:00";
  f.offset.value = s.when.offset || 0;
  f.date_from.value = s.date_from || "";
  f.date_to.value = s.date_to || "";
  $("#days").replaceChildren(...DAYS.map((d, i) => el("label", { class: "chip" + (s.days.includes(i) ? " on" : "") },
    el("input", { type: "checkbox", value: i, checked: s.days.includes(i), hidden: true,
      onchange: e => e.target.parentElement.classList.toggle("on", e.target.checked) }), d)));
  f.preset.replaceChildren(...show.presets.map(p => el("option", { value: p.id, text: p.name })));
  const p = profiles[(show.fixtures[0] || {}).profile] || { commands: [] };
  f.command.replaceChildren(...(p.commands || []).map((c, i) => el("option", { value: i, text: c.label })));
  const a = s.action;
  f.action_type.value = a.type === "blackout" ? (a.on ? "blackout_on" : "blackout_off") : a.type;
  if (a.preset) f.preset.value = a.preset;
  if (a.command != null) f.command.value = a.command;
  f.fade.value = a.fade != null ? a.fade : "";
  schedVisibility();
  $("#sched-dlg").showModal();
}
function schedVisibility() {
  const f = $("#sched-form");
  const wt = f.when_type.value, at = f.action_type.value;
  f.time.classList.toggle("hidden", wt !== "time");
  $(".offset", f).classList.toggle("hidden", wt === "time");
  f.preset.classList.toggle("hidden", at !== "preset");
  f.command.classList.toggle("hidden", at !== "command");
  $(".fade", f).classList.toggle("hidden", !["preset", "home"].includes(at));
}
$("#sched-form").when_type.addEventListener("change", schedVisibility);
$("#sched-form").action_type.addEventListener("change", schedVisibility);
$("#btn-new-sched").addEventListener("click", () => openSchedule(null));
$("#sched-form").addEventListener("submit", async e => {
  if (e.submitter && e.submitter.value !== "save") return;
  e.preventDefault();
  const f = e.target;
  const at = f.action_type.value;
  let action;
  if (at === "preset") action = { type: "preset", preset: f.preset.value };
  else if (at === "blackout_on" || at === "blackout_off") action = { type: "blackout", on: at === "blackout_on" };
  else if (at === "home") action = { type: "home" };
  else action = { type: "command", fixture: "*", command: +f.command.value };
  if (["preset", "home"].includes(at) && f.fade.value !== "") action.fade = +f.fade.value;
  const when = f.when_type.value === "time" ? { type: "time", time: f.time.value }
    : { type: f.when_type.value, offset: +f.offset.value || 0 };
  await api("POST", "/api/schedules", {
    id: f.id.value || undefined, name: f.name.value, enabled: f.enabled.checked,
    days: $$("#days input:checked").map(i => +i.value), when,
    date_from: f.date_from.value || null, date_to: f.date_to.value || null, action,
  });
  $("#sched-dlg").close();
  toast("Schedule saved");
  loadShow(); poll();
});

// ----------------------------------------------------------------- admin ---
function renderAdmin() {
  const o = show.settings.output;
  const of = $("#out-form");
  for (const k of ["type", "host", "net", "subnet", "universe", "sacn_universe", "sacn_host", "priority", "serial_port", "fps"])
    if (of[k]) of[k].value = o[k] != null ? o[k] : "";
  outVisibility();
  const lf = $("#loc-form");
  lf.lat.value = show.settings.location.lat;
  lf.lon.value = show.settings.location.lon;
  lf.startup.value = show.settings.startup;
  lf.startup_preset.replaceChildren(el("option", { value: "", text: "-" }),
    ...show.presets.map(p => el("option", { value: p.id, text: p.name })));
  lf.startup_preset.value = show.settings.startup_preset || "";
  $("#pw-form").user.value = show.user || "admin";
  const cam = show.settings.camera || {}, cf = $("#cam-form");
  cf.enabled.checked = !!cam.enabled;
  for (const k of ["width", "height", "interval"]) cf[k].value = cam[k] != null ? cam[k] : "";
  loadCameras(cam.device);
  renderPatch();
  const sel = $("#profile-select");
  const cur = sel.value;
  sel.replaceChildren(...Object.values(profiles).map(p =>
    el("option", { value: p.id, text: `${p.manufacturer} ${p.model}${p.builtin ? "" : " (edited)"}` })));
  if (cur && profiles[cur]) sel.value = cur;
  loadProfileEditor();
}

function outVisibility() {
  const t = $("#out-form").type.value;
  $$("#out-form [data-for]").forEach(l => l.classList.toggle("hidden", !l.dataset.for.split(" ").includes(t)));
}
$("#out-form").type.addEventListener("change", outVisibility);
$("#out-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target, out = {};
  for (const k of ["type", "host", "sacn_host", "serial_port"]) out[k] = f[k].value.trim();
  for (const k of ["net", "subnet", "universe", "sacn_universe", "priority", "fps"]) out[k] = +f[k].value;
  const r = await api("PUT", "/api/settings", { output: out });
  $("#out-msg").textContent = r.output_error ? "Error: " + r.output_error : "Output running.";
  loadShow();
});
$("#loc-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  await api("PUT", "/api/settings", {
    location: { lat: +f.lat.value, lon: +f.lon.value },
    startup: f.startup.value, startup_preset: f.startup_preset.value || null,
  });
  toast("Saved"); loadShow(); poll();
});
// Fill the camera dropdown with what's plugged in; keep the saved device even if it's gone.
async function loadCameras(current) {
  const sel = $("#cam-form").device;
  let want = current !== undefined ? current : sel.value;
  let cams = [];
  try { cams = (await api("GET", "/api/cameras")).cameras; } catch (err) { /* toast shown */ }
  const same = cams.find(c => c.node === want);      // saved as /dev/videoN: show its stable name
  if (same) want = same.device;
  const opts = cams.map(c => el("option", { value: c.device, text: `${c.name} (${c.node})` }));
  if (want && !cams.some(c => c.device === want))
    opts.unshift(el("option", { value: want, text: `${want} (not connected)` }));
  if (!opts.length) opts.push(el("option", { value: "", text: "No camera found - plug one in and press Rescan" }));
  sel.replaceChildren(...opts);
  if (want) sel.value = want;
  return cams.length;
}
$("#btn-cam-scan").addEventListener("click", async () => {
  const n = await loadCameras();
  toast(n === 1 ? "1 camera found" : n + " cameras found");
});
$("#cam-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  await api("PUT", "/api/settings", { camera: {
    enabled: f.enabled.checked, device: f.device.value.trim(),
    width: +f.width.value, height: +f.height.value, interval: +f.interval.value } });
  toast("Saved"); await loadShow(); poll(); refreshCamera();
});
$("#pw-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  await api("POST", "/api/password", { user: f.user.value, current: f.current.value, new: f.new.value });
  toast("Login changed - the browser will ask you to log in again");
  f.current.value = f.new.value = "";
  setTimeout(() => location.reload(), 1500);
});

function renderPatch() {
  if (!patchDraft) patchDraft = JSON.parse(JSON.stringify(show.fixtures));
  const t = $("#patch-table");
  t.replaceChildren(el("tr", {}, ["Name", "Profile", "Mode", "Address", "Channels", ""].map(h => el("th", { text: h }))));
  patchDraft.forEach((f, i) => {
    const p = profiles[f.profile];
    const name = el("input", { value: f.name, onchange: e => f.name = e.target.value });
    const prof = el("select", { onchange: e => { f.profile = e.target.value; f.mode = Object.keys(profiles[f.profile].modes)[0]; renderPatch(); } },
      Object.values(profiles).map(x => el("option", { value: x.id, text: x.model, selected: x.id === f.profile })));
    const mode = el("select", { onchange: e => { f.mode = e.target.value; renderPatch(); } },
      Object.keys(p ? p.modes : {}).map(m => el("option", { value: m, text: `${m} (${p.modes[m].footprint} ch)`, selected: m === f.mode })));
    const addr = el("input", { type: "number", min: 1, max: 512, value: f.address, class: "num",
      onchange: e => { f.address = +e.target.value; renderPatch(); } });
    const fp = p && p.modes[f.mode] ? p.modes[f.mode].footprint : 0;
    t.append(el("tr", {}, el("td", {}, name), el("td", {}, prof), el("td", {}, mode), el("td", {}, addr),
      el("td", { class: "muted", text: fp ? `${f.address}-${f.address + fp - 1}` : "?" }),
      el("td", {}, el("button", { class: "danger", onclick: () => { patchDraft.splice(i, 1); renderPatch(); } }, "Remove"))));
  });
}
$("#btn-add-fixture").addEventListener("click", () => {
  const pid = Object.keys(profiles)[0];
  const p = profiles[pid];
  const mode = Object.keys(p.modes)[0];
  let next = 1;
  for (const f of patchDraft) {
    const fp = profiles[f.profile] ? profiles[f.profile].modes[f.mode].footprint : 0;
    next = Math.max(next, f.address + fp);
  }
  let n = patchDraft.length + 1;
  const ids = new Set(patchDraft.map(f => f.id));
  while (ids.has("f" + n)) n++;
  patchDraft.push({ id: "f" + n, name: `${p.model} ${n}`, profile: pid, mode, address: next });
  renderPatch();
});
$("#btn-save-patch").addEventListener("click", async () => {
  await api("PUT", "/api/fixtures", { fixtures: patchDraft });
  toast("Patch saved"); loadShow();
});

// Profile editor: table for channel numbers, JSON for everything else.
function loadProfileEditor() {
  const p = profiles[$("#profile-select").value];
  if (!p) return;
  editProfile = JSON.parse(JSON.stringify(p));
  delete editProfile.builtin;
  $("#profile-note").textContent = (p.verified ? "" : "⚠ NOT VERIFIED. ") + (p.notes || "");
  renderProfileTable();
}
function renderProfileTable() {
  const p = editProfile;
  const modes = Object.keys(p.modes);
  $("#profile-json").value = JSON.stringify(p, null, 2);
  const chanOf = (m, a) => p.modes[m].channels.find(c => c.attr === a) || {};
  const setChan = (m, a, key, v) => {
    const list = p.modes[m].channels;
    let c = list.find(x => x.attr === a);
    if (!c) { c = { attr: a }; list.push(c); }
    if (v === "") delete c[key]; else c[key] = +v;
    if (c.ch == null) list.splice(list.indexOf(c), 1);
    list.sort((x, y) => x.ch - y.ch);
    $("#profile-json").value = JSON.stringify(p, null, 2);
  };
  const t = el("table", { class: "list" },
    el("tr", {}, el("th", { text: "Attribute" }), el("th", { text: "Default (0-255)" }), el("th", { text: "Snap" }),
      modes.map(m => el("th", { text: `${m}: ch / fine` }))),
    el("tr", {}, el("td", { class: "muted", text: "Footprint" }), el("td"), el("td"),
      modes.map(m => el("td", {}, el("input", { type: "number", class: "num", value: p.modes[m].footprint,
        onchange: e => { p.modes[m].footprint = +e.target.value; $("#profile-json").value = JSON.stringify(p, null, 2); } })))));
  for (const [a, spec] of Object.entries(p.attributes)) {
    t.append(el("tr", {},
      el("td", {}, el("input", { value: spec.label, title: a, onchange: e => { spec.label = e.target.value; renderProfileTable(); } })),
      el("td", {}, el("input", { type: "number", min: 0, max: 255, step: "any", class: "num", value: spec.default || 0,
        onchange: e => { spec.default = +e.target.value; renderProfileTable(); } })),
      el("td", {}, el("input", { type: "checkbox", checked: !!spec.snap,
        onchange: e => { spec.snap = e.target.checked; renderProfileTable(); } })),
      modes.map(m => {
        const c = chanOf(m, a);
        return el("td", {},
          el("input", { type: "number", min: 1, max: 512, class: "num", value: c.ch != null ? c.ch : "", placeholder: "-",
            onchange: e => setChan(m, a, "ch", e.target.value) }), " ",
          el("input", { type: "number", min: 1, max: 512, class: "num", value: c.fine != null ? c.fine : "", placeholder: "-",
            onchange: e => setChan(m, a, "fine", e.target.value) }));
      })));
  }
  $("#profile-table").replaceChildren(t);
}
$("#profile-select").addEventListener("change", loadProfileEditor);
$("#profile-json").addEventListener("change", e => {
  try { editProfile = JSON.parse(e.target.value); renderProfileTable(); }
  catch (err) { toast("JSON error: " + err.message, true); }
});
$("#btn-profile-save").addEventListener("click", async () => {
  await api("PUT", `/api/profiles/${editProfile.id}`, editProfile);
  toast("Profile saved"); loadShow();
});
$("#btn-profile-revert").addEventListener("click", async () => {
  if (!confirm("Discard your edits and go back to the built-in profile?")) return;
  await api("POST", `/api/profiles/${editProfile.id}/revert`);
  toast("Reverted"); loadShow();
});

// Channel test
let testTimer = null;
$("#test-val").addEventListener("input", e => {
  $("#test-val-txt").textContent = e.target.value;
  clearTimeout(testTimer);
  testTimer = setTimeout(() => api("POST", "/api/test", { channel: +$("#test-ch").value, value: +e.target.value }), 40);
});
$("#btn-test-release").addEventListener("click", () => api("POST", "/api/test", { channel: +$("#test-ch").value, value: -1 }));
$("#btn-test-clear").addEventListener("click", () => api("POST", "/api/test", { clear: true }));

$("#restore-file").addEventListener("change", async e => {
  const file = e.target.files[0];
  if (!file || !confirm("Replace the whole show (patch, presets, schedule, settings) with this backup?")) return;
  e.target.value = "";
  let data;
  try { data = JSON.parse(await file.text()); } catch (err) { return toast("Not a valid backup file", true); }
  await api("POST", "/api/restore", data);
  toast("Restored"); loadShow();
});

// ---------------------------------------------------------------- camera ---
let camBusy = false, camLast = 0, camUrl = null;
async function refreshCamera() {
  const cam = state && state.camera;
  $("#cam-card").classList.toggle("hidden", !(cam && cam.enabled));
  if (!cam || !cam.enabled || camBusy) return;
  camBusy = true;
  camLast = Date.now();
  try {
    const r = await fetch("/api/camera.jpg?t=" + camLast);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "HTTP " + r.status);
    const url = URL.createObjectURL(await r.blob());
    $("#cam-img").src = url;
    if (camUrl) URL.revokeObjectURL(camUrl);
    camUrl = url;
    $("#cam-img").classList.remove("stale");
    $("#cam-info").textContent = "Taken " + (r.headers.get("X-Taken") || "").replace("T", " ");
  } catch (err) {
    $("#cam-img").classList.add("stale");
    $("#cam-info").textContent = "Camera: " + err.message;
  } finally {
    camBusy = false;
  }
}
$("#btn-cam-refresh").addEventListener("click", refreshCamera);
function cameraTick() {
  const cam = state && state.camera;
  const liveVisible = !$("#tab-live").classList.contains("hidden") && !document.hidden;
  if (cam && cam.enabled && liveVisible && $("#cam-auto").checked &&
      Date.now() - camLast >= (cam.interval || 5) * 1000) refreshCamera();
}

// ------------------------------------------------------------------ poll ---
function renderMonitor() {
  let end = 32;
  for (const f of show.fixtures) {
    const p = profiles[f.profile];
    if (p && p.modes[f.mode]) end = Math.max(end, f.address + p.modes[f.mode].footprint - 1);
  }
  const box = $("#dmx-monitor");
  if (box.children.length !== end) {
    box.replaceChildren(...Array.from({ length: end }, (_, i) => el("div", {}, el("b", { text: i + 1 }), el("span"))));
  }
  state.dmx.slice(0, end).forEach((v, i) => {
    const c = box.children[i];
    c.lastChild.textContent = v;
    c.classList.toggle("hot", v > 0);
  });
}

async function poll() {
  try { state = await api("GET", "/api/state"); } catch (e) { return; }
  const st = state.status;
  $("#clock").textContent = state.time.replace("T", " ");
  const out = $("#outstate");
  out.textContent = st.output_error ? "Output error" : st.output;
  out.className = "pill " + (st.output_error ? "err" : "ok");
  out.title = st.output_error || "";
  $("#btn-blackout").classList.toggle("on", st.blackout);
  $("#btn-blackout").textContent = st.blackout ? "Blackout ON" : "Blackout";
  if (document.activeElement !== $("#gm")) {
    $("#gm").value = Math.round(st.grand_master * 100);
    $("#gm-val").textContent = Math.round(st.grand_master * 100) + "%";
  }
  $("#last-action").textContent = "Last: " + st.last_action + (st.test_channels ? ` · ${st.test_channels} test channel(s) active` : "");
  $$("#preset-grid .preset").forEach(b => b.classList.toggle("active", b.dataset.id === st.active_preset));
  const warn = [];
  if (state.default_password) warn.push("Default login admin/admin is active - change it under Admin.");
  if (st.output_error) warn.push("DMX output problem: " + st.output_error);
  if (Object.values(profiles).some(p => !p.verified)) warn.push("Fixture profile not verified against the manual yet - see Admin > Fixture profile.");
  $("#warn").innerHTML = "";
  warn.forEach(w => $("#warn").append(el("div", { text: w })));
  $("#warn").classList.toggle("hidden", !warn.length);
  $("#sun-info").textContent = state.sunrise ? `Today: sunrise ${state.sunrise}, sunset ${state.sunset} (board time ${state.time.slice(11, 16)})` : "";
  $("#upcoming").replaceChildren(...state.upcoming.map(u => el("li", {},
    el("time", { text: u.at.replace("T", " ").slice(5) }), `${u.name} - ${describeAction(u.action)}`)));
  if (!state.upcoming.length) $("#upcoming").append(el("li", { class: "muted", text: "Nothing scheduled." }));
  $("#history").replaceChildren(...state.log.map(u => el("li", {},
    el("time", { text: u.at.replace("T", " ").slice(5) }), `${u.source} - ${describeAction(u.action)}`)));
  syncFromState();
  $("#cam-card").classList.toggle("hidden", !(state.camera && state.camera.enabled));
  cameraTick();
  if (!$("#tab-admin").classList.contains("hidden")) renderMonitor();
}

(async () => {
  await loadShow();
  const tab = location.hash.slice(1);
  if (tab) { const b = $(`nav button[data-tab="${tab}"]`); if (b) b.click(); }
  await poll();
  setInterval(poll, 1000);
})();
