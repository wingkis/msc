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
// The refs a memory had when its edit form opened. saveMemory only sends refs
// when they differ, so an untouched box can never wipe provenance.
let currentEditRefs = [];

// preflight verdicts, spelled out: a glyph would say "green tick" to nobody.
const REF_VERDICT_LABELS = { ok: '成立', missing: '已失效', unchecked: '查不了' };
const REF_VERDICT_SUMMARY = {
  active: '服务端能验的都还在。git: / cmd: / host: 它够不着，要回写这条的那台机器上跑 cli/msc-verify.sh。',
  uncheckable: '服务端一样都验不了 —— 这不是「都错了」，是它够不着。',
  contradicted: '有出处已经查不到了。确认真的不再成立就点「推翻」写下原因；只是端点搬了家就改 refs。',
};

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
    case 'voice': initVoicePage(); break;
    case 'pics': loadPics(); break;
    case 'insights': loadHeatmap(); break;
    // The digest half of the UI rides along: the card on settings, a short
    // status + recent runs on the dashboard.
    case 'settings': loadSettings(); loadModelCard(); loadRagCards(); loadRagStatus(); loadPicSources(); loadPicSettings(); break;
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
      '<div class="empty-state"><svg class="icon empty-state-icon" aria-hidden="true"><use href="#i-alert"/></svg><div class="empty-state-text">加载失败，请检查网络或配置 API Key</div></div>';
  }
}

function renderRecentMemories(memories) {
  const container = document.getElementById('recent-memories');
  if (!memories || memories.length === 0) {
    container.innerHTML = '<div class="empty-state"><svg class="icon empty-state-icon" aria-hidden="true"><use href="#i-inbox"/></svg><div class="empty-state-text">暂无记忆</div></div>';
    return;
  }
  container.innerHTML = memories.map(m => renderMemoryCard(m)).join('');
}

// A contradicted memory is not deleted, so it reappears once the filter asks
// for it — the badge is what stops it reading like a live fact.
function statusBadge(status) {
  if (!status || status === 'active') return '';
  return `<span class="status-badge status-${escAttr(status)}">${status === 'contradicted' ? '已被推翻' : '已过时'}</span>`;
}

function renderMemoryCard(m) {
  const tagsHtml = (m.tags || []).map(t => `<span class="tag">${escHtml(t.name)}</span>`).join('');
  const date = m.updated_at ? m.updated_at.substring(0, 10) : '';
  const idLabel = m.id != null ? `#${m.id}` : '';
  // Echo lives on the live cards only: the API resolves a slug among non-archived
  // memories, so a button on an archived card would always fail.
  const actions = m.archived
    ? `<button class="btn btn-sm restore-btn" onclick="event.stopPropagation();restoreMemoryUI('${escHtml(m.slug)}')">恢复</button>
       <button class="btn btn-sm btn-hard" onclick="event.stopPropagation();hardDeleteMemoryUI('${escHtml(m.slug)}')">彻底删除</button>`
    : `<button class="btn btn-ghost btn-sm echo-inline-btn" title="让模型给这条记忆写回响评注"
              onclick="event.stopPropagation();startEchoFor('${escHtml(m.slug)}')">回响</button>`;
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
          ${statusBadge(m.status)}
          ${(m.refs || []).length ? `<span class="ref-count">出处 ${m.refs.length}</span>` : ''}
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
  const invalidated = document.getElementById('filter-invalidated');
  const params = { limit: 500 };
  if (archived && archived.checked) params.include_archived = 'true';
  if (invalidated && invalidated.checked) params.include_invalidated = 'true';

  try {
    const [memories, branches] = await Promise.all([API.listMemories(params), API.listBranches()]);
    allMemories = memories;
    renderMemoriesList(memories);
    populateBranchFilter(branches, memories);
  } catch (e) {
    document.getElementById('memories-list').innerHTML =
      '<div class="empty-state"><svg class="icon empty-state-icon" aria-hidden="true"><use href="#i-alert"/></svg><div class="empty-state-text">加载失败：' + escHtml(e.message) + '</div></div>';
  }
}

function renderMemoriesList(memories) {
  const container = document.getElementById('memories-list');
  visibleMemories = memories || [];
  document.getElementById('memories-count').textContent = `共 ${memories.length} 条记忆`;
  if (!memories.length) {
    container.innerHTML = '<div class="empty-state"><svg class="icon empty-state-icon" aria-hidden="true"><use href="#i-inbox"/></svg><div class="empty-state-text">没有找到记忆</div></div>';
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
  const mode = document.getElementById('search-mode-select').value;
  if (mode !== 'keyword') params.mode = mode;  // keyword is also the server default
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
    // 「搜索并总结」 works on exactly what is on screen, so a new search both
    // re-arms the button and clears the previous digest.
    lastSearch = { q, slugs: results.map(r => r.slug).filter(Boolean) };
    hideSearchDigest();
    updateSearchDigestGate();
    // Semantic asked for but the provider failed: results are keyword-only.
    if (mode !== 'keyword' && window.mscLastSemanticError) {
      toast('语义搜索不可用，已退回关键词: ' + window.mscLastSemanticError, 'error');
    }
  } catch (e) {
    document.getElementById('search-results').innerHTML =
      `<div class="empty-state"><svg class="icon empty-state-icon" aria-hidden="true"><use href="#i-alert"/></svg><div class="empty-state-text">搜索失败: ${escHtml(e.message)}</div></div>`;
    document.getElementById('search-results').style.display = 'flex';
    document.getElementById('search-empty').style.display = 'none';
    lastSearch = { q, slugs: [] };
    hideSearchDigest();
    updateSearchDigestGate();
  }
}

function renderSearchResult(r) {
  const tagsHtml = (r.tags || []).map(t => `<span class="tag">${escHtml(t.name)}</span>`).join('');
  const idLabel = r.id != null ? `#${r.id}` : '';
  // match 只在 semantic/hybrid 模式下出现：这条是被哪一路找到的。
  const matchBadge = r.match
    ? `<span class="match-badge match-${escHtml(r.match)}">${
        r.match === 'both' ? '关键词+语义' : r.match === 'semantic' ? '语义' : '关键词'
      }</span>`
    : '';
  return `
    <div class="memory-card" onclick="showMemoryDetail('${escHtml(r.slug)}')">
      <div class="memory-priority-dot priority-${escHtml(r.priority)}"></div>
      <div class="memory-content">
        <div class="memory-name"><span class="memory-id">${idLabel}</span>${escHtml(r.name)}</div>
        <div class="memory-desc">${escHtml(r.description || '')}</div>
        <div class="memory-meta">
          <span class="category-badge category-${escHtml(r.category)}">${escHtml(r.category)}</span>
          ${matchBadge}
          ${tagsHtml}
        </div>
      </div>
      <button class="btn btn-ghost btn-sm echo-inline-btn" title="让模型给这条记忆写回响评注"
              onclick="event.stopPropagation(); startEchoFor('${escHtml(r.slug)}')">回响</button>
    </div>
  `;
}

// ── Digest of the current search hits ───────────────────────────────
// The server adds the grounding brief (only what the retrieved memories say)
// and previews first: nothing reaches the corpus until 存为记忆 is clicked.
let lastSearch = { q: '', slugs: [] };

function updateSearchDigestGate() {
  const btn = document.getElementById('search-digest-btn');
  if (btn) btn.disabled = lastSearch.slugs.length === 0;
}

function hideSearchDigest() {
  const box = document.getElementById('search-digest');
  if (box) { box.style.display = 'none'; box.innerHTML = ''; }
}

async function searchAndSummarize(save) {
  if (!lastSearch.slugs.length) {
    toast('先搜索出结果，再点「搜索并总结」', 'error');
    return;
  }
  const box = document.getElementById('search-digest');
  const btn = document.getElementById('search-digest-btn');
  btn.disabled = true;
  box.style.display = 'block';
  box.innerHTML = '<div class="search-digest-head"><span>' +
    (save ? '正在写入小结记忆…' : `模型正在读这 ${lastSearch.slugs.length} 条命中，回答「${lastSearch.q}」…`) +
    '</span></div><div class="loading-hint">本地模型，十几秒到一两分钟，期间不要刷新</div>';

  try {
    const result = await API.summarizeSearch({
      query: lastSearch.q, slugs: lastSearch.slugs, dry_run: !save,
      tags: ['summary', 'search'],
    });
    renderSearchDigest(result, !!save);
    if (save && result.created) toast(`小结已存为 ${result.created.slug}`, 'info');
  } catch (e) {
    box.innerHTML = '<div class="search-digest-head"><span>总结失败</span></div>' +
      `<div class="digest-error">${escHtml(e.message)}</div>` +
      '<div class="setting-hint">模型没有回答，记忆库一条都没动。搜索没有命中时也不要点总结。</div>';
  } finally {
    updateSearchDigestGate();
  }
}

function renderSearchDigest(result, saved) {
  const box = document.getElementById('search-digest');
  const bits = [`模型 ${result.model || '?'}`, `取材 ${(result.sources || []).length} 条`,
                `${result.latency_ms || 0}ms`];
  if (result.dropped) bits.push(`超出输入上限，未送入 ${result.dropped} 条`);
  if ((result.missing || []).length) bits.push(`已失效 ${result.missing.length} 条`);

  const actions = saved
    ? (result.created
        ? `<a class="btn btn-sm" href="javascript:void(0)" ` +
          `onclick="showMemoryDetail('${escHtml(result.created.slug)}')">打开这条小结</a>`
        : '<span class="setting-hint">没有写入（模型没有输出内容）</span>')
    : '<button class="btn btn-sm btn-primary" onclick="searchAndSummarize(true)">存为记忆</button>' +
      '<span class="setting-hint">预览未写库；来源记忆不会被归档</span>';

  box.innerHTML = `
    <div class="search-digest-head">
      <span>回答：「${escHtml(result.query || lastSearch.q)}」</span>
      <span class="search-digest-meta">${escHtml(bits.join(' · '))}</span>
      <button class="btn btn-sm btn-ghost" onclick="hideSearchDigest()">收起</button>
    </div>
    <div class="search-digest-body">${escHtml(result.body || '（模型没有输出内容）')}</div>
    <div class="search-digest-actions">${actions}</div>`;
}

// Echo entry points (search cards, memory cards, the detail modal) all funnel
// here: the commentary itself is long (1-2 minutes), so it always runs in the
// insights panel where the status line and the result live.
function startEchoFor(slug) {
  navTo('insights');
  document.getElementById('echo-slug').value = slug;
  runEcho();
}

// The detail modal's 回响 button: the modal has no room for a 1-2 minute job,
// so hand off to the insights panel. closeDetailModal() clears the slug, so
// read it first.
function echoCurrentMemory() {
  const slug = currentDetailSlug;
  if (!slug) return;
  closeDetailModal();
  startEchoFor(slug);
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
      <div class="detail-refs">${renderRefsBlock(m)}</div>
      <div id="detail-extra" class="detail-extra"><div class="loading-hint">加载历史与关联…</div></div>
    `;
    document.getElementById('detail-overlay').classList.add('active');
    // An archived memory is read-only in this modal: editing it would silently
    // revive it, so the footer swaps in 恢复 instead of 软删除/编辑.
    setDetailFooter('detail-restore-btn', !!m.archived);
    setDetailFooter('detail-delete-btn', !m.archived);
    setDetailFooter('detail-edit-btn', !m.archived);
    // Same rule as the cards: an archived memory cannot be echoed (the slug
    // lookup skips archived rows), so no button on it either.
    setDetailFooter('detail-echo-btn', !m.archived);
    // Provenance actions: nothing to probe without refs, and the two verbs are
    // opposites — a live memory can be contradicted, a contradicted one can
    // only come back through verify.
    const live = !m.archived;
    const settled = (m.status || 'active') === 'active';
    setDetailFooter('detail-probe-btn', live && (m.refs || []).length > 0);
    setDetailFooter('detail-invalidate-btn', live && settled);
    setDetailFooter('detail-verify-btn', live && !settled);
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
    const [versions, related, reviews] = await Promise.all([
      API.listVersions(slug), API.listRelations(slug), API.reviews(slug, { limit: 20 }),
    ]);
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

    // Verdicts are recorded, not overwritten: the status column says what is
    // believed now, this says the same claim was contradicted and restored.
    const reviewHtml = reviews.length
      ? reviews.map(r => `
          <div class="review-row">
            <span class="review-action review-${escAttr(r.action)}">${escHtml(REVIEW_ACTION_LABELS[r.action] || r.action)}</span>
            <span class="review-move">${escHtml(r.from_status || '?')} → ${escHtml(r.to_status || '?')}</span>
            <span class="version-when">${escHtml(fmtDate(r.created_at))}${r.actor ? ' · ' + escHtml(r.actor) : ''}</span>
            <div class="review-reason">${escHtml(r.reason || '')}</div>
          </div>`).join('')
      : '<div class="empty-hint">还没有复核记录 —— 没人判过它，不等于它是对的</div>';

    const relatedHtml = related.length
      ? related.map(r => `
          <span class="related-item" onclick="showMemoryDetail('${escHtml(r.slug)}')" title="${escHtml(r.relation_type)}">
            ${escHtml(r.slug)} <em>${escHtml(r.relation_type)}</em>
          </span>`).join('')
      : '<div class="empty-hint">暂无关联记忆</div>';

    box.innerHTML = `
      <div class="detail-section">
        <h4>复核历史 (${reviews.length})</h4>
        <div class="review-list">${reviewHtml}</div>
      </div>
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

// ── Provenance: refs, re-check, contradiction, the only way back ─────
const REVIEW_ACTION_LABELS = {
  invalidate: '推翻', verify: '复核', decay: '到期降级',
};

function refChip(ref) {
  const kind = String(ref).split(':')[0];
  const target = String(ref).slice(kind.length + 1);
  const jumpable = kind === 'url' || kind === 'mem';
  const go = jumpable ? ` onclick="openRefTarget('${escAttr(kind)}', '${escAttr(target)}')"` : '';
  const hint = jumpable ? ' title="打开这个出处"' : '';
  return `<span class="ref-chip ref-${escAttr(kind)}"${go}${hint}>${escHtml(ref)}</span>`;
}

function openRefTarget(kind, target) {
  if (kind === 'mem') { showMemoryDetail(target); return; }
  if (kind === 'url') { window.open(target, '_blank', 'noopener'); return; }
  toast(kind + ' 的出处要在写它的那台机器上看：' + target, 'info');
}

// The refs block doubles as the contradiction sheet: a reason typed into a
// second modal stacked on the first gets lost, and the reason is the one part
// a later reader cannot reconstruct.
function renderRefsBlock(m) {
  const refs = m.refs || [];
  const status = m.status || 'active';
  const banner = status === 'active' ? '' : `
    <div class="status-note">
      <strong>${status === 'contradicted' ? '已被推翻' : '已过时'}</strong>
      ${m.invalidated_reason ? ' · ' + escHtml(m.invalidated_reason) : ''}
      ${m.invalidated_at ? ' · ' + escHtml(fmtDate(m.invalidated_at)) : ''}
      ${m.verified_at ? ' · 上次复核 ' + escHtml(fmtDate(m.verified_at)) : ''}
      <div class="ref-note">它不在列表与搜索的默认结果里，但内容一个字没删 —— 错的信念也是证据。</div>
    </div>`;
  const chips = refs.length
    ? `<div class="ref-list">${refs.map(refChip).join('')}</div>`
    : '<div class="empty-hint">没有挂出处：读者没法复核这条是从哪来的</div>';
  // Shown even when the memory is live: "nobody has checked this since 2025" is
  // the fact a reader is otherwise never told.
  const checkedLine = m.verified_at
    ? `上次复核 ${escHtml(fmtDate(m.verified_at))}`
    : '从未复核过' + (m.created_at ? `（写下已 ${escHtml(fmtDate(m.created_at))}）` : '');
  return `
    <h4>出处 (${refs.length})</h4>
    ${banner}
    ${chips}
    <div class="ref-note">${checkedLine}</div>
    <div id="detail-ref-verdicts" class="ref-verdicts"></div>
    <div id="invalidate-form" class="invalidate-form" style="display:none">
      <input type="text" id="invalidate-reason" placeholder="为什么不成立了（必填，后来人只能看见这句话）">
      <input type="text" id="invalidate-supersedes" placeholder="取代它的那条 slug（可选）">
      <select id="invalidate-status">
        <option value="contradicted">已被推翻</option>
        <option value="stale">已过时</option>
      </select>
      <button class="btn btn-danger btn-sm" onclick="confirmInvalidate()">确认推翻</button>
      <button class="btn btn-ghost btn-sm" onclick="hideInvalidateForm()">取消</button>
    </div>`;
}

async function probeCurrentRefs() {
  const slug = currentDetailSlug;
  if (!slug) return;
  const box = document.getElementById('detail-ref-verdicts');
  if (box) box.innerHTML = '<div class="loading-hint">复核中…</div>';
  try {
    const report = await API.preflight({ slug, budget_ms: 4000 });
    renderRefVerdicts((report.items || [])[0]);
  } catch (e) {
    if (box) box.innerHTML = `<div class="empty-hint">复核失败: ${escHtml(e.message)}</div>`;
  }
}

function renderRefVerdicts(item) {
  const box = document.getElementById('detail-ref-verdicts');
  if (!box) return;
  const probes = (item && item.probes) || [];
  if (!probes.length) {
    box.innerHTML = '<div class="empty-hint">这条没有可复核的出处</div>';
    return;
  }
  box.innerHTML = probes.map(p => `
    <div class="ref-row verdict-${escAttr(p.verdict)}">
      <span class="ref-verdict">${escHtml(REF_VERDICT_LABELS[p.verdict] || p.verdict)}</span>
      <span class="ref-target" title="${escAttr(p.detail || '')}">${escHtml(p.ref)}</span>
    </div>`).join('')
    + `<div class="ref-note">${escHtml(REF_VERDICT_SUMMARY[item.verdict] || '')}</div>`;
}

function showInvalidateForm() {
  const form = document.getElementById('invalidate-form');
  if (!form) return;
  form.style.display = '';
  const reason = document.getElementById('invalidate-reason');
  if (reason) reason.focus();
}

function hideInvalidateForm() {
  const form = document.getElementById('invalidate-form');
  if (form) form.style.display = 'none';
}

async function confirmInvalidate() {
  const slug = currentDetailSlug;
  if (!slug) return;
  const reason = (document.getElementById('invalidate-reason').value || '').trim();
  if (!reason) { toast('写下为什么不成立 —— 原因是后来人唯一的线索', 'error'); return; }
  const supersededBy = (document.getElementById('invalidate-supersedes').value || '').trim();
  const status = document.getElementById('invalidate-status').value;
  const payload = { reason, status, if_status: 'active' };
  if (supersededBy) payload.superseded_by = supersededBy;
  try {
    await API.invalidate(slug, payload);
    toast(status === 'contradicted' ? `已推翻 ${slug}：退出默认列表，内容保留` : `已把 ${slug} 标为过时`,
          'info', { actionLabel: '标回成立', action: () => verifyMemoryUI(slug) });
    showMemoryDetail(slug);
    reloadCurrentPage();
  } catch (e) {
    // 409 = somebody else judged it while this form was open. Overwriting them
    // silently is the failure the precondition exists to prevent.
    toast(isMovedConflict(e) ? '这条已经被别人改过：请重新打开再判（内容已刷新）'
                             : '推翻失败: ' + e.message, 'error');
    showMemoryDetail(slug);
  }
}

function verifyCurrentMemory() {
  if (currentDetailSlug) verifyMemoryUI(currentDetailSlug);
}

// verdict "ok" is the ONLY route back to active — editing the body deliberately
// does not restore a memory, because the session hook rewrites bodies every
// round and that would launder a wrong belief back into fact nightly.
async function verifyMemoryUI(slug, verdict = 'ok') {
  try {
    // Read first, then name the revision: if another agent judged it in between
    // the server answers 409 instead of letting the later write own the truth.
    const before = await API.getMemory(slug);
    const m = await API.verify(slug, { verdict, reason: '在 Web UI 里人工复核',
                                       if_status: before.status || 'active' });
    toast(`已复核 ${slug}: ${m.status === 'active' ? '回到列表与搜索' : m.status}`);
    if (currentDetailSlug === slug) showMemoryDetail(slug);
    reloadCurrentPage();
  } catch (e) {
    toast(isMovedConflict(e) ? '这条已经被别人改过：请重新打开再判（内容已刷新）'
                             : '复核写回失败: ' + e.message, 'error');
    if (currentDetailSlug === slug) showMemoryDetail(slug);
  }
}

// FastAPI hands the 409 detail through as its message text.
function isMovedConflict(error) {
  return /moved since you read it/.test(String(error && error.message || ''));
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
// 「人工维护」是 2.6.6 的 no-review 标签：有记录的豁免。勾选框是它唯一的真相源，
// 所以每个开表单的入口都要显式设值 —— 留着上一条的勾选，下一条就会静默继承豁免，
// 而静默的豁免正是复核队列反对的那种数。
function setNoReview(checked) {
  const box = document.getElementById('form-no-review');
  if (box) box.checked = !!checked;
}

function hasNoReview(m) {
  return (m.tags || []).some(tag => (tag.name || tag) === 'no-review');
}

function openAddModal() {
  currentEditSlug = null;
  document.getElementById('modal-title').textContent = '新建记忆';
  document.getElementById('form-name').value = '';
  document.getElementById('form-description').value = '';
  document.getElementById('form-body').value = '';
  document.getElementById('form-tags').value = '';
  document.getElementById('form-branch').value = '';
  document.getElementById('form-refs').value = '';
  currentEditRefs = [];
  document.getElementById('form-category').value = 'knowledge';
  document.getElementById('form-priority').value = 'medium';
  setNoReview(false);
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
    currentEditRefs = m.refs || [];
    document.getElementById('form-refs').value = currentEditRefs.join('  ');
    document.getElementById('form-category').value = m.category;
    document.getElementById('form-priority').value = m.priority;
    setNoReview(hasNoReview(m));
    document.getElementById('modal-overlay').classList.add('active');
  }).catch(e => alert('加载失败: ' + e.message));
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
  currentEditSlug = null;
}

// The checkbox is what the operator sees, so it wins over whatever text the
// tag box happens to hold: ticked adds the tag, unticked removes it. A stored
// "no-review" that the box says nothing about would keep a memory exempt after
// the person maintaining it decided otherwise.
function memoryFormTags() {
  const tags = document.getElementById('form-tags').value
    .split(',').map(tag => tag.trim()).filter(Boolean);
  const wanted = document.getElementById('form-no-review').checked;
  const kept = tags.filter(tag => tag !== 'no-review');
  return wanted ? kept.concat(['no-review']) : kept;
}

async function saveMemory() {
  const name = document.getElementById('form-name').value.trim();
  if (!name) { alert('请输入名称'); return; }

  const data = {
    name,
    description: document.getElementById('form-description').value.trim(),
    body: document.getElementById('form-body').value,
    tags: memoryFormTags(),
    category: document.getElementById('form-category').value,
    priority: document.getElementById('form-priority').value,
    branch: document.getElementById('form-branch').value.trim(),
  };

  // refs go out only when the box differs from what the memory had: omitting
  // them on a PUT means "leave them alone", so an untouched field must never be
  // read as "delete every ref" — while a cleared one still must be able to.
  const refs = (document.getElementById('form-refs').value || '')
    .split(/[\s,]+/).map(part => part.trim()).filter(Boolean);
  if (refs.join(' ') !== currentEditRefs.join(' ')) data.refs = refs;

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
    <div class="kv"><span>差异</span><span>${drift === 0 ? '<span class="status-ok">一致</span>' : `仅库内 ${ (status.only_in_db||[]).length } / 仅文件 ${(status.only_in_files||[]).length}`}</span></div>
    <div class="kv"><span>搜索索引</span><span>${status.search_index_unindexed === 0 ? '<span class="status-ok">完整</span>' : status.search_index_unindexed + ' 条未索引'}</span></div>
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

// The dot used to be a text bullet, and the state class was 'sync-'+status
// which no CSS rule matched: the indicator stayed green through a failed sync.
const SYNC_LABELS = { ready: '就绪', syncing: '同步中…', error: '错误' };

function updateSyncStatus(status, message) {
  const indicator = document.querySelector('.sync-indicator');
  if (!indicator) return;
  const state = SYNC_LABELS[status] ? status : 'ready';
  indicator.className = 'sync-indicator is-' + state;
  indicator.innerHTML = '<span class="sync-dot" aria-hidden="true"></span>' + SYNC_LABELS[state];
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
        authStatus.textContent = '鉴权模式（需要 API Key / token）';
        authStatus.style.color = 'var(--priority-high)';
      } else {
        authStatus.textContent = '局域网模式 — 无需登录，无 token 过期';
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

// For values interpolated into double-quoted HTML attributes. escHtml misses
// quotes, which is how an attacker-controlled model name could escape an
// attribute; this closes the quote holes too.
function escAttr(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
      if (box) box.innerHTML = `<span class="status-ok">可用</span> · ${escHtml(chat.model || '')} · ${chat.latency_ms || 0}ms · 回复「${escHtml(chat.reply || '')}」`
        + (report.models_error ? `<br>（${escHtml(report.models_error)}）` : '');
    } else {
      if (box) box.innerHTML = `<span class="status-bad">${escHtml(report.error || '未知错误')}</span>`;
    }
  } catch (e) {
    if (box) box.innerHTML = `<span class="status-bad">${escHtml(e.message)}</span>`;
  }
  loadModelCard();
}

function renderModelStatus(card) {
  const box = document.getElementById('model-status');
  if (!box || box.textContent) return;
  if (!card.ready) {
    box.innerHTML = `未配置：填好 API 地址与模型名后，记忆页的「小结所选」就能用。<br>当前端点: <code>${escHtml(card.endpoint)}</code>`;
  } else if (card.last_error) {
    box.innerHTML = `<span class="status-warn">上次失败: ${escHtml(card.last_error)}</span>`;
  } else if (card.last_ok_at) {
    box.innerHTML = `<span class="status-ok">上次成功: ${escHtml(fmtDate(card.last_ok_at))}</span>`;
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
          (card.last_error ? `<br><span class="status-warn">${escHtml(card.last_error)}</span>` : '')
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

// ── Provider cards ──────────────────────────────────────────────────
// Same fill/collect/save/test shape as the chat card, one prefix per purpose.
// vision (照片描述) belongs here too: the backend has served that card since
// the pic branch landed, but the settings page never rendered it, so the
// endpoint that captions the library could only be moved by hand.
const CARD_PURPOSES = ['embed', 'rerank', 'whisper', 'echo', 'vision'];
// purpose -> [[input id suffix, card field], ...] for the numbers a card shows.
const CARD_NUMBER_FIELDS = {
  embed: [['max-input', 'max_input_chars'], ['timeout', 'timeout_seconds']],
  vision: [['max-tokens', 'max_tokens'], ['timeout', 'timeout_seconds']],
};
const ragCards = {};

function ragField(purpose, field) {
  return document.getElementById(`rag-${purpose}-${field}`);
}

function fillRagCard(card) {
  const p = card.purpose;
  ragCards[p] = card;
  setFieldValue(`rag-${p}-base-url`, card.base_url);
  setFieldValue(`rag-${p}-name`, card.model);
  setChecked(`rag-${p}-enabled`, card.enabled);
  setChecked(`rag-${p}-key-clear`, false);
  const key = ragField(p, 'key');
  if (key) {
    key.value = '';
    key.placeholder = card.api_key_set ? `已保存 ${card.api_key_masked}，留空＝不修改` : '留空＝不修改';
  }
  (CARD_NUMBER_FIELDS[p] || []).forEach(([suffix, field]) =>
    setFieldValue(`rag-${p}-${suffix}`, card[field]));
  setFieldValue(`rag-${p}-extra`, card.extra_json);   // only vision/chat have one
  const box = ragField(p, 'status');
  if (box && !box.textContent) {
    if (card.last_error) box.innerHTML = `<span class="status-warn">上次失败: ${escHtml(card.last_error)}</span>`;
    else if (card.ready) {
      box.innerHTML = `<span class="status-ok">就绪${p === 'embed' && card.probed_dim ? ` · ${card.probed_dim} 维` : ''}</span>`;
    } else if (p === 'vision') {
      box.innerHTML = '<span class="status-warn">未填 API 地址／模型名 —— 照片只出缩略图，描述队列原地等着</span>';
    }
  }
}

async function loadRagCards() {
  try {
    const cards = await API.listModelCards();
    cards.filter(c => CARD_PURPOSES.includes(c.purpose)).forEach(fillRagCard);
  } catch (e) {
    console.warn('rag cards load failed:', e.message);
  }
}

function collectRagCard(purpose) {
  const patch = {
    base_url: (ragField(purpose, 'base-url') || {}).value?.trim() || '',
    model: (ragField(purpose, 'name') || {}).value?.trim() || '',
    enabled: !!(ragField(purpose, 'enabled') || {}).checked,
  };
  (CARD_NUMBER_FIELDS[purpose] || []).forEach(([suffix, field]) => {
    const value = parseInt((ragField(purpose, suffix) || {}).value, 10);
    if (value > 0) patch[field] = value;      // blank keeps the stored value
  });
  const extra = ragField(purpose, 'extra');
  if (extra) patch.extra_json = extra.value.trim();   // filled from the card on load
  const key = (ragField(purpose, 'key') || {}).value;
  if ((ragField(purpose, 'key-clear') || {}).checked) patch.api_key = '';
  else if (key) patch.api_key = key;
  return patch;
}

async function saveRagCard(purpose) {
  const box = ragField(purpose, 'status');
  if (box) box.textContent = '保存中…';
  try {
    fillRagCard(await API.updateModelCard(purpose, collectRagCard(purpose)));
    if (box) box.textContent = '已保存';
    loadRagStatus();
  } catch (e) {
    if (box) box.innerHTML = `<span class="status-bad">${escHtml(e.message)}</span>`;
  }
}

async function testRagCard(purpose) {
  const box = ragField(purpose, 'status');
  if (box) box.textContent = '正在连接…';
  try {
    const report = await API.testModelCard(purpose);
    // The chat-shaped probes (echo, vision) answer with a reply, and both
    // report the model list — for vision that list is the whole point, the
    // model id has to name a multimodal one the server actually loaded.
    const options = document.getElementById(`rag-${purpose}-model-options`);
    if (options && report.models && report.models.length) {
      options.innerHTML = report.models.map(m => `<option value="${escHtml(m)}"></option>`).join('');
    }
    if (report.ok) {
      const extra = purpose === 'embed' && report.probe ? ` · 维度 ${report.probe.dim}` : '';
      const said = report.chat ? ` · ${report.chat.latency_ms || 0}ms · 回复「${escHtml(report.chat.reply || '')}」` : '';
      if (box) box.innerHTML = `<span class="status-ok">可用${extra}</span>${said}`
        + (report.models_error ? `<br>（${escHtml(report.models_error)}）` : '');
    } else {
      if (box) box.innerHTML = `<span class="status-bad">${escHtml(report.error || '探针未通过')}</span>`;
    }
  } catch (e) {
    if (box) box.innerHTML = `<span class="status-bad">${escHtml(e.message)}</span>`;
  }
  loadRagCards();
  loadRagStatus();
}

async function loadRagStatus() {
  const box = document.getElementById('rag-status');
  if (!box) return;
  try {
    const s = await API.ragStatus();
    const vectors = Object.entries(s.vectors || {})
      .map(([model, count]) => {
        const active = model === s.active_model;
        // Model names are free text from any provider card or /rag/import —
        // never interpolate them into a JS string inside onclick. The button
        // hands over itself and reads its data-model attribute instead.
        const del = active ? ''
          : ` <button class="btn btn-sm btn-ghost" data-model="${escAttr(model)}" onclick="purgeRagVectors(this)">清理</button>`;
        return `<div class="kv"><span>${escHtml(model)}</span><span>${count} 条向量${del}</span></div>`;
      }).join('') || '<div class="empty-hint">还没有向量</div>';
    box.innerHTML = `
      <div class="kv"><span>RAG 开关</span><span>${s.enabled ? '开' : '关（MSC_RAG_ENABLED=false）'}</span></div>
      <div class="kv"><span>当前 embed 模型</span><code>${escHtml(s.active_model || '未配置')}</code></div>
      ${vectors}
      <div class="kv"><span>嵌入队列</span><span>${s.dirty === 0 ? '<span class="status-ok">已清空</span>' : s.dirty + ' 条待嵌入'}</span></div>
    `;
    setFieldValue('rag-set-recall', s.settings?.recall_top);
    setFieldValue('rag-set-rrf', s.settings?.rrf_k);
    setFieldValue('rag-set-rerank', s.settings?.rerank_topn);
    setFieldValue('rag-set-batch', s.settings?.embed_batch);
  } catch (e) {
    box.innerHTML = `<div class="empty-hint">RAG 状态读取失败: ${escHtml(e.message)}</div>`;
  }
}

async function reindexRag() {
  const box = document.getElementById('rag-status-result');
  if (box) box.textContent = '排队中…';
  try {
    const result = await API.ragReindex();
    if (box) box.textContent = result.message;
    loadRagStatus();
  } catch (e) {
    if (box) box.textContent = '重建失败: ' + e.message;
  }
}

async function purgeRagVectors(button) {
  const model = button.dataset.model || '';
  if (!confirm(`删除模型「${model}」的全部向量？\n当前 embed 卡片用的模型不能删；切回该模型前需要重新嵌入。`)) return;
  try {
    const result = await API.ragPurgeVectors(model);
    toast(`已清理 ${result.removed} 条向量`);
    loadRagStatus();
  } catch (e) {
    toast('清理失败: ' + e.message, 'error');
  }
}

async function saveRagSettings() {
  const num = (id) => parseInt((document.getElementById(id) || {}).value, 10) || undefined;
  try {
    await API.ragUpdateSettings({
      recall_top: num('rag-set-recall'), rrf_k: num('rag-set-rrf'),
      rerank_topn: num('rag-set-rerank'), embed_batch: num('rag-set-batch'),
    });
    toast('融合参数已保存');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}

// ── Voice page ──────────────────────────────────────────────────────
// PCM from the mic → client-side WAV (16-bit mono 16k). whisper.cpp will not
// decode webm, so the browser does the container work. Mic needs
// https or localhost; plain http falls back to file upload.
const voice = { ctx: null, stream: null, processor: null, samples: [], recording: false, source: null };

function initVoicePage() {
  const status = document.getElementById('voice-status');
  const recordBtn = document.getElementById('voice-record-btn');
  const secure = window.isSecureContext || location.hostname === 'localhost';
  if (!secure || !navigator.mediaDevices) {
    recordBtn.disabled = true;
    if (status) status.textContent = '当前是 http 访问，麦克风不可用 — 用下面的文件上传代替';
  } else if (status && !status.textContent) {
    status.textContent = '就绪';
  }
}

async function toggleVoiceRecord() {
  if (voice.recording) { stopVoiceRecord(); return; }
  const status = document.getElementById('voice-status');
  try {
    voice.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    if (status) status.textContent = '麦克风不可用: ' + e.message;
    return;
  }
  voice.ctx = new (window.AudioContext || window.webkitAudioContext)();
  const targetRate = 16000;
  // ScriptProcessor is deprecated but universal; onaudioend fires inconsistently.
  voice.source = voice.ctx.createMediaStreamSource(voice.stream);
  voice.processor = voice.ctx.createScriptProcessor(4096, 1, 1);
  voice.samples = [];
  voice.processor.onaudioprocess = (ev) => {
    voice.samples.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
  };
  // A zero-gain node keeps the processor alive without routing the live mic
  // into the speakers (the callback fires without an audible howl).
  const sink = voice.ctx.createGain();
  sink.gain.value = 0;
  voice.source.connect(voice.processor);
  voice.processor.connect(sink);
  sink.connect(voice.ctx.destination);
  voice.recording = true;
  document.getElementById('voice-record-btn').textContent = '录音中…';
  document.getElementById('voice-stop-btn').style.display = '';
  if (status) status.textContent = '录音中，说完点停止';
}

function stopVoiceRecord() {
  if (!voice.recording) return;
  voice.recording = false;
  try { voice.processor.disconnect(); voice.source.disconnect(); } catch (e) { /* already gone */ }
  (voice.stream.getTracks() || []).forEach(t => t.stop());
  const sampleRate = voice.ctx.sampleRate;
  voice.ctx.close();

  document.getElementById('voice-record-btn').textContent = '开始录音';
  document.getElementById('voice-stop-btn').style.display = 'none';
  const status = document.getElementById('voice-status');
  if (status) status.textContent = '编码上传中…';
  uploadWav(encodeWav(downsample(voice.samples, sampleRate, 16000), 16000), 'voice-note.wav');
}

function downsample(chunks, fromRate, toRate) {
  const merged = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let off = 0;
  for (const c of chunks) { merged.set(c, off); off += c.length; }
  if (fromRate === toRate) return merged;
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(merged.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio), end = Math.min(Math.floor((i + 1) * ratio), merged.length);
    let sum = 0;
    for (let j = start; j < end; j++) sum += merged[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (offset, str) => { for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i)); };
  writeStr(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); writeStr(8, 'WAVE');
  writeStr(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  writeStr(36, 'data'); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([view], { type: 'audio/wav' });
}

async function transcribeFile() {
  const input = document.getElementById('voice-file');
  if (!input.files || !input.files[0]) { toast('先选一个音频文件', 'error'); return; }
  await uploadWav(input.files[0], input.files[0].name);
}

let voiceSimilar = [];

async function uploadWav(blob, filename) {
  const status = document.getElementById('voice-status');
  const form = new FormData();
  form.append('file', blob, filename);
  try {
    const result = await API.ragTranscribe(form);
    document.getElementById('voice-text').value = result.text;
    voiceSimilar = result.similar || [];
    renderVoiceSimilar();
    if (status) status.textContent = `转写完成（${result.latency_ms}ms）`;
  } catch (e) {
    if (status) status.textContent = '转写失败: ' + e.message;
    toast('转写失败: ' + e.message, 'error');
  }
}

function renderVoiceSimilar() {
  const box = document.getElementById('voice-similar');
  if (!voiceSimilar.length) {
    box.innerHTML = '<div class="empty-hint">没有相似记忆（或未配置 embed），可以直接新建</div>';
    return;
  }
  box.innerHTML = voiceSimilar.map(item => `
    <div class="log-item">
      <span>#${item.id} <a href="javascript:void(0)" onclick="showMemoryDetail('${escHtml(item.slug)}')">${escHtml(item.name)}</a></span>
      <span>cos ${item.cos}</span>
      <button class="btn btn-sm btn-ghost" onclick="appendVoiceToMemory('${escHtml(item.slug)}')">追加到这条</button>
    </div>`).join('');
}

async function appendVoiceToMemory(slug) {
  const text = document.getElementById('voice-text').value.trim();
  if (!text) { toast('转写框是空的', 'error'); return; }
  try {
    const m = await API.getMemory(slug);
    currentEditSlug = slug;
    document.getElementById('modal-title').textContent = '编辑记忆: ' + m.name;
    document.getElementById('form-name').value = m.name;
    document.getElementById('form-description').value = m.description || '';
    document.getElementById('form-body').value = (m.body || '') + '\n\n' + text;
    document.getElementById('form-tags').value = (m.tags || []).map(t => t.name).join(', ');
    document.getElementById('form-branch').value = m.branch || '';
    currentEditRefs = m.refs || [];
    document.getElementById('form-refs').value = currentEditRefs.join('  ');
    document.getElementById('form-category').value = m.category;
    document.getElementById('form-priority').value = m.priority;
    setNoReview(hasNoReview(m));
    document.getElementById('modal-overlay').classList.add('active');
  } catch (e) {
    toast('打开失败: ' + e.message, 'error');
  }
}

function createMemoryFromVoice() {
  const text = document.getElementById('voice-text').value.trim();
  if (!text) { toast('转写框是空的', 'error'); return; }
  currentEditSlug = null;
  document.getElementById('modal-title').textContent = '新建记忆（语音）';
  document.getElementById('form-name').value = text.split('\n')[0].slice(0, 40);
  document.getElementById('form-description').value = '';
  document.getElementById('form-body').value = text;
  document.getElementById('form-tags').value = 'voice';
  document.getElementById('form-branch').value = '';
  document.getElementById('form-refs').value = 'host:' + location.hostname;
  currentEditRefs = [];
  document.getElementById('form-category').value = 'knowledge';
  document.getElementById('form-priority').value = 'medium';
  setNoReview(false);
  document.getElementById('modal-overlay').classList.add('active');
}

// ── Insights page ───────────────────────────────────────────────────
async function loadDailyPick(refresh) {
  const status = document.getElementById('pick-status');
  const box = document.getElementById('daily-pick');
  if (status) status.textContent = refresh ? '模型写作中（1-3 分钟）…' : '读取中…';
  if (refresh) box.innerHTML = '';
  try {
    const result = await API.ragDailyPick(refresh);
    box.innerHTML = `<div class="pick-meta">${escHtml(result.date)} · 模型 ${escHtml(result.model || '')}` +
      ` · ${result.cached ? '今日缓存，秒开' : `刚写，${result.latency_ms || 0}ms`} · 取材 ${ (result.sources || []).length } 条</div>` +
      `<div class="pick-text">${escHtml(result.body)}</div>`;
    if (status) status.textContent = '';
  } catch (e) {
    if (status) status.textContent = '失败: ' + e.message;
  }
}

async function runEcho() {
  const slug = document.getElementById('echo-slug').value.trim();
  if (!slug) { toast('先填要评注的 slug 或名称', 'error'); return; }
  const status = document.getElementById('echo-status');
  status.textContent = '模型阅读中（1-2 分钟）…';
  const save = document.getElementById('echo-save').checked;
  const savePath = document.getElementById('echo-save-path').value.trim();
  try {
    const result = await API.ragEcho({
      slug, instruction: document.getElementById('echo-instruction').value.trim(),
      save, save_path: savePath || null,
    });
    const savedNote = result.saved_to
      ? `<div class="pick-meta">已存为 <code>${escHtml(result.saved_to)}</code>（下个同步周期入库）</div>` : '';
    document.getElementById('echo-result').innerHTML =
      `<div class="pick-meta">${escHtml(result.name)} · 卡片 ${escHtml(result.card || 'chat')} · 模型 ${escHtml(result.model)} · ${result.latency_ms}ms</div>` +
      `<div class="pick-text">${escHtml(result.body)}</div>` + savedNote;
    status.textContent = '';
  } catch (e) {
    status.textContent = '失败: ' + e.message;
  }
}

async function loadHealthReport() {
  const status = document.getElementById('health-status');
  const box = document.getElementById('health-report');
  status.textContent = '体检中…';
  try {
    const r = await API.ragHealthReport();
    const dupHtml = (r.duplicates || []).map(p => `
      <div class="log-item">
        <span>cos ${p.cos}</span>
        <a href="javascript:void(0)" onclick="showMemoryDetail('${escHtml(p.a.slug)}')">${escHtml(p.a.name)}</a> ↔
        <a href="javascript:void(0)" onclick="showMemoryDetail('${escHtml(p.b.slug)}')">${escHtml(p.b.name)}</a>
      </div>`).join('') || '<div class="empty-hint">没有近重复</div>';
    const staleHtml = (r.stale_temporary || []).map(m => `
      <div class="log-item">
        <a href="javascript:void(0)" onclick="showMemoryDetail('${escHtml(m.slug)}')">${escHtml(m.name)}</a>
        <span class="log-when">${escHtml((m.updated_at || '').substring(0, 10))}</span>
      </div>`).join('') || '<div class="empty-hint">没有过期 temporary</div>';
    box.innerHTML = `
      <h4>近重复对（建议「小结所选」合并）</h4>${dupHtml}
      ${r.duplicates_note ? `<div class="setting-hint">${escHtml(r.duplicates_note)}</div>` : ''}
      ${r.duplicates_error ? `<div class="setting-hint">${escHtml(r.duplicates_error)}</div>` : ''}
      <h4>过期 temporary（&gt;90 天，建议归档）</h4>${staleHtml}
      <h4>归档</h4><div class="kv"><span>已归档 ${r.archived?.count ?? 0} 条</span><span>最旧 ${escHtml((r.archived?.oldest || '—').substring(0, 10))}</span></div>
      <div class="setting-hint">${escHtml(r.suggestion || '')}</div>`;
    status.textContent = '';
  } catch (e) {
    status.textContent = '体检失败: ' + e.message;
  }
}

async function loadHeatmap() {
  const box = document.getElementById('rag-heatmap');
  const days = parseInt(document.getElementById('heatmap-days').value, 10) || 120;
  try {
    const data = await API.ragHeatmap(days);
    const counts = {};
    (data.counts || []).forEach(c => { counts[c.day] = c.count; });
    const max = Math.max(1, ...Object.values(counts));
    const cells = [];
    const today = new Date();
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today.getTime() - i * 86400000);
      const key = d.toISOString().substring(0, 10);
      const n = counts[key] || 0;
      const level = n === 0 ? 0 : Math.min(4, 1 + Math.floor((n / max) * 3));
      cells.push(`<span class="heat-cell heat-${level}" title="${key}: ${n} 条"></span>`);
    }
    box.innerHTML = cells.join('');
  } catch (e) {
    box.innerHTML = `<div class="empty-hint">热力图失败: ${escHtml(e.message)}</div>`;
  }
}
