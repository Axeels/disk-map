const CATEGORY_COLOR = {
  Folder: "#1f6f62",
  Video: "#ff6b4a",
  Audio: "#c084fc",
  Images: "#ffc14a",
  Archives: "#8b7cff",
  "Disk images": "#ff5d8f",
  Documents: "#3ddec8",
  Programs: "#5aa6ff",
  System: "#8b97a8",
  Dumps: "#ff8a3d",
  Dev: "#d6ef55",
  Other: "#5c6b73",
};
const DARK_TEXT = new Set(["Images", "Dev"]);

const state = {
  screen: "home",
  view: "map",
  overview: null,
  folder: null,
  full: false,
  selection: new Map(),
  anchor: null,
  displayRows: [],
  tableSort: { key: "size", dir: "desc" },
  tableLimit: 200,
  filePage: null,
  fileTimer: null,
  dups: null,
  dupMin: String(20 * 1024 * 1024),
  dupPolling: false,
  modalOpen: false,
  scanning: false,
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function formatBytes(value) {
  if (value == null || value === "" || Number.isNaN(Number(value))) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  if (index === 0) return `${Math.round(size)} B`;
  const digits = size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[index]}`;
}

function formatNum(value) {
  return Number(value || 0).toLocaleString();
}

function formatWhen(stamp) {
  if (!stamp) return "—";
  return new Date(stamp * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function ago(stamp) {
  if (!stamp) return "unknown age";
  const days = (Date.now() / 1000 - stamp) / 86400;
  if (days < 1) return "today";
  if (days < 45) return `${Math.round(days)} days ago`;
  if (days < 500) return `${Math.round(days / 30)} months ago`;
  return `${(days / 365).toFixed(1)} years ago`;
}

function pct(value) {
  const n = Number(value || 0);
  if (n <= 0) return "0%";
  if (n < 0.1) return "<0.1%";
  if (n >= 10) return `${n.toFixed(0)}%`;
  return `${n.toFixed(1)}%`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function norm(path) {
  return String(path || "").replace(/[\\/]+$/, "").toLowerCase();
}

function isUnder(child, parent) {
  const left = norm(child);
  const right = norm(parent);
  return left === right || left.startsWith(right + "\\");
}

function parentPath(path) {
  const trimmed = String(path || "").replace(/[\\/]+$/, "");
  const index = Math.max(trimmed.lastIndexOf("\\"), trimmed.lastIndexOf("/"));
  if (index < 0) return trimmed;
  if (index <= 2 && trimmed[1] === ":") return trimmed.slice(0, 3);
  return trimmed.slice(0, index);
}

async function api(url, { method = "GET", body } = {}) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { error: text };
  }
  if (!response.ok) {
    const error = new Error((data && data.error) || response.statusText || "Request failed");
    error.payload = data;
    throw error;
  }
  return data;
}

function toast(message, kind) {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast${kind ? ` ${kind}` : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add("hidden"), 4600);
}

function show(screen) {
  state.screen = screen;
  for (const name of ["home", "scan", "results"]) {
    $(`view-${name}`).classList.toggle("hidden", name !== screen);
  }
  renderHeader();
}

function renderHeader() {
  const meta = $("top-meta");
  const actions = $("top-actions");
  actions.innerHTML = "";
  const overview = state.overview;
  if (state.screen === "results" && overview) {
    const drive = overview.drive;
    const free = drive ? `${formatBytes(drive.free)} free on ${drive.path}` : "";
    meta.innerHTML = `<strong>${esc(overview.target)}</strong><br>${esc(formatBytes(overview.scanned_bytes))} counted${free ? ` · ${esc(free)}` : ""}`;
    actions.append(button("Change target", () => {
      show("home");
      loadHome();
    }));
    actions.append(button("Rescan", () => startScan(overview.target), "primary"));
  } else if (state.screen === "scan") {
    meta.textContent = "Scan in progress";
  } else {
    meta.textContent = overview ? "A scan is still loaded." : "Nothing scanned yet.";
    if (overview) actions.append(button("Back to results", () => show("results")));
  }
  actions.append(button("Quit", confirmQuit, "ghost"));
}

function button(label, onClick, kind) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = `btn${kind ? ` ${kind}` : ""}`;
  node.textContent = label;
  node.addEventListener("click", onClick);
  return node;
}

async function loadHome() {
  const host = $("view-home");
  host.innerHTML = `<div class="home"><div class="home-inner"><p class="kicker">Local only</p><h1>Loading drives…</h1></div></div>`;
  try {
    const data = await api("/api/home");
    renderHome(data.drives || [], data.shortcuts || []);
  } catch (error) {
    host.innerHTML = `<div class="home"><div class="home-inner"><h1>Couldn’t load drives</h1><p class="lede">${esc(error.message)}</p></div></div>`;
  }
}

function renderHome(drives, shortcuts) {
  const cards = drives.map((drive) => {
    const percent = drive.total ? (drive.used / drive.total) * 100 : 0;
    const tone = percent >= 90 ? "hot" : percent >= 75 ? "warn" : "";
    const title = drive.label ? `${drive.label}` : drive.kind;
    return `<article class="drive-card">
      <div class="drive-top">
        <div>
          <div class="drive-letter">${esc(drive.letter)}:</div>
          <div class="drive-kind">${esc(title)} · ${esc(drive.kind)}</div>
        </div>
        <div class="drive-free">${drive.ready ? `${esc(formatBytes(drive.free))} free` : "Not ready"}</div>
      </div>
      <div class="meter ${tone}"><span style="width:${Math.max(0, Math.min(100, percent))}%"></span></div>
      <div class="drive-foot"><span>${drive.ready ? esc(formatBytes(drive.used)) + " used" : "—"}</span><span>${drive.ready ? esc(formatBytes(drive.total)) : ""}</span></div>
      <button type="button" class="btn primary" data-scan="${esc(drive.path)}" ${drive.ready ? "" : "disabled"}>Scan ${esc(drive.letter)}:</button>
    </article>`;
  }).join("");
  const pills = shortcuts.map((item) => `<button type="button" class="pill" data-scan="${esc(item.path)}">${esc(item.label)}<small>${esc(item.path)}</small></button>`).join("");
  $("view-home").innerHTML = `<div class="home"><div class="home-inner">
    <div class="hero">
      <p class="kicker">Disk Map</p>
      <h1>What’s using your space?</h1>
      <p class="lede">Pick a drive, or start in a folder you already suspect. The map shows the heavy folders and files, including the ones buried in AppData, temp, caches, and old downloads.</p>
    </div>
    <div class="section-label">Drives</div>
    <div class="drive-grid">${cards || `<p class="fine">No drives were visible.</p>`}</div>
    <div class="section-label">Common hiding spots</div>
    <div class="pills">${pills}</div>
    <form class="path-card" id="path-form">
      <input id="path-input" placeholder="Or paste a folder path" spellcheck="false" />
      <button type="button" class="btn" id="browse">Browse</button>
      <button type="submit" class="btn primary">Scan folder</button>
    </form>
    <p class="safety">Removing a file sends it to the Recycle Bin unless you explicitly choose permanent delete. Whole drives, Windows, System32, and WinSxS stay blocked. Sizes include every file. Lists show the larger ones first, and any folder can list its small files on demand.</p>
  </div></div>`;
  $("view-home").querySelectorAll("[data-scan]").forEach((node) => {
    node.addEventListener("click", () => startScan(node.dataset.scan));
  });
  $("browse").addEventListener("click", browseFolder);
  $("path-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const path = $("path-input").value.trim();
    if (!path) {
      toast("Paste a folder path, or pick a drive.", "warn");
      return;
    }
    startScan(path);
  });
}

async function browseFolder() {
  const node = $("browse");
  node.disabled = true;
  node.textContent = "Waiting for folder…";
  try {
    const data = await api("/api/browse", { method: "POST", body: {} });
    if (data.path) startScan(data.path);
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    if (node.isConnected) {
      node.disabled = false;
      node.textContent = "Browse";
    }
  }
}

let scanToken = 0;

async function startScan(path) {
  const token = ++scanToken;
  try {
    await api("/api/scan", { method: "POST", body: { path } });
  } catch (error) {
    toast(error.message, "bad");
    return;
  }
  state.scanning = true;
  state.selection.clear();
  $("scan-target").textContent = path;
  $("scan-bytes").textContent = "0 B";
  $("scan-counts").textContent = "Starting…";
  $("scan-current").textContent = "";
  show("scan");
  pollScan(token);
}

async function pollScan(token) {
  while (state.screen === "scan" && token === scanToken) {
    let status;
    try {
      status = await api("/api/status");
    } catch (error) {
      toast(error.message, "bad");
      show("home");
      return;
    }
    if (token !== scanToken) return;
    paintScan(status);
    if (status.error) {
      toast(status.error, "bad");
      show("home");
      await loadHome();
      return;
    }
    if (!status.running && status.done) {
      state.scanning = false;
      await showResults();
      return;
    }
    if (!status.running && !status.done) {
      state.scanning = false;
      toast("Scan stopped before any results were ready.", "warn");
      show("home");
      await loadHome();
      return;
    }
    await sleep(350);
  }
}

function paintScan(status) {
  $("scan-target").textContent = status.target || "Scanning";
  $("scan-bytes").textContent = formatBytes(status.bytes || 0);
  const rate = status.elapsed > 0 ? Math.round((status.files || 0) / status.elapsed) : 0;
  $("scan-counts").textContent = `${formatNum(status.files)} files · ${formatNum(status.dirs)} folders · ${formatNum(rate)} files/sec`;
  $("scan-current").textContent = status.current || "";
}

async function showResults() {
  state.overview = await api("/api/overview");
  state.full = false;
  state.tableLimit = 200;
  $("folder-filter").value = "";
  state.folder = await api(folderUrl(state.overview.target, false));
  fillCategorySelect();
  show("results");
  setView("map", false);
  renderResults();
  if (state.overview.aborted) toast("Scan stopped early. Sizes below are a lower bound.", "warn");
}

function folderUrl(path, full) {
  const query = new URLSearchParams();
  if (path) query.set("path", path);
  if (full) query.set("full", "1");
  return `/api/folder?${query.toString()}`;
}

function fillCategorySelect() {
  const select = $("file-cat");
  const current = select.value;
  const names = (state.overview.categories || []).map((item) => item.name);
  select.innerHTML = `<option value="">All types</option>${names.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("")}`;
  select.value = current;
}

function setView(view, render = true) {
  state.view = view;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === view);
  });
  $("panel-map").classList.toggle("hidden", view !== "map");
  $("panel-files").classList.toggle("hidden", view !== "files");
  $("panel-insights").classList.toggle("hidden", view !== "insights");
  if (!render) return;
  if (view === "map") requestAnimationFrame(drawTreemap);
  if (view === "files") loadFiles();
  if (view === "insights") renderInsights();
}

function renderResults() {
  renderBanners();
  renderCrumbs();
  renderFolderHead();
  renderLegend();
  renderFolderTable();
  requestAnimationFrame(drawTreemap);
  renderInspector();
  if (state.view === "files") loadFiles();
  if (state.view === "insights") renderInsights();
}

function renderBanners() {
  const overview = state.overview;
  const notes = [];
  if (overview.aborted) {
    notes.push(`<div class="banner">Scan stopped early. These sizes are a lower bound — some folders were not fully read.</div>`);
  }
  if (overview.denied_count) {
    notes.push(`<div class="banner info">Windows blocked ${formatNum(overview.denied_count)} folder${overview.denied_count === 1 ? "" : "s"}. The rest of the scan is still usable. Details are in Insights.</div>`);
  }
  $("banners").innerHTML = notes.join("");
}

function renderCrumbs() {
  const crumbs = (state.folder && state.folder.crumbs) || [];
  $("crumb").innerHTML = crumbs.map((crumb, index) => {
    const sep = index ? `<span class="sep">/</span>` : "";
    return `${sep}<button type="button" data-crumb="${esc(crumb.path)}">${esc(crumb.name)}</button>`;
  }).join("");
}

function renderFolderHead() {
  const folder = state.folder;
  if (!folder) return;
  $("folder-title").textContent = folder.name;
  const other = folder.other;
  const extra = other ? ` · ${formatNum(other.count)} smaller files bundled (${formatBytes(other.bytes)})` : "";
  const fileLabel = `${formatNum(folder.file_count)} file${folder.file_count === 1 ? "" : "s"}`;
  $("folder-sub").textContent = `${formatBytes(folder.size)} · ${fileLabel}${extra}`;
  $("list-all").textContent = state.full ? "Show summary" : "Show every file here";
}

function renderLegend() {
  const seen = new Set();
  for (const item of (state.folder && state.folder.treemap) || []) {
    if (item.kind === "dir") seen.add("Folder");
    else seen.add(item.category || "Other");
  }
  $("legend").innerHTML = [...seen].map((name) => `<span><i class="swatch" style="background:${colorFor({ kind: name === "Folder" ? "dir" : "file", category: name }, 0)}"></i>${esc(name)}</span>`).join("");
}

function colorFor(entry, index) {
  if (entry.kind === "dir") {
    const hue = 158 + (index * 29) % 46;
    const light = 18 + Math.min(16, (entry.percent || 0) / 6);
    return `hsl(${hue} 34% ${light}%)`;
  }
  if (entry.kind === "other") return "#2c3834";
  return CATEGORY_COLOR[entry.category] || CATEGORY_COLOR.Other;
}

function drawTreemap() {
  const host = $("treemap");
  if (!host || state.screen !== "results" || state.view !== "map") return;
  const width = host.clientWidth;
  const height = host.clientHeight;
  const items = (state.folder && state.folder.treemap) || [];
  host.innerHTML = "";
  if (!items.length || width < 8 || height < 8) {
    host.innerHTML = `<div class="empty">Nothing in this folder has a size to draw.</div>`;
    return;
  }
  const rects = treemapRects(items, width, height);
  for (const [index, rect] of rects.entries()) {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "cell";
    if (rect.kind !== "dir" && DARK_TEXT.has(rect.category)) cell.classList.add("dark-text");
    if (rect.path && state.selection.has(rect.path)) cell.classList.add("selected");
    cell.style.left = `${rect.x}px`;
    cell.style.top = `${rect.y}px`;
    cell.style.width = `${Math.max(rect.w, 1)}px`;
    cell.style.height = `${Math.max(rect.h, 1)}px`;
    cell.style.background = colorFor(rect, index);
    cell.dataset.kind = rect.kind;
    if (rect.path) cell.dataset.path = rect.path;
    const showName = rect.w > 72 && rect.h > 28;
    const showSize = rect.w > 72 && rect.h > 46;
    cell.innerHTML = `${showName ? `<span class="nm">${esc(rect.name)}</span>` : ""}${showSize ? `<span class="sz">${esc(formatBytes(rect.size))}</span>` : ""}`;
    cell.title = `${rect.name} · ${formatBytes(rect.size)}`;
    host.append(cell);
  }
}

function normalizeSizes(sizes, dx, dy) {
  const total = sizes.reduce((sum, value) => sum + value, 0);
  const area = dx * dy;
  return sizes.map((value) => (value * area) / total);
}

function layoutRow(sizes, x, y, dx, dy) {
  const width = sizes.reduce((sum, value) => sum + value, 0) / dy;
  const rects = [];
  for (const size of sizes) {
    const h = size / width;
    rects.push({ x, y, dx: width, dy: h });
    y += h;
  }
  return rects;
}

function layoutCol(sizes, x, y, dx, dy) {
  const height = sizes.reduce((sum, value) => sum + value, 0) / dx;
  const rects = [];
  for (const size of sizes) {
    const w = size / height;
    rects.push({ x, y, dx: w, dy: height });
    x += w;
  }
  return rects;
}

function layout(sizes, x, y, dx, dy) {
  return dx >= dy ? layoutRow(sizes, x, y, dx, dy) : layoutCol(sizes, x, y, dx, dy);
}

function leftover(sizes, x, y, dx, dy) {
  if (dx >= dy) {
    const width = sizes.reduce((sum, value) => sum + value, 0) / dy;
    return [x + width, y, dx - width, dy];
  }
  const height = sizes.reduce((sum, value) => sum + value, 0) / dx;
  return [x, y + height, dx, dy - height];
}

function worstRatio(sizes, x, y, dx, dy) {
  let worst = 0;
  for (const rect of layout(sizes, x, y, dx, dy)) {
    if (!(rect.dx > 0) || !(rect.dy > 0)) return Infinity;
    worst = Math.max(worst, rect.dx / rect.dy, rect.dy / rect.dx);
  }
  return worst || Infinity;
}

function squarify(sizes, x, y, dx, dy) {
  if (!sizes.length || dx < 1 || dy < 1) return [];
  if (sizes.length === 1) return layout(sizes, x, y, dx, dy);
  let index = 1;
  while (
    index < sizes.length &&
    worstRatio(sizes.slice(0, index), x, y, dx, dy) >= worstRatio(sizes.slice(0, index + 1), x, y, dx, dy)
  ) {
    index += 1;
  }
  const current = sizes.slice(0, index);
  const rest = sizes.slice(index);
  const [lx, ly, lw, lh] = leftover(current, x, y, dx, dy);
  return layout(current, x, y, dx, dy).concat(squarify(rest, lx, ly, Math.max(lw, 0), Math.max(lh, 0)));
}

function treemapRects(items, width, height) {
  const sorted = items.filter((item) => item.size > 0).slice().sort((a, b) => b.size - a.size);
  if (!sorted.length) return [];
  const normed = normalizeSizes(sorted.map((item) => item.size), width, height);
  const rects = squarify(normed, 0, 0, width, height);
  return sorted.map((item, index) => {
    const rect = rects[index] || { x: 0, y: 0, dx: 0, dy: 0 };
    let x = rect.x + 1.5;
    let y = rect.y + 1.5;
    let w = rect.dx - 3;
    let h = rect.dy - 3;
    if (w < 1) { x = rect.x; w = Math.max(rect.dx, 1); }
    if (h < 1) { y = rect.y; h = Math.max(rect.dy, 1); }
    return { ...item, x, y, w, h };
  });
}

function visibleFolderRows() {
  const query = $("folder-filter").value.trim().toLowerCase();
  let rows = (state.folder && state.folder.entries) || [];
  if (query) rows = rows.filter((row) => (row.name || "").toLowerCase().includes(query));
  const { key, dir } = state.tableSort;
  const mult = dir === "asc" ? 1 : -1;
  rows = rows.slice().sort((a, b) => {
    let left;
    let right;
    if (key === "name") {
      left = (a.name || "").toLowerCase();
      right = (b.name || "").toLowerCase();
    } else if (key === "mtime") {
      left = a.mtime || 0;
      right = b.mtime || 0;
    } else if (key === "category") {
      left = (a.category || "").toLowerCase();
      right = (b.category || "").toLowerCase();
    } else {
      left = a.size || 0;
      right = b.size || 0;
    }
    if (left < right) return -1 * mult;
    if (left > right) return 1 * mult;
    return 0;
  });
  return rows;
}

function renderFolderTable() {
  const rows = visibleFolderRows();
  state.displayRows = rows.slice(0, state.tableLimit);
  const arrow = (key) => (state.tableSort.key === key ? (state.tableSort.dir === "asc" ? " ↑" : " ↓") : "");
  const body = state.displayRows.map((row, index) => {
    const selected = row.path && state.selection.has(row.path);
    const kind = row.kind === "dir" ? "Folder" : (row.ext || row.category || "File");
    return `<tr class="${selected ? "selected" : ""}" data-index="${index}" data-kind="${esc(row.kind)}" ${row.path ? `data-path="${esc(row.path)}"` : ""}>
      <td>${row.path ? `<input type="checkbox" ${selected ? "checked" : ""} />` : ""}</td>
      <td><div class="name-cell"><b>${esc(row.name)}</b>${row.error ? `<span class="badge bad">No access</span>` : ""}</div></td>
      <td>${esc(kind)}</td>
      <td class="mono">${esc(formatBytes(row.size))}<div class="meter row-meter"><span style="width:${Math.min(100, row.percent || 0)}%"></span></div></td>
      <td>${esc(pct(row.percent))}</td>
      <td>${row.kind === "file" ? esc(formatWhen(row.mtime)) : "—"}</td>
    </tr>`;
  }).join("");
  const more = rows.length > state.displayRows.length
    ? `<tr><td colspan="6"><button type="button" class="btn small" id="show-more-rows">Show more (${formatNum(rows.length - state.displayRows.length)} hidden)</button></td></tr>`
    : "";
  $("folder-table").innerHTML = rows.length
    ? `<table><thead><tr>
        <th><input type="checkbox" id="select-shown" /></th>
        <th><button type="button" data-sort="name">Name${arrow("name")}</button></th>
        <th><button type="button" data-sort="category">Type${arrow("category")}</button></th>
        <th><button type="button" data-sort="size">Size${arrow("size")}</button></th>
        <th>Share</th>
        <th><button type="button" data-sort="mtime">Modified${arrow("mtime")}</button></th>
      </tr></thead><tbody>${body}${more}</tbody></table>`
    : `<div class="empty">Nothing in this folder matches.</div>`;
  const selectShown = $("select-shown");
  if (selectShown) {
    const selectable = state.displayRows.filter((row) => row.path);
    selectShown.checked = selectable.length > 0 && selectable.every((row) => state.selection.has(row.path));
  }
}

function entryFromRow(row) {
  return {
    kind: row.kind,
    name: row.name,
    path: row.path,
    size: row.size,
    mtime: row.mtime || 0,
    ext: row.ext || "",
    category: row.category || (row.kind === "dir" ? "Folder" : "Other"),
    percent: row.percent || 0,
    file_count: row.file_count,
    error: row.error || "",
    tip: row.tip || "",
  };
}

function setSelection(entries, { additive = false } = {}) {
  if (!additive) state.selection.clear();
  for (const entry of entries) {
    if (!entry || !entry.path || entry.kind === "other") continue;
    state.selection.set(entry.path, entryFromRow(entry));
  }
  paintSelection();
}

function toggleSelection(entry) {
  if (!entry || !entry.path) return;
  if (state.selection.has(entry.path)) state.selection.delete(entry.path);
  else state.selection.set(entry.path, entryFromRow(entry));
  paintSelection();
}

function paintSelection() {
  document.querySelectorAll("[data-path]").forEach((node) => {
    const on = state.selection.has(node.dataset.path);
    node.classList.toggle("selected", on);
    const box = node.querySelector && node.querySelector('input[type="checkbox"]');
    if (box && document.activeElement !== box) box.checked = on;
  });
  const selectShown = $("select-shown");
  if (selectShown) {
    const selectable = state.displayRows.filter((row) => row.path);
    selectShown.checked = selectable.length > 0 && selectable.every((row) => state.selection.has(row.path));
  }
  renderInspector();
}

function renderInspector() {
  const host = $("inspector");
  const items = [...state.selection.values()];
  if (!items.length) {
    const folder = state.folder;
    host.innerHTML = `<p class="kicker">Inspector</p><h2>${esc(folder ? folder.name : "Nothing selected")}</h2>
      <div class="big-size">${esc(formatBytes(folder && folder.size))}</div>
      <p class="fine">Click a block or a row. Ctrl+click adds to the selection. Double-click a folder to open it. Delete sends the selection to the Recycle Bin.</p>
      ${folder && folder.path !== (state.overview && state.overview.target) ? `<div class="action-col"><button type="button" class="btn" id="select-current">Select this folder</button></div>` : ""}`;
    const selectCurrent = $("select-current");
    if (selectCurrent && folder) {
      selectCurrent.addEventListener("click", () => setSelection([folder]));
    }
    return;
  }
  const bytes = items.reduce((sum, item) => sum + (Number(item.size) || 0), 0);
  if (items.length === 1) {
    const item = items[0];
    host.innerHTML = `<p class="kicker">${esc(item.kind === "dir" ? "Folder" : item.category || "File")}</p>
      <h2>${esc(item.name)}</h2>
      <div class="big-size">${esc(formatBytes(item.size))}</div>
      <p class="fine">${esc(pct(item.percent))} of this folder${item.mtime ? ` · ${esc(ago(item.mtime))}` : ""}${item.file_count != null && item.kind === "dir" ? ` · ${esc(formatNum(item.file_count))} files` : ""}</p>
      <div class="path">${esc(item.path)}</div>
      ${item.tip ? `<p class="warn-note">${esc(item.tip)}</p>` : ""}
      ${item.error ? `<p class="warn-note">${esc(item.error)}</p>` : ""}
      <div class="action-col">
        <button type="button" class="btn primary" id="act-recycle">Move to Recycle Bin</button>
        <button type="button" class="btn" id="act-reveal">Reveal in Explorer</button>
        <button type="button" class="btn" id="act-open">${item.kind === "dir" ? "Open folder" : "Open"}</button>
        <button type="button" class="btn" id="act-copy">Copy path</button>
        <button type="button" class="btn danger" id="act-permanent">Delete permanently</button>
      </div>`;
  } else {
    host.innerHTML = `<p class="kicker">Selection</p><h2>${items.length} items</h2>
      <div class="big-size">${esc(formatBytes(bytes))}</div>
      <div class="path">${items.slice(0, 8).map((item) => esc(item.name)).join("<br>")}${items.length > 8 ? "<br>…" : ""}</div>
      <div class="action-col">
        <button type="button" class="btn primary" id="act-recycle">Move to Recycle Bin</button>
        <button type="button" class="btn" id="act-copy">Copy paths</button>
        <button type="button" class="btn ghost" id="act-clear">Clear selection</button>
        <button type="button" class="btn danger" id="act-permanent">Delete permanently</button>
      </div>`;
  }
  $("act-recycle")?.addEventListener("click", () => openDelete("recycle"));
  $("act-permanent")?.addEventListener("click", () => openDelete("permanent"));
  $("act-reveal")?.addEventListener("click", () => reveal(items[0].path));
  $("act-open")?.addEventListener("click", () => openItem(items[0]));
  $("act-copy")?.addEventListener("click", copySelection);
  $("act-clear")?.addEventListener("click", () => {
    state.selection.clear();
    paintSelection();
  });
}

async function loadFolder(path, { full = false } = {}) {
  state.full = full;
  try {
    state.folder = await api(folderUrl(path, full));
  } catch (error) {
    toast(error.message, "bad");
    return;
  }
  state.tableLimit = 200;
  renderCrumbs();
  renderFolderHead();
  renderLegend();
  renderFolderTable();
  drawTreemap();
  renderInspector();
}

async function loadFiles() {
  const overview = state.overview;
  if (!overview) return;
  const query = new URLSearchParams({
    q: $("file-search").value.trim(),
    min_bytes: $("file-min").value,
    category: $("file-cat").value,
    older_days: $("file-age").value,
    sort: "size",
    direction: "desc",
    offset: String(state.fileOffset || 0),
    limit: "200",
  });
  if ($("file-under").checked && state.folder) query.set("under", state.folder.path);
  try {
    state.filePage = await api(`/api/files?${query.toString()}`);
  } catch (error) {
    $("file-results").innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }
  renderFiles();
}

function renderFiles() {
  const page = state.filePage;
  if (!page) return;
  const minNote = state.overview
    ? `File search covers ${formatNum(state.overview.indexed_files)} files at ${formatBytes(state.overview.min_indexed_bytes)} and up${state.overview.index_capped ? " (the largest ones — the index is capped)" : ""}. Folder sizes still include everything smaller.`
    : "";
  $("file-note").textContent = `${formatNum(page.total)} matching files. ${minNote}`;
  const folders = (page.folders || []).map((folder) => `<tr data-kind="dir" data-path="${esc(folder.path)}">
      <td></td><td><b>${esc(folder.name)}</b></td><td>Folder</td><td class="mono">${esc(formatBytes(folder.size))}</td><td class="path-cell">${esc(folder.path)}</td>
    </tr>`).join("");
  const files = page.files.map((file) => {
    const selected = state.selection.has(file.path);
    return `<tr class="${selected ? "selected" : ""}" data-kind="file" data-path="${esc(file.path)}">
      <td><input type="checkbox" ${selected ? "checked" : ""} /></td>
      <td><b>${esc(file.name)}</b></td>
      <td>${esc(file.category)}</td>
      <td class="mono">${esc(formatBytes(file.size))}</td>
      <td>${esc(formatWhen(file.mtime))}</td>
      <td class="path-cell" title="${esc(file.path)}">${esc(file.path)}</td>
    </tr>`;
  }).join("");
  const folderHead = folders ? `<tr><td colspan="6" class="fine">Matching folders</td></tr>${folders}` : "";
  $("file-results").innerHTML = `<table><thead><tr><th></th><th>Name</th><th>Type</th><th>Size</th><th>Modified</th><th>Path</th></tr></thead><tbody>${folderHead}${files || `<tr><td colspan="6"><div class="empty">No files match.</div></td></tr>`}</tbody></table>`;
  state.fileRows = [...(page.folders || []), ...page.files];
  const offset = page.offset || 0;
  const shown = offset + page.files.length;
  $("file-pager").innerHTML = page.total > page.limit
    ? `<button type="button" class="btn small" id="file-prev" ${offset === 0 ? "disabled" : ""}>Previous</button><span>${formatNum(offset + 1)}–${formatNum(shown)} of ${formatNum(page.total)}</span><button type="button" class="btn small" id="file-next" ${shown >= page.total ? "disabled" : ""}>Next</button>`
    : "";
  $("file-prev")?.addEventListener("click", () => {
    state.fileOffset = Math.max(0, (state.fileOffset || 0) - 200);
    loadFiles();
  });
  $("file-next")?.addEventListener("click", () => {
    state.fileOffset = (state.fileOffset || 0) + 200;
    loadFiles();
  });
}

function renderInsights() {
  const overview = state.overview;
  if (!overview) return;
  const maxChild = Math.max(...(overview.root_children || []).map((item) => item.size), 1);
  const maxCat = Math.max(...(overview.categories || []).map((item) => item.bytes), 1);
  const maxAge = Math.max(...(overview.age || []).map((item) => item.bytes), 1);
  const childRows = (overview.root_children || []).map((item) => barRow(item.name, item.size, item.size / maxChild, item.path)).join("");
  const catRows = (overview.categories || []).map((item) => {
    const width = item.bytes / maxCat;
    return `<div class="bar-row"><button type="button" data-cat="${esc(item.name)}">${esc(item.name)}</button><div class="hbar"><span style="width:${width * 100}%"></span></div><span class="mono">${esc(formatBytes(item.bytes))}</span></div>`;
  }).join("");
  const ageRows = (overview.age || []).map((item) => `<div class="bar-row"><span>${esc(item.label)}</span><div class="hbar"><span style="width:${(item.bytes / maxAge) * 100}%"></span></div><span class="mono">${esc(formatBytes(item.bytes))}</span></div>`).join("");
  const extRows = (overview.extensions || []).slice(0, 8).map((item) => `<div class="bar-row"><span>${esc(item.ext)}</span><div class="hbar"><span style="width:${(item.bytes / maxCat) * 100}%"></span></div><span class="mono">${esc(formatBytes(item.bytes))}</span></div>`).join("");
  const clutter = (overview.clutter || []).map((item) => `<article class="clutter">
      <div><b>${esc(item.label)}</b> · ${esc(item.name)}<p>${esc(item.tip || item.path)}</p></div>
      <div class="action-col">
        <span class="mono">${esc(formatBytes(item.size))}</span>
        <button type="button" class="btn small" data-open="${esc(item.path)}">Show</button>
      </div>
    </article>`).join("");
  const largest = fileLines(overview.largest);
  const oldLarge = fileLines(overview.old_large);
  const surprises = (overview.surprises || []).map((item) => `<article class="clutter"><div><b>${esc(item.name)}</b><p>${esc(item.tip)}</p></div><span class="mono">${esc(formatBytes(item.size))}</span></article>`).join("");
  const empty = (overview.empty_dirs || []).slice(0, 12).map((path) => `<button type="button" class="pill" data-open="${esc(path)}">${esc(path.split(/[\\/]/).pop())}<small>${esc(path)}</small></button>`).join("");
  const denied = (overview.denied_paths || []).slice(0, 8).map((path) => `<div class="path-cell">${esc(path)}</div>`).join("");
  let gap = "";
  if (overview.is_drive_root && overview.unaccounted != null) {
    if (overview.unaccounted > 64 * 1024 * 1024) {
      gap = `<div class="banner">${esc(formatBytes(overview.unaccounted))} of the drive’s used space was not counted. That is usually protected folders, skipped links, or files Windows did not list.${overview.reparse_skipped ? ` ${formatNum(overview.reparse_skipped)} links were skipped so the same files were not counted twice.` : ""}</div>`;
    } else if (overview.unaccounted < -64 * 1024 * 1024) {
      gap = `<div class="banner info">The counted files add up to a bit more than the drive’s used space. Compression and links can do that.</div>`;
    } else {
      gap = `<div class="banner info">This scan accounts for the used space on the drive.</div>`;
    }
  }
  $("panel-insights").innerHTML = `
    <div class="stat-row">
      <div class="stat"><span>Counted</span><b>${esc(formatBytes(overview.scanned_bytes))}</b></div>
      <div class="stat"><span>Files</span><b>${esc(formatNum(overview.file_count))}</b></div>
      <div class="stat"><span>Folders</span><b>${esc(formatNum(overview.dir_count))}</b></div>
      <div class="stat"><span>Scan time</span><b>${esc(Number(overview.elapsed || 0).toFixed(1))}s</b></div>
    </div>
    ${gap}
    <div class="insight-grid">
      <section class="insight-card"><h3>Where the space is</h3>${childRows || `<p class="fine">No subfolders.</p>`}</section>
      <section class="insight-card"><h3>File types</h3>${catRows || `<p class="fine">No files.</p>`}<div style="height:10px"></div>${extRows}</section>
    </div>
    <section class="insight-card"><h3>Age of the files</h3>${ageRows}</section>
    <section class="insight-card"><h3>Worth a look</h3><div class="card-list">${clutter || `<p class="fine">No large caches, temp folders, or known space hogs stood out.</p>`}</div></section>
    <div class="insight-grid">
      <section class="insight-card"><h3>Largest files</h3><div class="card-list">${largest || `<p class="fine">No large files were indexed.</p>`}</div></section>
      <section class="insight-card"><h3>Large and untouched for 6 months</h3><div class="card-list">${oldLarge || `<p class="fine">Nothing over 100 MB has been sitting that long.</p>`}</div></section>
    </div>
    <section class="insight-card"><h3>System files and disk images</h3><div class="card-list">${surprises || `<p class="fine">No page file, hibernation file, disk image, or crash dump stood out.</p>`}</div></section>
    <section class="insight-card" id="dup-box"></section>
    <section class="insight-card"><h3>Empty folders${overview.empty_capped ? " (first batch)" : ""}</h3><p class="fine">${formatNum(overview.empty_total)} empty folder${overview.empty_total === 1 ? "" : "s"}. Some programs expect these folders to exist.</p><div class="pills">${empty || ""}</div></section>
    <section class="insight-card"><h3>Couldn’t read</h3>${denied || `<p class="fine">Every folder in the scan was readable.</p>`}</section>
  `;
  $("panel-insights").querySelectorAll("[data-open]").forEach((node) => {
    node.addEventListener("click", () => focusPath(node.dataset.open, "dir"));
  });
  $("panel-insights").querySelectorAll("[data-open-file]").forEach((node) => {
    node.addEventListener("click", () => focusPath(node.dataset.openFile, "file"));
  });
  $("panel-insights").querySelectorAll("[data-cat]").forEach((node) => {
    node.addEventListener("click", () => {
      $("file-cat").value = node.dataset.cat;
      state.fileOffset = 0;
      setView("files");
    });
  });
  renderDupBox();
}

function barRow(name, size, width, path) {
  return `<div class="bar-row"><button type="button" data-open="${esc(path)}">${esc(name)}</button><div class="hbar"><span style="width:${Math.max(1, width * 100)}%"></span></div><span class="mono">${esc(formatBytes(size))}</span></div>`;
}

function fileLines(files) {
  return (files || []).map((file) => `<button type="button" class="clutter" data-open-file="${esc(file.path)}" data-parent="${esc(parentPath(file.path))}">
    <span><b>${esc(file.name)}</b><p>${esc(ago(file.mtime))} · ${esc(file.category)}</p></span>
    <span class="mono">${esc(formatBytes(file.size))}</span>
  </button>`).join("");
}

function renderDupBox() {
  const host = $("dup-box");
  if (!host) return;
  const progress = host.querySelector("[data-dup-progress]");
  if (state.dups && state.dups.running && progress) {
    progress.textContent = `Checked ${formatNum(state.dups.done)} of ${formatNum(state.dups.total)}`;
    return;
  }
  const dups = state.dups || { groups: [], running: false, wasted: 0, note: "", error: null };
  const groups = (dups.groups || []).map((group, groupIndex) => {
    const files = group.files.map((file, fileIndex) => `<label class="check"><input type="checkbox" data-dup="${groupIndex}:${fileIndex}" /> ${esc(file.path)} <span class="fine">${esc(ago(file.mtime))}</span></label>`).join("");
    return `<article class="clutter" style="display:block"><b>${esc(formatBytes(group.size))} · ${group.files.length} copies · ${esc(formatBytes(group.wasted))} extra</b>
      ${group.partial ? `<p class="warn-note">Only a sample of these large files was compared. Open them and confirm they match before removing one.</p>` : `<p class="fine">Same size and same contents. Nothing is selected until you check it.</p>`}
      <div class="action-col" style="margin-top:8px">${files}
        ${group.partial ? "" : `<button type="button" class="btn small" data-keep-newest="${groupIndex}">Select older copies</button>`}
      </div></article>`;
  }).join("");
  host.innerHTML = `<h3>Duplicate files</h3>
    <p class="fine">Groups files that have the same size, then checks their contents. Useful for copied installers, videos, and downloads.</p>
    <div class="folder-tools" style="margin:10px 0">
      <select id="dup-min">
        <option value="${10 * 1024 * 1024}">10 MB and up</option>
        <option value="${20 * 1024 * 1024}">20 MB and up</option>
        <option value="${100 * 1024 * 1024}">100 MB and up</option>
        <option value="${500 * 1024 * 1024}">500 MB and up</option>
      </select>
      <button type="button" class="btn primary" id="dup-go">${dups.running ? "Searching…" : "Find duplicates"}</button>
    </div>
    <p class="fine" data-dup-progress>${dups.running ? `Checked ${formatNum(dups.done)} of ${formatNum(dups.total)}` : dups.error ? esc(dups.error) : dups.groups && dups.groups.length ? `${dups.groups.length} groups · ${esc(formatBytes(dups.wasted))} could be freed by keeping one of each` : "No duplicate search yet."}</p>
    ${dups.note ? `<p class="fine">${esc(dups.note)}</p>` : ""}
    <div class="card-list">${groups}</div>
    ${groups ? `<button type="button" class="btn" id="dup-recycle">Move checked copies to the Recycle Bin</button>` : ""}`;
  const select = $("dup-min");
  if (select) select.value = state.dupMin;
  select?.addEventListener("change", () => { state.dupMin = select.value; });
  $("dup-go")?.addEventListener("click", startDuplicates);
  $("dup-recycle")?.addEventListener("click", recycleCheckedDuplicates);
  host.querySelectorAll("[data-keep-newest]").forEach((node) => {
    node.addEventListener("click", () => {
      const group = dups.groups[Number(node.dataset.keepNewest)];
      if (!group) return;
      let keep = 0;
      group.files.forEach((file, index) => {
        if ((file.mtime || 0) > (group.files[keep].mtime || 0)) keep = index;
      });
      host.querySelectorAll(`[data-dup^="${node.dataset.keepNewest}:"]`).forEach((box) => {
        const index = Number(box.dataset.dup.split(":")[1]);
        box.checked = index !== keep;
      });
    });
  });
}

async function startDuplicates() {
  try {
    await api("/api/duplicates", { method: "POST", body: { min_bytes: Number(state.dupMin) } });
  } catch (error) {
    toast(error.message, "bad");
    return;
  }
  pollDuplicates();
}

async function pollDuplicates() {
  if (state.dupPolling) return;
  state.dupPolling = true;
  try {
    while (true) {
      state.dups = await api("/api/duplicates");
      if (state.view === "insights") renderDupBox();
      if (!state.dups.running) break;
      await sleep(700);
    }
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    state.dupPolling = false;
  }
}

function recycleCheckedDuplicates() {
  const paths = [];
  document.querySelectorAll("[data-dup]:checked").forEach((box) => {
    const [groupIndex, fileIndex] = box.dataset.dup.split(":").map(Number);
    const file = state.dups.groups[groupIndex] && state.dups.groups[groupIndex].files[fileIndex];
    if (file) paths.push(file);
  });
  if (!paths.length) {
    toast("Check the copies you want to remove.", "warn");
    return;
  }
  state.selection.clear();
  for (const file of paths) state.selection.set(file.path, entryFromRow(file));
  openDelete("recycle");
}

async function focusPath(path, kind) {
  if (kind === "file") {
    await loadFolder(parentPath(path));
    const entry = (state.folder.entries || []).find((item) => norm(item.path) === norm(path));
    if (entry) setSelection([entry]);
  } else {
    await loadFolder(path);
  }
  setView("map", false);
  requestAnimationFrame(drawTreemap);
}

async function openDelete(mode) {
  const paths = [...state.selection.keys()];
  if (!paths.length) {
    toast("Select a file or folder first.", "warn");
    return;
  }
  let plan;
  try {
    plan = await api("/api/delete", { method: "POST", body: { paths, mode, dry_run: true } });
  } catch (error) {
    toast(error.message, "bad");
    return;
  }
  if (plan.error) {
    toast(plan.error, "bad");
    return;
  }
  const ok = plan.items.filter((item) => !item.blocked);
  const blocked = plan.items.filter((item) => item.blocked);
  const warnings = [...new Set(ok.map((item) => item.warning).filter(Boolean))];
  const list = ok.slice(0, 12).map((item) => esc(item.path)).join("<br>");
  const extra = ok.length > 12 ? `<br>…and ${ok.length - 12} more` : "";
  const blockedHtml = blocked.length
    ? `<p class="warn-note">${blocked.length} item${blocked.length === 1 ? "" : "s"} will be skipped. ${esc(blocked[0].blocked)}</p>`
    : "";
  const warningHtml = warnings.map((note) => `<p class="warn-note">${esc(note)}</p>`).join("");
  const permanent = mode === "permanent";
  openModal({
    title: permanent ? "Delete permanently?" : "Move to the Recycle Bin?",
    body: `${permanent
      ? "<p>These items will be removed immediately. They will not go to the Recycle Bin.</p>"
      : "<p>You can restore these from the Recycle Bin later.</p>"}
      <p><strong>${esc(formatBytes(plan.bytes))}</strong> · ${ok.length} item${ok.length === 1 ? "" : "s"}</p>
      <div class="path-list">${list || "Nothing can be removed."}${extra}</div>
      ${warningHtml}${blockedHtml}
      ${permanent ? `<input id="confirm-delete" placeholder="Type DELETE to confirm" />` : ""}`,
    confirmLabel: permanent ? "Delete permanently" : "Move to Recycle Bin",
    danger: permanent,
    disabled: ok.length === 0,
    onConfirm: async ({ button: confirmButton, close }) => {
      if (permanent && $("confirm-delete").value !== "DELETE") {
        toast("Type DELETE in the box to confirm.", "warn");
        return;
      }
      confirmButton.disabled = true;
      confirmButton.textContent = "Removing…";
      try {
        const body = { paths: ok.map((item) => item.path), mode };
        if (permanent) body.confirm = "DELETE";
        const result = await api("/api/delete", { method: "POST", body });
        close();
        await afterDelete(result);
      } catch (error) {
        toast(error.message, "bad");
        confirmButton.disabled = false;
        confirmButton.textContent = permanent ? "Delete permanently" : "Move to Recycle Bin";
      }
    },
  });
}

async function afterDelete(result) {
  const removed = (result.results || []).filter((item) => item.ok);
  const failed = (result.results || []).filter((item) => !item.ok);
  const verb = result.mode === "permanent" ? "Deleted" : "Moved to the Recycle Bin";
  if (removed.length) toast(`${verb}: ${removed.length} item${removed.length === 1 ? "" : "s"}${failed.length ? `. ${failed.length} failed.` : ""}`);
  else toast(failed[0] ? failed[0].error : "Nothing was removed.", "bad");
  const current = state.folder && state.folder.path;
  const lost = current && removed.some((item) => isUnder(current, item.path));
  state.selection.clear();
  state.overview = await api("/api/overview");
  renderHeader();
  renderBanners();
  if (lost) {
    const gone = removed.find((item) => isUnder(current, item.path));
    await loadFolder(parentPath(gone.path));
  } else if (current) {
    await loadFolder(current, { full: state.full });
  }
  if (state.view === "files") await loadFiles();
  if (state.view === "insights") renderInsights();
}

function openModal({ title, body, confirmLabel, danger, disabled, onConfirm }) {
  state.modalOpen = true;
  $("modal-root").innerHTML = `<div class="modal-back"><div class="modal" role="dialog" aria-modal="true">
    <h3>${esc(title)}</h3>
    ${body}
    <div class="modal-actions">
      <button type="button" class="btn" id="modal-cancel">Cancel</button>
      <button type="button" class="btn ${danger ? "danger" : "primary"}" id="modal-ok" ${disabled ? "disabled" : ""}>${esc(confirmLabel)}</button>
    </div>
  </div></div>`;
  const close = () => {
    $("modal-root").innerHTML = "";
    state.modalOpen = false;
  };
  $("modal-cancel").addEventListener("click", close);
  $("modal-ok").addEventListener("click", () => onConfirm({ button: $("modal-ok"), close }));
  ($("confirm-delete") || $("modal-ok")).focus();
}

function confirmQuit() {
  openModal({
    title: "Stop Disk Map?",
    body: "<p>The page will disconnect. Start it again by double-clicking Start Disk Map.bat.</p>",
    confirmLabel: "Stop",
    danger: true,
    onConfirm: async ({ close }) => {
      close();
      try { await api("/api/shutdown", { method: "POST", body: {} }); } catch { /* server is gone */ }
      document.body.innerHTML = `<div class="scan-wrap"><div class="scan-card"><p class="kicker">Stopped</p><h2>Disk Map has stopped.</h2><p class="lede">You can close this tab.</p></div></div>`;
    },
  });
}

async function reveal(path) {
  try {
    await api("/api/reveal", { method: "POST", body: { path } });
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function openItem(item) {
  if (item.kind === "dir") {
    await loadFolder(item.path);
    setView("map", false);
    requestAnimationFrame(drawTreemap);
    return;
  }
  try {
    await api("/api/open", { method: "POST", body: { path: item.path } });
  } catch (error) {
    toast(error.message, "bad");
  }
}

async function copySelection() {
  const text = [...state.selection.keys()].join("\r\n");
  try {
    await navigator.clipboard.writeText(text);
    toast("Path copied.");
  } catch {
    toast("Couldn’t copy to the clipboard.", "bad");
  }
}

function onFolderClick(event) {
  const sort = event.target.closest("[data-sort]");
  if (sort) {
    const key = sort.dataset.sort;
    if (state.tableSort.key === key) state.tableSort.dir = state.tableSort.dir === "asc" ? "desc" : "asc";
    else state.tableSort = { key, dir: key === "name" ? "asc" : "desc" };
    renderFolderTable();
    return;
  }
  if (event.target.id === "show-more-rows") {
    state.tableLimit += 200;
    renderFolderTable();
    return;
  }
  if (event.target.id === "select-shown") {
    const on = event.target.checked;
    for (const row of state.displayRows) {
      if (!row.path) continue;
      if (on) state.selection.set(row.path, entryFromRow(row));
      else state.selection.delete(row.path);
    }
    paintSelection();
    return;
  }
  const rowEl = event.target.closest("tr[data-index]");
  if (!rowEl) return;
  const row = state.displayRows[Number(rowEl.dataset.index)];
  if (!row || row.kind === "other" || !row.path) {
    if (row && row.kind === "other") toast("These small files are inside the folder total. Use “Show every file here” to pick them one by one.", "warn");
    return;
  }
  if (event.target.matches('input[type="checkbox"]')) {
    toggleSelection(row);
    state.anchor = Number(rowEl.dataset.index);
    return;
  }
  if (event.shiftKey && state.anchor != null) {
    const start = Math.min(state.anchor, Number(rowEl.dataset.index));
    const end = Math.max(state.anchor, Number(rowEl.dataset.index));
    if (!event.ctrlKey && !event.metaKey) state.selection.clear();
    for (let index = start; index <= end; index += 1) {
      const item = state.displayRows[index];
      if (item && item.path) state.selection.set(item.path, entryFromRow(item));
    }
    paintSelection();
    return;
  }
  state.anchor = Number(rowEl.dataset.index);
  if (event.ctrlKey || event.metaKey) toggleSelection(row);
  else setSelection([row]);
}

function onFolderDblClick(event) {
  const rowEl = event.target.closest("tr[data-index]");
  if (!rowEl) return;
  const row = state.displayRows[Number(rowEl.dataset.index)];
  if (!row) return;
  if (row.kind === "dir") loadFolder(row.path);
  else if (row.kind === "file") reveal(row.path);
}

function onMapClick(event) {
  const cell = event.target.closest(".cell");
  if (!cell) return;
  if (cell.dataset.kind === "other" || !cell.dataset.path) {
    toast("That block is a bundle of smaller items. Open the list below to pick a specific file.", "warn");
    return;
  }
  const entry = ((state.folder && state.folder.treemap) || []).find((item) => item.path === cell.dataset.path);
  if (!entry) return;
  if (event.ctrlKey || event.metaKey) toggleSelection(entry);
  else setSelection([entry]);
}

function onMapDblClick(event) {
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.kind !== "dir" || !cell.dataset.path) return;
  loadFolder(cell.dataset.path);
}

function onFileClick(event) {
  const rowEl = event.target.closest("tr[data-path]");
  if (!rowEl || !$("file-results").contains(rowEl)) return;
  const entry = (state.fileRows || []).find((item) => item.path === rowEl.dataset.path);
  if (!entry) return;
  if (rowEl.dataset.kind === "dir" && !event.target.matches('input[type="checkbox"]')) {
    focusPath(entry.path, "dir");
    return;
  }
  if (event.target.matches('input[type="checkbox"]')) toggleSelection(entry);
  else if (event.ctrlKey || event.metaKey) toggleSelection(entry);
  else setSelection([entry]);
}

function showTip(event) {
  const cell = event.target.closest(".cell");
  const tip = $("tip");
  if (!cell) {
    tip.classList.add("hidden");
    return;
  }
  const entry = ((state.folder && state.folder.treemap) || []).find((item) => item.path && item.path === cell.dataset.path) || { name: cell.title, size: 0 };
  tip.innerHTML = `<b>${esc(entry.name || "Item")}</b><span>${esc(formatBytes(entry.size))} · ${esc(pct(entry.percent))}${entry.path ? `<br>${esc(entry.path)}` : ""}</span>`;
  tip.classList.remove("hidden");
  const x = Math.min(event.clientX + 14, window.innerWidth - 340);
  const y = Math.min(event.clientY + 14, window.innerHeight - 90);
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

function wire() {
  $("scan-cancel").addEventListener("click", async () => {
    $("scan-cancel").disabled = true;
    try { await api("/api/cancel", { method: "POST", body: {} }); } catch (error) { toast(error.message, "bad"); }
  });
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  });
  $("crumb").addEventListener("click", (event) => {
    const crumb = event.target.closest("[data-crumb]");
    if (crumb) loadFolder(crumb.dataset.crumb);
  });
  $("folder-filter").addEventListener("input", () => {
    state.tableLimit = 200;
    renderFolderTable();
  });
  $("list-all").addEventListener("click", () => {
    if (!state.folder) return;
    loadFolder(state.folder.path, { full: !state.full });
  });
  $("folder-table").addEventListener("click", onFolderClick);
  $("folder-table").addEventListener("dblclick", onFolderDblClick);
  $("treemap").addEventListener("click", onMapClick);
  $("treemap").addEventListener("dblclick", onMapDblClick);
  $("treemap").addEventListener("mousemove", showTip);
  $("treemap").addEventListener("mouseleave", () => $("tip").classList.add("hidden"));
  $("file-results").addEventListener("click", onFileClick);
  $("file-results").addEventListener("dblclick", (event) => {
    const rowEl = event.target.closest("tr[data-path]");
    if (!rowEl) return;
    if (rowEl.dataset.kind === "dir") focusPath(rowEl.dataset.path, "dir");
    else reveal(rowEl.dataset.path);
  });
  for (const id of ["file-search", "file-min", "file-cat", "file-age", "file-under"]) {
    $(id).addEventListener("input", () => {
      state.fileOffset = 0;
      clearTimeout(state.fileTimer);
      state.fileTimer = setTimeout(loadFiles, id === "file-search" ? 220 : 0);
    });
    $(id).addEventListener("change", () => {
      state.fileOffset = 0;
      loadFiles();
    });
  }
  $("export-csv").addEventListener("click", () => { window.location = "/api/export.csv"; });
  document.addEventListener("keydown", (event) => {
    if (state.modalOpen) {
      if (event.key === "Escape") {
        $("modal-root").innerHTML = "";
        state.modalOpen = false;
      }
      return;
    }
    const typing = event.target.matches("input, textarea, select");
    if (event.key === "Escape") {
      state.selection.clear();
      paintSelection();
    }
    if (typing) return;
    if (event.key === "Delete" && state.selection.size) {
      event.preventDefault();
      openDelete("recycle");
    }
    if (event.key === "Backspace" && state.screen === "results" && state.view === "map") {
      const crumbs = state.folder && state.folder.crumbs;
      if (crumbs && crumbs.length > 1) {
        event.preventDefault();
        loadFolder(crumbs[crumbs.length - 2].path);
      }
    }
  });
  const observer = new ResizeObserver(() => {
    if (state.screen === "results" && state.view === "map") drawTreemap();
  });
  observer.observe($("treemap"));
}

wire();
show("home");
loadHome();
