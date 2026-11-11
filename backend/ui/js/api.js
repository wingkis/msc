/**
 * MSC Memory — API Client
 * Lightweight helper for MSC REST API calls
 */
const API_BASE = '/api/v1';

// LAN mode: no token, no login. The backend runs with MSC_AUTH_ENABLED=false,
// so every request below goes out unauthenticated on purpose.
function getHeaders(extra = {}) {
  return { 'Content-Type': 'application/json', ...extra };
}

// FastAPI reports validation failures with detail as a LIST of objects, so
// String(detail) gave "[object Object]" — an out-of-range 描述并发 looked like
// a broken page rather than "8 is the max". Flatten it into readable text.
function apiErrorText(data, status) {
  const detail = data && data.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map(item => {
      if (!item || !item.msg) return '';
      const where = Array.isArray(item.loc)
        ? item.loc.filter(part => part !== 'body').join('.') : '';
      return where ? `${where}: ${item.msg}` : String(item.msg);
    }).filter(Boolean);
    if (parts.length) return parts.join('；');
  }
  return `HTTP ${status}`;
}

async function apiFetch(path, options = {}) {
  try {
    const res = await fetch(API_BASE + path, {
      ...options,
      headers: { ...getHeaders(options.headers || {}), ...options.headers }
    });
    // Parse defensively: a plain-text 500 ("Internal Server Error") would
    // otherwise surface as a raw JSON.parse SyntaxError in the UI.
    const text = await res.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (e) {
        data = { detail: text.slice(0, 200) };
      }
    }
    if (!res.ok) throw new Error(apiErrorText(data, res.status));
    return data;
  } catch (e) {
    console.error('API error:', path, e);
    throw e;
  }
}

// ── Public API Methods ──────────────────────────────────────────────
const API = {
  // Config — reports whether the server needs auth ({auth_enabled, mode})
  getConfig() {
    return apiFetch('/config');
  },
  getHealth() {
    return apiFetch('/health');
  },
  getStats() {
    return apiFetch('/stats');
  },

  // Memories
  listMemories(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return apiFetch('/memories?' + qs);
  },
  getMemory(slug) {
    // include_archived widens resolution only: live memories are returned the
    // same way, but soft-deleted ones stop 404-ing when the UI opens them.
    return apiFetch(`/memories/${encodeURIComponent(slug)}?include_archived=true`);
  },
  createMemory(data) {
    return apiFetch('/memories', { method: 'POST', body: JSON.stringify(data) });
  },
  updateMemory(slug, data) {
    return apiFetch(`/memories/${encodeURIComponent(slug)}`, { method: 'PUT', body: JSON.stringify(data) });
  },
  deleteMemory(slug, hard = false) {
    return apiFetch(`/memories/${encodeURIComponent(slug)}${hard ? '?hard=true' : ''}`, { method: 'DELETE' });
  },
  restoreMemory(slug) {
    return apiFetch(`/memories/${encodeURIComponent(slug)}/restore`, { method: 'POST' });
  },
  purgeArchived() {
    return apiFetch('/memories/archived', { method: 'DELETE' });
  },

  // Versions
  listVersions(slug) {
    return apiFetch(`/memories/${encodeURIComponent(slug)}/versions`);
  },
  revertVersion(slug, versionNum) {
    return apiFetch(`/memories/versions/${encodeURIComponent(slug)}/restore/${versionNum}`, { method: 'POST' });
  },

  // Relations
  listRelations(slug) {
    return apiFetch(`/memories/${encodeURIComponent(slug)}/relate`);
  },
  relate(slug, target) {
    return apiFetch(`/memories/${encodeURIComponent(slug)}/relate?target=${encodeURIComponent(target)}`,
      { method: 'POST' });
  },

  // Search. mode: keyword | semantic | hybrid — keyword keeps the 2.x shape,
  // the others add a "match" field per result.
  //
  // This path goes through raw fetch (not apiFetch) so it can read the
  // X-Semantic-Error header: a configured-but-broken embed provider degrades
  // to keyword results and says why. apiFetch discards headers, so the UI
  // would otherwise never see the reason. Exposed as a window global because
  // the API-method gate in test_ui.py reserves API.* names for methods.
  search(q, params = {}) {
    params.q = q;
    const qs = new URLSearchParams(params).toString();
    return fetch(API_BASE + '/search?' + qs).then(async res => {
      const text = await res.text();
      let data = null;
      if (text) { try { data = JSON.parse(text); } catch (e) { data = { detail: text.slice(0, 200) }; } }
      if (!res.ok) throw new Error(apiErrorText(data, res.status));
      const header = res.headers.get('X-Semantic-Error');
      window.mscLastSemanticError = header ? decodeURIComponent(header) : null;
      return data;
    });
  },
  recommend(slug) {
    return apiFetch(`/search/recommend/${encodeURIComponent(slug)}`);
  },

  // Graph / vocab
  // maxDegree caps edges per node server-side: the auto-linker can produce
  // thousands of keyword edges and one hub then spins the layout forever.
  getGraph(center, maxDegree, relations) {
    const params = {};
    if (center) params.center = center;
    if (maxDegree) params.max_degree = maxDegree;
    // Empty = every relation type; otherwise only the listed ones come back.
    if (relations && relations.length) params.relations = relations.join(',');
    const qs = new URLSearchParams(params).toString();
    return apiFetch('/memories/graph' + (qs ? '?' + qs : ''));
  },
  autoLinkGraph({ minFreq = 3, maxCoverage = 0.3, minShared = 3, rebuild = false } = {}) {
    return apiFetch(`/memories/graph/auto-link?min_freq=${minFreq}&max_coverage=${maxCoverage}&min_shared=${minShared}&rebuild=${rebuild}`,
      { method: 'POST' });
  },
  clearAutoLinks() {
    return apiFetch('/memories/graph/auto-links', { method: 'DELETE' });
  },
  listTags() {
    return apiFetch('/tags');
  },
  listBranches() {
    return apiFetch('/branches');
  },

  // Sync
  syncPush() {
    return apiFetch('/sync/push', { method: 'POST' });
  },
  syncPull() {
    return apiFetch('/sync/pull', { method: 'POST' });
  },
  syncStatus() {
    return apiFetch('/sync/status');
  },
  syncReindex() {
    return apiFetch('/sync/reindex', { method: 'POST' });
  },
  syncLogs(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return apiFetch('/sync/logs?' + qs);
  },

  // Backup
  createBackup() {
    return apiFetch('/backup/create', { method: 'POST' });
  },
  listBackups() {
    return apiFetch('/backup/list');
  },
  deleteBackup(filename) {
    return apiFetch(`/backup/backups/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  },
  restoreBackup(filename) {
    return apiFetch(`/backup/backups/${encodeURIComponent(filename)}/restore`, { method: 'POST' });
  },
  backupDownloadUrl(filename) {
    return `${API_BASE}/backup/backups/${encodeURIComponent(filename)}/download`;
  },

  // Multipart upload: apiFetch would force JSON headers, whisper needs the
  // browser to set the boundary, so this path skips the Content-Type default.
  async apiUpload(path, formData) {
    const res = await fetch(API_BASE + path, { method: 'POST', body: formData });
    const text = await res.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = { detail: text.slice(0, 200) }; } }
    if (!res.ok) throw new Error(apiErrorText(data, res.status));
    return data;
  },

  // Model card + digests — Web UI only, deliberately outside the agent contract
  getModelConfig() {
    return apiFetch('/model');
  },
  listModelCards() {
    return apiFetch('/model/cards');
  },
  getModelCard(purpose) {
    return apiFetch('/model?purpose=' + encodeURIComponent(purpose));
  },
  updateModelCard(purpose, data) {
    return apiFetch('/model?purpose=' + encodeURIComponent(purpose), { method: 'PUT', body: JSON.stringify(data) });
  },
  testModelCard(purpose) {
    return apiFetch('/model/test?purpose=' + encodeURIComponent(purpose), { method: 'POST' });
  },

  // RAG — index status, hybrid search, voice, insights
  ragStatus() {
    return apiFetch('/rag/status');
  },
  ragReindex() {
    return apiFetch('/rag/reindex', { method: 'POST' });
  },
  ragPurgeVectors(model) {
    return apiFetch('/rag/vectors?model=' + encodeURIComponent(model), { method: 'DELETE' });
  },
  ragSearch(q, top = 10) {
    return apiFetch(`/rag/search?q=${encodeURIComponent(q)}&top=${top}`);
  },
  ragTranscribe(formData) {
    return this.apiUpload('/rag/transcribe', formData);
  },
  ragHealthReport() {
    return apiFetch('/rag/health-report');
  },
  ragDailyPick(refresh = false) {
    return apiFetch('/rag/daily-pick' + (refresh ? '?refresh=true' : ''));
  },
  ragEcho(data) {
    return apiFetch('/rag/echo', { method: 'POST', body: JSON.stringify(data) });
  },
  ragHeatmap(days = 120) {
    return apiFetch('/rag/heatmap?days=' + days);
  },
  ragUpdateSettings(data) {
    return apiFetch('/rag/settings', { method: 'PUT', body: JSON.stringify(data) });
  },
  updateModelConfig(data) {
    return apiFetch('/model', { method: 'PUT', body: JSON.stringify(data) });
  },
  testModelConfig() {
    return apiFetch('/model/test', { method: 'POST' });
  },
  summarizeWindow(date) {
    const qs = date ? `?date=${encodeURIComponent(date)}` : '';
    return apiFetch('/summarize/window' + qs);
  },
  summarizeSelected(data) {
    return apiFetch('/summarize/selected', { method: 'POST', body: JSON.stringify(data) });
  },
  // Digest of the current search hits. dry_run=true previews without writing;
  // the server adds the grounding brief and never archives the sources here.
  summarizeSearch(data) {
    return apiFetch('/summarize/search', { method: 'POST', body: JSON.stringify(data) });
  },
  summarizeDaily(data) {
    return apiFetch('/summarize/daily', { method: 'POST', body: JSON.stringify(data) });
  },
  listSummaryRuns(limit = 10) {
    return apiFetch(`/summarize/runs?limit=${limit}`);
  },
  restoreDigestSources(slug) {
    return apiFetch(`/summarize/${encodeURIComponent(slug)}/restore-sources`, { method: 'POST' });
  },

  // Photos (msc-pic) — source cards, gallery, thumbnails. Web UI only.
  picStatus() {
    return apiFetch('/pic/status');
  },
  picUpdateSettings(data) {
    return apiFetch('/pic/settings', { method: 'PUT', body: JSON.stringify(data) });
  },
  picListSources() {
    return apiFetch('/pic/sources');
  },
  picCreateSource(data) {
    return apiFetch('/pic/sources', { method: 'POST', body: JSON.stringify(data) });
  },
  picUpdateSource(id, data) {
    return apiFetch(`/pic/sources/${id}`, { method: 'PUT', body: JSON.stringify(data) });
  },
  picDeleteSource(id) {
    return apiFetch(`/pic/sources/${id}`, { method: 'DELETE' });
  },
  picProbe(id) {
    return apiFetch(`/pic/sources/${id}/probe`, { method: 'POST' });
  },
  picScan(id) {
    return apiFetch(`/pic/sources/${id}/scan`, { method: 'POST' });
  },
  picPause(id) {
    return apiFetch(`/pic/sources/${id}/pause`, { method: 'POST' });
  },
  picResume(id) {
    return apiFetch(`/pic/sources/${id}/resume`, { method: 'POST' });
  },
  picBrowse(path = '') {
    return apiFetch('/pic/browse?path=' + encodeURIComponent(path));
  },
  picItems(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return apiFetch('/pic/items?' + qs);
  },
  picFolders(sourceId) {
    return apiFetch('/pic/folders?source_id=' + encodeURIComponent(sourceId));
  },
  picItem(id) {
    return apiFetch(`/pic/item/${id}`);
  },
  picSearch(q, top = 24) {
    return apiFetch(`/pic/search?q=${encodeURIComponent(q)}&top=${top}`);
  },
  picBatch(ids, action) {
    return apiFetch('/pic/items/batch', { method: 'POST', body: JSON.stringify({ ids, action }) });
  },
  picPurge(ids, confirm = true) {
    return apiFetch('/pic/items/purge', { method: 'POST', body: JSON.stringify({ ids, confirm }) });
  },
  picOriginalUrl(id) {
    return `${API_BASE}/pic/item/${id}/original`;
  },
  picThumbUrl(id) {
    return `${API_BASE}/pic/item/${id}/thumb`;
  }
};
