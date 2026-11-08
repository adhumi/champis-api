/* Backoffice Champis (spec › Backoffice, lot BO1) : trois onglets en lecture, sans dépendance.
   La page ne parle qu'à l'API, sur la même origine ; chaque action porte X-Champis-Admin: 1. */
"use strict";

const $ = (id) => document.getElementById(id);
const HEADERS = { "X-Champis-Admin": "1" };
const KINDS = {};
let timers = { health: null, jobs: null, job: null, tools: null, species: null };
let selectedSpecies = null;

async function api(path, options = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...options, headers: { ...HEADERS, ...(options.headers || {}) } });
  if (r.status === 401) { showLogin(); throw new Error("401"); }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) { /* texte brut */ }
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

// ------------------------------------------------------------------ connexion

function showLogin() {
  clearTimers();
  $("login").hidden = false;
  $("tabs").hidden = true;
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = true));
  $("key").focus();
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    const r = await fetch("/admin/login", {
      method: "POST", credentials: "same-origin",
      headers: { ...HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify({ key: $("key").value }),
    });
    if (r.status === 204) { $("key").value = ""; return enter(); }
    $("login-error").textContent = r.status === 429 ? "Trop d'essais : attends une minute." : "Clé refusée.";
  } catch (err) {
    $("login-error").textContent = "Le serveur ne répond pas.";
  }
});

$("logout").addEventListener("click", async () => {
  try { await api("/admin/logout", { method: "POST" }); } catch (_) { /* déjà sorti */ }
  showLogin();
});

async function enter() {
  try { await api("/admin/api/me"); } catch (_) { return; }
  $("login").hidden = true;
  $("tabs").hidden = false;
  showTab(location.hash.replace("#", "") || "health");
}

// ------------------------------------------------------------------ onglets

document.querySelectorAll("#tabs button[data-tab]").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab)));

function clearTimers() { Object.keys(timers).forEach((k) => { clearTimeout(timers[k]); timers[k] = null; }); }

function showTab(name) {
  clearTimers();
  document.querySelectorAll("#tabs button[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== name));
  location.hash = name;
  ({ health: loadHealth, usage: loadUsage, jobs: loadJobs, tools: loadTools, species: loadSpecies })[name]();
}

// ------------------------------------------------------------------ utilitaires d'affichage

const fmt = {
  size(n) { if (n == null) return "—"; for (const [u, d] of [["Go", 1e9], ["Mo", 1e6], ["ko", 1e3]]) if (n >= d) return (n / d).toFixed(n / d < 100 ? 1 : 0).replace(".", ",") + " " + u; return n + " o"; },
  when(iso) { if (!iso) return "jamais"; const d = new Date(iso); return d.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }); },
  ago(iso) { if (!iso) return ""; const h = (Date.now() - new Date(iso)) / 36e5; return h < 1 ? `il y a ${Math.round(h * 60)} min` : h < 48 ? `il y a ${h.toFixed(0)} h` : `il y a ${Math.round(h / 24)} j`; },
  duration(a, b) { if (!a) return "—"; const s = ((b ? new Date(b) : Date.now()) - new Date(a)) / 1000; return s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, "0")} s` : `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} min`; },
  n(v) { return v == null ? "—" : Number(v).toLocaleString("fr-FR"); },
};

function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) { if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v); }
  for (const c of [].concat(children)) if (c != null) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}

function card(title, pairs) {
  const dl = el("dl");
  for (const [k, v] of pairs) if (v !== undefined) { dl.append(el("dt", { text: k }), el("dd", {}, v)); }
  return el("div", { class: "card" }, [el("h3", { text: title }), dl]);
}

function table(node, header, rows, options = {}) {
  node.replaceChildren();
  const tr = el("tr");
  header.forEach((h, i) => tr.append(el("th", { class: options.numeric && i >= options.numeric ? "num" : "" , text: h })));
  node.append(el("thead", {}, tr));
  const body = el("tbody");
  for (const r of rows) {
    const row = el("tr", r.onclick ? { class: "clickable" } : {});
    r.cells.forEach((c, i) => row.append(el("td", { class: options.numeric && i >= options.numeric ? "num" : "" }, c)));
    if (r.onclick) row.addEventListener("click", r.onclick);
    body.append(row);
  }
  node.append(body);
}

function status(s) { return el("span", { class: `status ${s}`, text: s }); }

// ------------------------------------------------------------------ santé

async function loadHealth() {
  let h;
  try { h = await api("/admin/api/health"); } catch (err) { $("health-meta").textContent = `Erreur : ${err.message}`; return; }
  $("health-meta").textContent = `Relevé à ${fmt.when(h.generated)} · version ${h.api.build} · démarrée ${fmt.when(h.api.started_at)} · ${h.api.workers} workers · tuiles en mode ${h.api.tiles_mode} · rafraîchi toutes les 60 s`;
  const alerts = $("alerts");
  alerts.replaceChildren(...(h.alerts.length ? h.alerts.map((a) => el("li", { class: a.level, text: a.text })) : [el("li", { class: "ok", text: "Rien à signaler." })]));

  const c = h.cache, cards = [];
  cards.push(card("Cache météo", [
    ["cellules", `${fmt.n(c.seeded)} amorcées sur ${fmt.n(c.cells)}`],
    ["indice par département", `${fmt.when(c.dept_index_at)} (jour ${c.today || "—"})`],
    ["cache publié importé", c.cache_published ? `${fmt.when(c.cache_published)}, ${c.cache_published_age_h} h` : "jamais"],
    ["météo précalculée", `${fmt.n(c.cell_weather)} entrées, ${fmt.n(c.cell_field)} lignes compactes`],
  ]));
  cards.push(card("Volume", [
    ["dossier", h.disk.path],
    ["libre", `${fmt.size(h.disk.free_bytes)} sur ${fmt.size(h.disk.total_bytes)}`],
    ["cache SQLite", fmt.size(h.disk.cache_db_bytes)],
    ["cache des tuiles", h.tile_cache ? `${fmt.n(h.tile_cache.entries)} plages, ${fmt.size(h.tile_cache.bytes)} sur ${fmt.size(h.tile_cache.max_bytes)}${h.tile_cache.complete ? "" : " (parcours partiel)"}` : "désactivé"],
  ]));
  if (h.database) cards.push(card(`Base (${h.database.dialect})`, h.database.tables
    ? Object.entries(h.database.tables).map(([t, n]) => [t, fmt.n(n)]) : [["erreur", h.database.error]]));
  const jobsPairs = Object.entries(h.jobs).map(([k, j]) => [j.label, j.last
    ? el("span", {}, [status(j.last.status), ` ${fmt.ago(j.last.created_at)}`, j.last.summary ? ` · ${j.last.summary}` : ""])
    : "aucune exécution inscrite"]);
  if (jobsPairs.length) cards.push(card("Dernières tâches", jobsPairs));
  const b = h.bucket;
  if (!b.configured) cards.push(card("Bucket", [["état", "non configuré"]]));
  else if (b.error) cards.push(card("Bucket", [["état", `injoignable (${b.error})`]]));
  else {
    const m = b.manifest;
    cards.push(card("Bucket", [
      ["manifeste des tuiles", m ? `${fmt.when(m.generated)} · ${fmt.n(m.tiles)} fichiers, ${fmt.size(m.bytes)}` : "absent"],
      ["jeux", m ? m.sets.filter((s) => s.depts.length > 1).map((s) => `${s.depts.length} départements : ${Object.entries(s.kinds).map(([k, n]) => `${n} ${k}`).join(", ")}`).join(" · ") + (m.per_dept_sets ? ` · ${m.per_dept_sets} jeux par département` : "") : undefined],
      ["cache publié", b.cache ? `${fmt.when(b.cache.published)} · ${fmt.n(b.cache.cells)} cellules · ${fmt.size(b.cache.bytes)}` : "aucun"],
      ["amorçage publié", b.seed ? `${fmt.when(b.seed.published)} · ${fmt.n(b.seed.history_cells)} cellules` : "aucun"],
    ]));
  }
  if (h.workers && h.workers.length) cards.push(card("Service d'exécution", h.workers.flatMap((w) => [
    [w.name, el("span", {}, [w.silent_min != null && w.silent_min > 2 ? status("manque") : status("ok"), ` battement ${fmt.ago(w.heartbeat_at)}`])],
    ["volume", w.disk_free_mb != null ? `${fmt.size(w.disk_free_mb * 1e6)} libres sur ${fmt.size(w.disk_total_mb * 1e6)}` : "—"],
    ["tâche en cours", w.current_job ? el("a", { href: "#jobs", text: w.current_job.slice(0, 8) }) : "aucune"],
    ["version", w.build || "—"],
  ])));
  else cards.push(card("Service d'exécution", [["état", "aucun battement : le service champis-jobs n'a pas encore tourné"]]));
  const days = Object.entries(h.traffic);
  cards.push(card("Trafic (7 jours)", days.length
    ? days.map(([d, v]) => [d, `${fmt.n(v.http_429 || 0)} × 429 · ${fmt.n(v.http_5xx || 0)} × 5xx`]) : [["relevé", "rien"]]));
  $("health-cards").replaceChildren(...cards);
  table($("checks"), ["état", "point", "détail"], h.checks.map((k) => ({ cells: [status(k.status), k.name, k.hint && k.status !== "ok" ? `${k.detail} — ${k.hint}` : k.detail] })));
  timers.health = setTimeout(loadHealth, 60000);
}

// ------------------------------------------------------------------ usage

function sparkline(days, values) {
  const w = 300, hgt = 48, n = days.length, bw = w / n;
  const max = Math.max(1, ...days.map((d) => values[d] || 0));
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${hgt}`); svg.setAttribute("class", "spark"); svg.setAttribute("preserveAspectRatio", "none");
  days.forEach((d, i) => {
    const v = values[d]; const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    const bh = v == null ? 1 : Math.max(1, (v / max) * hgt);
    r.setAttribute("x", (i * bw).toFixed(2)); r.setAttribute("y", (hgt - bh).toFixed(2));
    r.setAttribute("width", Math.max(0.5, bw - 0.6).toFixed(2)); r.setAttribute("height", bh.toFixed(2));
    if (v == null) r.setAttribute("class", "empty");
    const t = document.createElementNS("http://www.w3.org/2000/svg", "title"); t.textContent = `${d} : ${v == null ? "·" : v}`; r.append(t);
    svg.append(r);
  });
  return svg;
}

function renderDevices(u) {
  const d = u.devices;
  const kpi = (v, label) => el("div", {}, [el("strong", { text: fmt.n(v) }), el("span", { text: label })]);
  const cards = [];
  if (!d.enabled) cards.push(el("div", { class: "card" }, [el("h3", { text: "Appareils actifs" }),
    el("p", { class: "meta", text: "Secret CHAMPIS_MEASURE_SECRET absent : les appareils ne sont pas comptés." })]));
  cards.push(el("div", { class: "card" }, [
    el("h3", { text: "Actifs (ont parlé à l'API)" }),
    el("div", { class: "kpis" }, [kpi(d.today, "aujourd'hui"), kpi(d.yesterday, "hier"), kpi(d.week, "7 jours"), kpi(d.month, "30 jours"), kpi(d.unique, "13 mois"), kpi(d.with_coins, "avec coins")]),
    el("p", { class: "meta", text: "Appareils distincts par jour sur 90 jours ; un appareil hors ligne ne compte pas." }),
    sparkline(u.days, d.daily),
  ]));
  $("usage-devices").replaceChildren(...cards);
}

function renderLayers(u) {
  const cards = [];
  for (const [client, label] of [["app", "App"], ["web", "Site"]]) {
    for (const [window_, title] of [["7d", "7 jours"], ["30d", "30 jours"]]) {
      const shares = (u.layers[window_] || {})[client] || {};
      const total = Object.values(shares).reduce((s, n) => s + n, 0);
      const rows = Object.entries(shares).sort((a, b) => b[1] - a[1]);
      const bars = el("div", { class: "bars" });
      for (const [target, n] of rows) {
        const pct = total ? (100 * n) / total : 0;
        bars.append(el("span", { text: target }), el("div", { class: "bar" }, [el("div", { style: `width:${pct.toFixed(1)}%` })]),
          el("span", { class: "pct", text: `${pct.toFixed(0)} % · ${fmt.n(n)}` }));
      }
      cards.push(el("div", { class: "card" }, [
        el("h3", { text: `${label} · ${title}` }),
        el("p", { class: "meta", text: total ? `${fmt.n(total)} requêtes météo` : "rien de relevé" }),
        bars,
      ]));
    }
  }
  $("usage-layers").replaceChildren(...cards);
}

async function loadUsage() {
  let u;
  try { u = await api("/admin/api/usage?days=90"); } catch (err) { $("usage-charts").replaceChildren(el("p", { class: "error", text: err.message })); return; }
  renderDevices(u);
  renderLayers(u);
  const names = Object.keys(u.labels).concat(
    Object.keys(u.counters).filter((n) => !(n in u.labels) && !n.startsWith("layer_")).sort());
  $("usage-charts").replaceChildren(...names.map((name) => {
    const values = u.counters[name] || {}; const days = u.days;
    const total = days.reduce((s, d) => s + (values[d] || 0), 0);
    const last = values[days[days.length - 1]];
    return el("div", { class: "card" }, [
      el("h3", { text: u.labels[name] ? `${u.labels[name]} (${name})` : name }),
      el("p", { class: "meta", text: `90 jours : ${fmt.n(total)} · aujourd'hui : ${last == null ? "·" : fmt.n(last)}` }),
      sparkline(days, values),
    ]);
  }));
  table($("usage-table"), u.table.header, u.table.rows.map((r) => ({ cells: r })), { numeric: 1 });
}

// ------------------------------------------------------------------ tâches

$("jobs-refresh").addEventListener("click", loadJobs);
$("jobs-kind").addEventListener("change", loadJobs);

async function loadJobs() {
  clearTimeout(timers.jobs);
  let data;
  const kind = $("jobs-kind").value;
  try { data = await api(`/admin/api/jobs?limit=100${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`); } catch (err) { table($("jobs-table"), ["erreur"], [{ cells: [err.message] }]); return; }
  Object.assign(KINDS, data.kinds);
  const select = $("jobs-kind");
  if (select.options.length === 1) for (const [k, v] of Object.entries(data.kinds)) select.append(el("option", { value: k, text: v.label }));
  table($("jobs-table"), ["tâche", "origine", "état", "début", "durée", "dernier mot"], data.jobs.map((j) => ({
    cells: [KINDS[j.kind] ? KINDS[j.kind].label : j.kind, j.origin, status(j.status), fmt.when(j.started_at || j.created_at), fmt.duration(j.started_at, j.finished_at), j.summary || j.message || (j.step_name ? `[${j.step_index}/${j.step_total}] ${j.step_name}` : "")],
    onclick: () => showJob(j.id),
  })));
  if (data.jobs.some((j) => j.status === "running" || j.status === "queued")) timers.jobs = setTimeout(loadJobs, 5000);
}

async function showJob(id) {
  clearTimeout(timers.job);
  let d;
  try { d = await api(`/admin/api/jobs/${id}`); } catch (err) { return; }
  const j = d.job;
  $("job-detail").hidden = false;
  $("job-title").textContent = `${KINDS[j.kind] ? KINDS[j.kind].label : j.kind} · ${fmt.when(j.started_at || j.created_at)}`;
  $("job-status").replaceChildren(status(j.status), ` · origine ${j.origin} · ${fmt.duration(j.started_at, j.finished_at)}`, j.worker ? ` · sur ${j.worker}` : "", j.exit_code != null ? ` · code ${j.exit_code}` : "", j.summary ? ` · ${j.summary}` : "");
  const pct = j.progress_total ? Math.min(100, (100 * j.progress_done) / j.progress_total) : (j.status === "ok" ? 100 : j.step_total ? (100 * ((j.step_index || 1) - 1)) / j.step_total : 0);
  $("job-bar").style.width = `${pct}%`;
  $("job-progress").textContent = [
    j.step_name ? `étape ${j.step_index}/${j.step_total} : ${j.step_name}` : "",
    j.progress_total ? `${fmt.n(j.progress_done)} / ${fmt.n(j.progress_total)} ${j.progress_unit || ""}` : "",
    j.message || "",
  ].filter(Boolean).join(" · ");
  $("job-events").replaceChildren(...d.events.map((e) => el("li", { class: e.level }, [el("span", { class: "at", text: new Date(e.at).toLocaleTimeString("fr-FR") }), e.text])));
  const log = $("job-log"); log.hidden = !d.log_url; if (d.log_url) log.href = d.log_url;
  const cancel = $("job-cancel");
  cancel.hidden = !(j.status === "running" || j.status === "queued") || j.cancel_requested;
  cancel.onclick = async () => {
    if (!confirm("Annuler cette tâche ? Les étapes faites restent faites ; relancer reprendra là où ça s'est arrêté.")) return;
    try { await api(`/admin/api/jobs/${id}/cancel`, { method: "POST" }); } catch (err) { alert(err.message); }
    showJob(id);
  };
  $("job-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  if (j.status === "running" || j.status === "queued") timers.job = setTimeout(() => showJob(id), 3000);
}

enter();

// ------------------------------------------------------------------ outils

async function loadTools() {
  clearTimeout(timers.tools);
  let t;
  try { t = await api("/admin/api/tools"); } catch (err) { $("tools-list").replaceChildren(el("p", { class: "error", text: err.message })); return; }
  Object.assign(KINDS, Object.fromEntries(t.tools.map((x) => [x.kind, { label: x.label }])));
  const workers = $("tools-workers");
  workers.replaceChildren(...(t.workers.length ? t.workers.map((w) => {
    const silent = (Date.now() - new Date(w.heartbeat_at)) / 60000;
    return el("li", { class: silent > 2 ? "warn" : "ok" }, `Service ${w.name} : battement ${fmt.ago(w.heartbeat_at)}${w.disk_free_mb != null ? `, ${fmt.size(w.disk_free_mb * 1e6)} libres` : ""}${w.current_job ? `, tâche en cours ${w.current_job.slice(0, 8)}` : ", libre"}`);
  }) : [el("li", { class: "warn", text: "Aucun service d'exécution n'a encore battu : une tâche lancée attendra qu'il démarre." })]));
  const queue = $("tools-queue");
  queue.replaceChildren();
  if (t.queue.length) {
    const tbl = el("table", { class: "queue" });
    table(tbl, ["file", "sorte", "origine", "état", "depuis", "dernier mot"], t.queue.map((j) => ({
      cells: [j.id.slice(0, 8), KINDS[j.kind] ? KINDS[j.kind].label : j.kind, j.origin, status(j.status), fmt.ago(j.started_at || j.created_at), j.message || (j.step_name ? `[${j.step_index}/${j.step_total}] ${j.step_name}` : "")],
      onclick: () => { showTab("jobs"); showJob(j.id); },
    })));
    queue.append(tbl);
  }
  $("tools-list").replaceChildren(...t.tools.map((tool) => toolCard(tool, t.queue)));
  loadArtifacts();
  if (t.queue.length) timers.tools = setTimeout(loadTools, 5000);
}

async function loadArtifacts() {
  let a;
  try { a = await api("/admin/api/artifacts"); } catch (err) { table($("tools-artifacts"), ["sorties"], [{ cells: [err.message] }]); return; }
  table($("tools-artifacts"), ["fichier", "produit le", "taille", "commande", "sha256"], a.artifacts.length
    ? a.artifacts.map((f) => ({ cells: [el("a", { href: f.url, text: f.name }), fmt.when(f.generated), fmt.size(f.bytes), f.command || "", (f.sha256 || "").slice(0, 12)] }))
    : [{ cells: ["aucune", "", "", "", ""] }]);
}

function toolCard(tool, queue) {
  const busy = queue.find((j) => j.kind === tool.kind);
  const form = el("form");
  for (const f of tool.fields) {
    let input;
    if (f.kind === "bool") { input = el("input", { type: "checkbox", name: f.name }); input.checked = !!f.default; }
    else if (f.kind === "depts") { input = el("input", { type: "text", name: f.name, value: f.default, placeholder: "all, ou 25 39 70", size: "18" }); }
    else if (f.kind === "number") { input = el("input", { type: "number", name: f.name, step: "any", value: f.default }); if (f.min != null) input.min = f.min; if (f.max != null) input.max = f.max; }
    else { input = el("select", { name: f.name }); for (const c of f.choices || []) input.append(el("option", { value: c, text: c })); input.value = String(f.default); }
    form.append(el("label", {}, [el("span", { text: f.label }), input]));
  }
  const button = el("button", { type: "submit", text: busy ? `déjà ${busy.status}` : "Lancer" });
  if (busy && tool.exclusive) button.disabled = true;
  const result = el("span", { class: "meta" });
  form.append(el("div", { class: "row" }, [button, result]));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const args = {};
    for (const f of tool.fields) {
      const input = form.elements[f.name];
      args[f.name] = f.kind === "bool" ? input.checked : input.value;
    }
    if (tool.confirm && (tool.kind !== "publish" || (args.prune && !args.dry_run)) && !confirm(tool.confirm)) return;
    button.disabled = true;
    try {
      const r = await api("/admin/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: tool.kind, args }) });
      result.textContent = `en file (${r.id.slice(0, 8)})`;
      result.className = "meta ok";
      setTimeout(loadTools, 800);
    } catch (err) { result.textContent = err.message; result.className = "error"; button.disabled = false; }
  });
  return el("div", { class: "card tool" }, [
    el("h3", { text: tool.label }),
    el("p", { class: "meta", text: `${tool.duration} · ${tool.needs}${tool.exclusive ? " · une seule à la fois" : ""}${tool.min_free_gb ? ` · ${tool.min_free_gb} Go libres requis` : ""}` }),
    form,
  ]);
}

// ------------------------------------------------------------------ espèces (lot BO5)

const STATES = { a_jour: "à jour", perimee: "périmée", jamais_publiee: "jamais publiée", en_cours: "en cours" };

async function launch(kind, args) {
  return api("/admin/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, args }) });
}

async function loadSpecies() {
  clearTimeout(timers.species);
  let d;
  try { d = await api("/admin/api/species"); } catch (err) { table($("species-table"), ["erreur"], [{ cells: [err.message] }]); return; }
  const zoomSelect = $("species-zoom");
  if (!zoomSelect.options.length) for (const z of [9, 10]) zoomSelect.append(el("option", { value: z, text: `${z} (${z === 9 ? "≈ A3" : "≈ A1"})` }));
  $("species-meta").textContent = [
    d.manifest_generated ? `jeu publié du ${fmt.when(d.manifest_generated)}` : "aucun jeu publié",
    d.inventory_at ? `volume du worker relevé ${fmt.ago(d.inventory_at)}` : "worker muet : état du volume inconnu",
    d.grid.current ? "grille météo à jour" : "grille météo à refaire",
  ].join(" · ");
  $("species-render-all").onclick = async (e) => {
    e.target.disabled = true;
    try { await launch("render", { species: "all", zoom: zoomSelect.value, force: false }); } catch (err) { alert(err.message); }
    setTimeout(loadSpecies, 800);
  };
  const share = (u) => (u.app_share == null && u.web_share == null) ? "·" : `${u.app_share == null ? "·" : u.app_share + " %"} app · ${u.web_share == null ? "·" : u.web_share + " %"} site`;
  table($("species-table"), ["espèce", "groupe", "état", "usage 30 j (espèce et groupe)"], d.species.map((sp) => ({
    cells: [el("b", { text: sp.name }), sp.group.name, el("span", { class: `state ${sp.state}`, text: STATES[sp.state] || sp.state }), share(sp.usage)],
    onclick: () => { selectedSpecies = sp.id; renderSheet(d); },
  })));
  if (!selectedSpecies && d.species.length) selectedSpecies = d.species[0].id;
  renderSheet(d);
  if (d.species.some((sp) => sp.jobs.length)) timers.species = setTimeout(loadSpecies, 5000);
}

function renderSheet(d) {
  const sp = d.species.find((x) => x.id === selectedSpecies);
  const sheet = $("species-sheet");
  document.querySelectorAll("#species-table tbody tr").forEach((tr, i) => tr.classList.toggle("selected", d.species[i] && d.species[i].id === selectedSpecies));
  if (!sp) { sheet.hidden = true; return; }
  sheet.hidden = false;
  const busy = sp.jobs.length > 0;
  const pub = sp.published, w = sp.worker;
  const block = (dot, title, detail, label, action, disabled) => {
    const b = el("div", { class: "block" }, [el("span", { class: `dot ${dot}` }), el("b", { text: title }), el("span", { class: "meta", text: detail })]);
    if (label) {
      const btn = el("button", { class: "quiet", text: label });
      btn.disabled = !!disabled || busy;
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try { await action(); } catch (err) { alert(err.message); }
        setTimeout(loadSpecies, 800);
      });
      b.append(btn);
    }
    return b;
  };
  const groupSpecies = sp.group.species.join(" ");
  const blocks = [];
  blocks.push(block("ok", "Fichier", `species/${sp.id}.json · schéma ${sp.schema_version} · empreinte ${sp.hash} · about ${sp.about ? "présent" : "absent"}`, "voir", () => { window.open(`https://github.com/adhumi/champis/blob/main/species/${sp.id}.json`, "_blank"); }));
  const tDot = !w ? "" : w.t_current >= w.depts ? "ok" : "warn";
  blocks.push(block(tDot, "Terrain sur le worker", w ? `T à jour ${fmt.n(w.t_current)} / ${fmt.n(w.depts)} départements · ${fmt.n(w.t_stale)} périmés · dernier batch ${w.batch_last ? fmt.when(w.batch_last) : "jamais"}` : "volume inconnu : le worker n'a pas encore battu", "Recalculer le terrain", () => launch("batch", { depts: "all", species: sp.id, jobs: 2, clean_raw: true, force: false }), !w));
  const tilesDetail = pub && pub.hash ? `publié ${pub.hash}${pub.hash === sp.hash ? "" : " (périmé)"} · ${pub.depts} dép. · ${fmt.size(pub.bytes)} · ${pub.dept_sets} jeux par département · G ${sp.group.name} ${pub.group_current ? "à jour" : "à refaire"}` : "aucun jeu France publié";
  const tilesDot = !pub || !pub.hash ? "" : pub.hash === sp.hash && pub.group_current ? "ok" : "warn";
  blocks.push(block(tilesDot, "Tuiles", `${tilesDetail}${w ? ` · sur le worker : ${w.tiles_current} à jour, ${w.tiles_stale} périmés` : ""}`, "Construire les tuiles", () => launch("tiles", { depts: "all", species: groupSpecies, jobs: 2, per_dept: true, groups: true, force: false }), !w || !w.t_current));
  const canPublish = !!(w && w.tiles_current);
  blocks.push(block(canPublish ? "ok" : "", "Publication", "les PMTiles du worker vers le bucket · d'abord « montrer seulement » et lire le journal · jamais --prune depuis une fiche", "Montrer seulement", () => launch("publish", { depts: "all", species: groupSpecies, per_dept: true, groups: true, prune: false, dry_run: true }), !canPublish));
  blocks.push(block(canPublish ? "ok" : "", "Publication réelle", `téléverse les tuiles du groupe ${sp.group.name} et réécrit le manifeste`, "Publier", async () => { if (confirm(`Publier les tuiles du groupe ${sp.group.name} dans le bucket ?`)) await launch("publish", { depts: "all", species: groupSpecies, per_dept: true, groups: true, prune: false, dry_run: false }); }, !canPublish));
  blocks.push(block(d.grid.current ? "ok" : "warn", "Grille météo", d.grid.current ? `à jour (empreinte ${d.grid.hash})` : `empreinte ${d.grid.hash || "absente"} ≠ ${d.grid.current_hash} : à refaire pour toutes les espèces, puis à committer`, "weather-grid", () => launch("weather-grid", { depts: "all", to_bucket: true }), !w));
  const mapDetail = sp.maps.length ? sp.maps.map((m) => `z${m.zoom} (${fmt.size(m.bytes)}${m.current ? "" : ", ancien jeu"})`).join(" · ") : "aucune carte";
  blocks.push(block(sp.maps.some((m) => m.current) ? "ok" : "", "Carte HD", mapDetail, "Générer", () => launch("render", { species: sp.id, zoom: $("species-zoom").value, force: false }), !pub || !pub.hash));
  const jobs = sp.jobs.length
    ? el("p", { class: "meta" }, ["tâche en cours : ", ...sp.jobs.map((j) => el("a", { href: "#jobs", text: `${KINDS[j.kind] ? KINDS[j.kind].label : j.kind} (${j.status}${j.step_name ? ", " + j.step_name : ""})` }))])
    : el("p", { class: "meta", text: "aucune tâche en cours pour cette espèce" });
  sheet.replaceChildren(el("div", { class: "sheet" }, [
    el("h2", {}, [sp.name, " ", el("span", { class: "meta", text: `· ${sp.latin} · groupe ${sp.group.name}` })]),
    el("p", {}, [el("span", { class: `state ${sp.state}`, text: STATES[sp.state] || sp.state }), el("span", { class: "meta", text: `${sp.usage.app_share == null ? "" : ` · ${sp.usage.app_share} % des requêtes de l'app`}${sp.usage.web_share == null ? "" : ` · ${sp.usage.web_share} % du site`}` })]),
    ...blocks,
    jobs,
    fileDetails(sp),
  ]));
}

// ------------------------------------------------------------------ fichier espèce, lecture structurée

/* Le fichier species/<id>.json tel que le référentiel le porte (GET /admin/api/species/<id>), en
   lecture seule : en-tête, terrain T, phénologie Φ (courbe calculée par l'API), météo M, fiche en
   mots, références. Les clés inconnues de la page sont montrées brutes : rien n'est caché. */
const SVG = "http://www.w3.org/2000/svg";
const fileCache = {};
const fileOpen = {};
const ASPECTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "flat"];
const MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."];
const MONTH_STARTS = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
const TRAPEZOID_KEYS = ["min", "opt_low", "opt_high", "max"];

function num(v, digits = 2) { return v == null ? "—" : Number(v).toLocaleString("fr-FR", { maximumFractionDigits: digits }); }
function svgEl(tag, attrs = {}) { const e = document.createElementNS(SVG, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; }
function isTrapezoid(v) { return v && typeof v === "object" && TRAPEZOID_KEYS.every((k) => k in v); }

function fileDetails(sp) {
  const d = el("details", { class: "file" }, [el("summary", { text: "Contenu du fichier" }), el("p", { class: "meta", text: "chargement…" })]);
  if (fileOpen[sp.id]) d.open = true;
  const fill = async () => {
    try {
      fileCache[sp.id] = fileCache[sp.id] || await api(`/admin/api/species/${sp.id}`);
      d.replaceChildren(el("summary", { text: "Contenu du fichier" }), renderFile(fileCache[sp.id]));
    } catch (err) {
      d.replaceChildren(el("summary", { text: "Contenu du fichier" }), el("p", { class: "meta", text: `fichier illisible : ${err.message}` }));
    }
  };
  d.addEventListener("toggle", () => { fileOpen[sp.id] = d.open; if (d.open) fill(); });
  if (d.open) fill();
  return d;
}

function trapezoid(t, unit) {
  const vals = TRAPEZOID_KEYS.map((k) => Number(t[k]));
  const lo = vals[0], span = vals[3] - lo || 1;
  const W = 160, H = 26, pad = 2, top = 3, bottom = H - 3;
  const x = (v) => pad + ((v - lo) / span) * (W - 2 * pad);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "trap" });
  svg.append(svgEl("line", { x1: pad, y1: bottom, x2: W - pad, y2: bottom, class: "axis" }));
  svg.append(svgEl("polygon", { points: `${x(vals[0])},${bottom} ${x(vals[1])},${top} ${x(vals[2])},${top} ${x(vals[3])},${bottom}` }));
  const label = `${num(vals[0])} · ${num(vals[1])}–${num(vals[2])} · ${num(vals[3])}${unit ? ` ${unit}` : ""}`;
  return el("span", { class: "trapwrap" }, [svg, el("span", { class: "meta", text: label })]);
}

function bars(obj, order) {
  const known = order ? order.filter((k) => k in obj) : Object.keys(obj).filter((k) => k !== "default").sort((a, b) => obj[b] - obj[a]);
  const rest = Object.keys(obj).filter((k) => !known.includes(k));
  const g = el("div", { class: known.length + rest.length > 12 ? "bars wide" : "bars" });
  for (const k of known.concat(rest)) {
    const v = Number(obj[k]);
    g.append(el("span", { text: k }), el("div", { class: "bar" }, el("div", { style: `width:${Math.round(Math.min(1, Math.max(0, v)) * 100)}%` })), el("span", { class: "pct", text: num(v) }));
  }
  return g;
}

function refChips(keys, dict) {
  if (!keys || !keys.length) return null;
  return el("span", { class: "refs" }, keys.map((k) => el("abbr", { title: dict[k] || "référence absente du fichier", text: k })));
}

function pairs(rows) {
  const dl = el("dl", { class: "pairs" });
  for (const [k, v] of rows) if (v != null) dl.append(el("dt", { text: k }), el("dd", {}, v));
  return dl;
}

/* Paramètres d'un bloc : scalaires en « clé valeur », trapèzes dessinés, le reste en JSON. */
function params(obj, units = {}, skip = []) {
  const out = [];
  for (const [k, v] of Object.entries(obj)) {
    if (skip.includes(k)) continue;
    if (isTrapezoid(v)) out.push(el("span", { class: "param" }, [el("span", { class: "k", text: k }), trapezoid(v, units[k])]));
    else if (v && typeof v === "object") out.push(el("span", { class: "param" }, [el("span", { class: "k", text: k }), el("code", { text: JSON.stringify(v) })]));
    else out.push(el("span", { class: "param" }, [el("span", { class: "k", text: k }), `${num(v)}${units[k] ? ` ${units[k]}` : ""}`]));
  }
  return el("div", { class: "params" }, out);
}

function phiChart(curves) {
  const W = 460, H = 96, padL = 6, padR = 6, top = 6, bottom = 18;
  const x = (i) => padL + (i / 364) * (W - padL - padR);
  const y = (v) => top + (1 - v) * (H - top - bottom);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "phi" });
  for (const v of [0, 0.25, 1]) svg.append(svgEl("line", { x1: padL, y1: y(v), x2: W - padR, y2: y(v), class: v === 0 ? "axis" : "grid" }));
  MONTH_STARTS.forEach((d, i) => {
    svg.append(svgEl("line", { x1: x(d), y1: y(1), x2: x(d), y2: y(0), class: "grid" }));
    const t = svgEl("text", { x: x(d) + 2, y: H - 5 }); t.textContent = MONTHS[i]; svg.append(t);
  });
  curves.forEach((c, i) => {
    const path = svgEl("path", { class: `line line${i}`, d: c.days.map((v, j) => `${j ? "L" : "M"}${x(j).toFixed(1)},${y(v).toFixed(1)}`).join(" ") });
    svg.append(path);
  });
  const legend = el("p", { class: "meta" }, curves.map((c, i) => el("span", { class: `legend line${i}`, text: `à ${num(c.altitude_m, 0)} m` })));
  return el("div", { class: "phiwrap" }, [svg, legend]);
}

function section(title, hint, children) {
  return el("section", { class: "file-section" }, [el("h4", {}, [title, hint ? el("span", { class: "meta", text: ` ${hint}` }) : null]), ...[].concat(children).filter(Boolean)]);
}

function renderFile(data) {
  const f = data.file;
  const refs = { ...(f.references || {}), ...((f.about || {}).references || {}) };
  const chips = (keys) => refChips(keys, refs);
  const known = ["id", "schema_version", "group", "names", "notes", "references", "terrain", "phenology", "weather", "about"];
  const out = el("div", { class: "file-body" });

  out.append(section("En-tête", null, pairs([
    ["id", el("code", { text: f.id })],
    ["schéma", `version ${num(f.schema_version, 0)} · empreinte ${data.hash}`],
    ["groupe", f.group ? `${f.group.fr || f.group.id} (${f.group.id})` : null],
    ["noms", f.names ? `${f.names.fr || ""} · ${f.names.la || ""}` : null],
    ["notes", f.notes],
  ])));

  const t = f.terrain || {};
  const trefs = t.refs || {};
  out.append(section("Terrain → potentiel T", "T = hôte × Σ poids × facteur ; altitude hors [min, max] → T = 0", [
    el("h5", {}, ["Hôtes ", el("span", { class: "meta", text: "poids 0–1 par hôte BD Forêt, default pour une clé absente" }), chips(trefs.hosts)]),
    t.hosts ? bars(t.hosts) : null,
    el("h5", {}, ["Couvert ", chips(trefs.cover)]), t.cover ? bars(t.cover, ["fermee", "ouverte", "sans_couvert", "plantation", "non_foret"]) : null,
    pairs([
      ["pH (plancher 0,15)", t.ph ? el("span", {}, [trapezoid(t.ph), chips(trefs.ph)]) : null],
      ["altitude", t.altitude_m ? el("span", {}, [trapezoid(t.altitude_m, "m"), chips(trefs.altitude_m || trefs.altitude)]) : null],
      ["pente", t.slope_deg ? el("span", {}, [trapezoid(t.slope_deg, "°"), chips(trefs.slope_deg || trefs.slope)]) : null],
    ]),
    el("h5", {}, ["Exposition ", chips(trefs.aspect)]), t.aspect ? bars(t.aspect, ASPECTS) : null,
    el("h5", {}, ["Poids ", el("span", { class: "meta", text: t.weights ? `somme ${num(Object.values(t.weights).reduce((a, b) => a + Number(b), 0))}` : "" }), chips(trefs.weights)]),
    t.weights ? bars(t.weights, ["cover", "ph", "altitude", "aspect", "slope"]) : null,
    params(t, {}, ["hosts", "cover", "ph", "altitude_m", "slope_deg", "aspect", "weights", "refs"]),
  ]));

  const p = f.phenology || {};
  const shift = p.altitude_shift;
  out.append(section("Phénologie → fenêtre Φ", "0 hors saison, plancher 0,25 en saison, 1 au pic, rampes linéaires", [
    pairs([
      ["saison", p.season ? `${p.season.start} → ${p.season.end}` : null],
      ["pic", p.peak ? `${p.peak.start} → ${p.peak.end}` : null],
      ["rampe", p.ramp_days != null ? `${num(p.ramp_days, 0)} jours` : null],
      ["pic secondaire", p.secondary_peak ? `${p.secondary_peak.start} → ${p.secondary_peak.end}, Φ = ${num(p.secondary_peak.phi)}` : null],
      ["décalage en altitude", shift ? `référence ${num(shift.reference_m, 0)} m · début ${num(shift.start_days_per_100m, 0)} j et fin ${num(shift.end_days_per_100m, 0)} j par 100 m · au plus ±${num(shift.max_days, 0)} j` : null],
      ["références", chips(p.refs)],
    ]),
    data.phi && data.phi.length ? phiChart(data.phi) : null,
    params(p, {}, ["season", "peak", "ramp_days", "secondary_peak", "altitude_shift", "refs"]),
  ]));

  const w = f.weather || {};
  const wrefs = w.refs || {};
  const units = { window_days: "j", c: "°C", mm: "mm", event_mm: "mm", event_days: "j", lag_days: "j", tmin_c: "°C", ramp_c: "°C", nights: "nuits", moderate_mm: "mm", severe_mm: "mm", tmax_c: "°C", days: "j" };
  const factors = [["temp_mean", "température moyenne"], ["rain", "pluie cumulée"], ["trigger", "pluie déclenchante"], ["soil_moisture_pct", "humidité du sol (percentile)"], ["frost", "gel (rampe)"], ["water_balance", "bilan hydrique"], ["heat", "chaleur"], ["memory", "mémoire"], ["persistence", "persistance"]];
  const wtable = el("table", { class: "factors" }, [el("thead", {}, el("tr", {}, [el("th", { text: "facteur" }), el("th", { text: "paramètres" }), el("th", { text: "réf." })]))]);
  const tbody = el("tbody");
  for (const [k, label] of factors) if (w[k] != null) {
    const v = w[k];
    tbody.append(el("tr", {}, [el("td", {}, [el("b", { text: label }), el("div", { class: "meta", text: k })]), el("td", {}, isTrapezoid(v) ? trapezoid(v) : typeof v === "object" ? params(v, units) : num(v)), el("td", {}, chips(wrefs[k]))]));
  }
  for (const k of Object.keys(w)) if (!factors.some(([f0]) => f0 === k) && !["weights", "refs"].includes(k)) {
    tbody.append(el("tr", {}, [el("td", {}, el("b", { text: k })), el("td", {}, typeof w[k] === "object" ? params(w[k], units) : num(w[k])), el("td", {}, chips(wrefs[k]))]));
  }
  wtable.append(tbody);
  out.append(section("Météo → indice M", "M = (Σ poids × facteur) × gel × bilan hydrique × chaleur × mémoire, puis persistance", [
    wtable,
    el("h5", {}, ["Poids ", el("span", { class: "meta", text: w.weights ? `somme ${num(Object.values(w.weights).reduce((a, b) => a + Number(b), 0))}` : "" }), chips(wrefs.weights)]),
    w.weights ? bars(w.weights, ["temp", "rain", "trigger", "soil_moisture"]) : null,
  ]));

  const a = f.about;
  if (a) {
    const conf = a.confusions && a.confusions.length
      ? el("table", {}, [el("thead", {}, el("tr", {}, ["espèce", "risque", "comment la distinguer", "réf."].map((h) => el("th", { text: h })))),
        el("tbody", {}, a.confusions.map((c) => el("tr", {}, [el("td", {}, [el("b", { text: c.name }), el("div", { class: "meta", text: c.latin })]), el("td", { text: c.risk }), el("td", { text: c.tell }), el("td", {}, chips(c.refs))])))])
      : el("p", { class: "meta", text: "aucune confusion listée" });
    const photo = a.photo
      ? el("figure", {}, [el("img", { src: a.photo.url, alt: "", loading: "lazy" }), el("figcaption", { class: "meta" }, [a.photo.author ? `${a.photo.author} · ` : "", a.photo.license || "", a.photo.page ? el("a", { href: a.photo.page, target: "_blank", rel: "noopener", text: " · page" }) : null])])
      : el("p", { class: "meta", text: "photo : aucune (null)" });
    out.append(section("Fiche en mots", "exclue de l'empreinte : la relire ne régénère rien", [
      pairs([["résumé", a.summary], ["habitat", a.habitat], ["saison", a.season], ["références", chips(a.refs)]]),
      el("h5", { text: "Confusions" }), conf,
      el("h5", { text: "Photo" }), photo,
      params(a, {}, ["summary", "habitat", "season", "confusions", "photo", "refs", "references"]),
    ]));
  } else {
    out.append(section("Fiche en mots", null, el("p", { class: "meta", text: "bloc about absent" })));
  }

  const refRows = Object.entries(refs).sort(([x], [y]) => x.localeCompare(y));
  out.append(section("Références", `${refRows.length} sources`, el("table", { class: "refs-table" }, [el("tbody", {}, refRows.map(([k, v]) => el("tr", {}, [el("td", {}, el("code", { text: k })), el("td", { text: v })])))])));

  const extra = Object.keys(f).filter((k) => !known.includes(k));
  if (extra.length) out.append(section("Autres champs", "inconnus de cette page, montrés tels quels", el("pre", { text: JSON.stringify(Object.fromEntries(extra.map((k) => [k, f[k]])), null, 2) })));
  return out;
}
