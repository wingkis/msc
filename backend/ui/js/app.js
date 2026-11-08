/**
 * MSC Memory — Main App Logic
 */

// ── State ───────────────────────────────────────────────────────────
let currentEditSlug = null;
let currentDetailSlug = null;
let allMemories = [];
let graphData = null;
let searchDebounce = null;
// Digest feature state: which memories are ticked, and what the modal covers.
let selectMode = false;
let selectedSlugs = new Set();
let modelCard = null;
let summaryContext = null;
let visibleMemories = [];

// ── Init ────────────────────────────────────────────────────────────
function initApp() {
  // No login step: the server runs in LAN mode (MSC_AUTH_ENABLED=false).
  // Clear any credentials left over from the old token-based UI.
  localStorage.removeItem('jwt_token');
  localStorage.removeItem('api_key');

  navTo('dashboard');
  updateSyncStatus('ready');
}

// ── Navigation ──────────────────────────────────────────────────────
function navTo(page, searchInputId) {
  // Hide all pages
  const pages = document.querySelectorAll('.page');
  pages.forEach(p => p.classList.remove('active'));

  // Clear nav active state
  const navLinks = document.querySelectorAll('.nav-link');
  navLinks.forEach(n => n.classList.remove('active'));

  // Show target page
  const pageEl = document.getElementById('page-' + page);
  if (!pageEl) { console.error('Page not found:', page); return; }
  pageEl.classList.add('active');

  // Set nav link active
  const navLink = document.querySelector(`.nav-link[data-page="${page}"]`);
  if (navLink) navLink.classList.add('active');

  // Load page data (always reload to ensure fresh data)
  switch (page) {
    case 'dashboard': loadDashboard(); loadDigestWidget(); break;
    case 'memories': loadMemories(); break;
    case 'search':
      populateTagFilter();
      if (searchInputId) {
        const src = document.getElementById(searchInputId);
        const dst = document.getElementById('search-input');
        if (src && dst) dst.value = src.value;
      }
      break;
    case 'graph': loadGraph(); break;
    // The digest half of the UI rides along: the card on settings, a short
    // status + recent runs on the dashboard.
    case 'settings': loadSettings(); loadModelCard(); break;
  }
}

// ── Dashboard ───────────────────────────────────────────────────────
async function loadDashboard() {
  try {
    const stats = await API.getStats();
    document.getElementById('stat-total').textContent = stats.total;
    document.getElementById('stat-config').textContent = stats.by_category?.config || 0;
    document.getElementById('stat-project').textContent = stats.by_category?.project || 0;
    document.getElementById('stat-user').textContent = stats.by_category?.user || 0;
    document.getElementById('stat-knowledge').textContent = stats.by_category?.knowledge || 0;
    document.getElementById('stat-reference').textContent = stats.by_category?.reference || 0;

    const memories = await API.listMemories({ limit: 8 });
    allMemories = memories;
    renderRecentMemories(memories);
    renderPriorityBars(stats.by_priority || {});
  } catch (e) {
    console.error('Dashboard load error:', e);
    document.getElementById('stat-total').textContent = '—';
    document.getElementById('recent-memories').innerHTML =
      '<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">加载失败，请检查网络或配置 API Key</div></div>';
  }
}

function renderRecentMemories(memories) {
  const container = document.getElementById('recent-memories');
  if (!memories || memories.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📭</div><div class="empty-state-text">暂无记忆</div></div>';
    return;
  }
  container.innerHTML = memories.map(m => renderMemoryCard(m)).join('');
}

function renderMemoryCard(m) {
  const tagsHtml = (m.tags || []).map(t => `<span class="tag">${escHtml(t.name)}</span>`).join('');
  const date = m.updated_at ? m.updated_at.substring(0, 10) : '';
  const idLabel = m.id != null ? `#${m.id}` : '';
  const actions = m.archived
    ? `<button class="btn btn-sm restore-btn" onclick="event.stopPropagation();restoreMemoryUI('${escHtml(m.slug)}')">恢复</button>
       <button class="btn btn-sm btn-hard" onclick="event.stopPropagation();hardDeleteMemoryUI('${escHtml(m.slug)}')">彻底删除</button>`
    : '';
  // Multi-select feeds the digest modal. Archived memories cannot be picked:
  // the API only resolves live ones.
  const selectBox = (selectMode && !m.archived)
    ? `<input type="checkbox" class="memory-select" ${selectedSlugs.has(m.slug) ? 'checked' : ''}
            onclick="event.stopPropagation();toggleSelectMemory('${escHtml(m.slug)}')"
            title="选中后参与小结">`
    : '';
  return `
    <div class="memory-card ${selectedSlugs.has(m.slug) ? 'memory-selected' : ''}"
         onclick="showMemoryDetail('${escHtml(m.slug)}')">
      ${selectBox}
      <div class="memory-priority-dot priority-${escHtml(m.priority)}"></div>
      <div class="memory-content">
        <div class="memory-name"><span class="memory-id">${idLabel}</span>${escHtml(m.name)}</div>
        <div class="memory-desc">${escHtml(m.description || (m.body ? m.body.substring(0, 200) : ''))}</div>
        <div class="memory-meta">
          <span class="category-badge category-${escHtml(m.category)}">${escHtml(m.category)}</span>
          ${tagsHtml}
          ${m.archived ? '<span class="archived-badge">已归档</span>' : ''}
          <span class="memory-date">${date}</span>
        </div>
      </div>
      ${actions}
    </div>
  `;
}

function renderPriorityBars(priorities) {
  const container = document.getElementById('priority-chart');
  if (!container) return;
  const values = Object.values(priorities);
  const max = values.length > 0 ? Math.max(...values) : 1;
  const colors = { critical: 'var(--priority-critical)', high: 'var(--priority-high)', medium: 'var(--priority-medium)', low: 'var(--priority-low)' };
  container.innerHTML = Object.entries(priorities).map(([p, c]) => `
    <div class="priority-bar">
      <span class="priority-bar-label">${escHtml(p)}</span>
      <div class="priority-bar-track">
        <div class="priority-bar-fill" style="width:${(c/max*100).toFixed(0)}%;background:${colors[p]||'var(--text-dim)'}"></div>
      </div>
      <span class="priority-bar-count">${c}</span>
    </div>
  `).join('');
}

// ── Memories List ───────────────────────────────────────────────────
async function loadMemories() {
  const archived = document.getElementById('filter-archived');
  const params = { limit: 500 };
  if (archived && archived.checked) params.include_archived = 'true';

  try {
    const [memories, branches] = await Promise.all([API.listMemories(params), API.listBranches()]);
    allMemories = memories;
    renderMemoriesList(memories);
    populateBranchFilter(branches, memories);
  } catch (e) {
    document.getElementById('memories-list').innerHTML =
      '<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">加载失败：' + escHtml(e.message) + '</div></div>';
  }
}

function renderMemoriesList(memories) {
  const container = document.getElementById('memories-list');
  visibleMemories = memories || [];
  document.getElementById('memories-count').textContent = `共 ${memories.length} 条记忆`;
  if (!memories.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📭</div><div class="empty-state-text">没有找到记忆</div></div>';
    return;
  }
  container.innerHTML = memories.map(m => renderMemoryCard(m)).join('');
}

function filterMemories() {
  // Category and priority checkboxes live in separate containers: reading
  // every .checkbox-list together used to feed priority values into the
  // category filter and empty the list.
  const categories = [...document.querySelectorAll('#filter-categories input:checked')].map(c => c.value);
  const priorities = [...document.querySelectorAll('#filter-priorities input:checked')].map(c => c.value);
  const branch = document.getElementById('filter-branch').value;

  let filtered = allMemories;
  if (categories.length > 0) filtered = filtered.filter(m => categories.includes(m.category));
  if (priorities.length > 0) filtered = filtered.filter(m => priorities.includes(m.priority));
  if (branch) filtered = filtered.filter(m => m.branch === branch);

  renderMemoriesList(filtered);
}

function clearFilters() {
  document.querySelectorAll('.checkbox-list input[type="checkbox"]').forEach(c => c.checked = false);
  document.getElementById('filter-branch').value = '';
  loadMemories();
}

function populateBranchFilter(branches, memories) {
  const select = document.getElementById('filter-branch');
  const previous = select.value;
  // The registry plus anything the memories actually use, so a branch that only
  // exists as a memory field is still filterable.
  const names = [...new Set([
    ...(branches || []).map(b => b.name),
    ...(memories || []).map(m => m.branch).filter(Boolean),
  ])].sort();
  select.innerHTML = '<option value="">全部分支</option>' +
    names.map(b => `<option value="${escHtml(b)}">${escHtml(b)}</option>`).join('');
  if (previous) select.value = previous;
}

function populateTagFilter() {
  const select = document.getElementById('search-tag-filter');
  if (!select || select.options.length > 1) return;
  API.listTags().then(tags => {
    select.innerHTML = '<option value="">所有标签</option>' +
      tags.map(t => `<option value="${escHtml(t.name)}">${escHtml(t.name)} (${t.count})</option>`).join('');
  }).catch(e => console.warn('tags load failed:', e.message));
}

// ── Search ──────────────────────────────────────────────────────────
function quickSearch(inputId) {
  const val = document.getElementById(inputId).value;
  if (val.trim()) {
    document.getElementById('search-input').value = val;
    navTo('search');
  }
}

async function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  if (!q) return;

  const params = {};
  const tag = document.getElementById('search-tag-filter').value;
  const category = document.getElementById('search-category-filter').value;
  const priority = document.getElementById('search-priority-filter').value;
  if (tag) params.tags = tag;
  if (category) params.categories = category;
  if (priority) params.priorities = priority;

  try {
    const results = await API.search(q, params);
    document.getElementById('search-results').innerHTML =
      results.map(r => renderSearchResult(r)).join('');
    document.getElementById('search-results').style.display = 'flex';
    document.getElementById('search-empty').style.display = 'none';
  } catch (e) {
    document.getElementById('search-results').innerHTML =
      `<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">搜索失败: ${escHtml(e.message)}</div></div>`;
    document.getElementById('search-results').style.display = 'flex';
    document.getElementById('search-empty').style.display = 'none';
  }
}

function renderSearchResult(r) {
  const tagsHtml = (r.tags || []).map(t => `<span class="tag">${escHtml(t.name)}</span>`).join('');
  const idLabel = r.id != null ? `#${r.id}` : '';
  return `
    <div class="memory-card" onclick="showMemoryDetail('${escHtml(r.slug)}')">
      <div class="memory-priority-dot priority-${escHtml(r.priority)}"></div>
      <div class="memory-content">
        <div class="memory-name"><span class="memory-id">${idLabel}</span>${escHtml(r.name)}</div>
        <div class="memory-desc">${escHtml(r.description || '')}</div>
        <div class="memory-meta">
          <span class="category-badge category-${escHtml(r.category)}">${escHtml(r.category)}</span>
          ${tagsHtml}
        </div>
      </div>
    </div>
  `;
}

// ── Memory Detail ───────────────────────────────────────────────────
async function showMemoryDetail(slug) {
  try {
    const m = await API.getMemory(slug);
    currentEditSlug = slug;
    currentDetailSlug = slug;
    document.getElementById('detail-title').textContent = m.name;
    const tagsHtml = (m.tags || []).map(t => `<span class="tag">${escHtml(t.name)}</span>`).join('');
    const when = `${fmtDate(m.created_at)} 创建 · ${fmtDate(m.updated_at)} 更新`;
    document.getElementById('detail-body').innerHTML = `
      <div class="detail-meta">
        <span class="category-badge category-${escHtml(m.category)}">${escHtml(m.category)}</span>
        <span class="memory-priority-dot priority-${escHtml(m.priority)}" title="${escHtml(m.priority)}"></span>
        ${tagsHtml}
        ${m.archived ? '<span class="archived-badge">已归档</span>' : ''}
      </div>
      <div class="detail-facts">
        <span>id: <code>#${m.id}</code></span>
        <span>slug: <code>${escHtml(m.slug)}</code></span>
        ${m.branch ? `<span>分支: <code>${escHtml(m.branch)}</code></span>` : ''}
        <span>${escHtml(when)}</span>
      </div>
      <div class="detail-description">${escHtml(m.description || '')}</div>
      <div class="detail-body">${escHtml(m.body || '')}</div>
      <div id="detail-extra" class="detail-extra"><div class="loading-hint">加载历史与关联…</div></div>
    `;
    document.getElementById('detail-overlay').classList.add('active');
    // An archived memory is read-only in this modal: editing it would silently
    // revive it, so the footer swaps in 恢复 instead of 软删除/编辑.
    setDetailFooter('detail-restore-btn', !!m.archived);
    setDetailFooter('detail-delete-btn', !m.archived);
    setDetailFooter('detail-edit-btn', !m.archived);
    loadDetailExtra(slug);
  } catch (e) {
    toast('加载失败: ' + e.message, 'error');
  }
}

function setDetailFooter(id, visible) {
  const button = document.getElementById(id);
  if (button) button.style.display = visible ? '' : 'none';
}

// Version history and linked memories load separately so the body shows first.
async function loadDetailExtra(slug) {
  const box = document.getElementById('detail-extra');
  if (!box) return;
  try {
    const [versions, related] = await Promise.all([API.listVersions(slug), API.listRelations(slug)]);
    if (!document.body.contains(box)) return;  // the modal was replaced while loading

    // Only the latest snapshot is kept per memory (undo of the last edit).
    const versionsHtml = versions.length
      ? versions.map(v => `
          <div class="version-item">
            <div class="version-info">
              <strong>v${escHtml(String(v.version_num))}</strong>
              <span class="version-when">${escHtml(fmtDate(v.created_at))}${v.created_by ? ' · ' + escHtml(v.created_by) : ''}</span>
              <span class="version-summary">${escHtml((v.change_summary || v.body || '').substring(0, 120))}</span>
            </div>
            <button class="btn btn-sm" onclick="revertToVersion('${escHtml(slug)}', ${escHtml(String(v.version_num))})">撤销到此版本</button>
          </div>`).join('')
      : '<div class="empty-hint">还没有编辑历史</div>';

    const relatedHtml = related.length
      ? related.map(r => `
          <span class="related-item" onclick="showMemoryDetail('${escHtml(r.slug)}')" title="${escHtml(r.relation_type)}">
            ${escHtml(r.slug)} <em>${escHtml(r.relation_type)}</em>
          </span>`).join('')
      : '<div class="empty-hint">暂无关联记忆</div>';

    box.innerHTML = `
      <div class="detail-section">
        <h4>关联记忆</h4>
        <div class="related-list">${relatedHtml}</div>
        <input type="text" id="relate-input" placeholder="关联另一个 slug…"
               onkeydown="if(event.key==='Enter')relateFromDetail('${escHtml(slug)}')">
        <button class="btn btn-sm btn-ghost" onclick="relateFromDetail('${escHtml(slug)}')">关联</button>
      </div>
      <div class="detail-section">
        <h4>上一版本快照 (${versions.length})</h4>
        <div class="version-list">${versionsHtml}</div>
      </div>
    `;
  } catch (e) {
    box.innerHTML = `<div class="empty-hint">历史/关联加载失败: ${escHtml(e.message)}</div>`;
  }
}

async function relateFromDetail(slug) {
  const input = document.getElementById('relate-input');
  const target = (input && input.value || '').trim();
  if (!target) return;
  try {
    await API.relate(slug, target);
    toast('已关联 ' + slug + ' ↔ ' + target);
    loadDetailExtra(slug);
  } catch (e) {
    toast('关联失败: ' + e.message, 'error');
  }
}

async function revertToVersion(slug, versionNum) {
  if (!confirm(`把「${slug}」恢复到 v${versionNum}？当前内容会先存进编辑历史。`)) return;
  try {
    await API.revertVersion(slug, versionNum);
    toast(`已恢复到 v${versionNum}`);
    showMemoryDetail(slug);
  } catch (e) {
    toast('恢复失败: ' + e.message, 'error');
  }
}

function closeDetailModal() {
  document.getElementById('detail-overlay').classList.remove('active');
  currentEditSlug = null;
  currentDetailSlug = null;
}

function editCurrentMemory() {
  const slug = currentEditSlug;
  closeDetailModal();
  if (slug) openEditModal(slug);
}

// Soft delete keeps the row and its history, so the UI offers an undo.
function deleteCurrentMemory() {
  const slug = currentEditSlug;
  if (!slug) return;
  if (!confirm(`软删除「${slug}」？记忆和编辑历史都保留，可以撤销。`)) return;
  API.deleteMemory(slug).then(() => {
    closeDetailModal();
    reloadCurrentPage();
    toast(`已软删除 ${slug}`, 'info', { actionLabel: '撤销', action: () => restoreMemoryUI(slug) });
  }).catch(e => toast('删除失败: ' + e.message, 'error'));
}

// Hard delete removes the row, its versions and its relations — no undo.
// Ids of the remaining memories are untouched.
function hardDeleteCurrentMemory() {
  const slug = currentEditSlug;
  if (!slug) return;
  hardDeleteMemoryUI(slug);
}

async function hardDeleteMemoryUI(slug) {
  if (!confirm(`彻底删除「${slug}」？\n会同时删除编辑历史与关联，不可撤销。`)) return;
  try {
    await API.deleteMemory(slug, true);
    closeDetailModal();
    reloadCurrentPage();
    toast(`已彻底删除 ${slug}`, 'info');
  } catch (e) {
    toast('彻底删除失败: ' + e.message, 'error');
  }
}

async function restoreMemoryUI(slug) {
  try {
    await API.restoreMemory(slug);
    toast(`已恢复 ${slug}`);
    reloadCurrentPage();
  } catch (e) {
    toast('恢复失败: ' + e.message, 'error');
  }
}

function reloadCurrentPage() {
  const activePage = document.querySelector('.page.active');
  if (activePage) navTo(activePage.id.replace('page-', ''));
}

// ── Add / Edit Modal ────────────────────────────────────────────────
function openAddModal() {
  currentEditSlug = null;
  document.getElementById('modal-title').textContent = '新建记忆';
  document.getElementById('form-name').value = '';
  document.getElementById('form-description').value = '';
  document.getElementById('form-body').value = '';
  document.getElementById('form-tags').value = '';
  document.getElementById('form-branch').value = '';
  document.getElementById('form-category').value = 'knowledge';
  document.getElementById('form-priority').value = 'medium';
  document.getElementById('modal-overlay').classList.add('active');
}

function openEditModal(slug) {
  API.getMemory(slug).then(m => {
    currentEditSlug = slug;
    document.getElementById('modal-title').textContent = '编辑记忆: ' + m.name;
    document.getElementById('form-name').value = m.name;
    document.getElementById('form-description').value = m.description || '';
    document.getElementById('form-body').value = m.body || '';
    document.getElementById('form-tags').value = (m.tags || []).map(t => t.name).join(', ');
    document.getElementById('form-branch').value = m.branch || '';
    document.getElementById('form-category').value = m.category;
    document.getElementById('form-priority').value = m.priority;
    document.getElementById('modal-overlay').classList.add('active');
  }).catch(e => alert('加载失败: ' + e.message));
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
  currentEditSlug = null;
}

async function saveMemory() {
  const name = document.getElementById('form-name').value.trim();
  if (!name) { alert('请输入名称'); return; }

  const data = {
    name,
    description: document.getElementById('form-description').value.trim(),
    body: document.getElementById('form-body').value,
    tags: document.getElementById('form-tags').value.split(',').map(t => t.trim()).filter(Boolean),
    category: document.getElementById('form-category').value,
    priority: document.getElementById('form-priority').value,
    branch: document.getElementById('form-branch').value.trim(),
  };

  try {
    if (currentEditSlug) {
      await API.updateMemory(currentEditSlug, data);
    } else {
      await API.createMemory(data);
    }
    closeModal();
    const activePage = document.querySelector('.page.active');
    if (activePage) {
      const pageId = activePage.id.replace('page-', '');
      navTo(pageId);
    }
  } catch (e) {
    alert('保存失败: ' + e.message);
  }
}

// ── Sync ────────────────────────────────────────────────────────────
async function doSync() {
  updateSyncStatus('syncing');
  try {
    const result = await API.syncPush();
    updateSyncStatus('ready', result.message || '同步完成');
    reloadCurrentPage();
  } catch (e) {
    updateSyncStatus('error', '同步失败: ' + e.message);
  }
}

// Pull = "an agent edited files on disk, pick that up".
async function doPull() {
  updateSyncStatus('syncing');
  try {
    const result = await API.syncPull();
    updateSyncStatus('ready', result.message || '拉取完成');
    toast(result.message || '拉取完成');
    reloadCurrentPage();
    loadSettings();
  } catch (e) {
    updateSyncStatus('error', '拉取失败: ' + e.message);
    toast('拉取失败: ' + e.message, 'error');
  }
}

async function doReindex() {
  try {
    const result = await API.syncReindex();
    toast(result.message || '索引已重建');
    loadSettings();
  } catch (e) {
    toast('重建索引失败: ' + e.message, 'error');
  }
}

function renderSyncStatus(status) {
  const box = document.getElementById('sync-detail');
  if (!box) return;
  const drift = (status.only_in_db || []).length + (status.only_in_files || []).length;
  box.innerHTML = `
    <div class="kv"><span>目录</span><code>${escHtml(status.memory_dir)}</code></div>
    <div class="kv"><span>数据库</span><span>${status.db_memories} 条（归档 ${status.db_archived}）</span></div>
    <div class="kv"><span>文件</span><span>${status.files} 个</span></div>
    <div class="kv"><span>差异</span><span>${drift === 0 ? '一致 ✓' : `仅库内 ${ (status.only_in_db||[]).length } / 仅文件 ${(status.only_in_files||[]).length}`}</span></div>
    <div class="kv"><span>搜索索引</span><span>${status.search_index_unindexed === 0 ? '完整 ✓' : status.search_index_unindexed + ' 条未索引'}</span></div>
    <div class="kv"><span>上次同步</span><span>${escHtml((status.last_sync && (status.last_sync.direction + ' ' + status.last_sync.detail)) || '无')}</span></div>
  `;
}

function renderSyncLogs(logs) {
  const box = document.getElementById('sync-logs');
  if (!box) return;
  if (!logs.length) { box.innerHTML = '<div class="empty-hint">暂无同步日志</div>'; return; }
  box.innerHTML = logs.slice(0, 10).map(l => `
    <div class="log-item">
      <span class="log-dir log-${escHtml(l.direction)}">${escHtml(l.direction)}</span>
      <span>${escHtml(l.detail || '')}</span>
      <span class="log-when">${escHtml(fmtDate(l.created_at))}</span>
    </div>`).join('');
}

function updateSyncStatus(status, message) {
  const indicator = document.querySelector('.sync-indicator');
  if (!indicator) return;
  indicator.className = 'sync-indicator sync-' + status;
  indicator.textContent = status === 'syncing' ? '● 同步中...' :
    status === 'error' ? '● 错误' : '● 就绪';
  if (message) {
    const result = document.getElementById('sync-result');
    if (result) result.textContent = message;
  }
}

// ── Settings ────────────────────────────────────────────────────────
async function loadSettings() {
  // Show the server's access mode (no login involved in LAN mode)
  const authStatus = document.getElementById('settings-auth-status');
  if (authStatus) {
    try {
      const cfg = await API.getConfig();
      if (cfg.auth_enabled) {
        authStatus.textContent = '🔒 鉴权模式（需要 API Key / token）';
        authStatus.style.color = 'var(--priority-high)';
      } else {
        authStatus.textContent = '🔓 局域网模式 — 无需登录，无 token 过期';
        authStatus.style.color = 'var(--text-muted)';
      }
    } catch (e) {
      authStatus.textContent = '无法读取服务器配置: ' + e.message;
      authStatus.style.color = 'var(--priority-critical)';
    }
  }
  // Server self-check + version
  const healthBox = document.getElementById('health-detail');
  const versionBox = document.getElementById('about-version');
  try {
    const health = await API.getHealth();
    if (versionBox) versionBox.textContent = '版本: ' + (health.version || '?');
    if (healthBox) {
      const index = health.search_index || {};
      healthBox.innerHTML = `
        <div class="kv"><span>状态</span><span>${escHtml(health.status || '?')} · 数据库 ${escHtml(health.database || '?')}</span></div>
        <div class="kv"><span>记忆</span><span>${health.memories ?? '—'} 条</span></div>
        <div class="kv"><span>搜索索引</span><span>${index.total ?? '—'} 条，未索引 ${index.unindexed ?? '—'}</span></div>
      `;
    }
  } catch (e) {
    if (healthBox) healthBox.innerHTML = `<div class="empty-hint">健康检查失败: ${escHtml(e.message)}</div>`;
  }

  try {
    renderSyncStatus(await API.syncStatus());
  } catch (e) {
    const box = document.getElementById('sync-detail');
    if (box) box.innerHTML = `<div class="empty-hint">读取同步状态失败: ${escHtml(e.message)}</div>`;
  }

  try {
    renderSyncLogs(await API.syncLogs({ limit: 10 }));
  } catch (e) {
    console.warn('sync logs failed:', e.message);
  }

  // The maintenance card is where a soft delete gets undone, so it lists them
  // instead of only counting them. /stats has no "archived" field — the count
  // comes from the same request.
  const archivedBox = document.getElementById('archived-count');
  if (archivedBox) {
    try {
      const [archived, stats] = await Promise.all([
        API.listMemories({ archived: true, limit: 500 }), API.getStats(),
      ]);
      renderArchivedList(archived);
      archivedBox.textContent = `已归档: ${archived.length} 条（活跃 ${stats.total ?? 0} 条）`;
    } catch (e) {
      archivedBox.textContent = '已归档: 读取失败';
    }
  }

  try {
    const backups = await API.listBackups();
    const container = document.getElementById('backup-list');
    if (!container) return;
    if (backups.length === 0) {
      container.innerHTML = '<div class="empty-hint">暂无备份</div>';
    } else {
      container.innerHTML = backups.map(b => `
        <div class="backup-item">
          <span class="backup-name" title="${escHtml(b.filename)}">${escHtml(b.filename)}</span>
          <span class="backup-size">${(b.size / 1024).toFixed(0)}KB · ${escHtml((b.created_at || '').substring(0, 10))}</span>
          <span class="backup-actions">
            <a class="btn btn-sm" href="${API.backupDownloadUrl(b.filename)}" download>下载</a>
            <button class="btn btn-sm" onclick="restoreBackupUI('${escHtml(b.filename)}')">恢复</button>
            <button class="btn btn-sm btn-danger" onclick="deleteBackupUI('${escHtml(b.filename)}')">删除</button>
          </span>
        </div>
      `).join('');
    }
  } catch (e) {
    console.error('Backup load error:', e);
  }
}

async function deleteBackupUI(filename) {
  if (!confirm(`删除备份 ${filename}？`)) return;
  try {
    await API.deleteBackup(filename);
    toast('已删除备份');
    loadSettings();
  } catch (e) {
    toast('删除备份失败: ' + e.message, 'error');
  }
}

async function purgeArchived() {
  if (!confirm('彻底清除所有已归档记忆?\n会删除归档记忆的行、编辑历史和关联,不可撤销。')) return;
  try {
    const result = await API.purgeArchived();
    toast(`已彻底删除 ${result.purged} 条归档记忆`);
    reloadCurrentPage();
    loadSettings();
  } catch (e) {
    toast('清除失败: ' + e.message, 'error');
  }
}

function renderArchivedList(memories) {
  const box = document.getElementById('archived-list');
  if (!box) return;
  if (!memories.length) { box.innerHTML = '<div class="empty-hint">没有已归档的记忆</div>'; return; }
  box.innerHTML = memories.map(m => `
    <div class="archived-item">
      <span class="memory-id">#${m.id}</span>
      <span class="archived-name" onclick="showMemoryDetail('${escHtml(m.slug)}')"
            title="${escHtml(m.slug)}">${escHtml(m.name)}</span>
      <span class="archived-when">${escHtml((m.updated_at || '').substring(0, 16).replace('T', ' '))}</span>
      <span class="archived-actions">
        <button class="btn btn-sm" onclick="showMemoryDetail('${escHtml(m.slug)}')">详细</button>
        <button class="btn btn-sm btn-primary" onclick="restoreArchived('${escHtml(m.slug)}')">恢复</button>
      </span>
    </div>`).join('');
}

async function restoreArchived(slug) {
  try {
    await API.restoreMemory(slug);
    closeDetailModal();
    toast(`已恢复 ${slug}`);
    loadSettings();
    const memoriesPage = document.getElementById('page-memories');
    if (memoriesPage && memoriesPage.classList.contains('active')) loadMemories();
  } catch (e) {
    toast('恢复失败: ' + e.message, 'error');
  }
}

// The detail modal's 恢复 button (archived memories only).
function restoreCurrentMemory() {
  const slug = currentDetailSlug || currentEditSlug;
  if (slug) restoreArchived(slug);
}

async function restoreBackupUI(filename) {
  if (!confirm(`恢复备份 ${filename}?\n当前数据库内容会被备份中的版本替换。`)) return;
  try {
    const result = await API.restoreBackup(filename);
    toast(`已恢复: ${result.memories} 条记忆、${result.files} 个文件`);
    reloadCurrentPage();
    loadSettings();
  } catch (e) {
    toast('恢复失败: ' + e.message, 'error');
  }
}

async function createBackup() {
  try {
    const result = await API.createBackup();
    toast('备份已创建: ' + result.filename);
    loadSettings();
  } catch (e) {
    toast('备份失败: ' + e.message, 'error');
  }
}

// ── Utilities ───────────────────────────────────────────────────────
// A toast instead of alert(): deleting is reversible, so the undo lives here.
function toast(message, kind = 'success', options = {}) {
  let box = document.getElementById('toast');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toast';
    box.className = 'toast';
    document.body.appendChild(box);
  }
  box.className = 'toast toast-' + kind + ' active';
  box.innerHTML = `<span>${escHtml(message)}</span>`;
  if (options.actionLabel) {
    const btn = document.createElement('button');
    btn.className = 'btn btn-sm toast-action';
    btn.textContent = options.actionLabel;
    btn.onclick = () => { box.classList.remove('active'); options.action(); };
    box.appendChild(btn);
  }
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => box.classList.remove('active'), options.timeout || 5000);
}

function fmtDate(value) {
  if (!value) return '';
  const date = new Date(value);
  if (isNaN(date)) return String(value).substring(0, 16).replace('T', ' ');
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function debouncedSearch() {
  if (searchDebounce) clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    const val = document.getElementById('memories-search-input').value.trim().toLowerCase();
    if (!val) { renderMemoriesList(allMemories); return; }
    const filtered = allMemories.filter(m =>
      m.name.toLowerCase().includes(val) ||
      (m.description || '').toLowerCase().includes(val) ||
      (m.body || '').toLowerCase().includes(val)
    );
    renderMemoriesList(filtered);
  }, 200);
}

// ── Model card ──────────────────────────────────────────────────────
// Everything about which model to use lives in the database, so this card is
// the only place a user needs to touch to switch to their own local endpoint.
async function loadModelCard() {
  try {
    modelCard = await API.getModelConfig();
    fillModelCard(modelCard);
    renderModelStatus(modelCard);
  } catch (e) {
    const box = document.getElementById('model-status');
    if (box) box.textContent = '模型卡片读取失败: ' + escHtml(e.message);
  }
  loadSummaryRuns();
}

function setFieldValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = (value === null || value === undefined) ? '' : value;
}

function setChecked(id, value) {
  const el = document.getElementById(id);
  if (el) el.checked = !!value;
}

function fillModelCard(card) {
  setFieldValue('model-label', card.label);
  setFieldValue('model-base-url', card.base_url);
  setFieldValue('model-name', card.model);
  setFieldValue('model-temperature', card.temperature);
  setFieldValue('model-max-tokens', card.max_tokens);
  setFieldValue('model-timeout', card.timeout_seconds);
  setFieldValue('model-max-input', card.max_input_chars);
  setFieldValue('model-auto-time', card.auto_time);
  setFieldValue('model-auto-tz', card.auto_timezone);
  setFieldValue('model-default-category', card.default_category);
  setFieldValue('model-default-priority', card.default_priority);
  setFieldValue('model-prompt', card.prompt_template);
  setFieldValue('model-extra', card.extra_json);
  setChecked('model-auto-enabled', card.auto_enabled);
  setChecked('model-archive-sources', card.archive_sources);
  setChecked('model-key-clear', false);

  const key = document.getElementById('model-key');
  if (key) {
    key.value = '';
    key.placeholder = card.api_key_set ? `已保存 ${card.api_key_masked}，留空＝不修改` : '留空＝不修改';
  }
}

function collectModelCard() {
  const num = (id, fallback) => {
    const raw = document.getElementById(id).value;
    const parsed = Number(raw);
    return raw !== '' && !isNaN(parsed) ? parsed : fallback;
  };
  const patch = {
    label: document.getElementById('model-label').value.trim(),
    base_url: document.getElementById('model-base-url').value.trim(),
    model: document.getElementById('model-name').value.trim(),
    temperature: num('model-temperature', 0.2),
    max_tokens: num('model-max-tokens', 1500),
    timeout_seconds: num('model-timeout', 180),
    max_input_chars: num('model-max-input', 12000),
    auto_time: document.getElementById('model-auto-time').value || '23:30',
    auto_timezone: document.getElementById('model-auto-tz').value.trim(),
    default_category: document.getElementById('model-default-category').value,
    default_priority: document.getElementById('model-default-priority').value,
    prompt_template: document.getElementById('model-prompt').value,
    extra_json: document.getElementById('model-extra').value.trim(),
    auto_enabled: document.getElementById('model-auto-enabled').checked,
    archive_sources: document.getElementById('model-archive-sources').checked,
  };
  // An empty key field keeps the stored key; clearing is a deliberate checkbox.
  const key = document.getElementById('model-key').value;
  if (document.getElementById('model-key-clear').checked) {
    patch.api_key = '';
  } else if (key) {
    patch.api_key = key;
  }
  return patch;
}

async function saveModelCard() {
  const box = document.getElementById('model-status');
  if (box) box.textContent = '保存中…';
  try {
    modelCard = await API.updateModelConfig(collectModelCard());
    fillModelCard(modelCard);
    renderModelStatus(modelCard);
    toast('模型卡片已保存');
  } catch (e) {
    if (box) box.textContent = '保存失败: ' + escHtml(e.message);
    toast('保存失败: ' + e.message, 'error');
  }
}

async function testModelCard() {
  const box = document.getElementById('model-status');
  if (box) box.textContent = '正在连接…';
  try {
    const report = await API.testModelConfig();
    const options = document.getElementById('model-options');
    if (options && report.models && report.models.length) {
      options.innerHTML = report.models.map(m => `<option value="${escHtml(m)}"></option>`).join('');
    }
    if (report.ok) {
      const chat = report.chat || {};
      if (box) box.innerHTML = `✅ 可用 · ${escHtml(chat.model || '')} · ${chat.latency_ms || 0}ms · 回复「${escHtml(chat.reply || '')}」`
        + (report.models_error ? `<br>（${escHtml(report.models_error)}）` : '');
    } else {
      if (box) box.innerHTML = `❌ ${escHtml(report.error || '未知错误')}`;
    }
  } catch (e) {
    if (box) box.innerHTML = `❌ ${escHtml(e.message)}`;
  }
  loadModelCard();
}

function renderModelStatus(card) {
  const box = document.getElementById('model-status');
  if (!box || box.textContent) return;
  if (!card.ready) {
    box.innerHTML = `未配置：填好 API 地址与模型名后，记忆页的「小结所选」就能用。<br>当前端点: <code>${escHtml(card.endpoint)}</code>`;
  } else if (card.last_error) {
    box.innerHTML = `⚠️ 上次失败: ${escHtml(card.last_error)}`;
  } else if (card.last_ok_at) {
    box.innerHTML = `✅ 上次成功: ${escHtml(fmtDate(card.last_ok_at))}`;
  }
}

function renderSummaryRuns(runs, containerId, max) {
  const box = document.getElementById(containerId);
  if (!box) return;
  const rows = (runs || []).slice(0, max || runs.length);
  if (!rows.length) { box.innerHTML = '<div class="empty-hint">还没有小结记录</div>'; return; }
  box.innerHTML = rows.map(run => `
    <div class="log-item">
      <span class="log-when">${escHtml(fmtDate(run.created_at))}</span>
      <span>${run.kind === 'auto' ? '自动' : '手动'} · ${run.source_count} 条` +
        (run.archived_count ? ` → 软删除 ${run.archived_count} 条` : '') + `
        ${run.slug ? `<a href="javascript:void(0)" onclick="showMemoryDetail('${escHtml(run.slug)}')">${escHtml(run.slug)}</a>` : ''}</span>
      ${run.ok ? '' : `<span class="digest-error">${escHtml(run.error || '失败')}</span>`}
    </div>`).join('');
}

async function loadSummaryRuns() {
  try {
    renderSummaryRuns(await API.listSummaryRuns(10), 'summary-runs');
  } catch (e) {
    console.warn('summary runs failed:', e.message);
  }
}

async function loadDigestWidget() {
  const box = document.getElementById('digest-status');
  try {
    const [card, runs] = await Promise.all([API.getModelConfig(), API.listSummaryRuns(3)]);
    modelCard = card;
    if (box) {
      box.innerHTML = card.ready
        ? `模型 <code>${escHtml(card.model)}</code><br>` +
          (card.auto_enabled ? `每日 ${escHtml(card.auto_time)} 自动小结（${escHtml(card.auto_timezone || '容器时区')}）`
                             : '自动小结未开启') +
          (card.last_error ? `<br>⚠️ ${escHtml(card.last_error)}` : '')
        : '未配置模型，去设置里的「模型卡片」填地址和模型名。';
    }
    renderSummaryRuns(runs, 'digest-runs', 3);
  } catch (e) {
    if (box) box.textContent = '小结状态读取失败: ' + e.message;
  }
}

// ── Multi-select for digests ────────────────────────────────────────
function toggleSelectMode() {
  selectMode = !selectMode;
  if (!selectMode) selectedSlugs.clear();
  const btn = document.getElementById('select-mode-btn');
  if (btn) btn.classList.toggle('btn-primary', selectMode);
  const bar = document.getElementById('selection-bar');
  if (bar) bar.style.display = selectMode ? 'flex' : 'none';
  updateSelectionBar();
  rerenderMemoriesPreservingFilter();
}

function rerenderMemoriesPreservingFilter() {
  const input = document.getElementById('memories-search-input');
  if (input && input.value.trim()) { renderMemoriesList(allMemories.filter(m =>
      m.name.toLowerCase().includes(input.value.trim().toLowerCase()) ||
      (m.description || '').toLowerCase().includes(input.value.trim().toLowerCase()) ||
      (m.body || '').toLowerCase().includes(input.value.trim().toLowerCase())));
  } else {
    filterMemories();
  }
}

function toggleSelectMemory(slug) {
  if (selectedSlugs.has(slug)) selectedSlugs.delete(slug); else selectedSlugs.add(slug);
  updateSelectionBar();
  rerenderMemoriesPreservingFilter();
}

function selectAllVisible() {
  visibleMemories.filter(m => !m.archived).forEach(m => selectedSlugs.add(m.slug));
  updateSelectionBar();
  rerenderMemoriesPreservingFilter();
}

function clearSelection() {
  selectedSlugs.clear();
  updateSelectionBar();
  rerenderMemoriesPreservingFilter();
}

function updateSelectionBar() {
  const box = document.getElementById('selection-count');
  if (box) box.textContent = `已选 ${selectedSlugs.size} 条`;
}

// ── Digest modal ────────────────────────────────────────────────────
async function openSummaryModal(mode) {
  if (!modelCard) {
    try { modelCard = await API.getModelConfig(); } catch (e) { /* status below handles it */ }
  }
  if (!modelCard || !modelCard.ready) {
    toast('先在 设置 → 模型卡片 里填 API 地址和模型名', 'error');
    navTo('settings');
    return;
  }
  if (mode === 'selected' && selectedSlugs.size === 0) {
    toast('先点「多选」勾选要合并的记忆', 'error');
    navTo('memories');
    return;
  }

  summaryContext = { mode, slugs: [...selectedSlugs], period: '' };
  setFieldValue('summary-name', '');
  setFieldValue('summary-category', modelCard.default_category);
  setFieldValue('summary-priority', modelCard.default_priority);
  setFieldValue('summary-tags', mode === 'daily' ? 'summary, daily' : 'summary');
  setFieldValue('summary-instruction', '');
  setChecked('summary-archive', modelCard.archive_sources);
  const preview = document.getElementById('summary-preview');
  if (preview) { preview.style.display = 'none'; preview.textContent = ''; }
  document.getElementById('summary-title').textContent =
    mode === 'daily' ? '生成当日小结' : `小结所选 ${summaryContext.slugs.length} 条记忆`;
  document.getElementById('summary-overlay').classList.add('active');
  loadSummaryScope();
}

function closeSummaryModal() {
  document.getElementById('summary-overlay').classList.remove('active');
  summaryContext = null;
}

async function loadSummaryScope() {
  const box = document.getElementById('summary-scope');
  if (!box || !summaryContext) return;
  box.textContent = '正在统计范围…';
  try {
    if (summaryContext.mode === 'daily') {
      const win = await API.summarizeWindow();
      summaryContext.period = win.period;
      const tz = win.timezone === 'container' ? '容器时区' : win.timezone;
      box.textContent = `范围：${win.period}（${tz}）当天创建或更新的 ${win.count} 条记忆` +
        (win.count === 0 ? ' —— 这天还没有记忆，换个日期或去记忆页手动勾选。' : '。');
    } else {
      box.textContent = `范围：手动勾选的 ${summaryContext.slugs.length} 条记忆。` +
        `模型输入上限 ${modelCard.max_input_chars} 字符，超限时较旧的会被跳过。`;
    }
  } catch (e) {
    box.textContent = '范围读取失败: ' + e.message;
  }
}

function buildSummaryRequest(dryRun) {
  return {
    name: document.getElementById('summary-name').value.trim(),
    category: document.getElementById('summary-category').value,
    priority: document.getElementById('summary-priority').value,
    tags: document.getElementById('summary-tags').value.split(',').map(t => t.trim()).filter(Boolean),
    instruction: document.getElementById('summary-instruction').value.trim(),
    archive_sources: document.getElementById('summary-archive').checked,
    dry_run: dryRun,
    slugs: summaryContext.slugs,
  };
}

function setSummaryBusy(busy) {
  ['summary-preview-btn', 'summary-run-btn'].forEach(id => {
    const button = document.getElementById(id);
    if (button) button.disabled = busy;
  });
}

async function previewSummary() {
  if (!summaryContext) return;
  const box = document.getElementById('summary-preview');
  setSummaryBusy(true);
  if (box) { box.style.display = 'block'; box.textContent = '模型正在生成…'; }
  try {
    const request = buildSummaryRequest(true);
    const result = summaryContext.mode === 'daily'
      ? await API.summarizeDaily(request) : await API.summarizeSelected(request);
    if (box) box.textContent = (result.body || '（没有内容）') +
      `\n\n— 模型 ${result.model || '?'} · ${result.latency_ms || 0}ms · 送入 ${(result.sources || []).length} 条` +
      (result.dropped ? ` · 跳过 ${result.dropped} 条` : '');
  } catch (e) {
    if (box) box.textContent = '预览失败: ' + e.message;
  } finally {
    setSummaryBusy(false);
  }
}

async function runSummary() {
  if (!summaryContext) return;
  const archive = document.getElementById('summary-archive').checked;
  const scopeLabel = summaryContext.mode === 'daily' ? '当天' : `所选 ${summaryContext.slugs.length} 条`;
  if (archive && !confirm(`写入小结后把${scopeLabel}记忆软删除？\n原始记忆和编辑历史都会保留，可以恢复。`)) return;

  setSummaryBusy(true);
  try {
    const request = buildSummaryRequest(false);
    const result = summaryContext.mode === 'daily'
      ? await API.summarizeDaily(request) : await API.summarizeSelected(request);
    closeSummaryModal();
    if (!result.created) {
      toast(result.message || '没有可总结的记忆', 'error');
      return;
    }
    const slug = result.created.slug;
    const archivedCount = (result.archived || []).length;
    toast(`已写入「${result.created.name}」` + (archivedCount ? `，软删除 ${archivedCount} 条原始记忆` : ''),
      'info', {
        timeout: 9000,
        actionLabel: archivedCount ? '恢复原始记忆' : '查看小结',
        action: () => archivedCount ? restoreDigestSources(slug) : showMemoryDetail(slug),
      });
    selectedSlugs.clear();
    updateSelectionBar();
    reloadCurrentPage();
  } catch (e) {
    toast('小结失败: ' + e.message, 'error');
  } finally {
    setSummaryBusy(false);
  }
}

async function restoreDigestSources(digestSlug) {
  try {
    const result = await API.restoreDigestSources(digestSlug);
    toast(result.message);
    reloadCurrentPage();
  } catch (e) {
    toast('恢复失败: ' + e.message, 'error');
  }
}
