const RAW_BASE = "https://raw.githubusercontent.com/asopitech-labs/global-executables/dictionary";
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
function limitedPopulation(source) {
  return Boolean(source.scope) || Object.keys(source).some((key) => key.startsWith("catalog_"));
}
function coverageScope(metadata, scope = null) {
  const coverage = metadata?.coverage || {};
  if ((scope && Object.keys(scope).length) || metadata?.negative_lookup !== "exhaustive" || !Object.keys(coverage).length) return "unknown";
  return Object.values(coverage).every((source) => source.status === "success" && source.coverage_kind === "exhaustive" && !limitedPopulation(source)) ? "exhaustive" : "unknown";
}
function coverageWarnings(metadata) {
  const npm = metadata?.coverage?.npm || {};
  if (npm.status === "success" && npm.coverage_kind === "exhaustive" && !limitedPopulation(npm)) return [];
  const provenance = Object.fromEntries(["status", "coverage_kind", "source", "scope", "catalog_complete", "catalog_packages", "catalog_snapshot", "catalog_source", "catalog_digest"].filter((key) => Object.hasOwn(npm, key)).map((key) => [key, npm[key]]));
  return [{ code: "npm_coverage_incomplete", ecosystem: "npm", message: "npm coverage is incomplete or limited to a declared population. A missing result is not proof that no npm package uses this executable and is not permission to adopt the name.", coverage: provenance, reference: "https://github.com/asopitech-labs/global-executables/issues/54" }];
}

function nextScheduled(now = new Date()) {
  const candidate = new Date(now);
  const hour = now.getUTCHours();
  candidate.setUTCHours(hour - (hour % 6), 17, 0, 0);
  if (candidate <= now) candidate.setUTCHours(hour - (hour % 6) + 6, 17, 0, 0);
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
  // next_crawl_at is baked at build time, so it turns into a past date as soon as the
  // page outlives it.  Fall back to the live schedule rather than advertising a time
  // that has already gone by.
  const baked = state.status?.next_crawl_at;
  const next = baked && new Date(baked) > new Date() ? baked : nextScheduled().toISOString();
  $("metric-next").textContent = formatDate(next, false);
  $("metric-next-note").textContent = `${new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(new Date(next))} · local time`;
  $("status-meta").textContent = `Report observed ${formatDate(state.status?.generated_at)} · ${state.status?.artifact_data_commit ? state.status.artifact_data_commit.slice(0, 8) : "live"}`;
  $("schedule-title").textContent = `Next scheduled crawl · ${formatDate(next)}`;
  $("schedule-detail").textContent = `Cron: ${state.status?.schedule || "17 */6 * * *"} UTC · progress is published after each CI refresh.`;
  const dictionaryCommit = state.status?.dictionary_commit?.slice(0, 8) || "live";
  $("footer-build").textContent = `Dictionary ${dictionaryCommit} · snapshot ${metadata.snapshot || "unknown"} · data served from GitHub raw content.`;

  const forecast = state.status?.forecast || {};
  const trend = forecast.backlog_change_per_day == null ? null : Number(forecast.backlog_change_per_day);
  $("forecast-progress").textContent = forecast.overall_progress_percent == null ? "—" : `${Number(forecast.overall_progress_percent).toFixed(2)}%`;
  $("forecast-remaining").textContent = forecast.remaining_work == null ? "—" : formatNumber(forecast.remaining_work);
  $("forecast-rate").textContent = Number.isFinite(trend) ? `${trend > 0 ? "+" : ""}${formatNumber(trend)}` : "—";
  $("forecast-window").textContent = forecast.observed_from && forecast.observed_to ? `${formatDate(forecast.observed_from, false)} – ${formatDate(forecast.observed_to, false)}` : "insufficient history";
  if (forecast.status === "estimated") {
    $("forecast-title").textContent = `Estimated completion · ${formatDate(forecast.estimated_completion_at, false)}`;
    $("forecast-detail").textContent = `Rolling 24-hour projection from ${formatNumber(forecast.samples)} published checkpoints; catalog growth and retries are reflected in the net trend.`;
  } else if (forecast.status === "complete") {
    $("forecast-title").textContent = "Current catalog complete";
    $("forecast-detail").textContent = "The published cursor has reached the current catalog with no retry backlog.";
  } else if (forecast.status === "non_converging") {
    $("forecast-title").textContent = "No finite completion date at the current trend";
    $("forecast-detail").textContent = "The measured backlog is growing because catalog growth and retries currently exceed net crawl progress.";
  } else {
    $("forecast-title").textContent = "Completion date unavailable";
    $("forecast-detail").textContent = "At least two distinct published checkpoints are required for a forecast.";
  }

  // The OS indexes come from their own pipeline: each run takes a whole file index,
  // so they have records and a coverage kind but never a cursor to advance.
  const osGrid = $("os-grid");
  osGrid.replaceChildren();
  const osSources = state.status?.production_report?.sources || {};
  for (const name of ["debian", "ubuntu", "arch", "homebrew"]) {
    const source = osSources[name];
    const card = document.createElement("article");
    card.className = "source-card";
    const complete = source?.coverage_kind === "exhaustive";
    const label = source ? (complete ? "exhaustive" : statusLabel(source.coverage_kind)) : "pending";
    const position = source?.status === "success" ? "index taken whole" : source ? statusLabel(source.status) : "not yet crawled";
    card.innerHTML = `<div class="source-card-top"><span class="source-name">${name}</span><span class="source-status ${complete ? "exhaustive" : ""}">${label}</span></div><div class="source-bar"><i style="width:${complete ? 100 : 3}%"></i></div><div class="source-stats"><span>${position}</span><span>${formatNumber(source?.records || 0)} records</span></div>`;
    osGrid.append(card);
  }

  // A recipe repository is read whole on every run, so it has records and a coverage
  // kind but no cursor to advance, and its evidence differs per source: vcpkg declares
  // its tools, while xmake only declares that a package is a binary.
  const recipeGrid = $("recipe-grid");
  recipeGrid.replaceChildren();
  const recipeSources = state.status?.recipe_report?.sources || {};
  for (const name of ["vcpkg", "xmake"]) {
    const source = recipeSources[name];
    const card = document.createElement("article");
    card.className = "source-card";
    const index = source?.indexes?.[0] || {};
    const declared = Number(index.declaring_packages || 0);
    const packages = Number(index.packages || 0);
    const label = source ? statusLabel(source.coverage_kind) : "pending";
    const width = packages ? Math.max(3, Math.min(100, (declared / packages) * 100)) : 3;
    const position = packages
      ? `${formatNumber(declared)} / ${formatNumber(packages)} packages declare a command`
      : source ? statusLabel(source.status) : "not yet collected";
    card.innerHTML = `<div class="source-card-top"><span class="source-name">${name}</span><span class="source-status">${label}</span></div><div class="source-bar"><i style="width:${width}%"></i></div><div class="source-stats"><span>${position}</span><span>${formatNumber(source?.records || 0)} records</span></div>`;
    recipeGrid.append(card);
  }

  const grid = $("source-grid");
  grid.replaceChildren();
  const order = ["npm", "npm-registry", "pypi", "crates", "go", "rubygems", "packagist", "nuget", "conan"];
  for (const name of order) {
    const source = sources[name] || { coverage_kind: "pending", complete: false };
    const card = document.createElement("article");
    card.className = "source-card";
    const complete = source.coverage_kind === "exhaustive" || source.complete;
    const npmCritical = name === "npm";
    const label = npmCritical && complete ? "critical 100%" : complete ? "exhaustive" : statusLabel(source.coverage_kind);
    const progress = source.catalog_size ? Math.min(100, (Number(source.cursor || 0) / Number(source.catalog_size)) * 100) : complete ? 100 : 3;
    // npm's cursor is a replication sequence, Go's is a timestamp; "through 2537341"
    // read like a truncated date.
    const cursorLabel = Number.isFinite(Number(source.since))
      ? `change ${formatNumber(Number(source.since))}`
      : `through ${String(source.since).slice(0, 10)}`;
    const position = source.refresh_cursor != null ? `refresh ${formatNumber(source.refresh_cursor)} / ${formatNumber(source.catalog_size)}`
      : source.catalog_size ? `${formatNumber(source.cursor || 0)} / ${formatNumber(source.catalog_size)}`
      : source.since ? cursorLabel : complete ? "catalog complete" : "in progress";
    const npmPosition = npmCritical ? `critical ${position}` : name === "npm-registry" && !source.catalog_size
      ? "registry: pending (historical public checkpoint 218,774 / 4.3 million)"
      : position;
    const errors = Number(source.failures || 0);
    // Records and failures are separate facts: a run that collected thousands of records
    // alongside two failures used to report only the failures.
    const stats = [`${formatNumber(source.records || 0)} records`];
    if (errors) stats.push(`${formatNumber(errors)} failures`);
    card.innerHTML = `<div class="source-card-top"><span class="source-name">${name}</span><span class="source-status ${complete ? "exhaustive" : ""}">${label}</span></div><div class="source-bar"><i style="width:${progress}%"></i></div><div class="source-stats"><span>${npmPosition}</span><span>${stats.join(" · ")}</span></div>`;
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
  if (!name || [...name].length > 255 || /[/\\\x00-\x1f\x7f]/.test(name) || name === "." || name === "..") throw new Error("invalid executable name: expected 1–255 characters without path separators or controls");
  const record = await getExecutable(name);
  const providers = record?.providers?.filter((provider) => matchesScope(provider, scope));
  const found = Boolean(record && providers?.length);
  const result = { name, found, status: found ? "collision" : coverageScope(state.metadata, scope) === "exhaustive" ? "clear_in_index" : "unknown", snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata, scope), checked_sources: state.metadata.checked_sources || [], searched_sources: state.metadata.checked_sources || [], coverage: state.metadata.coverage || {}, warnings: coverageWarnings(state.metadata), freshness: state.status?.freshness?.status || "unavailable" };
  if (found) result.providers = providers;
  else result.absence = { status: "not_found_in_current_index", confidence: result.coverage_scope === "exhaustive" ? "exhaustive" : "insufficient_coverage", searched_sources: state.metadata.checked_sources || [] };
  if (scope && Object.keys(scope).length) result.scope = scope;
  return result;
}
function verifiedPublishedRecipe(name, provider, environment, system) {
  if (!["direct", "filesystem"].includes(provider.confidence) || provider.alias_of || provider.package?.startsWith("-")) return null;
  if (system === "brew" && name === "hello" && provider.ecosystem === "homebrew" &&
      provider.package === "hello" && (provider.version || provider.latest_version) === "2.12.3" &&
      provider.source === "https://formulae.brew.sh/api/formula.json" &&
      environment.os === "macos" && environment.distribution_release === "15" &&
      environment.architecture === "arm64") {
    return {package_spec: "hello", package_version: "2.12.3", target: "macOS 15 arm64"};
  }
  if (environment.os !== "linux" || environment.distribution !== "ubuntu" ||
      environment.distribution_release !== "24.04" || environment.architecture !== "amd64") return null;
  if (system === "apt" && name === "hello" && provider.ecosystem === "ubuntu" &&
      provider.package === "hello" && provider.source === "https://archive.ubuntu.com/ubuntu/dists/noble/Contents-amd64.gz" &&
      provider.version == null && provider.latest_version == null &&
      environment.has_admin === true) {
    return {package_spec: "hello", package_version: null,
      target: "Ubuntu 24.04 amd64; apt repository hello", source_release: "24.04"};
  }
  const version = provider.version || provider.latest_version;
  const scope = environment.install_scope;
  if (scope !== "project") return null;
  if (system === "npm" && name === "tsc" && provider.ecosystem === "npm" &&
      provider.package === "typescript" && provider.source === "https://registry.npmjs.org/typescript/latest" &&
      version === "7.0.2" &&
      /^v?24(?:\.|$)/.test(String(environment.runtime_version))) {
    return {package_spec: "typescript@7.0.2", package_version: "7.0.2",
      target: `Ubuntu 24.04 amd64; Node.js 24; ${scope} scope`};
  }
  if (system === "pip" && name === "black" && provider.ecosystem === "pypi" &&
      provider.package === "black" && provider.source?.startsWith("https://files.pythonhosted.org/") &&
      version === "26.10.1" &&
      /^3\.12(?:\.|$)/.test(String(environment.runtime_version))) {
    return {package_spec: "black==26.10.1", package_version: "26.10.1",
      target: `Ubuntu 24.04 amd64; Python 3.12; ${scope} scope`};
  }
  return null;
}
function resolveInstallationFromCheck(checked, environment, preferences = {}) {
  const result = { contract_version: "installation-v1", name: checked.name, status: "needs_context", environment,
    snapshot: checked.snapshot, coverage_scope: checked.coverage_scope, warnings: checked.warnings,
    required_context: [], candidates: [], steps: [] };
  if (!checked.found) { result.status = "not_found_in_index"; result.absence = checked.absence; return result; }
  const system = environment.package_manager;
  if (!system) { result.required_context = ["package_manager"]; return result; }
  if (!["brew", "apt", "npm", "pip"].includes(system)) { result.status = "unsupported"; return result; }
  const distribution = environment.distribution;
  if (system === "apt" && !distribution) { result.required_context = ["distribution"]; return result; }
  if (system === "apt" && !["debian", "ubuntu"].includes(distribution)) { result.status = "unsupported"; return result; }
  if (system === "apt" && !environment.distribution_release) { result.required_context = ["distribution_release"]; return result; }
  if (system === "apt" && !environment.architecture) { result.required_context = ["architecture"]; return result; }
  if (system === "brew" && environment.os !== "macos") {
    result.required_context = environment.os ? [] : ["os"];
    result.status = result.required_context.length ? "needs_context" : "unsupported"; return result;
  }
  if (["npm", "pip"].includes(system) && !environment.runtime_version) { result.required_context = ["runtime_version"]; return result; }
  if (["npm", "pip"].includes(system) && !environment.install_scope) { result.required_context = ["install_scope"]; return result; }
  if (["npm", "pip"].includes(system) && !["user", "project"].includes(environment.install_scope)) { result.status = "unsupported"; return result; }
  const ecosystem = ({brew: "homebrew", npm: "npm", pip: "pypi"})[system] || distribution;
  let candidates = checked.providers.filter((p) => p.ecosystem === ecosystem);
  if (system === "apt") candidates = candidates.filter((p) => [undefined, distribution].includes(p.distribution) && [undefined, "deb", "apt"].includes(p.package_system));
  candidates = candidates.filter((p) => !preferences.package || p.package === preferences.package);
  result.candidates = candidates;
  if (!candidates.length) { result.status = "unsupported"; return result; }
  const verifiedCandidates = candidates.filter((p) => verifiedPublishedRecipe(checked.name, p, environment, system));
  const identities = new Set(candidates.map((p) => JSON.stringify([p.ecosystem, p.package])));
  if (identities.size > 1 && verifiedCandidates.length !== 1) { result.status = "ambiguous"; return result; }
  const provider = verifiedCandidates.length === 1 ? verifiedCandidates[0] : candidates[0];
  if (identities.size > 1) result.selection_reason = "Selected the only provider with a public install-and-invoke verification for this target; other observed providers remain candidates.";
  result.provider = provider;
  const verifiedRecipe = verifiedPublishedRecipe(checked.name, provider, environment, system);
  const sourceRelease = verifiedRecipe?.source_release ?? provider.distribution_release ?? null;
  result.applicability = {target: environment, source_distribution_release: sourceRelease,
    source_architecture: provider.architecture ?? null, validated_on_target: false};
  const releaseMatches = system !== "apt" || sourceRelease === environment.distribution_release;
  if (!["direct", "filesystem"].includes(provider.confidence) || provider.alias_of || provider.package.startsWith("-")) { result.status = "unverified"; return result; }
  const pkg = provider.package;
  const packageSpec = verifiedRecipe?.package_spec ?? pkg;
  let argv;
  if (system === "apt") {
    if (environment.has_admin !== true) { result.required_context = ["has_admin"]; return result; }
    argv = ["sudo", "apt-get", "install", "--", pkg];
  } else if (system === "brew") argv = ["brew", "install", "--", pkg];
  else if (system === "npm") argv = environment.install_scope === "project" ? ["npm", "install", "--", packageSpec] : ["npm", "install", "--global", "--", packageSpec];
  else {
    if (environment.install_scope === "project" && !environment.virtualenv_bin) { result.required_context = ["virtualenv_bin"]; return result; }
    const python = environment.install_scope === "project" ? environment.virtualenv_bin.replace(/\/$/, "") + "/python" : "python";
    argv = environment.install_scope === "project" ? [python, "-m", "pip", "install", "--", packageSpec] : [python, "-m", "pip", "install", "--user", "--", packageSpec];
  }
  const invoke = system === "npm" && environment.install_scope === "project" ? ["npm", "exec", "--", checked.name] : system === "pip" && environment.install_scope === "project" ? [environment.virtualenv_bin.replace(/\/$/, "") + "/" + checked.name] : [checked.name];
  result.status = "unverified";
  const methodDocs = {apt: "https://manpages.debian.org/apt-get.8", brew: "https://docs.brew.sh/Manpage#install-options-formulacask-", npm: "https://docs.npmjs.com/cli/commands/npm-install", pip: "https://pip.pypa.io/en/stable/cli/pip_install/"};
  result.method = {package_manager: system, documentation: methodDocs[system], basis: "package-manager command syntax", target_package_available: "unknown"};
  result.method_validation = ({
    "apt:undefined": {result: "pass", observed_at: "2026-10-20T00:58:00Z", target: "Debian 13 amd64", manager_version: "apt 3.0.3", fixture: "local ge-install-probe 1.0.0 .deb in an isolated root", invocation: "isolated-root/usr/bin/ge-install-probe"},
    "npm:project": {result: "pass", observed_at: "2026-10-19T10:45:00Z", target: "Debian 13 amd64", manager_version: "npm 11.9.0", runtime_version: "Node.js 24.19.0", fixture: "local ge-install-probe 1.0.0", invocation: "npm exec -- ge-install-probe"},
    "pip:project": {result: "pass", observed_at: "2026-10-19T10:46:00Z", target: "Debian 13 amd64", manager_version: "pip (Python 3.12.14)", runtime_version: "Python 3.12.14", fixture: "local ge-install-probe 1.0.0", invocation: "venv/bin/ge-install-probe"},
  })[`${system}:${environment.install_scope}`] ?? null;
  result.steps = [...(system === "apt" && !releaseMatches ? [{action: "check_availability", argv: ["apt-cache", "show", "--", pkg], platform: environment}] : []), {action: "install", argv, platform: environment}, {action: "invoke", argv: invoke, platform: environment}];
  result.invocation = {argv: invoke, path_note: system === "npm" && environment.install_scope === "project"
    ? "Run from the project directory where this package was installed."
    : "Ensure the selected package manager's executable directory is on PATH; verify the installed command before use."};
  result.verification = {confidence: provider.confidence, source: provider.source, observed_at: provider.last_observed_at ?? null,
    version: provider.version, note: "Package-manager syntax is documented; availability and entry-point behavior of this package on the target have not been verified.", source_release_matches_target: releaseMatches};
  if (verifiedRecipe) {
    result.status = "ready";
    result.applicability.validated_on_target = true;
    result.method.target_package_available = "verified";
    result.verification.note = "The public package installation and command invocation were verified on the stated target in CI.";
    result.verification.recipe_validation = {
      result: "pass", target: verifiedRecipe.target,
      package_version: verifiedRecipe.package_version, observed_at: "2026-10-20",
      evidence_url: "https://github.com/asopitech-labs/global-executables/actions/runs/39363743911",
    };
  }
  return result;
}
async function resolveInstallation(name, environment, preferences = {}) {
  return resolveInstallationFromCheck(await checkExecutable(name, null), environment, preferences);
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
  return { name, found: checked.found, snapshot: checked.snapshot, warnings: checked.warnings, coverage: checked.coverage, coverage_scope: checked.coverage_scope, assessment: { freshness: active ? "recent" : providers.length ? "stale" : "unknown", activity: active ? "active" : providers.length ? "inactive" : "unknown", popularity: "unknown", collision_risk: active ? "active_common" : providers.length ? "historical_low_activity" : "insufficient_evidence", methodology_version: "playground-client/1.0" }, signals, providers, scope };
}

function input(label, name, value = "", type = "text", placeholder = "") { return `<label>${label}<input name="${name}" type="${type}" value="${value}" placeholder="${placeholder}" /></label>`; }
function scopeInput() { return `<label>Scope <textarea name="scope" placeholder='{"language":"python"}'></textarea></label><span class="scope-hint">Optional JSON filter, e.g. {"registry":"npm"}</span>`; }
function installationEnvironmentFields() {
  return `<div class="form-grid">
    <label>Target OS <select name="target_os"><option value="">Choose OS</option><option value="linux">Linux</option><option value="macos">macOS</option><option value="windows">Windows</option></select></label>
    ${input("OS version", "os_version", "", "text", "e.g. macOS 15")}
    <label>Package manager <select name="package_manager"><option value="">Choose manager</option><option value="apt">apt</option><option value="brew">Homebrew</option><option value="npm">npm</option><option value="pip">pip</option></select></label>
    <label>Distribution <select name="distribution"><option value="">If applicable</option><option value="debian">Debian</option><option value="ubuntu">Ubuntu</option></select></label>
    ${input("Distribution release", "distribution_release", "", "text", "e.g. 13 or 24.04")}
    <label>Architecture <select name="architecture"><option value="">Choose architecture</option><option value="amd64">amd64 / x86_64</option><option value="arm64">arm64 / Apple Silicon</option></select></label>
    ${input("Runtime version", "runtime_version", "", "text", "e.g. Node.js 24 or Python 3.12")}
    <label>Install scope <select name="install_scope"><option value="">Choose scope</option><option value="project">Project / virtualenv</option><option value="user">User</option></select></label>
    <label>Administrator access <select name="has_admin"><option value="">Unknown</option><option value="true">Available</option><option value="false">Unavailable</option></select></label>
    ${input("Virtualenv bin directory", "virtualenv_bin", "", "text", "for project pip, e.g. /app/.venv/bin")}
  </div><label>Advanced environment overrides (JSON) <textarea name="environment" placeholder='{"execution_context":"container"}'></textarea></label><span class="scope-hint">Advanced JSON overrides the selected values.</span><label>Preferences (JSON) <textarea name="preferences" placeholder='{"package":"example"}'></textarea></label>`;
}
function installationEnvironmentFromForm(form) {
  const environment = {};
  for (const [inputName, fieldName] of [["target_os", "os"], ["os_version", "os_version"], ["package_manager", "package_manager"], ["distribution", "distribution"], ["distribution_release", "distribution_release"], ["architecture", "architecture"], ["runtime_version", "runtime_version"], ["install_scope", "install_scope"], ["virtualenv_bin", "virtualenv_bin"]]) {
    const value = String(form.get(inputName) || "").trim();
    if (value) environment[fieldName] = value;
  }
  const hasAdmin = String(form.get("has_admin") || "");
  if (hasAdmin) environment.has_admin = hasAdmin === "true";
  const overrides = JSON.parse(String(form.get("environment") || "{}"));
  if (!overrides || typeof overrides !== "object" || Array.isArray(overrides)) throw new Error("Environment overrides must be a JSON object.");
  const target = {...environment, ...overrides};
  if (target.os === "macos" && target.os_version && !target.distribution_release) target.distribution_release = target.os_version;
  return target;
}
function renderInstallationCard(result) {
  const card = $("installation-card");
  if (!card?.replaceChildren) return;
  card.replaceChildren();
  const items = result?.results || (result?.contract_version === "installation-v1" ? [result] : []);
  card.hidden = !items.length;
  for (const item of items) {
    const title = document.createElement("strong");
    title.textContent = `${item.name}: ${item.status}`;
    card.append(title);
    const explanation = document.createElement("span");
    explanation.textContent = item.status === "unverified"
      ? "The package mapping or target applicability has not been verified. Check these steps on the target before relying on them."
      : item.required_context?.length ? `Needed context: ${item.required_context.join(", ")}` : "Review coverage and provider evidence below.";
    card.append(explanation);
    const validation = item.verification?.recipe_validation;
    if (validation) {
      const detail = document.createElement("span");
      const target = typeof validation.target === "string" ? validation.target : JSON.stringify(validation.target);
      detail.textContent = `Install and invoke check: ${validation.result} on ${target} (${validation.observed_at})`;
      card.append(detail);
      if (/^https:\/\//.test(validation.evidence_url || "")) {
        const evidence = document.createElement("a");
        evidence.href = validation.evidence_url;
        evidence.textContent = "Verification run";
        evidence.target = "_blank";
        evidence.rel = "noopener noreferrer";
        card.append(evidence);
      }
    }
    for (const step of item.steps || []) {
      const line = document.createElement("span");
      line.textContent = `${step.action}: ${step.argv.join(" ")}`;
      card.append(line);
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-button";
      copy.textContent = `Copy ${step.action} argv`;
      copy.addEventListener("click", () => navigator.clipboard?.writeText(JSON.stringify(step.argv)));
      card.append(copy);
    }
    if (item.provider?.repository) {
      const link = document.createElement("a");
      if (/^https:\/\//.test(item.provider.repository)) {
        link.href = item.provider.repository;
        link.textContent = "Provider source";
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        card.append(link);
      }
    }
    if (/^https:\/\//.test(item.method?.documentation || "")) {
      const method = document.createElement("a");
      method.href = item.method.documentation;
      method.textContent = "Installation method documentation";
      method.target = "_blank";
      method.rel = "noopener noreferrer";
      card.append(method);
    }
  }
}
function renderForm() {
  const form = $("query-form");
  const operation = state.operation;
  const titles = { check_executable: "check_executable", check_executables: "check_executables", get_executable: "get_executable", search_executables: "search_executables", search_similar_executables: "search_similar_executables", assess_executable: "assess_executable", resolve_installation: "resolve_installation", resolve_installations: "resolve_installations", get_coverage: "get_coverage" };
  $("operation-title").textContent = titles[operation];
  if (operation === "get_coverage") { form.innerHTML = `<div class="run-row"><span class="scope-hint">Return snapshot, source coverage, and freshness context.</span><button class="run-button">Run query ↗</button></div>`; return; }
  let fields = "";
  if (operation === "resolve_installations") fields = `<label>Names <textarea name="names" placeholder="git\nblack">git\nblack</textarea></label>${installationEnvironmentFields()}`;
  else if (operation === "resolve_installation") fields = `${input("Executable name", "name", "git", "text", "e.g. git")}${installationEnvironmentFields()}`;
  else if (operation === "check_executables") fields = `<label>Names <textarea name="names" placeholder="envcp\nevpk\nphpunit">envcp\nevpk</textarea></label>${scopeInput()}`;
  else if (operation === "search_executables") fields = `<div class="form-grid">${input("Prefix", "prefix", "do", "text", "two or more characters")}${input("Length", "length", "", "number", "optional")}${input("Ecosystem", "ecosystem", "", "text", "npm, pypi, crates…")}${input("Limit", "limit", "25", "number", "1–500")}</div>${scopeInput()}`;
  else { fields = input("Executable name", "name", operation === "search_similar_executables" ? "kubctl" : "phpunit", "text", "e.g. docker"); if (operation === "search_similar_executables") fields += input("Limit", "limit", "20", "number", "1–100"); else fields += scopeInput(); }
  form.innerHTML = `${fields}<div class="run-row"><span class="scope-hint">Read-only · no request leaves GitHub Pages.</span><button class="run-button">Run query ↗</button></div>`;
}
async function runQuery(event) {
  event.preventDefault(); const form = new FormData(event.target); const op = state.operation; const scope = ["get_coverage", "resolve_installation", "resolve_installations"].includes(op) ? null : await scopeFromInput(String(form.get("scope") || ""));
  try { let result; if (op === "resolve_installation" || op === "resolve_installations") { const environment = installationEnvironmentFromForm(form); const preferences = JSON.parse(String(form.get("preferences") || "{}")); if (op === "resolve_installation") result = await resolveInstallation(String(form.get("name") || "").trim(), environment, preferences); else { const names = String(form.get("names") || "").split(/[,\n]/).map((name) => name.trim()).filter(Boolean); result = { results: await Promise.all(names.map((name) => resolveInstallation(name, environment, preferences))), snapshot: state.metadata.snapshot, contract_version: "installation-v1", warnings: coverageWarnings(state.metadata) }; } } else if (op === "get_coverage") result = { ...state.metadata, negative_lookup: coverageScope(state.metadata), warnings: coverageWarnings(state.metadata), freshness: state.status?.freshness || { status: "unavailable" } }; else if (op === "check_executable" || op === "get_executable" || op === "assess_executable") { const name = String(form.get("name") || "").trim(); if (op === "check_executable") result = await checkExecutable(name, scope); else if (op === "assess_executable") result = await assessExecutable(name, scope); else result = { record: await getExecutable(name), snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata, scope), scope }; } else if (op === "check_executables") { const names = String(form.get("names") || "").split(/[,\n]/).map((name) => name.trim()).filter(Boolean); result = { results: await Promise.all(names.map((name) => checkExecutable(name, scope))), snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata, scope), warnings: coverageWarnings(state.metadata) }; } else if (op === "search_executables") result = { executables: await searchExecutables({ prefix: String(form.get("prefix") || "").trim(), length: String(form.get("length") || "").trim(), ecosystem: String(form.get("ecosystem") || "").trim(), limit: form.get("limit"), scope }), snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata, scope), scope }; else result = { matches: await similar(String(form.get("name") || "").trim(), form.get("limit")), snapshot: state.metadata.snapshot, coverage_scope: coverageScope(state.metadata, scope), scope };
    state.result = result; renderInstallationCard(result); $("result").textContent = JSON.stringify(result, null, 2);
  } catch (error) { const result = { error: error.message }; state.result = result; renderInstallationCard(result); $("result").textContent = JSON.stringify(result, null, 2); }
}
async function boot() {
  try { state.metadata = await json(`${RAW_BASE}/data/metadata.json`); } catch (error) { $("result").textContent = JSON.stringify({ error: error.message }, null, 2); }
  try { state.status = await json("./status.json"); } catch { state.status = { crawl_report: { coverage_kind: "partial", status: "unavailable", sources: {} }, schedule: "47 4 * * *" }; }
  renderOverview(); renderForm();
  document.querySelectorAll(".operation").forEach((button) => button.addEventListener("click", () => { state.operation = button.dataset.operation; document.querySelectorAll(".operation").forEach((item) => item.classList.toggle("active", item === button)); renderForm(); }));
  $("query-form").addEventListener("submit", runQuery);
  $("copy-result").addEventListener("click", async () => { await navigator.clipboard?.writeText($("result").textContent); $("copy-result").textContent = "Copied"; setTimeout(() => { $("copy-result").textContent = "Copy JSON"; }, 1200); });
}
boot();
