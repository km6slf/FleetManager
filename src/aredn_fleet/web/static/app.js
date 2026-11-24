"use strict";
// AREDN Fleet Manager GUI. Plain JS, no build step.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  topo: { nodes: [], links: [], root: "" },
  selected: new Set(),
  sort: { key: "hop", dir: 1 },
  filter: "",
  currentJob: null,
  jobCursor: 0,
  pollTimer: null,
  localFw: [],
};

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const init = { headers: {}, ...opts };
  if (opts.body !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText);
  return data;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = ""), err ? 6000 : 2500);
}

async function guarded(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ tabs
function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "map") drawMap();
  if (name === "firmware") loadFirmware();
  if (name === "creds") loadCreds();
  if (name === "activity") loadJobs();
}
$$("#tabs button").forEach((b) => b.addEventListener("click", () => { history.replaceState(null, "", "#" + b.dataset.tab); showTab(b.dataset.tab); }));

// ------------------------------------------------------------------ nodes
const newestStable = () => state.localFw.filter((r) => /^\d+(\.\d+){3}$/.test(r.version))
  .map((r) => r.version).sort((a, b) => cmpVer(b, a))[0];
function cmpVer(a, b) {
  const pa = a.split(".").map(Number), pb = b.split(".").map(Number);
  for (let i = 0; i < 4; i++) if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0);
  return 0;
}

async function loadTopology() {
  state.topo = await api("/api/topology");
  const names = new Set(state.topo.nodes.map((n) => n.name));
  state.selected = new Set([...state.selected].filter((n) => names.has(n)));
  const at = state.topo.discovered_at;
  $("#disc-age").textContent = at ? `Last discovered ${new Date(at * 1000).toLocaleString()}` : "";
  renderNodes();
  if ($("#tab-map").classList.contains("active")) drawMap();
}

function visibleNodes() {
  const f = state.filter.toLowerCase();
  const rows = state.topo.nodes.filter((n) => !f || [n.name, n.model, n.firmware].join(" ").toLowerCase().includes(f));
  const { key, dir } = state.sort;
  const val = (n) => key === "cuts" ? n.cuts_off.length : key === "hop" ? (n.hop ?? 999) : String(n[key] || "").toLowerCase();
  return rows.sort((a, b) => (val(a) > val(b) ? 1 : val(a) < val(b) ? -1 : a.name.localeCompare(b.name)) * dir);
}

function renderNodes() {
  const rows = visibleNodes();
  const latest = newestStable();
  $("#empty-nodes").classList.toggle("hidden", state.topo.nodes.length > 0);
  $("#node-table tbody").innerHTML = rows.map((n) => {
    const sel = state.selected.has(n.name);
    const old = latest && /^\d/.test(n.firmware) && cmpVer(n.firmware, latest) < 0;
    const rf = rfCell(n);
    return `<tr data-node="${esc(n.name)}" class="${sel ? "sel" : ""}">
      <td class="chk"><input type="checkbox" ${sel ? "checked" : ""} tabindex="-1"></td>
      <td><b>${esc(n.name)}</b> ${n.name === state.topo.root ? '<span class="badge root">this computer</span>' : ""}
          ${n.reachable ? "" : '<span class="badge failed">unreachable</span>'}
          ${n.gateway ? '<span class="badge gateway" title="Shares its internet (WAN) connection with the mesh">gateway</span>' : ""}
          ${ROUTING_BADGE[n.routing] || ""}
          ${n.custom ? `<span class="badge custom" data-edit="${esc(n.name)}" title="${esc(n.custom.text + (n.custom.note ? " — " + n.custom.note : ""))}">custom</span>` : ""}</td>
      <td class="model" title="${esc(n.model)}">${esc(n.model)}</td>
      <td class="${old ? "fw-old" : ""}" title="${old ? "older than " + esc(latest) : ""}">${esc(n.firmware)}</td>
      <td>${n.hop ?? "–"}</td>
      <td class="${n.cuts_off.length ? "cut" : "muted"}" title="${esc(n.cuts_off.join(", "))}">${n.cuts_off.length ? n.cuts_off.length + " node" + (n.cuts_off.length > 1 ? "s" : "") : "–"}</td>
      <td class="rf">${rf}</td>
      <td class="own"><button class="ghost" data-edit="${esc(n.name)}" title="Settings for this node only">${n.custom ? "Edit" : "Set…"}</button></td></tr>`;
  }).join("");
  $("#chk-all").checked = rows.length > 0 && rows.every((n) => state.selected.has(n.name));
  $("#sel-count").textContent = state.selected.size;
  scheduleRadioPanel();
}

function rfCell(n) {
  if (n.radios) {
    const stale = n.radios_stale ? '<span class="chip off" title="Read when the node ran other firmware; channel lists may have changed">out of date — read radios again</span>' : "";
    return `<div class="chips">${stale}${n.radios.map((r) => {
      const mesh = ["mesh", "meshap", "meshptp", "meshsta"].includes(r.mode);
      const detail = mesh ? ` · ${esc(r.label)} · ${r.bandwidth} MHz` : "";
      return `<span class="chip ${r.mode === "off" ? "off" : ""}"><span class="bandtag">${esc(r.band_label)}</span>${esc(r.mode_label)}${detail}</span>`;
    }).join("")}</div>`;
  }
  return n.channel ? `<span class="muted" title="Read radios for every radio's details">ch ${esc(n.channel)} · ${esc(n.chanbw)} MHz</span>` : "";
}

$("#node-table tbody").addEventListener("click", (e) => {
  const ed = e.target.closest("[data-edit]");
  if (ed) { e.stopPropagation(); guarded(() => openNodeEditor(ed.dataset.edit)); return; }
  const tr = e.target.closest("tr[data-node]");
  if (!tr) return;
  toggle(tr.dataset.node);
});
function toggle(name) {
  state.selected.has(name) ? state.selected.delete(name) : state.selected.add(name);
  $("#plan-out").innerHTML = "";
  renderNodes();
  if ($("#tab-map").classList.contains("active")) drawMap();
}
$("#chk-all").addEventListener("click", (e) => {
  const rows = visibleNodes();
  rows.forEach((n) => (e.target.checked ? state.selected.add(n.name) : state.selected.delete(n.name)));
  renderNodes();
});
$("#sel-all").addEventListener("click", () => { visibleNodes().forEach((n) => state.selected.add(n.name)); renderNodes(); });
$("#sel-none").addEventListener("click", () => { state.selected.clear(); $("#plan-out").innerHTML = ""; renderNodes(); });
$("#filter").addEventListener("input", (e) => { state.filter = e.target.value; renderNodes(); });
$$("#node-table th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  const k = th.dataset.sort;
  state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : 1 };
  renderNodes();
}));

$("#btn-discover").addEventListener("click", () => guarded(async () => {
  const job = await api("/api/discover", { body: { start: $("#disc-start").value.trim() || "localnode" } });
  followJob(job.id, { onDone: loadTopology, quiet: true });
  toast("Discovering…");
}));

// ------------------------------------------------------------------ upgrade
function needSelection() {
  if (!state.selected.size) throw new Error("Select at least one node first");
  return [...state.selected];
}
function upgradeBody(execute, confirm = "") {
  return {
    nodes: needSelection(), version: $("#up-version").value,
    parallel: +$("#up-parallel").value || 1, soak: +$("#up-soak").value || 0,
    exclude_tunnels: $("#up-notun").checked, execute, confirm,
  };
}
function planHtml(p) {
  const waves = p.waves.map((w, i) => `<li>${w.map(esc).join(", ")}</li>`).join("");
  const warns = [...p.warnings, ...(p.unreachable.length ? ["Skipped, unreachable: " + p.unreachable.join(", ")] : [])]
    .map((w) => `<p class="warn">⚠ ${esc(w)}</p>`).join("");
  return `<p class="hint">Order (outermost first, this computer's node last):</p><ol>${waves}</ol>${warns}`;
}
$("#btn-plan").addEventListener("click", () => guarded(async () => {
  const b = upgradeBody(false);
  const p = await api("/api/plan", { body: { nodes: b.nodes, parallel: b.parallel, exclude_tunnels: b.exclude_tunnels } });
  $("#plan-out").innerHTML = planHtml(p);
}));
$("#btn-up-dry").addEventListener("click", () => guarded(async () => {
  if (!$("#up-version").value) throw new Error("Download firmware first (Firmware tab)");
  const job = await api("/api/upgrade", { body: upgradeBody(false) });
  followJob(job.id);
}));
$("#btn-up-go").addEventListener("click", (e) => guarded(async () => {
  const b = upgradeBody(false);
  if (!b.version) throw new Error("Download firmware first (Firmware tab)");
  const btn = e.currentTarget;
  busy(btn, true);
  let review;
  try { review = await runAndWait("/api/upgrade", b, "Checking every selected node…"); } finally { busy(btn, false); }
  const r = review.result;
  const go = r.results.filter((x) => x.status === "would-upgrade").length;
  if (!go && !r.blocked.length) { toast("Nothing to upgrade: every selected node is already on " + r.version); return; }
  const ok = await confirmDialog(`Review: upgrade to ${r.version}`, upgradeLedger(r), "upgrade",
    { wide: true, blocked: r.blocked.length > 0 });
  if (!ok) return;
  const job = await api("/api/upgrade", { body: { ...b, execute: true, confirm: "upgrade", reviewed_job: review.id } });
  followJob(job.id, { onDone: () => loadTopology() });
}));

// ------------------------------------------------------------------ radio
const MESH = ["mesh", "meshap", "meshptp", "meshsta"];
const MODE_NAMES = { mesh: "Mesh", meshap: "Mesh PtMP", meshptp: "Mesh PtP", meshsta: "Mesh Station", lan: "LAN Hotspot", wan: "WAN Client", off: "Off" };
let radioOpts = null;
let radioTimer = null;

function scheduleRadioPanel() {
  clearTimeout(radioTimer);
  radioTimer = setTimeout(() => guarded(async () => { await loadRadioPanel(); await loadSettingsPanel(); }), 150);
}

async function loadRadioPanel() {
  const nodes = [...state.selected];
  const box = $("#radio-bands");
  if (!nodes.length) {
    radioOpts = null;
    $("#radio-missing").classList.add("hidden");
    box.innerHTML = '<p class="hint">Select nodes to see their radios.</p>';
    return;
  }
  radioOpts = await api("/api/radio/options", { body: { nodes } });
  const miss = radioOpts.missing, stale = radioOpts.stale || [];
  const fresh = miss.filter((n) => !stale.includes(n));
  $("#radio-missing").classList.toggle("hidden", !miss.length);
  const list = (a) => `${a.slice(0, 3).join(", ")}${a.length > 3 ? "…" : ""}`;
  $("#radio-missing span").textContent = [
    fresh.length ? `Radio details not read yet for ${list(fresh)}.` : "",
    stale.length ? `Out of date after a firmware change for ${list(stale)}.` : "",
  ].filter(Boolean).join(" ");
  if (!radioOpts.bands.length) {
    box.innerHTML = '<p class="hint">No radio details for the selected nodes yet. Press <b>Read radios</b> (logs in over SSH, read-only).</p>';
    return;
  }
  mountBands(box, radioOpts.bands, { kind: "group" });
}

function nowLine(n, cur) {
  if (!cur) return "";
  const mesh = MESH.includes(cur.mode);
  const node = state.topo.nodes.find((x) => x.name === n);
  const own = node?.custom ? ` <span class="own-tag" title="${esc(node.custom.text)}">· own settings</span>` : "";
  return `<span><b>${esc(n)}</b>: ${esc(MODE_NAMES[cur.mode] || cur.mode)}${mesh ? ` · ch ${esc(cur.label)} · ${cur.bandwidth} MHz · ${esc(ssidFull(cur.ssid || "AREDN", cur.mode, cur.channel, cur.bandwidth, ""))}` : ""}${own}</span>`;
}

// SSID as node-setup builds it: base-<bw>-v3 for Mesh (base-<ch>-<bw>-v3 on 900 MHz),
// base-<ch>-<bw>-v3 for PtMP / PtP / Station.
function ssidFull(base, mode, ch, bw, band) {
  if (mode === "mesh" && !String(band).startsWith("900")) return `${base}-${bw}-v3`;
  return `${base}-${ch}-${bw}-v3`;
}

// ctx.kind: "group" (bulk panel: blank = leave as is) or "node" (blank = use the group's setting)
function bandHtml(b, ctx = { kind: "group" }) {
  const names = Object.keys(b.per_node);
  const lines = ctx.kind === "group"
    ? names.slice(0, 6).map((n) => nowLine(n, b.per_node[n])).join("") + (names.length > 6 ? `<span>…and ${names.length - 6} more</span>` : "")
    : nowLine(names[0], b.per_node[names[0]]);
  const curMode = b.current_mode;
  const blank = ctx.kind === "node" ? `Keep current (${esc(MODE_NAMES[curMode] || curMode || "?")})`
    : curMode ? `Leave as is (${esc(MODE_NAMES[curMode] || curMode)})` : "Leave as is (mixed)";
  // Only purposes every selected radio's hardware supports (as the nodes report it).
  const modeOpts = ["mesh", "meshap", "meshsta", "meshptp", "off"]
    .filter((m) => m === "off" || !b.supported_modes || b.supported_modes.includes(m))
    .map((m) => `<option value="${m}">${MODE_NAMES[m]}</option>`).join("");
  const without = ctx.kind === "group" && b.without.length ? `<p class="hint">No ${esc(b.band_label)} radio on: ${esc(b.without.join(", "))}. They're left alone.</p>` : "";
  const ownHere = ctx.kind === "group" ? names.filter((n) => state.topo.nodes.find((x) => x.name === n)?.custom?.radio?.[b.band]) : [];
  const ownNote = ownHere.length ? `<p class="hint"><span class="own-tag">Own ${esc(b.band_label)} settings:</span> ${esc(ownHere.join(", "))}. Changes here don't apply to ${ownHere.length > 1 ? "them" : "it"}; ${ownHere.length > 1 ? "they keep" : "it keeps"} its own values, and anything not set there stays as it is now.</p>` : "";
  const curSsid = b.current_ssid;
  return `<div class="band" data-band="${esc(b.band)}" data-kind="${ctx.kind}">
    <div class="band-head"><b>Radio ${esc(b.band_label)}</b>${ctx.kind === "group" ? `<span class="muted small">${names.length} node${names.length > 1 ? "s" : ""}</span>` : ""}</div>
    <div class="band-now">${lines}</div>
    <div class="grid2">
      <label>Purpose <select data-k="mode"><option value="">${blank}</option>${modeOpts}</select></label>
      <label>Channel width <select data-k="width"></select></label>
      <label class="wide">Channel <select data-k="channel"></select></label>
      <label class="wide">Mesh SSID
        <span class="ssid-row"><input data-k="ssid" maxlength="20" placeholder="${esc(ctx.kind === "node" ? `Keep current (${curSsid || "AREDN"})` : curSsid ? `Leave as is (${curSsid})` : "Leave as is")}"><span class="suffix"></span></span></label>
      <label class="wide peer hidden">Peer MAC (Mesh PtP) <input data-k="peer" placeholder="aa:bb:cc:dd:ee:ff"></label>
    </div>
    <p class="hint band-hint"></p>${without}${ownNote}</div>`;
}

function mountBands(container, bands, ctx, values = {}) {
  container.innerHTML = bands.map((b) => bandHtml(b, ctx)).join("");
  $$(".band", container).forEach((el) => {
    el._band = bands.find((b) => b.band === el.dataset.band);
    const v = values[el._band.band] || {};
    if (v.mode) $('[data-k="mode"]', el).value = v.mode;
    if (v.ssid) $('[data-k="ssid"]', el).value = v.ssid;
    if (v.peer) $('[data-k="peer"]', el).value = v.peer;
    el._initial = v;
    $$("select, input", el).forEach((s) => s.addEventListener(s.tagName === "INPUT" ? "input" : "change", () => refreshBand(el)));
    refreshBand(el);
  });
}

function refreshBand(el) {
  const b = el._band;
  const nodeCtx = el.dataset.kind === "node";
  const modeSel = $('[data-k="mode"]', el), wSel = $('[data-k="width"]', el), cSel = $('[data-k="channel"]', el);
  const mode = modeSel.value || b.current_mode;           // effective mode after the change
  const meshy = mode && MESH.includes(mode);
  $(".peer", el).classList.toggle("hidden", mode !== "meshptp");
  const hint = $(".band-hint", el);

  // Width: blank (leave as is / group setting) or any width every node supports.
  const wPrev = wSel.value || (el._initial?.width ? String(el._initial.width) : "");
  const curW = b.current ? b.current.width : null;
  const wBlank = nodeCtx ? `Keep current (${curW ?? "?"} MHz)` : curW ? `Leave as is (${curW} MHz)` : "Leave as is";
  wSel.innerHTML = `<option value="">${wBlank}</option>` + b.bws.map((w) => `<option value="${w}">${w} MHz</option>`).join("");
  if ([...wSel.options].some((o) => o.value === wPrev)) wSel.value = wPrev;
  wSel.disabled = !meshy;

  // Channel: exactly the node dialog's list for this mode and width, common to all nodes here.
  const width = wSel.value || (curW ? String(curW) : "");
  const table = b.channels[mode && mode !== "mesh" ? "managed" : "mesh"] || {};
  const opts = width ? (table[width] || []) : [];
  const cPrev = cSel.value || (el._initial?.channel != null ? String(el._initial.channel) : "");
  el._initial = { ...el._initial, width: undefined, channel: undefined };
  const cur = b.current;
  const curOk = cur && opts.some((c) => c.number === cur.channel);
  const keepLabel = cur ? (opts.find((c) => c.number === cur.channel)?.label || cur.channel) : null;
  const sameWidth = !wSel.value || +wSel.value === cur?.width;
  const cBlank = nodeCtx ? (curOk ? `Keep current (${esc(keepLabel)})` : "— choose —")
    : curOk ? `${sameWidth ? "Leave as is" : "Keep"} (${esc(keepLabel)})` : "— choose —";
  cSel.innerHTML = `<option value="">${cBlank}</option>` + opts.map((c) => `<option value="${c.number}">${esc(c.label)}</option>`).join("");
  if ([...cSel.options].some((o) => o.value === cPrev)) cSel.value = cPrev;
  cSel.disabled = !meshy || !width;

  // SSID preview: what the node will actually broadcast.
  const ssidIn = $('[data-k="ssid"]', el);
  const base = ssidIn.value.trim() || b.current_ssid || "AREDN";
  const ch = cSel.value !== "" ? +cSel.value : cur?.channel;
  const bw = wSel.value ? +wSel.value : cur?.width;
  ssidIn.disabled = !meshy;
  $(".suffix", el).textContent = meshy && bw ? `→ ${ssidFull(base, mode, ch ?? "?", bw, b.band)}` : "";
  const badSsid = ssidIn.value && !/^[A-Za-z0-9_.-]{1,20}$/.test(ssidIn.value.trim());

  hint.textContent = badSsid ? "SSID: up to 20 letters, digits, - _ or . (the node adds the rest)."
    : !meshy && mode !== "off" && mode ? `${MODE_NAMES[mode] || "This"} radios aren't changed here; choose a mesh purpose to use this radio for the mesh.`
    : meshy && !width ? "These nodes use different widths; choose a width to see channels."
    : meshy && !opts.length ? "No channel is available at this width on every node here."
    : "";
}

function bandValues(el) {
  const num = (s) => (s === "" ? null : +s);
  return {
    band: el._band.band,
    mode: $('[data-k="mode"]', el).value || null,
    width: num($('[data-k="width"]', el).value),
    channel: num($('[data-k="channel"]', el).value),
    ssid: $('[data-k="ssid"]', el).value.trim() || null,
    peer: $('[data-k="peer"]', el).value.trim() || null,
  };
}

// ------------------------------------------------------------------ node-specific settings
async function openNodeEditor(node) {
  const opts = await api("/api/radio/options", { body: { nodes: [node] } });
  if (!opts.bands.length) {
    throw new Error(`Read ${node}'s radios first (select it and press Read radios)`);
  }
  const n = state.topo.nodes.find((x) => x.name === node);
  const custom = n?.custom?.radio || {};
  $("#ne-node").textContent = node;
  $("#ne-note").value = n?.custom?.note || "";
  mountBands($("#ne-bands"), opts.bands, { kind: "node" }, custom);
  $("#ne-remove").classList.toggle("hidden", !n?.custom);
  const d = $("#node-editor");
  $("#ne-cancel").onclick = () => d.close();
  $("#ne-remove").onclick = () => guarded(async () => {
    await api(`/api/overrides/${encodeURIComponent(node)}`, { method: "DELETE" });
    d.close(); toast(`${node} now follows the group`); await loadTopology();
  });
  $("#ne-save").onclick = () => guarded(async () => {
    const radio = {};
    $$("#ne-bands .band").forEach((el) => {
      const v = bandValues(el);
      if (v.mode || v.width !== null || v.channel !== null || v.ssid || v.peer) radio[v.band] = v;
    });
    await api(`/api/overrides/${encodeURIComponent(node)}`, { method: "PUT", body: { radio, note: $("#ne-note").value.trim() } });
    d.close();
    toast(Object.keys(radio).length ? `Saved own settings for ${node}` : `${node} now follows the group`);
    await loadTopology();
  });
  d.showModal();
}

function radioChanges() {
  const out = [];
  for (const el of $$("#radio-bands .band")) {
    const b = el._band;
    const v = bandValues(el);
    const effMode = v.mode || b.current_mode;
    if (!v.mode && v.width === null && v.channel === null && !v.ssid) continue;
    const tableKey = effMode && effMode !== "mesh" ? "managed" : "mesh";
    const w = v.width ?? b.current?.width;
    const keepOk = b.current && (b.channels[tableKey][String(w)] || []).some((c) => c.number === b.current.channel);
    if (MESH.includes(effMode) && v.channel === null && !keepOk && (v.mode || v.width !== null)) {
      throw new Error(`Radio ${b.band_label}: choose a channel for the new settings`);
    }
    out.push(v);
  }
  const anyOwn = [...state.selected].some((n) => state.topo.nodes.find((x) => x.name === n)?.custom);
  if (!out.length && !anyOwn) throw new Error("Choose a purpose, width, channel or SSID to change");
  return out;
}

function radioBody(execute, confirm = "") {
  return { nodes: needSelection(), changes: radioChanges(), revert_after: +$("#ra-revert").value || 600, execute, confirm };
}

$("#btn-read-radios").addEventListener("click", () => guarded(async () => {
  const nodes = radioOpts?.missing?.length ? radioOpts.missing : needSelection();
  const job = await api("/api/radios/read", { body: { nodes } });
  toast("Reading radios…");
  followJob(job.id, { quiet: true, onDone: async () => { await loadTopology(); await loadRadioPanel(); } });
}));
$("#btn-ra-dry").addEventListener("click", () => guarded(async () => {
  followJob((await api("/api/radio", { body: radioBody(false) })).id, { onDone: () => loadTopology() });
}));
$("#btn-ra-go").addEventListener("click", (e) => guarded(async () => {
  const b = radioBody(false);
  const btn = e.currentTarget;
  busy(btn, true);
  let review;
  try { review = await runAndWait("/api/radio", b, "Reading every selected node's radios…"); } finally { busy(btn, false); }
  const r = review.result;
  const changing = r.clusters.flatMap((c) => c.nodes).filter((n) => n.status === "would-change").length;
  if (!changing && !r.blocked.length) { toast("Nothing to change: the selected nodes are already set that way"); return; }
  const ok = await confirmDialog("Review: radio changes", radioLedger(r), "switch",
    { wide: true, blocked: r.blocked.length > 0 });
  if (!ok) return;
  const job = await api("/api/radio", { body: { ...b, execute: true, confirm: "switch", reviewed_job: review.id } });
  followJob(job.id, { onDone: () => loadTopology() });
}));

// Routing protocol from the node's firmware version (OLSR-only nodes need a dual-protocol
// release before Babel-only firmware; Babel nodes are the norm, so they get no badge).
const ROUTING_BADGE = {
  olsr: '<span class="badge olsr" title="Speaks only OLSR (firmware before 3.25.5). Babel-only nodes can\'t see it. Upgrade it to an OLSR + Babel release (3.25.5.0 or later 3.25.x) before any Babel-only firmware.">OLSR</span>',
  dual: '<span class="badge dual" title="Speaks OLSR and Babel (3.25.5 to 3.25.10): the bridge between old and new nodes">OLSR+Babel</span>',
  unknown: '<span class="badge unknown" title="Nightly firmware: routing protocol can\'t be told from the version">routing?</span>',
};

// ------------------------------------------------------------------ node settings (from node dialogs)
let settingsInfo = null;

async function loadSettingsPanel() {
  const nodes = [...state.selected];
  const box = $("#settings-rows");
  const miss = $("#settings-missing");
  if (!nodes.length) {
    settingsInfo = null;
    miss.classList.add("hidden");
    box.innerHTML = '<p class="hint">Select nodes to see their settings.</p>';
    return;
  }
  const prev = settingsChoices();
  settingsInfo = await api("/api/settings/summary", { body: { nodes } });
  const need = [...settingsInfo.missing, ...settingsInfo.stale];
  miss.classList.toggle("hidden", !need.length);
  miss.querySelector("span").textContent = need.length
    ? `Settings not read yet${settingsInfo.stale.length ? " (or read on other firmware)" : ""} for ${need.slice(0, 3).join(", ")}${need.length > 3 ? "…" : ""}.` : "";
  if (!settingsInfo.rows.length) {
    box.innerHTML = '<p class="hint">Read settings to see the switches these nodes offer.</p>';
    return;
  }
  const n = settingsInfo.read.length;
  box.innerHTML = `<table class="settings"><tbody>${settingsInfo.rows.map((r) => {
    const notBulk = Object.keys(r.not_bulk).length === r.nodes.length;
    const why = Object.values(r.not_bulk)[0] || "";
    const counts = [`on ${r.on.length}`, `off ${r.off.length}`].concat(r.missing.length ? [`not on ${r.missing.length}`] : [])
      .concat(r.disabled.length ? [`disabled on ${r.disabled.length}`] : []).join(" · ");
    const tip = [r.descriptions.join(" / "), r.help, `Setting: ${r.key}`, `Dialog: ${r.dialogs.join(", ")}`,
      r.on.length ? `On: ${r.on.join(", ")}` : "", r.off.length ? `Off: ${r.off.join(", ")}` : "",
      r.missing.length ? `Not in the firmware of: ${r.missing.join(", ")}` : ""].filter(Boolean).join("\n");
    const v = prev[r.key] ?? "";
    return `<tr title="${esc(tip)}"><td><b>${esc(r.labels.join(" / "))}</b><div class="muted small">${esc(counts)} of ${n}</div>
      ${notBulk ? `<div class="muted small">Set on the node: ${esc(why)}</div>` : ""}</td>
      <td><select data-setting="${esc(r.key)}" ${notBulk ? "disabled" : ""}>
        <option value="" ${v === "" ? "selected" : ""}>Leave</option>
        <option value="on" ${v === "on" ? "selected" : ""}>On</option>
        <option value="off" ${v === "off" ? "selected" : ""}>Off</option></select></td></tr>`;
  }).join("")}</tbody></table>`;
}

function settingsChoices() {
  const out = {};
  document.querySelectorAll("#settings-rows select[data-setting]").forEach((s) => { if (s.value) out[s.dataset.setting] = s.value; });
  return out;
}

function settingsBody(execute, confirm = "") {
  const ch = Object.entries(settingsChoices()).map(([key, v]) => ({ key, on: v === "on" }));
  if (!ch.length) throw new Error("Choose On or Off for at least one setting");
  return { nodes: needSelection(), changes: ch, execute, confirm };
}

function labelFor(key) {
  return (settingsInfo?.rows || []).find((r) => r.key === key)?.labels.join(" / ") || key;
}

function settingsLedger(r) {
  const word = (b) => (b === true ? "on" : b === false ? "off" : "–");
  let changing = 0;
  const rows = r.results.map((x, i) => {
    const head = `<td>${i + 1}</td><td><b>${esc(x.node)}</b></td>`;
    if (x.status === "failed") return `<tr>${head}<td colspan="2"></td><td class="st-blocked">BLOCKED</td><td class="why">${esc(x.detail)}</td></tr>`;
    if (x.status === "would-change") changing += 1;
    return (x.steps || []).map((s, j) => {
      const st = s.status === "change" ? '<span class="st-go">will change</span>'
        : s.status === "unchanged" ? '<span class="st-skip">already set</span>'
        : `<span class="st-skip">skipped</span>`;
      const val = s.status === "change" || s.status === "unchanged"
        ? cell(word(s.before), word(s.after)) : `<td class="same">${esc(word(s.before))}</td>`;
      return `<tr>${j === 0 ? head : "<td></td><td></td>"}<td>${esc(s.label)}</td>${val}<td>${st}</td><td class="why">${esc(s.detail || "")}</td></tr>`;
    }).join("");
  }).join("");
  return `<p class="summary-line"><b>${changing}</b> node(s) will change, one at a time, outermost first (the node this computer connects through last). Each change uses the node's own save and commit steps and is read back before the next node; the run stops at the first failure.</p>
    <table class="ledger"><thead><tr><th>#</th><th>Node</th><th>Setting (node's label)</th><th>Now → after</th><th>Status</th><th></th></tr></thead><tbody>${rows}</tbody></table>
    ${r.blocked.length ? `<p class="st-blocked">Blocked: ${esc(r.blocked.join(", "))}. Fix these (or deselect them) and review again.</p>` : ""}`;
}

$("#btn-read-settings").addEventListener("click", () => guarded(async () => {
  const nodes = settingsInfo && (settingsInfo.missing.length || settingsInfo.stale.length)
    ? [...settingsInfo.missing, ...settingsInfo.stale] : needSelection();
  const job = await api("/api/settings/read", { body: { nodes } });
  toast("Reading settings…");
  followJob(job.id, { quiet: true, onDone: () => loadSettingsPanel() });
}));
$("#btn-se-dry").addEventListener("click", () => guarded(async () => {
  followJob((await api("/api/settings", { body: settingsBody(false) })).id, { onDone: () => loadSettingsPanel() });
}));
$("#btn-se-go").addEventListener("click", (e) => guarded(async () => {
  const b = settingsBody(false);
  const btn = e.currentTarget;
  busy(btn, true);
  let review;
  try { review = await runAndWait("/api/settings", b, "Reading every selected node's settings…"); } finally { busy(btn, false); }
  const r = review.result;
  if (!r.results.some((x) => x.status === "would-change") && !r.blocked.length) {
    toast("Nothing to change: the selected nodes are already set that way (or don't have these switches)");
    return;
  }
  const ok = await confirmDialog("Review: settings changes", settingsLedger(r), "apply", { wide: true, blocked: r.blocked.length > 0 });
  if (!ok) return;
  const job = await api("/api/settings", { body: { ...b, execute: true, confirm: "apply", reviewed_job: review.id } });
  followJob(job.id, { onDone: () => loadSettingsPanel() });
}));

// ------------------------------------------------------------------ review (dry run -> ledger)
async function runAndWait(path, body, label) {
  const job = await api(path, { body });
  toast(label);
  for (;;) {
    await new Promise((r) => setTimeout(r, 1000));
    const j = await api(`/api/jobs/${job.id}`);
    if (j.status !== "running") {
      loadJobs();
      if (j.status !== "done" || !j.result) throw new Error(`The check didn't finish (${j.status}); see Activity`);
      return { id: job.id, result: j.result };
    }
  }
}

function busy(btn, on) { btn.disabled = on; btn.dataset.label ||= btn.textContent; btn.textContent = on ? "Checking…" : btn.dataset.label; }

function upgradeLedger(r) {
  const order = { "would-upgrade": 0, skipped: 1, failed: 2 };
  const byWave = {};
  r.results.forEach((x) => (byWave[x.wave ?? "–"] ||= []).push(x));
  const go = r.results.filter((x) => x.status === "would-upgrade").length;
  const rows = Object.keys(byWave).sort((a, b) => a - b).map((w) =>
    `<tr class="group"><td colspan="6">Wave ${esc(w)}${byWave[w].some((x) => x.node === state.topo.root) ? " (the node this computer connects through goes last)" : ""}</td></tr>` +
    byWave[w].sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3)).map((x) => {
      const st = x.status === "would-upgrade" ? '<span class="st-go">will upgrade</span>'
        : x.status === "skipped" ? '<span class="st-skip">already current</span>'
        : '<span class="st-blocked">BLOCKED</span>';
      return `<tr><td><b>${esc(x.node)}</b>${x.routing_label ? `<div class="muted small">${esc(x.routing_label)}</div>` : ""}</td><td>${esc(x.model)}</td><td>${esc(x.profile || "")}</td>
        <td class="${x.from_version === x.to_version ? "same" : "chg"}">${esc(x.from_version || "?")}<span class="arrow">→</span><b>${esc(x.to_version)}</b></td>
        <td>${st}</td><td class="why">${x.status === "failed" ? esc(x.detail) : ""}${(x.notes || []).map((n) => `<div class="lost">⚠ ${esc(n)}</div>`).join("")}</td></tr>`;
    }).join("")).join("");
  const warns = [...(r.warnings || []), ...(r.unreachable?.length ? ["Skipped, unreachable: " + r.unreachable.join(", ")] : [])]
    .map((w) => `<p class="warn">⚠ ${esc(w)}</p>`).join("");
  const apFirst = (r.ptmp_order || []).map(([a, s]) => `${a} before ${s}`).join(", ");
  return `<p class="summary-line"><b>${go}</b> node(s) will be upgraded to <b>${esc(r.version)}</b> (${esc(r.channel)}; routing: ${esc(r.target_routing || "?")}), outermost first, one wave at a time. Each wave is checked before the next starts, and the run stops at the first failure.</p>
    <p class="summary-line">Before each node is flashed, its configuration is backed up to <code>${esc(r.backup_dir)}</code> (it can include the node's password hash and Wi-Fi keys; it stays on this computer).</p>
    ${apFirst ? `<p class="summary-line">PtMP access points go before their Mesh Stations: ${esc(apFirst)}.</p>` : ""}
    ${r.rc ? `<p class="summary-line"><b>Release-candidate checks are on:</b> Mesh Stations wait for their access point to run 4.26.7.0 or the RC; AC radios using channels 48, 52, 56 or 64 (also through a wide channel) are blocked; IPQ40xx nodes on 4.26.7.0 are flagged for a possible power cycle.</p>` : ""}
    <table class="ledger"><thead><tr><th>Node</th><th>Model</th><th>Image</th><th>Firmware</th><th>Status</th><th></th></tr></thead><tbody>${rows}</tbody></table>${warns}
    ${r.blocked.length ? `<p class="st-blocked">Blocked: ${esc(r.blocked.join(", "))}. Fix these (or deselect them) and review again.</p>` : ""}`;
}

function cell(before, after) {
  if (before === after) return `<td class="same">${esc(after ?? "–")}</td>`;
  return `<td class="chg">${esc(before ?? "–")}<span class="arrow">→</span><b>${esc(after ?? "–")}</b></td>`;
}

function radioLedger(r) {
  let changing = 0;
  const body = r.clusters.map((c) => {
    const rows = c.nodes.map((n) => {
      const own = n.own ? ' <span class="own">own settings</span>' : "";
      if (n.status === "failed") {
        return `<tr><td><b>${esc(n.node)}</b>${own}</td><td colspan="5"></td><td class="st-blocked">BLOCKED</td><td class="why">${esc(n.detail)}</td></tr>`;
      }
      if (!n.ledger.length || n.status === "unchanged") {
        return `<tr><td><b>${esc(n.node)}</b>${own}</td><td colspan="5" class="same">${esc(n.detail || "no change")}</td><td class="st-skip">unchanged</td><td></td></tr>`;
      }
      changing += 1;
      return n.ledger.map((l, i) => `<tr>
        <td>${i === 0 ? `<b>${esc(n.node)}</b>${own}` : ""}</td><td>${esc(l.band_label)}</td>
        ${cell(l.before.mode, l.after.mode)}${cell(l.before.channel, l.after.channel)}${cell(l.before.width, l.after.width)}${cell(l.before.ssid, l.after.ssid)}
        <td>${i === 0 ? '<span class="st-go">will change</span>' : ""}</td>
        <td class="lost">${i === 0 && n.lost_links.length ? "will lose RF link to " + esc(n.lost_links.join(", ")) + " (intended)" : ""}</td></tr>`).join("");
    }).join("");
    return `<tr class="group"><td colspan="8">RF cluster ${c.index}: these nodes switch at the same moment</td></tr>${rows}`;
  }).join("");
  const warns = (r.warnings || []).map((w) => `<p class="warn">⚠ ${esc(w)}</p>`).join("");
  return `<p class="summary-line"><b>${changing}</b> node(s) will change. Nodes reboot if AREDN needs it, and each puts its old settings back unless every node in its cluster is seen on the new settings with the RF links it should have.</p>
    <table class="ledger"><thead><tr><th>Node</th><th>Radio</th><th>Purpose</th><th>Channel</th><th>Width</th><th>SSID</th><th>Status</th><th></th></tr></thead><tbody>${body}</tbody></table>${warns}
    ${r.blocked.length ? `<p class="st-blocked">Blocked: ${esc(r.blocked.join(", "))}. Fix these (or deselect them) and review again.</p>` : ""}`;
}

// ------------------------------------------------------------------ confirm dialog
function confirmDialog(title, html, word, { wide = false, blocked = false } = {}) {
  const d = $("#confirm");
  d.classList.toggle("wide", wide);
  $("#cf-title").textContent = title;
  $("#cf-body").innerHTML = html;
  $("#cf-word").textContent = word;
  const input = $("#cf-input"), ok = $("#cf-ok");
  input.value = "";
  ok.disabled = true;
  input.disabled = blocked;
  input.placeholder = blocked ? "Fix the blocked nodes first" : "";
  input.oninput = () => (ok.disabled = blocked || input.value.trim().toLowerCase() !== word);
  return new Promise((resolve) => {
    d.onclose = () => resolve(d.returnValue === "ok" && !ok.disabled);
    d.showModal();
    input.focus();
  });
}

// ------------------------------------------------------------------ map
function drawMap() {
  const svg = $("#map");
  const { nodes, links, root } = state.topo;
  $("#empty-map").classList.toggle("hidden", nodes.length > 0);
  if (!nodes.length) { svg.innerHTML = ""; return; }
  const W = svg.clientWidth || 900, H = svg.clientHeight || 500;
  const cols = {};
  nodes.forEach((n) => (cols[n.hop ?? "x"] ||= []).push(n));
  const keys = Object.keys(cols).sort((a, b) => (a === "x") - (b === "x") || a - b);
  const pos = {};
  keys.forEach((k, ci) => {
    const col = cols[k].sort((a, b) => a.name.localeCompare(b.name));
    const x = keys.length === 1 ? W / 2 : 60 + (ci * (W - 260)) / (keys.length - 1);
    col.forEach((n, ri) => (pos[n.name] = { x, y: ((ri + 1) * H) / (col.length + 1) }));
  });
  const ln = links.filter((l) => pos[l.a] && pos[l.b]).map((l) =>
    `<line class="${esc(l.kind)}" x1="${pos[l.a].x}" y1="${pos[l.a].y}" x2="${pos[l.b].x}" y2="${pos[l.b].y}"/>`).join("");
  const nd = nodes.map((n) => {
    const p = pos[n.name];
    const cls = ["node", state.selected.has(n.name) && "sel", n.cuts_off.length && "cut", n.name === root && "root"].filter(Boolean).join(" ");
    return `<g class="${cls}" data-node="${esc(n.name)}" transform="translate(${p.x},${p.y})">
      <title>${esc(n.name)}\n${esc(n.model)}\n${esc(n.firmware)}${n.cuts_off.length ? "\nOnly path to: " + esc(n.cuts_off.join(", ")) : ""}</title>
      <circle r="11"/><text x="16" y="-2">${esc(n.name)}</text><text class="fw" x="16" y="12">${esc(n.firmware)}</text></g>`;
  }).join("");
  svg.innerHTML = ln + nd;
}
$("#map").addEventListener("click", (e) => { const g = e.target.closest("g[data-node]"); if (g) toggle(g.dataset.node); });
window.addEventListener("resize", () => $("#tab-map").classList.contains("active") && drawMap());

// ------------------------------------------------------------------ firmware
async function loadFirmware() {
  const [loc, info] = await Promise.all([api("/api/firmware/local"), api("/api/info")]);
  state.localFw = loc.releases;
  $("#fw-empty").classList.toggle("hidden", loc.releases.length > 0);
  const byChannel = (a, b) => (a.channel === "nightly") - (b.channel === "nightly") || a.channel.localeCompare(b.channel);
  $("#fw-table tbody").innerHTML = loc.releases.slice().sort(byChannel).map((r) =>
    `<tr><td>${esc(r.channel)}</td><td><b>${esc(r.version)}</b></td><td>${r.profiles}</td><td>${r.images}</td></tr>`).join("");
  $("#fw-dir").textContent = "Stored in " + info.firmware_dir;
  const sel = $("#up-version"), cur = sel.value;
  // Stable releases first, newest on top; nightlies last and clearly labelled. Default = newest stable.
  const isStable = (v) => /^\d+(\.\d+){3}$/.test(v);
  const ordered = loc.releases.slice().sort((a, b) =>
    isStable(a.version) !== isStable(b.version) ? (isStable(a.version) ? -1 : 1)
      : isStable(a.version) ? cmpVer(b.version, a.version) : (a.version < b.version ? 1 : -1));
  sel.innerHTML = ordered.map((r) => `<option value="${esc(r.version)}">${esc(r.version)}${isStable(r.version) ? "" : " — nightly (testing only)"}</option>`).join("")
    || '<option value="">— none downloaded —</option>';
  if (cur && loc.releases.some((r) => r.version === cur)) sel.value = cur;
  renderNodes();
}
async function checkChannels() {
  try {
    const c = await api(`/api/firmware/channels?stable=${+$("#fw-stable").value || 2}&nightly=${$("#fw-nightly").checked}`);
    $("#fw-channels").innerHTML = "Available now: " + c.channels.map((x) => `<b>${esc(x.version)}</b> (${esc(x.name)})`).join(", ");
  } catch (e) {
    const el = $("#fw-channels");
    el.textContent = "Can't reach the AREDN download server from this computer right now. Downloaded firmware still works.";
    el.title = e.message;
  }
}
$("#fw-stable").addEventListener("change", checkChannels);
$("#fw-nightly").addEventListener("change", checkChannels);
$("#btn-fetch").addEventListener("click", () => guarded(async () => {
  const scope = $('input[name="scope"]:checked').value;
  const boards = $("#fw-boards").value.split(/[\s;]+/).map((s) => s.trim()).filter(Boolean);
  if (scope === "board" && !boards.length) throw new Error("Enter at least one board name");
  const job = await api("/api/firmware/fetch", { body: {
    scope, boards, stable: +$("#fw-stable").value || 2, nightly: $("#fw-nightly").checked, include_factory: $("#fw-factory").checked,
    version: $("#fw-version").value.trim() || null,
  } });
  followJob(job.id, { onDone: loadFirmware });
}));

// ------------------------------------------------------------------ credentials
$$('input[name="crkind"]').forEach((r) => r.addEventListener("change", () => {
  const key = $('input[name="crkind"]:checked').value === "key";
  $("#cr-key-wrap").classList.toggle("hidden", !key);
  $("#cr-pw-wrap").classList.toggle("hidden", key);
}));
async function loadCreds() {
  const c = await api("/api/creds");
  const works = {};
  Object.entries(c.known_good).forEach(([n, l]) => (works[l] ||= []).push(n));
  $("#cred-empty").classList.toggle("hidden", c.credentials.length > 0);
  $("#cred-table tbody").innerHTML = c.credentials.map((x) => `<tr>
    <td><b>${esc(x.label)}</b></td><td>${x.kind === "key" ? "SSH key" : "Password"}</td>
    <td class="muted">${esc(x.source)}</td><td class="muted">${esc((works[x.label] || []).join(", ") || "–")}</td>
    <td><button class="ghost" data-del="${esc(x.label)}">Remove</button></td></tr>`).join("");
}
$("#cred-table").addEventListener("click", (e) => {
  const l = e.target.dataset?.del;
  if (l) guarded(async () => { await api("/api/creds/" + encodeURIComponent(l), { method: "DELETE" }); loadCreds(); });
});
$("#btn-cred-add").addEventListener("click", () => guarded(async () => {
  const kind = $('input[name="crkind"]:checked').value;
  await api("/api/creds", { body: { label: $("#cr-label").value.trim(), kind,
    key_path: $("#cr-key").value.trim() || null, password: $("#cr-pw").value || null } });
  $("#cr-pw").value = "";
  $("#cr-label").value = "";
  toast("Login added");
  loadCreds();
}));

// ------------------------------------------------------------------ jobs / activity
async function loadJobs() {
  const { jobs } = await api("/api/jobs");
  $("#job-empty").classList.toggle("hidden", jobs.length > 0);
  $("#job-list").innerHTML = jobs.map((j) => `<li data-job="${j.id}" class="${j.id === state.currentJob ? "active" : ""}">
    <span class="t">${esc(j.title)}</span>
    <span><span class="badge ${j.status}">${j.status}</span> <span class="muted small">${new Date(j.started * 1000).toLocaleTimeString()}</span></span></li>`).join("");
  $("#activity-dot").classList.toggle("hidden", !jobs.some((j) => j.status === "running"));
}
$("#job-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-job]");
  if (li) followJob(li.dataset.job, { switchTab: false });
});

function followJob(id, { onDone, switchTab = true, quiet = false } = {}) {
  state.currentJob = id;
  state.jobCursor = 0;
  $("#log").innerHTML = "";
  if (switchTab && !quiet) showTab("activity");
  clearTimeout(state.pollTimer);
  const tick = async () => {
    let j;
    try { j = await api(`/api/jobs/${id}?since=${state.jobCursor}`); } catch (e) { toast(e.message, true); return; }
    if (state.currentJob !== id) return;
    state.jobCursor = j.next;
    const log = $("#log");
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.insertAdjacentHTML("beforeend", j.events.map((ev) =>
      `<div class="${esc(ev.level)}"><span class="ts">${new Date(ev.t * 1000).toLocaleTimeString()}</span>  ${esc(ev.msg)}</div>`).join(""));
    if (atBottom) log.scrollTop = log.scrollHeight;
    $("#job-title").textContent = j.title;
    const st = $("#job-status");
    st.textContent = j.status + (j.stop_requested && j.status === "running" ? " (stopping)" : "");
    st.className = "badge " + j.status;
    $("#btn-log-copy").disabled = $("#btn-log-save").disabled = false;
    $("#btn-log-copy").onclick = () => guarded(async () => {
      const r = await fetch(`/api/jobs/${id}/log.txt`, { credentials: "same-origin" });
      await copyText(await r.text());
      toast("Log copied to the clipboard");
    });
    $("#btn-log-save").onclick = () => guarded(async () => {
      const r = await api(`/api/jobs/${id}/save`, { method: "POST" });
      toast(`Saved to ${r.path}`);
    });
    $("#btn-stop").classList.toggle("hidden", j.status !== "running");
    $("#btn-stop").onclick = () => guarded(() => api(`/api/jobs/${id}/stop`, { method: "POST" }));
    loadJobs();
    if (j.status === "running") state.pollTimer = setTimeout(tick, 1000);
    else {
      if (quiet) toast(j.status === "done" ? "Done" : `Job ${j.status}`, j.status !== "done");
      onDone && onDone(j);
    }
  };
  tick();
}

// ------------------------------------------------------------------ boot
(async () => {
  try {
    const info = await api("/api/info");
    $("#version").textContent = "v" + info.version;
    $("#portable").classList.toggle("hidden", !info.portable);
  } catch (e) {
    document.body.innerHTML = `<div class="empty"><h2>Not signed in</h2><p>${esc(e.message)}</p></div>`;
    return;
  }
  await Promise.all([loadTopology(), loadFirmware()]);
  checkChannels();
  loadJobs();
  const want = location.hash.slice(1);
  if (want && $(`#tab-${want}`)) showTab(want);
})();

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return; } catch (_) { /* fall back below */ }
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  if (!ok) throw new Error("couldn't reach the clipboard; use Save log… instead");
}
