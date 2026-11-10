// ── Photos (msc-pic) ────────────────────────────────────────────────
// The gallery renders placeholders and finished tiles in one grid: a tile is
// a grey hatched box while "discovered", gets its webp thumb as soon as the
// worker finishes that photo, and a red badge when it failed. Nothing here
// waits for a whole folder to finish — the same incremental contract the
// backend worker keeps.

let picOffset = 0;
let picTotal = 0;
const PIC_PAGE = 200;
let picObserver = null;
let picPollTimer = null;
let picSentinelOn = false;
let picLoadingMore = false;
let picSearchMode = false;   // grid shows search hits, not the gallery page
let picView = '';            // '' gallery | trash | missing
let picCurrentId = null;     // detail modal target
let picLastStatus = null;    // last /pic/status payload, for cheap re-renders
let picFoldersStamp = '';    // fingerprint of the folder list now in the select
const picSelected = new Set();

function picFilterParams() {
  const params = { limit: PIC_PAGE, offset: picOffset };
  const src = document.getElementById('pic-source-filter').value;
  if (src) params.source_id = src;
  const kind = document.getElementById('pic-kind-filter').value;
  if (kind) params.kind = kind;
  const month = document.getElementById('pic-month').value.trim();
  if (month) { params.taken_from = month; params.taken_to = month; }
  const q = document.getElementById('pic-search').value.trim();
  if (q) params.q = q;
  const folder = document.getElementById('pic-folder').value;
  if (folder) params.folder = folder;
  if (picView) params.view = picView;
  return params;
}

// One button out of every corner: clears source/kind/month/folder/text
// filters, the semantic-search result mode, the trash/missing view and the
// selection — always lands back on the plain gallery.
function picResetView() {
  document.getElementById('pic-source-filter').value = '';
  document.getElementById('pic-kind-filter').value = '';
  document.getElementById('pic-month').value = '';
  document.getElementById('pic-search').value = '';
  document.getElementById('pic-semantic').value = '';
  picView = '';
  picSearchMode = false;
  picSelected.clear();
  const empty = document.getElementById('pic-empty-trash-btn');
  if (empty) empty.style.display = 'none';
  const back = document.getElementById('pic-back-btn');
  if (back) back.style.display = 'none';
  const folderSel = document.getElementById('pic-folder');
  if (folderSel) { folderSel.innerHTML = '<option value="">全部目录</option>'; folderSel.disabled = true; }
  picFoldersStamp = '';
  updatePicTabs();
  updatePicActions();
  loadPics();
}

// 相册/缺失/回收站 are tabs, not a <select>: one state variable, and the tab
// strip plus the trash-only button follow it.
function picSetView(view) {
  picView = view;
  updatePicTabs();
  const empty = document.getElementById('pic-empty-trash-btn');
  if (empty) empty.style.display = view === 'trash' ? '' : 'none';
  loadPics();
}

function updatePicTabs() {
  document.querySelectorAll('.pic-tab').forEach(tab =>
    tab.classList.toggle('active', (tab.dataset.view || '') === picView));
}

async function loadPicFolders(status) {
  const sel = document.getElementById('pic-folder');
  if (!sel) return;
  const current = sel.value;
  const src = document.getElementById('pic-source-filter').value;
  if (!src) { sel.disabled = true; return; }
  sel.disabled = false;
  // A source can carry tens of thousands of rel_paths and this endpoint walks
  // all of them to fill the dropdown. Only refetch when that source actually
  // moved: its last completed walk plus its live row count.
  const row = ((status && status.sources) || []).find(s => String(s.id) === src);
  const counts = (row && row.counts) || {};
  const total = Object.keys(counts).reduce((sum, key) => sum + (counts[key] || 0), 0);
  const stamp = `${src}|${(row && row.last_scan_at) || ''}|${total}`;
  if (stamp === picFoldersStamp && sel.options.length > 1) return;
  sel.innerHTML = '<option value="">全部目录</option>';
  try {
    const r = await API.picFolders(src);
    sel.innerHTML = '<option value="">全部目录</option>' + (r.folders || []).map(f =>
      `<option value="${escHtml(f)}">${escHtml(f)}</option>`).join('');
    if (current) sel.value = current;
    picFoldersStamp = stamp;
  } catch (e) { /* no folders yet */ }
}

async function loadPics() {
  picOffset = 0;
  picSearchMode = false;
  picSelected.clear();
  updatePicActions();
  updatePicTabs();
  initPicSentinel();
  const back = document.getElementById('pic-back-btn');
  if (back) back.style.display = 'none';
  try {
    const [status, page] = await Promise.all([
      API.picStatus(), API.picItems(picFilterParams()),
    ]);
    fillPicSourceFilter(status.sources || []);
    loadPicFolders(status);
    renderPicGrid(page.items || [], false);
    picOffset = (page.items || []).length;
    picTotal = page.total || 0;
    // Counts last: the "显示 x/total" chip reads the paging window above.
    renderPicStatusLine(status);
  } catch (e) {
    const note = document.getElementById('pic-note');
    if (note) note.textContent = '照片功能不可用：' + e.message;
    const empty = document.getElementById('pic-empty');
    const text = document.getElementById('pic-empty-text');
    const hint = document.getElementById('pic-empty-hint');
    if (empty) empty.style.display = '';
    if (text) text.textContent = '照片功能不可用';
    if (hint) hint.textContent = e.message;
  }
  startPicPoll();
}

function picChip(cls, html) {
  return `<span class="pic-chip ${cls}">${html}</span>`;
}

function renderPicStatusLine(status) {
  const el = document.getElementById('pic-counts');
  if (!el) return;
  if (status) picLastStatus = status;
  status = status || picLastStatus || {};
  const c = status.counts || {};
  const parts = [];
  if (c.embedded) parts.push(picChip('pic-chip-ok', `已索引 <b>${c.embedded}</b>`));
  if (c.captioned) parts.push(picChip('pic-chip-warn', `待嵌入 <b>${c.captioned}</b>`));
  if (c.thumb_ready) parts.push(picChip('pic-chip-warn', `待描述 <b>${c.thumb_ready}</b>`));
  if (c.discovered) parts.push(picChip('pic-chip-warn', `待缩略图 <b>${c.discovered}</b>`));
  if (c.failed) parts.push(picChip('pic-chip-bad', `失败 <b>${c.failed}</b>`));
  const scanning = (status.sources || []).filter(s => s.state === 'scanning').length;
  // The 暂停图像描述 switch is visible from the gallery too: chip + live sync
  // of the settings-page button, whoever flipped it from either page.
  if (status.settings && status.settings.caption_paused !== undefined) {
    const paused = !!status.settings.caption_paused;
    if (paused !== picCaptionPaused) { picCaptionPaused = paused; picPaintCaptionPauseBtn(); }
  }
  if (picCaptionPaused) parts.push(picChip('pic-chip-dim', '描述已暂停'));
  if (status.missing) parts.push(picChip('pic-chip-dim', `缺失 <b>${status.missing}</b>`));
  if (status.trashed) parts.push(picChip('pic-chip-dim', `回收站 <b>${status.trashed}</b>`));
  if (scanning) parts.push(picChip('pic-chip-live', `${scanning} 个源正在扫描`));
  if (picTotal && !picSearchMode) {
    parts.push(picChip('pic-chip-dim', `显示 <b>${picOffset}</b>/${picTotal}`));
  }
  el.innerHTML = parts.length ? parts.join('') : picChip('pic-chip-dim', '还没有照片');
}

function fillPicSourceFilter(sources) {
  const sel = document.getElementById('pic-source-filter');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">全部来源</option>' + sources.map(s =>
    `<option value="${s.id}">${escHtml(s.name)}</option>`).join('');
  if (current) sel.value = current;
}

function picTileHtml(item) {
  const state = item.state || 'discovered';
  const badge = state === 'failed'
    ? `<span class="pic-badge" title="${escHtml(item.last_error || '')}">失败</span>`
    : '';
  const match = item.match
    ? `<span class="pic-match pic-match-${item.match}">${item.match === 'both' ? '双命中' : item.match === 'semantic' ? '语义' : '关键词'}</span>`
    : '';
  // Flags live inside the media box: they overlay the photo (or the skeleton)
  // instead of drifting over the caption row below it.
  const inner = item.thumb_available
    ? `<img data-src="${item.thumb_url}" alt="${escHtml(item.name)}">`
    : '<div class="pic-placeholder"><span class="pic-spin"></span></div>';
  const cap = item.caption ? `<div class="pic-tile-cap">${escHtml(item.caption)}</div>` : '';
  const check = '<label class="pic-check" onclick="event.stopPropagation()">' +
    `<input type="checkbox" onchange="picToggleSelect(${item.id})" ` +
    `${picSelected.has(item.id) ? 'checked' : ''}></label>`;
  const dup = item.dup ? '<span class="pic-dup" title="与别的照片内容相同（连拍/副本）">副本</span>' : '';
  let tileBtns = '';
  if (picView === 'trash') {
    tileBtns = `<div class="pic-tile-btns" onclick="event.stopPropagation()">` +
      `<button class="btn" onclick="picRestoreOne(${item.id})">恢复</button>` +
      `<button class="btn btn-danger" onclick="picPurgeOne(${item.id})">彻底删除</button></div>`;
  } else if (picView === 'missing') {
    tileBtns = `<div class="pic-tile-btns" onclick="event.stopPropagation()">` +
      `<button class="btn" onclick="picKeepOne(${item.id})">保留</button>` +
      `<button class="btn btn-danger" onclick="picForgetOne(${item.id})">清除</button></div>`;
  }
  const info = `<div class="pic-tile-info">` +
    `<div class="pic-tile-name">${escHtml(item.name)}</div>${cap}${tileBtns}</div>`;
  return `<div class="pic-tile state-${state}${picSelected.has(item.id) ? ' is-selected' : ''}" ` +
    `data-id="${item.id}" title="${escHtml(item.rel_path)}" ` +
    `onclick="picOpenDetail(${item.id})">` +
    `<div class="pic-tile-media">${inner}${check}${badge}${dup}${match}</div>${info}</div>`;
}

// Poll updates in place: same ids in the same order → mutate each tile's
// state class, swap placeholder for the thumb once it exists, add the caption
// line. Rebuilding the whole grid (the old behaviour) reloaded every image
// and made the page flicker every few seconds. A full re-render is reserved
// for when the id sequence actually changes (rescan, new photos, filtering).
function picSyncGrid(items) {
  const grid = document.getElementById('pic-grid');
  const tiles = Array.from(grid.querySelectorAll('.pic-tile'));
  if (tiles.length !== items.length ||
      items.some((it, i) => tiles[i].dataset.id !== String(it.id))) {
    renderPicGrid(items, false);
    picOffset = items.length;  // keep the paging window aligned with what's rendered
    return;
  }
  items.forEach((item, i) => picUpdateTile(tiles[i], item));
  observeLazyPics();  // covers tiles that were individually rebuilt above
}

function picUpdateTile(tile, item) {
  const state = item.state || 'discovered';
  const wantFailed = state === 'failed';
  const hasFailed = !!tile.querySelector('.pic-badge');
  const wantDup = !!item.dup;
  const hasDup = !!tile.querySelector('.pic-dup');
  // Badge appearance needs structure the live tile lacks: rebuild just that tile.
  if ((wantFailed && !hasFailed) || (!wantFailed && hasFailed) ||
      (wantDup !== hasDup)) {
    tile.outerHTML = picTileHtml(item);
    return;
  }
  tile.className = 'pic-tile state-' + state +
    (picSelected.has(item.id) ? ' is-selected' : '');
  const placeholder = tile.querySelector('.pic-placeholder');
  if (item.thumb_available && placeholder) {
    const img = document.createElement('img');
    img.alt = item.name;
    img.addEventListener('load', () => img.classList.add('pic-loaded'));
    img.src = item.thumb_url;   // the tile is on screen already: no lazy dance
    placeholder.replaceWith(img);
  } else if (item.thumb_available) {
    // Same id, different bytes: thumb_url carries ?v=<content_hash>, so a
    // changed version means the old picture must be refetched — never let a
    // stale (or id-reused) tile survive an in-place update.
    const img = tile.querySelector('img');
    const shown = img && (img.getAttribute('src') || img.dataset.src);
    if (img && shown && shown !== item.thumb_url) {
      img.classList.remove('pic-loaded');
      img.addEventListener('load', () => img.classList.add('pic-loaded'));
      if (img.hasAttribute('data-src')) img.dataset.src = item.thumb_url;
      else img.src = item.thumb_url;
    }
  }
  let capEl = tile.querySelector('.pic-tile-cap');
  if (item.caption) {
    if (!capEl) {
      capEl = document.createElement('div');
      capEl.className = 'pic-tile-cap';
      const info = tile.querySelector('.pic-tile-info') || tile;
      info.insertBefore(capEl, info.querySelector('.pic-tile-btns') || null);
    }
    if (capEl.textContent !== item.caption) capEl.textContent = item.caption;
  }
}

function renderPicGrid(items, append) {
  const grid = document.getElementById('pic-grid');
  if (!grid) return;
  if (!append) grid.innerHTML = '';
  let lastMonth = append ? grid.dataset.lastMonth || '' : '';
  let html = '';
  for (const item of items) {
    const month = (item.taken_at || '').slice(0, 7) || '未定时间';
    if (month !== lastMonth) {
      html += `<div class="pic-month-head" onclick="picPickMonth('${month}')">${month} <small>点选整月</small></div>`;
      lastMonth = month;
    }
    html += picTileHtml(item);
  }
  grid.dataset.lastMonth = lastMonth;
  grid.insertAdjacentHTML('beforeend', html);
  observeLazyPics();
  const note = document.getElementById('pic-note');
  if (note) note.textContent = (items.length && !append) ? '' : note.textContent;
  picUpdateEmptyState();
  picRefreshTail();
}

// The gallery is empty for a different reason in every view — say which one
// instead of leaving a blank rectangle where the photos should be.
function picUpdateEmptyState() {
  const empty = document.getElementById('pic-empty');
  if (!empty) return;
  const tiles = document.querySelectorAll('#pic-grid .pic-tile').length;
  empty.style.display = tiles ? 'none' : '';
  if (tiles) return;
  const text = document.getElementById('pic-empty-text');
  const hint = document.getElementById('pic-empty-hint');
  const copy = picSearchMode
    ? ['语义搜索没有命中', '换一句更具体的描述；embed 卡没配置时这里只有关键词腿。']
    : picView === 'trash'
      ? ['回收站是空的', '相册里的「移入回收站」只动 MSC 的记录，原图不会被删。']
      : picView === 'missing'
        ? ['没有缺失的照片', '原图从源里消失时才会进这个视图，缩略图和描述都还在。']
        : ['还没有照片', '在「设置 → 照片源」添加目录，worker 每 15 秒推进一段，中断可续传。'];
  if (text) text.textContent = copy[0];
  if (hint) hint.textContent = copy[1];
}

// Paging tail: PIC_PAGE tiles at a time, so the grid must say when the walk
// is over — and picLoadMore was, until now, never called by anything.
function picRefreshTail() {
  const bar = document.getElementById('pic-loadmore');
  if (!bar) return;
  if (picSearchMode || picLoadingMore) return;
  if (picTotal && picOffset >= picTotal) {
    bar.style.display = '';
    bar.textContent = `已显示全部 ${picTotal} 张`;
  } else if (!picOffset) {
    bar.style.display = 'none';
  } else {
    bar.style.display = '';
    bar.textContent = '继续滚动载入更多…';
  }
}

function observeLazyPics() {
  if (!('IntersectionObserver' in window)) {
    document.querySelectorAll('#pic-grid img[data-src]').forEach(img => {
      img.addEventListener('load', () => img.classList.add('pic-loaded'));
      img.src = img.dataset.src; img.removeAttribute('data-src');
    });
    return;
  }
  if (!picObserver) {
    picObserver = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        img.addEventListener('load', () => img.classList.add('pic-loaded'));
        img.src = img.dataset.src;
        img.removeAttribute('data-src');
        picObserver.unobserve(img);
      }
    }, { rootMargin: '200px' });
  }
  document.querySelectorAll('#pic-grid img[data-src]').forEach(img => picObserver.observe(img));
}

// The grid only ever held the first PIC_PAGE photos: picLoadMore existed but
// nothing called it. The sentinel under the grid now pulls the next page in
// as it scrolls into view.
function initPicSentinel() {
  if (picSentinelOn) return;
  const sentinel = document.getElementById('pic-sentinel');
  if (!sentinel) return;
  picSentinelOn = true;
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) picLoadMore();
    }, { rootMargin: '600px' }).observe(sentinel);
  } else {
    window.addEventListener('scroll', () => {
      if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 400) {
        picLoadMore();
      }
    });
  }
}

async function picLoadMore() {
  if (picSearchMode || picLoadingMore) return;
  if (!picOffset || picOffset >= picTotal) return;
  picLoadingMore = true;
  const bar = document.getElementById('pic-loadmore');
  if (bar) { bar.style.display = ''; bar.textContent = '载入更多…'; }
  try {
    const page = await API.picItems(picFilterParams());
    const got = (page.items || []).length;
    renderPicGrid(page.items || [], true);
    picOffset += got;
    picTotal = page.total || picTotal;
    if (!got) picOffset = picTotal;  // page emptied under us: stop the retry loop
  } catch (e) {
    if (bar) { bar.style.display = ''; bar.textContent = '载入失败：' + e.message; }
  } finally {
    picLoadingMore = false;
    renderPicStatusLine();   // the 显示 x/total chip follows the new page
    picRefreshTail();
  }
}

function picFilterChanged() { loadPics(); }

// ── semantic search over photo captions ─────────────────────────────

// Enter in the semantic box runs the search (and stops the browser's implicit
// form submit, which would reload the page).
function picSemanticKey(event) {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  picSemantic();
}

async function picSemantic() {
  const q = document.getElementById('pic-semantic').value.trim();
  if (!q) { loadPics(); return; }
  picSearchMode = true;
  try {
    const r = await API.picSearch(q, 24);
    picOffset = 0; picTotal = 0; // search view: no paging
    renderPicGrid(r.results || [], false);
    const back = document.getElementById('pic-back-btn');
    if (back) back.style.display = '';
    const el = document.getElementById('pic-counts');
    el.innerHTML = r.note
      ? picChip('pic-chip-warn', '语义搜索：' + escHtml(r.note))
      : picChip('pic-chip-ok', `「${escHtml(q)}」命中 <b>${(r.results || []).length}</b> 张`) +
        picChip('pic-chip-dim', '模型 ' + escHtml(r.model || '仅关键词'));
  } catch (e) {
    document.getElementById('pic-counts').innerHTML =
      picChip('pic-chip-bad', '语义搜索失败：' + escHtml(e.message));
  }
}

function picBackToGallery() {
  const back = document.getElementById('pic-back-btn');
  if (back) back.style.display = 'none';
  loadPics();
}

// ── selection, trash, purge ─────────────────────────────────────────

function picToggleSelect(id) {
  if (picSelected.has(id)) picSelected.delete(id); else picSelected.add(id);
  const tile = document.querySelector(`#pic-grid .pic-tile[data-id="${id}"]`);
  if (tile) tile.classList.toggle('is-selected', picSelected.has(id));
  updatePicActions();
}

function picClearSelection() {
  picSelected.clear();
  updatePicActions();
  const grid = document.getElementById('pic-grid');
  grid.querySelectorAll('.pic-check input').forEach(cb => { cb.checked = false; });
  grid.querySelectorAll('.pic-tile.is-selected').forEach(t => t.classList.remove('is-selected'));
}

function updatePicActions() {
  const bar = document.getElementById('pic-actions');
  if (!bar) return;
  bar.style.display = picSelected.size ? '' : 'none';
  document.getElementById('pic-sel-count').textContent = picSelected.size;
  const show = (id, on) => { document.getElementById(id).style.display = on ? '' : 'none'; };
  show('pic-archive-btn', picView === '');
  show('pic-restore-btn', picView === 'trash');
  show('pic-purge-btn', picView === 'trash');
  show('pic-keep-btn', picView === 'missing');
  show('pic-forget-btn', picView === 'missing');
}

async function picSelectAll() {
  const params = picFilterParams();
  params.ids_only = true; params.offset = 0; params.limit = 1000;
  try {
    const r = await API.picItems(params);
    (r.ids || []).forEach(id => picSelected.add(id));
    updatePicActions();
    // Tick the visible checkboxes in place. A loadPics() here used to wipe
    // the selection it had just made (loadPics clears picSelected).
    const tiles = document.querySelectorAll('#pic-grid .pic-tile');
    tiles.forEach(tile => {
      const on = picSelected.has(Number(tile.dataset.id));
      const cb = tile.querySelector('.pic-check input');
      if (cb) cb.checked = on;
      tile.classList.toggle('is-selected', on);
    });
    if (r.truncated || r.ids.length > tiles.length) {
      toast(`已选 ${r.ids.length} 张（含未翻页的部分），操作会作用于全部`, 'success');
    }
  } catch (e) { toast(e.message, 'error'); }
}

async function picPickMonth(month) {
  document.getElementById('pic-month').value = month === '未定时间' ? '' : month;
  picSearchMode = false;
  await loadPics();
  await picSelectAll();
}

async function picEmptyTrash() {
  const params = picFilterParams();
  params.ids_only = true; params.offset = 0; params.limit = 1000; params.view = 'trash';
  const r = await API.picItems(params);
  const ids = r.ids || [];
  if (!ids.length) { toast('回收站已经是空的', 'success'); return; }
  if (prompt(`清空回收站：${ids.length} 张的原图会从磁盘删除，不可恢复。输入 DELETE 确认：`) !== 'DELETE') return;
  let purged = 0, errors = [];
  for (let i = 0; i < ids.length; i += 500) {
    const part = await API.picPurge(ids.slice(i, i + 500), true);
    purged += part.purged; errors = errors.concat(part.errors || []);
  }
  toast(`回收站已清空：${purged} 张` + (errors.length ? `，${errors.length} 张失败` : ''),
        errors.length ? 'error' : 'success');
  loadPics();
}

async function picKeepSelected() { await API.picBatch([...picSelected], 'unmissing'); toast('已保留', 'success'); loadPics(); }
async function picForgetSelected() {
  const r = await API.picBatch([...picSelected], 'forget');
  toast(`已清除 ${r.changed} 张的缩略图和索引（原图未动）`, 'success');
  loadPics();
}
async function picKeepOne(id) { await API.picBatch([id], 'unmissing'); loadPics(); }
async function picForgetOne(id) { await API.picBatch([id], 'forget'); loadPics(); }

async function picArchiveSelected() {
  const r = await API.picBatch([...picSelected], 'archive');
  toast(`${r.changed} 张已移入回收站（原图未动）`, 'success');
  loadPics();
}

async function picRestoreSelected() {
  const r = await API.picBatch([...picSelected], 'restore');
  toast(`已恢复 ${r.changed} 张`, 'success');
  loadPics();
}

async function picPurgeSelected() {
  if (prompt(`彻底删除 ${picSelected.size} 张：磁盘上的原图也会被删，不可恢复。输入 DELETE 确认：`) !== 'DELETE') return;
  const r = await API.picPurge([...picSelected], true);
  toast(`彻底删除 ${r.purged} 张` + (r.errors.length ? `，${r.errors.length} 张失败` : ''),
        r.errors.length ? 'error' : 'success');
  loadPics();
}

async function picRestoreOne(id) {
  await API.picBatch([id], 'restore');
  toast('已恢复', 'success');
  loadPics();
}

async function picPurgeOne(id) {
  if (prompt('彻底删除这张：磁盘上的原图也会被删，不可恢复。输入 DELETE 确认：') !== 'DELETE') return;
  const r = await API.picPurge([id], true);
  toast(r.purged ? '原图与索引已彻底删除' : `删除失败：${(r.errors || [])[0] || ''}`,
        r.purged ? 'success' : 'error');
  loadPics();
}

// ── detail modal ─────────────────────────────────────────────────────
// The modal walks the tiles that are actually rendered: ← / → move through
// the same list the grid shows, so browsing a month is arrow-keys only.
let picDetailList = [];
let picDetailIndex = -1;
let picDetailThumb = '';
let picDetailOriginal = false;

function picOpenDetail(id) {
  picDetailList = Array.from(document.querySelectorAll('#pic-grid .pic-tile'))
    .map(tile => Number(tile.dataset.id));
  picDetailIndex = picDetailList.indexOf(id);
  return picShowItem(id);
}

async function picShowItem(id) {
  try {
    const it = await API.picItem(id);
    picCurrentId = id;
    picDetailThumb = it.thumb_url || '';
    picDetailOriginal = false;
    document.getElementById('pic-detail-title').textContent = it.name;
    const img = document.getElementById('pic-detail-img');
    img.onload = null;
    img.onerror = null;
    img.src = picDetailThumb;
    img.style.display = it.thumb_available ? '' : 'none';
    const orig = document.getElementById('pic-detail-orig-btn');
    orig.textContent = '查看原图';
    orig.disabled = !it.thumb_available && it.state === 'failed';
    document.getElementById('pic-detail-download').href = API.picOriginalUrl(id);
    picDetailHint(it);
    document.getElementById('pic-detail-pos').textContent = picDetailIndex >= 0
      ? `${picDetailIndex + 1} / ${picDetailList.length}` : '';
    document.getElementById('pic-detail-caption').textContent = it.caption || '';
    document.getElementById('pic-detail-tags').innerHTML =
      (it.tags || '').split(/[,，、;；\s]+/).filter(Boolean)
        .map(tag => `<span class="pic-tag">${escHtml(tag)}</span>`).join('');
    const when = it.taken_at ? `${it.taken_at}（${{exif: '相机时间', exif_naive: '相机时间·无时区', mtime: '文件时间'}[it.taken_at_src] || ''}）` : '时间未知';
    document.getElementById('pic-detail-meta').innerHTML =
      `<div class="kv"><span>状态</span><code>${escHtml(it.state)}</code></div>` +
      `<div class="kv"><span>来源</span><code>${escHtml(it.source_name)}</code></div>` +
      `<div class="kv"><span>路径</span><code>${escHtml(it.rel_path)}</code></div>` +
      `<div class="kv"><span>拍摄</span><code>${escHtml(when)}</code></div>` +
      `<div class="kv"><span>像素</span><code>${it.width}×${it.height}</code></div>` +
      `<div class="kv"><span>大小</span><code>${Math.round((it.size_bytes || 0) / 1024)} KB</code></div>` +
      `<div class="kv"><span>标识</span><code>${escHtml(it.origin_uri || '')}</code></div>`;
    document.getElementById('pic-detail-overlay').classList.add('active');
  } catch (e) { toast(e.message, 'error'); }
}

function picDetailHint(it) {
  const hint = document.getElementById('pic-detail-hint');
  if (it.thumb_available) { hint.style.display = 'none'; hint.textContent = ''; return; }
  hint.style.display = '';
  hint.textContent = it.state === 'failed'
    ? `这张处理失败：${it.last_error || '未知原因'}`
    : '缩略图还没生成，worker 正在排队处理';
}

function picDetailStep(delta) {
  if (!picDetailList.length || picDetailIndex < 0) return;
  const next = picDetailIndex + delta;
  if (next < 0 || next >= picDetailList.length) return;
  picDetailIndex = next;
  picShowItem(picDetailList[next]);
}

function closePicDetail() {
  document.getElementById('pic-detail-overlay').classList.remove('active');
}

// Thumb ↔ original in place. The original streams from wherever it lives
// (SMB/WebDAV included), so the stage says what it is waiting for.
function picToggleOriginal() {
  if (!picCurrentId) return;
  const img = document.getElementById('pic-detail-img');
  const btn = document.getElementById('pic-detail-orig-btn');
  const hint = document.getElementById('pic-detail-hint');
  if (!picDetailOriginal) {
    picDetailOriginal = true;
    img.style.display = '';
    hint.style.display = '';
    hint.textContent = '正在从源读取原图（远端源会慢一些）…';
    img.onload = () => { if (picDetailOriginal) hint.style.display = 'none'; };
    img.onerror = () => {
      hint.style.display = '';
      hint.textContent = '原图读取失败：源不可达，或超过 MSC_PIC_MAX_ORIGINAL_MB 上限';
    };
    img.src = API.picOriginalUrl(picCurrentId);
    btn.textContent = '回到缩略图';
  } else {
    picDetailOriginal = false;
    img.onload = null;
    img.onerror = null;
    img.src = picDetailThumb;
    img.style.display = picDetailThumb ? '' : 'none';
    btn.textContent = '查看原图';
    hint.style.display = 'none';
  }
}

// Esc closes, ← / → walk the on-screen tiles.
document.addEventListener('keydown', event => {
  const overlay = document.getElementById('pic-detail-overlay');
  if (!overlay || !overlay.classList.contains('active')) return;
  if (event.key === 'Escape') { closePicDetail(); return; }
  const tag = (event.target && event.target.tagName) || '';
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (event.key === 'ArrowLeft') { event.preventDefault(); picDetailStep(-1); }
  else if (event.key === 'ArrowRight') { event.preventDefault(); picDetailStep(1); }
});

function startPicPoll() {
  if (picPollTimer) return;
  picPollTimer = setInterval(async () => {
    const page = document.getElementById('page-pics');
    if (!page || !page.classList.contains('active')) return;
    try {
      const status = await API.picStatus();
      const pending = (status.counts.discovered || 0) + (status.counts.thumb_ready || 0);
      if (pending && !picSearchMode && !picView) {
        const params = picFilterParams();
        params.offset = 0;  // poll refreshes the on-screen window, not the paged tail
        const first = await API.picItems(params);
        if (first.items && !first.items.length && first.total) return;  // paranoid: never blank a full grid
        picSyncGrid(first.items || []);
        picTotal = first.total || picTotal;
        picRefreshTail();
      }
      renderPicStatusLine(status);  // after the paging window settles
    } catch (e) { /* page idle, next tick */ }
  }, 5000);
}

// ── Settings: photo source cards ────────────────────────────────────

async function loadPicSources() {
  const box = document.getElementById('pic-sources-list');
  if (!box) return;
  try {
    const sources = await API.picListSources();
    if (!sources.length) {
      box.innerHTML = '<div class="setting-hint">还没有照片源：添加一个目录，worker 会在 15 秒内开始摄入。</div>';
      return;
    }
    box.innerHTML = sources.map(s => {
      const c = s.counts || {};
      const stateName = { idle: '空闲', scanning: '扫描中', paused: '已暂停', error: '出错' }[s.state] || s.state;
      const stateChip = { idle: 'pic-chip-dim', scanning: 'pic-chip-live',
                          paused: 'pic-chip-warn', error: 'pic-chip-bad' }[s.state] || 'pic-chip-dim';
      const addr = s.kind === 'local' ? s.root_path
        : s.kind === 'smb' ? `smb://${s.host}/${s.share}/${s.subpath || ''}` : s.url;
      const buttons =
        `<button class="btn btn-sm" onclick="picProbeSrc(${s.id})">探测</button>` +
        `<button class="btn btn-sm" onclick="picScanSrc(${s.id})">重新扫描</button>` +
        (s.state === 'paused'
          ? `<button class="btn btn-sm" onclick="picResumeSrc(${s.id})">继续</button>`
          : `<button class="btn btn-sm" onclick="picPauseSrc(${s.id})">暂停</button>`) +
        `<button class="btn btn-sm btn-danger" onclick="picDeleteSrc(${s.id})">删除</button>`;
      const chips = [
        picChip(stateChip, escHtml(stateName)),
        picChip('pic-chip-ok', `索引 <b>${c.embedded || 0}</b>`),
        c.discovered ? picChip('pic-chip-warn', `待缩略图 <b>${c.discovered}</b>`) : '',
        c.thumb_ready ? picChip('pic-chip-warn', `待描述 <b>${c.thumb_ready}</b>`) : '',
        c.captioned ? picChip('pic-chip-warn', `待索引 <b>${c.captioned}</b>`) : '',
        c.failed ? picChip('pic-chip-bad', `失败 <b>${c.failed}</b>`) : '',
        s.resuming ? picChip('pic-chip-dim', '断点待续') : '',
      ].filter(Boolean).join('');
      return `<div class="pic-src">` +
        `<div class="pic-src-head">` +
        `<span class="pic-src-kind pic-src-${escHtml(s.kind)}">` +
        `${{ local: '本地', smb: 'SMB', webdav: 'WebDAV' }[s.kind] || escHtml(s.kind)}</span>` +
        `<b class="pic-src-name" title="${escHtml(addr)}">${escHtml(s.name)}</b></div>` +
        `<code class="pic-src-addr">${escHtml(addr)}</code>` +
        `<div class="pic-stats">${chips}</div>` +
        (s.last_error ? `<div class="pic-src-error">${escHtml(s.last_error)}</div>` : '') +
        `<div class="pic-src-btns">${buttons}</div></div>`;
    }).join('');
  } catch (e) {
    box.innerHTML = `<div class="setting-hint">照片功能不可用：${escHtml(e.message)}</div>`;
  }
}

async function loadPicSettings() {
  try {
    const status = await API.picStatus();
    const s = status.settings || {};
    document.getElementById('pic-set-scan-seconds').value = s.scan_seconds ?? '';
    document.getElementById('pic-set-thumb-batch').value = s.thumb_batch ?? '';
    document.getElementById('pic-set-rescan').value = s.rescan_hours ?? '';
    document.getElementById('pic-set-thumb-px').value = s.thumb_px ?? '';
    picCaptionPaused = !!s.caption_paused;
    picPaintCaptionPauseBtn();
  } catch (e) { /* kill switch off — leave the fields empty */ }
}

// Park the vision stage without touching any source card: scan and thumbs
// keep flowing, caption queue waits, zero vision calls while the model rests.
let picCaptionPaused = false;

function picPaintCaptionPauseBtn() {
  const btn = document.getElementById('pic-cap-pause-btn');
  if (btn) btn.textContent = picCaptionPaused ? '恢复图像描述' : '暂停图像描述';
}

async function picToggleCaptionPause() {
  const out = document.getElementById('pic-cap-pause-status');
  try {
    const r = await API.picUpdateSettings({ caption_paused: picCaptionPaused ? 0 : 1 });
    picCaptionPaused = !!((r.settings || {}).caption_paused);
    picPaintCaptionPauseBtn();
    if (out) out.textContent = picCaptionPaused
      ? '已暂停：待描述的照片原地排队'
      : '已恢复：下一轮（≤15 秒）继续';
  } catch (e) { if (out) out.textContent = e.message; }
}

async function savePicSettings() {
  const out = document.getElementById('pic-settings-status');
  try {
    const data = {
      scan_seconds: parseInt(document.getElementById('pic-set-scan-seconds').value) || undefined,
      thumb_batch: parseInt(document.getElementById('pic-set-thumb-batch').value) || undefined,
      rescan_hours: parseInt(document.getElementById('pic-set-rescan').value) || undefined,
      thumb_px: parseInt(document.getElementById('pic-set-thumb-px').value) || undefined,
    };
    const r = await API.picUpdateSettings(data);
    out.textContent = `已保存（${JSON.stringify(r.settings)}）`;
  } catch (e) { out.textContent = e.message; }
}

function picKindChanged() {
  const kind = document.getElementById('pic-src-kind').value;
  document.getElementById('pic-field-local').style.display = kind === 'local' ? '' : 'none';
  document.getElementById('pic-field-smb').style.display = kind === 'smb' ? '' : 'none';
  document.getElementById('pic-field-webdav').style.display = kind === 'webdav' ? '' : 'none';
}

let picBrowseDirs = [];

async function picBrowsePick() {
  const sel = document.getElementById('pic-browse-list');
  const path = document.getElementById('pic-src-path').value.trim();
  try {
    const r = await API.picBrowse(path);
    picBrowseDirs = r.dirs || [];
    sel.style.display = '';
    sel.innerHTML = `<option value="">${escHtml(r.path || r.message || '')} — 选择子目录…</option>` +
      picBrowseDirs.map(d => `<option value="${escHtml(d.path)}">${escHtml(d.name)}</option>`).join('');
  } catch (e) {
    document.getElementById('pic-src-status').textContent = e.message;
  }
}

function picBrowseChoose() {
  const sel = document.getElementById('pic-browse-list');
  if (sel.value) {
    document.getElementById('pic-src-path').value = sel.value;
    sel.value = '';
  }
}

async function picAddSource() {
  const out = document.getElementById('pic-src-status');
  const kind = document.getElementById('pic-src-kind').value;
  const data = {
    name: document.getElementById('pic-src-name').value.trim(),
    kind: kind,
  };
  if (kind === 'local') data.root_path = document.getElementById('pic-src-path').value.trim();
  if (kind === 'smb') {
    data.host = document.getElementById('pic-src-host').value.trim();
    data.share = document.getElementById('pic-src-share').value.trim();
    data.subpath = document.getElementById('pic-src-subpath').value.trim();
    data.username = document.getElementById('pic-src-user').value.trim();
    data.password = document.getElementById('pic-src-pass').value;
  }
  if (kind === 'webdav') {
    data.url = document.getElementById('pic-src-url').value.trim();
    data.username = document.getElementById('pic-src-user2').value.trim();
    data.password = document.getElementById('pic-src-pass2').value;
  }
  try {
    const r = await API.picCreateSource(data);
    out.textContent = `已添加（源 #${r.id}），worker 将在 15 秒内开始扫描`;
    loadPicSources();
  } catch (e) { out.textContent = e.message; }
}

async function picProbeSrc(id) {
  try {
    const r = await API.picProbe(id);
    toast(r.ok ? `源 ${id} 可达` : `源 ${id}：${r.error || '不可达'}`, r.ok ? 'success' : 'error');
  } catch (e) { toast(e.message, 'error'); }
  loadPicSources();
}

async function picScanSrc(id) { await API.picScan(id); toast(`源 ${id} 已排入扫描`, 'success'); loadPicSources(); }
async function picPauseSrc(id) { await API.picPause(id); toast(`源 ${id} 已暂停（游标保留）`, 'success'); loadPicSources(); }
async function picResumeSrc(id) { await API.picResume(id); toast(`源 ${id} 继续`, 'success'); loadPicSources(); }

async function picDeleteSrc(id) {
  if (!confirm('删除照片源只移除 MSC 里的缩略图和描述，原图不会被删除。确定？')) return;
  try {
    const r = await API.picDeleteSource(id);
    toast(`已删除源及 ${r.items_removed} 条派生记录（原图未动）`, 'success');
  } catch (e) { toast(e.message, 'error'); }
  loadPicSources();
}
