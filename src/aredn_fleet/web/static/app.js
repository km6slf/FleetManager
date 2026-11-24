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
          ${n.reachable ? "" : '<span class="badge failed">unreachable</span>'}</td>
      <td class="model" title="${esc(n.model)}">${esc(n.model)}</td>
      <td class="${old ? "fw-old" : ""}" title="${old ? "older than " + esc(latest) : ""}">${esc(n.firmware)}</td>
      <td>${n.hop ?? "–"}</td>
      <td class="${n.cuts_off.length ? "cut" : "muted"}" title="${esc(n.cuts_off.join(", "))}">${n.cuts_off.length ? n.cuts_off.length + " node" + (n.cuts_off.length > 1 ? "s" : "") : "–"}</td>
      <td class="rf">${rf}</td></tr>`;
  }).join("");
  $("#chk-all").checked = rows.length > 0 && rows.every((n) => state.selected.has(n.name));
  $("#sel-count").textContent = state.selected.size;
  scheduleRadioPanel();
}

function rfCell(n) {
  if (n.radios) {
    return `<div class="chips">${n.radios.map((r) => {
      const mesh = ["mesh", "meshap", "meshptp", "meshsta"].includes(r.mode);
      const detail = mesh ? ` · ${esc(r.label)} · ${r.bandwidth} MHz` : "";
      return `<span class="chip ${r.mode === "off" ? "off" : ""}"><span class="bandtag">${esc(r.band_label)}</span>${esc(r.mode_label)}${detail}</span>`;
    }).join("")}</div>`;
  }
  return n.channel ? `<span class="muted" title="Read radios for every radio's details">ch ${esc(n.channel)} · ${esc(n.chanbw)} MHz</span>` : "";
}

$("#node-table tbody").addEventListener("click", (e) => {
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
$("#btn-up-go").addEventListener("click", () => guarded(async () => {
  const b = upgradeBody(false);
  if (!b.version) throw new Error("Download firmware first (Firmware tab)");
  const p = await api("/api/plan", { body: { nodes: b.nodes, parallel: b.parallel, exclude_tunnels: b.exclude_tunnels } });
  const ok = await confirmDialog(`Upgrade ${b.nodes.length} node(s) to ${b.version}`,
    `${planHtml(p)}<p class="hint">Each wave is verified before the next starts. The run stops at the first failure and never continues toward this computer past it.</p>`, "upgrade");
  if (!ok) return;
  const job = await api("/api/upgrade", { body: upgradeBody(true, "upgrade") });
  followJob(job.id);
}));

// ------------------------------------------------------------------ radio
const MESH = ["mesh", "meshap", "meshptp", "meshsta"];
const MODE_NAMES = { mesh: "Mesh", meshap: "Mesh PtMP", meshptp: "Mesh PtP", meshsta: "Mesh Station", lan: "LAN Hotspot", wan: "WAN Client", off: "Off" };
let radioOpts = null;
let radioTimer = null;

function scheduleRadioPanel() {
  clearTimeout(radioTimer);
  radioTimer = setTimeout(() => guarded(loadRadioPanel), 150);
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
  const miss = radioOpts.missing;
  $("#radio-missing").classList.toggle("hidden", !miss.length);
  $("#radio-missing span").textContent = miss.length
    ? `Radio details not read yet for ${miss.length} selected node${miss.length > 1 ? "s" : ""} (${miss.slice(0, 3).join(", ")}${miss.length > 3 ? "…" : ""}).`
    : "";
  if (!radioOpts.bands.length) {
    box.innerHTML = '<p class="hint">No radio details for the selected nodes yet. Press <b>Read radios</b> (logs in over SSH, read-only).</p>';
    return;
  }
  box.innerHTML = radioOpts.bands.map(bandHtml).join("");
  $$(".band", box).forEach((el) => {
    $$("select", el).forEach((s) => s.addEventListener("change", () => refreshBand(el)));
    refreshBand(el);
  });
}

function nowLine(n, cur) {
  if (!cur) return "";
  const mesh = MESH.includes(cur.mode);
  return `<span><b>${esc(n)}</b>: ${esc(MODE_NAMES[cur.mode] || cur.mode)}${mesh ? ` · ch ${esc(cur.label)} · ${cur.bandwidth} MHz` : ""}</span>`;
}

function bandHtml(b) {
  const names = Object.keys(b.per_node);
  const lines = names.slice(0, 6).map((n) => nowLine(n, b.per_node[n])).join("");
  const more = names.length > 6 ? `<span>…and ${names.length - 6} more</span>` : "";
  const curMode = b.current_mode;
  const keep = curMode ? `Leave as is (${esc(MODE_NAMES[curMode] || curMode)})` : "Leave as is (mixed)";
  const modeOpts = ["mesh", "meshap", "meshsta", "meshptp", "off"]
    .map((m) => `<option value="${m}">${MODE_NAMES[m]}</option>`).join("");
  const without = b.without.length ? `<p class="hint">No ${esc(b.band_label)} radio on: ${esc(b.without.join(", "))}. They're left alone.</p>` : "";
  return `<div class="band" data-band="${esc(b.band)}">
    <div class="band-head"><b>Radio ${esc(b.band_label)}</b><span class="muted small">${names.length} node${names.length > 1 ? "s" : ""}</span></div>
    <div class="band-now">${lines}${more}</div>
    <div class="grid2">
      <label>Purpose <select data-k="mode"><option value="">${keep}</option>${modeOpts}</select></label>
      <label>Channel width <select data-k="width"></select></label>
      <label class="wide">Channel <select data-k="channel"></select></label>
      <label class="wide peer hidden">Peer MAC (Mesh PtP) <input data-k="peer" placeholder="aa:bb:cc:dd:ee:ff"></label>
    </div>
    <p class="hint band-hint"></p>${without}</div>`;
}

function bandData(el) { return radioOpts.bands.find((b) => b.band === el.dataset.band); }

function refreshBand(el) {
  const b = bandData(el);
  const modeSel = $('[data-k="mode"]', el), wSel = $('[data-k="width"]', el), cSel = $('[data-k="channel"]', el);
  const mode = modeSel.value || b.current_mode;           // effective mode after the change
  const meshy = mode && MESH.includes(mode);
  $(".peer", el).classList.toggle("hidden", mode !== "meshptp");
  const hint = $(".band-hint", el);

  // Width: leave as is (when all nodes agree) or any width every node supports.
  const wPrev = wSel.value;
  const curW = b.current ? b.current.width : null;
  wSel.innerHTML = (curW ? `<option value="">Leave as is (${curW} MHz)</option>` : `<option value="">Leave as is</option>`)
    + b.bws.map((w) => `<option value="${w}">${w} MHz</option>`).join("");
  if ([...wSel.options].some((o) => o.value === wPrev)) wSel.value = wPrev;
  wSel.disabled = !meshy;

  // Channel: exactly the node dialog's list for this mode and width, common to all selected nodes.
  const width = wSel.value || (curW ? String(curW) : "");
  const table = b.channels[mode && mode !== "mesh" ? "managed" : "mesh"] || {};
  const opts = width ? (table[width] || []) : [];
  const cPrev = cSel.value;
  const cur = b.current;
  // Current channel stays selectable if it exists at the chosen width/mode on every node.
  const curOk = cur && opts.some((c) => c.number === cur.channel);
  const keepLabel = cur ? (opts.find((c) => c.number === cur.channel)?.label || cur.channel) : null;
  const sameWidth = !wSel.value || +wSel.value === cur?.width;
  cSel.innerHTML = (curOk ? `<option value="">${sameWidth ? "Leave as is" : "Keep"} (${esc(keepLabel)})</option>` : `<option value="">— choose —</option>`)
    + opts.map((c) => `<option value="${c.number}">${esc(c.label)}</option>`).join("");
  if ([...cSel.options].some((o) => o.value === cPrev)) cSel.value = cPrev;
  cSel.disabled = !meshy || !width;

  hint.textContent = !meshy && mode !== "off"
    ? `${MODE_NAMES[mode] || "This"} radios aren't changed here; choose a mesh purpose to use this radio for the mesh.`
    : meshy && !width ? "These nodes use different widths; choose a width to see channels."
    : meshy && !opts.length ? "No channel is available at this width on every selected node."
    : "";
}

function radioChanges() {
  const out = [];
  for (const el of $$("#radio-bands .band")) {
    const b = bandData(el);
    const mode = $('[data-k="mode"]', el).value || null;
    const width = $('[data-k="width"]', el).value ? +$('[data-k="width"]', el).value : null;
    const cRaw = $('[data-k="channel"]', el).value;
    const channel = cRaw === "" ? null : +cRaw;
    const peer = $('[data-k="peer"]', el).value.trim() || null;
    const effMode = mode || b.current_mode;
    if (!mode && width === null && channel === null) continue;
    const tableKey = effMode && effMode !== "mesh" ? "managed" : "mesh";
    const w = width ?? b.current?.width;
    const keepOk = b.current && (b.channels[tableKey][String(w)] || []).some((c) => c.number === b.current.channel);
    if (MESH.includes(effMode) && channel === null && !keepOk) {
      throw new Error(`Radio ${b.band_label}: choose a channel for the new settings`);
    }
    out.push({ band: b.band, mode, width, channel, peer });
  }
  if (!out.length) throw new Error("Choose a purpose, width or channel to change");
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
$("#btn-ra-go").addEventListener("click", () => guarded(async () => {
  const b = radioBody(false);
  const what = b.changes.map((c) => {
    const band = radioOpts.bands.find((x) => x.band === c.band).band_label;
    const bits = [c.mode && MODE_NAMES[c.mode], c.channel !== null && `channel ${c.channel}`, c.width && `${c.width} MHz`].filter(Boolean);
    return `<li>Radio ${esc(band)}: ${esc(bits.join(", "))}</li>`;
  }).join("");
  const ok = await confirmDialog(`Change radios on ${b.nodes.length} node(s)`,
    `<ol>${what}</ol><p>Nodes in each RF cluster switch at the same moment and reboot if AREDN needs it. Each node puts its old settings back after ${b.revert_after}s unless every node in its cluster is seen on the new settings with its RF neighbours.</p><p class="hint">Run the dry run first: it re-reads every radio and checks each choice against that node.</p>`, "switch");
  if (!ok) return;
  followJob((await api("/api/radio", { body: radioBody(true, "switch") })).id, { onDone: () => loadTopology() });
}));

// ------------------------------------------------------------------ confirm dialog
function confirmDialog(title, html, word) {
  const d = $("#confirm");
  $("#cf-title").textContent = title;
  $("#cf-body").innerHTML = html;
  $("#cf-word").textContent = word;
  const input = $("#cf-input"), ok = $("#cf-ok");
  input.value = "";
  ok.disabled = true;
  input.oninput = () => (ok.disabled = input.value.trim().toLowerCase() !== word);
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
