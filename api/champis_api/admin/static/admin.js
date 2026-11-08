/* Backoffice Champis (spec › Backoffice, lot BO1) : trois onglets en lecture, sans dépendance.
   La page ne parle qu'à l'API, sur la même origine ; chaque action porte X-Champis-Admin: 1. */
"use strict";

const $ = (id) => document.getElementById(id);
const HEADERS = { "X-Champis-Admin": "1" };
const KINDS = {};
let timers = { health: null, jobs: null, job: null, tools: null };

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
  ({ health: loadHealth, usage: loadUsage, jobs: loadJobs, tools: loadTools, renders: loadRenders })[name]();
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
  if (t.queue.length) timers.tools = setTimeout(loadTools, 5000);
}

function toolCard(tool, queue) {
  const busy = queue.find((j) => j.kind === tool.kind);
  const form = el("form");
  for (const f of tool.fields) {
    let input;
    if (f.kind === "bool") { input = el("input", { type: "checkbox", name: f.name }); input.checked = !!f.default; }
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
    el("p", { class: "meta", text: `${tool.duration} · ${tool.needs}${tool.exclusive ? " · une seule à la fois" : ""}` }),
    form,
  ]);
}

// ------------------------------------------------------------------ cartes HD

async function loadRenders() {
  let r;
  try { r = await api("/admin/api/renders"); } catch (err) { table($("renders-table"), ["erreur"], [{ cells: [err.message] }]); return; }
  const zoomSelect = $("renders-zoom");
  if (!zoomSelect.options.length) for (const z of r.zooms) zoomSelect.append(el("option", { value: z, text: `${z} (${z === 9 ? "≈ A3" : "≈ A1"})` }));
  $("renders-meta").textContent = r.generated ? `jeu de tuiles du ${fmt.when(r.generated)}` : "aucun manifeste de tuiles";
  const launch = async (species, button) => {
    button.disabled = true;
    try {
      await api("/admin/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "render", args: { species, zoom: zoomSelect.value, force: false } }) });
      button.textContent = "en file";
    } catch (err) { alert(err.message); button.disabled = false; }
  };
  $("renders-all").onclick = (e) => launch("all", e.target);
  table($("renders-table"), ["espèce", "T publié", "cartes présentes", "générer"], r.species.map((sp) => {
    const files = sp.files.length ? el("span", {}, sp.files.flatMap((f, i) => [i ? " · " : "", el("a", { href: f.url, text: `z${f.zoom} (${fmt.size(f.bytes)}${f.current ? "" : ", ancien jeu"})` })])) : "aucune";
    const button = el("button", { class: "quiet", text: "Générer" });
    if (!sp.published) button.disabled = true;
    button.addEventListener("click", () => launch(sp.species, button));
    return { cells: [el("span", {}, [el("b", { text: sp.name }), ` (${sp.latin})`]), sp.published ? status("ok") : el("span", { class: "meta", text: "pas de tuiles" }), files, button] };
  }));
}
