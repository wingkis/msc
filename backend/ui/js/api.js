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
    if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
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
    return apiFetch(`/memories/${encodeURIComponent(slug)}`);
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

  // Search
  search(q, params = {}) {
    params.q = q;
    const qs = new URLSearchParams(params).toString();
    return apiFetch('/search?' + qs);
  },
  recommend(slug) {
    return apiFetch(`/search/recommend/${encodeURIComponent(slug)}`);
  },

  // Graph / vocab
  getGraph(center) {
    const qs = center ? `?center=${encodeURIComponent(center)}` : '';
    return apiFetch(`/memories/graph${qs}`);
  },
  autoLinkGraph({ minFreq = 3, maxCoverage = 0.4, minShared = 2, rebuild = false } = {}) {
    return apiFetch(`/memories/graph/auto-link?min_freq=${minFreq}&max_coverage=${maxCoverage}&min_shared=${minShared}&rebuild=${rebuild}`,
      { method: 'POST' });
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
  }
};
