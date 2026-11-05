/**
 * MSC Memory — Main App Logic
 */

// ── State ───────────────────────────────────────────────────────────
let currentEditSlug = null;
let currentDetailSlug = null;
let allMemories = [];
let graphData = null;
let searchDebounce = null;

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
    case 'dashboard': loadDashboard(); break;
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
    case 'settings': loadSettings(); break;
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
  return `
    <div class="memory-card" onclick="showMemoryDetail('${escHtml(m.slug)}')">
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
    loadDetailExtra(slug);
  } catch (e) {
    toast('加载失败: ' + e.message, 'error');
  }
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

  const archivedBox = document.getElementById('archived-count');
  if (archivedBox) {
    try {
      const stats = await API.getStats();
      archivedBox.textContent = `已归档: ${stats.archived ?? 0} 条（活跃 ${stats.total ?? 0} 条）`;
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
