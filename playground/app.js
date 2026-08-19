const RAW_BASE = "https://raw.githubusercontent.com/asopitech-labs/global-executables/main";
const DATA_BASE = `${RAW_BASE}/data`;
const state = { metadata: null, status: null, operation: "check_executable", result: null };
const $ = (id) => document.getElementById(id);

async function json(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}: ${url}`);
  return response.json();
}

async function asset(path) { return json(`${DATA_BASE}/${path}`); }

function formatNumber(value) { return Number(value || 0).toLocaleString(); }
function formatDate(value, withTime = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, withTime ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" }).format(date);
}
function statusLabel(value) { return String(value || "partial").replaceAll("_", " "); }
function coverageScope(metadata) {
  const coverage = metadata?.coverage || {};
  return metadata?.negative_lookup === "exhaustive" && Object.values(coverage).length && Object.values(coverage).every((item) => item.status === "success" && item.coverage_kind === "exhaustive") ? "exhaustive" : "unknown";
}

function nextScheduled(now = new Date()) {
  const candidate = new Date(now);
  candidate.setUTCSeconds(0, 0);
  for (let day = 0; day < 3; day += 1) {
    for (const hour of [0, 6, 12, 18]) {
      candidate.setUTCDate(now.getUTCDate() + day);
      candidate.setUTCHours(hour, 47, 0, 0);
      if (candidate > now) return candidate;
    }
  }
  return candidate;
}

function renderOverview() {
  const metadata = state.metadata || {};
  const report = state.status?.crawl_report || {};
  const sources = report.sources || {};
  $("metric-snapshot").textContent = metadata.snapshot || "—";
  $("metric-count").textContent = formatNumber(metadata.unique_executables);
  $("metric-sweep").textContent = statusLabel(report.coverage_kind || "partial");
  $("metric-sweep-note").textContent = report.status === "success" ? "latest crawl published" : "partial progress published";
  const next = state.status?.next_crawl_at || nextScheduled().toISOString();
  $("metric-next").textContent = formatDate(next, false);
  $("metric-next-note").textContent = `${new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(new Date(next))} · local time`;
  $("status-meta").textContent = `Report observed ${formatDate(state.status?.generated_at)} · ${state.status?.artifact_data_commit ? state.status.artifact_data_commit.slice(0, 8) : "live"}`;
  $("schedule-title").textContent = `Next scheduled crawl · ${formatDate(next)}`;
  $("schedule-detail").textContent = `Cron: ${state.status?.schedule || "47 */6 * * *"} UTC · Pages redeploys after the report is published.`;
  $("footer-build").textContent = `Main snapshot ${metadata.snapshot || "unknown"} · data served from GitHub raw content.`;

  const grid = $("source-grid");
  grid.replaceChildren();
  const order = ["npm", "pypi", "crates", "go", "rubygems", "packagist"];
  for (const name of order) {
    const source = sources[name] || { coverage_kind: "pending", complete: false };
    const card = document.createElement("article");
    card.className = "source-card";
    const complete = source.coverage_kind === "exhaustive" || source.complete;
    const progress = source.catalog_size ? Math.min(100, (Number(source.cursor || 0) / Number(source.catalog_size)) * 100) : complete ? 100 : 3;
    const position = source.catalog_size ? `${formatNumber(source.cursor || 0)} / ${formatNumber(source.catalog_size)}` : source.page ? `catalog page ${formatNumber(source.page)}` : source.since ? `through ${String(source.since).slice(0, 10)}` : complete ? "catalog complete" : "in progress";
    const errors = Number(source.failures || 0);
    card.innerHTML = `<div class="source-card-top"><span class="source-name">${name}</span><span class="source-status ${complete ? "exhaustive" : ""}">${complete ? "exhaustive" : statusLabel(source.coverage_kind)}</span></div><div class="source-bar"><i style="width:${progress}%"></i></div><div class="source-stats"><span>${position}</span><span>${errors ? `${formatNumber(errors)} failures` : `${formatNumber(source.records || 0)} records`}</span></div>`;
    grid.append(card);
  }
}

function asciiShard(command) {
  const folded = command.toLowerCase();
  return /^[a-z0-9][a-z0-9._+@-]*$/.test(folded) ? (folded + "_").slice(0, 2) : null;
}
async function digest(value) {
  const bytes = new TextEncoder().encode(value);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
async function executablePath(name) {
  const normalized = asciiShard(name);
  const file = /^[A-Za-z0-9._+@-]+$/.test(name) ? `${name}.json` : `${await digest(name)}.json`;
  const directory = normalized || `_${(await digest(name)).slice(0, 2)}`;
  return `executables/${directory}/${file}`;
}
async function getExecutable(name) {
  if (!name || /[/\\\x00-\x1f\x7f]/.test(name) || name === "." || name === "..") return null;
  try { return await asset(await executablePath(name)); } catch (error) { if (error.message.startsWith("404")) return null; throw error; }
}
async function index(path) { return asset(`indexes/${path}.json`); }
async function setFor(path) { return new Set(await index(path)); }
async function scopeFromInput(value) {
  if (!value.trim()) return null;
  const parsed = JSON.parse(value);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("Scope must be a JSON object.");
  return parsed;
}
function matchesScope(provider, scope) { return !scope || Object.entries(scope).every(([key, value]) => provider[key] === value); }

async function checkExecutable(name, scope) {
  const record = await getExecutable(name);
  const providers = record?.providers?.filter((provider) => matchesScope(provider, scope));
  const found = Boolean(record && providers?.length);
  const result = { name, found, status: found ? "collision" : coverageScope(state.metadata) === "exhaustive" ? "clear_in_index" : "unknown", snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata), checked_sources: state.metadata.checked_sources || [], searched_sources: state.metadata.checked_sources || [], coverage: state.metadata.coverage || {}, freshness: state.status?.freshness?.status || "unavailable" };
  if (found) result.providers = providers;
  else result.absence = { status: "not_found_in_current_index", confidence: "insufficient_coverage", searched_sources: state.metadata.checked_sources || [] };
  if (scope) result.scope = scope;
  return result;
}
async function searchExecutables({ prefix = "", length, ecosystem, limit = 100, scope }) {
  const sets = [];
  if (prefix) {
    if (prefix.length < 2) throw new Error("Use at least a two-character prefix for a bounded browser search.");
    const prefixShard = asciiShard(prefix);
    if (!prefixShard) throw new Error("Non-ASCII prefix search is not available in the bounded browser client.");
    sets.push(await setFor(`prefix/${prefixShard}`));
  }
  if (length) sets.push(await setFor(`length/${length}`));
  if (ecosystem) sets.push(await setFor(`ecosystem/${ecosystem}`));
  if (scope) for (const [dimension, value] of Object.entries(scope)) sets.push(await setFor(`scope/${dimension}/${value}`));
  if (!sets.length) throw new Error("Add a prefix, length, ecosystem, or scope to keep the browser query bounded.");
  const names = [...sets.reduce((a, b) => new Set([...a].filter((value) => b.has(value))))].filter((name) => (!prefix || name.startsWith(prefix)) && (!length || name.length === Number(length)));
  names.sort((a, b) => a.localeCompare(b));
  return names.slice(0, Math.max(1, Math.min(500, Number(limit) || 100)));
}
function grams(value) { const padded = `  ${value.toLowerCase()}  `; return [...Array(Math.max(0, padded.length - 2))].map((_, i) => padded.slice(i, i + 3)); }
async function similar(name, limit) {
  const postings = await Promise.all([...new Set(grams(name))].map(async (gram) => { try { return await setFor(`trigram/${[...new TextEncoder().encode(gram)].map((byte) => byte.toString(16).padStart(2, "0")).join("")}`); } catch { return new Set(); } }));
  const candidates = new Set(postings.flatMap((set) => [...set]));
  const distance = (a, b) => { const row = [...Array(b.length + 1)].map((_, i) => i); for (let i = 1; i <= a.length; i += 1) { let previous = row[0]; row[0] = i; for (let j = 1; j <= b.length; j += 1) { const current = row[j]; row[j] = Math.min(row[j] + 1, row[j - 1] + 1, previous + (a[i - 1] !== b[j - 1])); previous = current; } } return row[b.length]; };
  const target = new Set(grams(name));
  return [...candidates].map((candidate) => { const candidateGrams = new Set(grams(candidate)); const union = new Set([...target, ...candidateGrams]); const similarity = [...target].filter((gram) => candidateGrams.has(gram)).length / union.size; return { name: candidate, edit_distance: distance(name.toLowerCase(), candidate.toLowerCase()), trigram_similarity: Number(similarity.toFixed(3)) }; }).filter((item) => item.name.startsWith(name) || name.startsWith(item.name) || item.edit_distance <= 2 || item.trigram_similarity >= .3).sort((a, b) => a.edit_distance - b.edit_distance || b.trigram_similarity - a.trigram_similarity || a.name.localeCompare(b.name)).slice(0, Math.max(1, Math.min(100, Number(limit) || 20)));
}
async function assessExecutable(name, scope) {
  const checked = await checkExecutable(name, scope);
  const providers = checked.providers || [];
  const signals = providers.map((provider) => { const release = provider.latest_release_at ? new Date(provider.latest_release_at) : null; const recent = release && (Date.now() - release.getTime()) / 86400000 <= 365; const stale = release && (Date.now() - release.getTime()) / 86400000 > 365; return { freshness: recent ? "recent" : stale ? "stale" : "unknown", activity: recent ? "active" : stale ? "inactive" : "unknown", popularity: "unknown", latest_release_at: provider.latest_release_at, last_observed_at: provider.last_observed_at, usage_metrics: provider.usage_metrics || [] }; });
  const active = signals.some((signal) => signal.activity === "active");
  return { name, found: checked.found, snapshot: checked.snapshot, coverage: checked.coverage, coverage_scope: checked.coverage_scope, assessment: { freshness: active ? "recent" : providers.length ? "stale" : "unknown", activity: active ? "active" : providers.length ? "inactive" : "unknown", popularity: "unknown", collision_risk: active ? "active_common" : providers.length ? "historical_low_activity" : "insufficient_evidence", methodology_version: "playground-client/1.0" }, signals, providers, scope };
}

function input(label, name, value = "", type = "text", placeholder = "") { return `<label>${label}<input name="${name}" type="${type}" value="${value}" placeholder="${placeholder}" /></label>`; }
function scopeInput() { return `<label>Scope <textarea name="scope" placeholder='{"language":"python"}'></textarea></label><span class="scope-hint">Optional JSON filter, e.g. {"registry":"npm"}</span>`; }
function renderForm() {
  const form = $("query-form");
  const operation = state.operation;
  const titles = { check_executable: "check_executable", check_executables: "check_executables", get_executable: "get_executable", search_executables: "search_executables", search_similar_executables: "search_similar_executables", assess_executable: "assess_executable", get_coverage: "get_coverage" };
  $("operation-title").textContent = titles[operation];
  if (operation === "get_coverage") { form.innerHTML = `<div class="run-row"><span class="scope-hint">Return snapshot, source coverage, and freshness context.</span><button class="run-button">Run query ↗</button></div>`; return; }
  let fields = "";
  if (operation === "check_executables") fields = `<label>Names <textarea name="names" placeholder="envcp\nevpk\nphpunit">envcp\nevpk</textarea></label>${scopeInput()}`;
  else if (operation === "search_executables") fields = `<div class="form-grid">${input("Prefix", "prefix", "do", "text", "two or more characters")}${input("Length", "length", "", "number", "optional")}${input("Ecosystem", "ecosystem", "", "text", "npm, pypi, crates…")}${input("Limit", "limit", "25", "number", "1–500")}</div>${scopeInput()}`;
  else { fields = input("Executable name", "name", operation === "search_similar_executables" ? "kubctl" : "phpunit", "text", "e.g. docker"); if (operation === "search_similar_executables") fields += input("Limit", "limit", "20", "number", "1–100"); else fields += scopeInput(); }
  form.innerHTML = `${fields}<div class="run-row"><span class="scope-hint">Read-only · no request leaves GitHub Pages.</span><button class="run-button">Run query ↗</button></div>`;
}
async function runQuery(event) {
  event.preventDefault(); const form = new FormData(event.target); const op = state.operation; const scope = op === "get_coverage" ? null : await scopeFromInput(String(form.get("scope") || ""));
  try { let result; if (op === "get_coverage") result = { ...state.metadata, freshness: state.status?.freshness || { status: "unavailable" } }; else if (op === "check_executable" || op === "get_executable" || op === "assess_executable") { const name = String(form.get("name") || "").trim(); if (op === "check_executable") result = await checkExecutable(name, scope); else if (op === "assess_executable") result = await assessExecutable(name, scope); else result = { record: await getExecutable(name), snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata), scope }; } else if (op === "check_executables") { const names = String(form.get("names") || "").split(/[,\n]/).map((name) => name.trim()).filter(Boolean); result = { results: await Promise.all(names.map((name) => checkExecutable(name, scope))), snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata) }; } else if (op === "search_executables") result = { executables: await searchExecutables({ prefix: String(form.get("prefix") || "").trim(), length: String(form.get("length") || "").trim(), ecosystem: String(form.get("ecosystem") || "").trim(), limit: form.get("limit"), scope }), snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata), scope }; else result = { matches: await similar(String(form.get("name") || "").trim(), form.get("limit")), snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata), scope };
    state.result = result; $("result").textContent = JSON.stringify(result, null, 2);
  } catch (error) { const result = { error: error.message }; state.result = result; $("result").textContent = JSON.stringify(result, null, 2); }
}
async function boot() {
  try { state.metadata = await json(`${RAW_BASE}/data/metadata.json`); } catch (error) { $("result").textContent = JSON.stringify({ error: error.message }, null, 2); }
  try { state.status = await json("./status.json"); } catch { state.status = { crawl_report: { coverage_kind: "partial", status: "unavailable", sources: {} }, schedule: "47 */6 * * *" }; }
  renderOverview(); renderForm();
  document.querySelectorAll(".operation").forEach((button) => button.addEventListener("click", () => { state.operation = button.dataset.operation; document.querySelectorAll(".operation").forEach((item) => item.classList.toggle("active", item === button)); renderForm(); }));
  $("query-form").addEventListener("submit", runQuery);
  $("copy-result").addEventListener("click", async () => { await navigator.clipboard?.writeText($("result").textContent); $("copy-result").textContent = "Copied"; setTimeout(() => { $("copy-result").textContent = "Copy JSON"; }, 1200); });
}
boot();
