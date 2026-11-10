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
let picSearchMode = false;   // grid shows search hits, not the gallery page
let picView = '';            // '' gallery | trash | missing
let picCurrentId = null;     // detail modal target
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
  document.getElementById('pic-view').value = '';
  picView = '';
  picSearchMode = false;
  picSelected.clear();
  const empty = document.getElementById('pic-empty-trash-btn');
  if (empty) empty.style.display = 'none';
  const back = document.getElementById('pic-back-btn');
  if (back) back.style.display = 'none';
  const folderSel = document.getElementById('pic-folder');
  if (folderSel) { folderSel.innerHTML = '<option value="">全部目录</option>'; folderSel.disabled = true; }
  updatePicActions();
  loadPics();
}

function picViewChanged() {
  picView = document.getElementById('pic-view').value;
  const empty = document.getElementById('pic-empty-trash-btn');
  if (empty) empty.style.display = picView === 'trash' ? '' : 'none';
  loadPics();
}

async function loadPicFolders() {
  const sel = document.getElementById('pic-folder');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">全部目录</option>';
  const src = document.getElementById('pic-source-filter').value;
  if (!src) { sel.disabled = true; return; }
  sel.disabled = false;
  try {
    const r = await API.picFolders(src);
    sel.innerHTML = '<option value="">全部目录</option>' + (r.folders || []).map(f =>
      `<option value="${escHtml(f)}">${escHtml(f)}</option>`).join('');
    if (current) sel.value = current;
  } catch (e) { /* no folders yet */ }
}

async function loadPics() {
  picOffset = 0;
  picSearchMode = false;
  picSelected.clear();
  updatePicActions();
  const back = document.getElementById('pic-back-btn');
  if (back) back.style.display = 'none';
  try {
    const [status, page] = await Promise.all([
      API.picStatus(), API.picItems(picFilterParams()),
    ]);
    renderPicStatusLine(status);
    fillPicSourceFilter(status.sources || []);
    loadPicFolders();
    renderPicGrid(page.items || [], false);
    picOffset = (page.items || []).length;
    picTotal = page.total || 0;
  } catch (e) {
    const note = document.getElementById('pic-note');
    if (note) note.textContent = '照片功能不可用：' + e.message;
  }
  startPicPoll();
}

function renderPicStatusLine(status) {
  const el = document.getElementById('pic-counts');
  if (!el) return;
  const c = status.counts || {};
  const parts = [];
  if (c.embedded) parts.push(`已索引 ${c.embedded}`);
  if (c.captioned) parts.push(`待嵌入 ${c.captioned}`);
  if (c.thumb_ready) parts.push(`待描述 ${c.thumb_ready}`);
  if (c.discovered) parts.push(`待缩略图 ${c.discovered}`);
  if (c.failed) parts.push(`失败 ${c.failed}`);
  const scanning = (status.sources || []).filter(s => s.state === 'scanning').length;
  if (status.missing) parts.push(`缺失 ${status.missing}`);
  if (status.trashed) parts.push(`回收站 ${status.trashed}`);
  el.textContent = (parts.length ? parts.join(' · ') : '还没有照片') +
    (scanning ? ` · ${scanning} 个源正在扫描` : '') +
    (picTotal ? `（显示 ${picOffset}/${picTotal}）` : '');
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
    ? `<span class="pic-badge" title="${escHtml(item.last_error || '')}">✕</span>`
    : '';
  const match = item.match
    ? `<span class="pic-match pic-match-${item.match}">${item.match === 'both' ? '双命中' : item.match === 'semantic' ? '语义' : '关键词'}</span>`
    : '';
  const media = item.thumb_available
    ? `<img data-src="${item.thumb_url}" alt="${escHtml(item.name)}">`
    : '<div class="pic-placeholder">⏳</div>';
  const cap = item.caption ? `<div class="pic-tile-cap">${escHtml(item.caption)}</div>` : '';
  const check = '<label class="pic-check" onclick="event.stopPropagation()">' +
    `<input type="checkbox" onchange="picToggleSelect(${item.id})" ` +
    `${picSelected.has(item.id) ? 'checked' : ''}></label>`;
  const dup = item.dup ? '<span class="pic-dup" title="与别的照片内容相同（连拍/副本）">⧉ 副本</span>' : '';
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
  return `<div class="pic-tile state-${state}" data-id="${item.id}" title="${escHtml(item.rel_path)}" ` +
    `onclick="picOpenDetail(${item.id})">${check}${badge}${match}${dup}${media}` +
    `<div class="pic-tile-name">${escHtml(item.name)}</div>${cap}${tileBtns}</div>`;
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
  tile.className = 'pic-tile state-' + state;
  const placeholder = tile.querySelector('.pic-placeholder');
  if (item.thumb_available && placeholder) {
    const img = document.createElement('img');
    img.alt = item.name;
    img.src = item.thumb_url;   // the tile is on screen already: no lazy dance
    placeholder.replaceWith(img);
  }
  let capEl = tile.querySelector('.pic-tile-cap');
  if (item.caption) {
    if (!capEl) {
      capEl = document.createElement('div');
      capEl.className = 'pic-tile-cap';
      const btns = tile.querySelector('.pic-tile-btns');
      tile.insertBefore(capEl, btns || null);
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
}

function observeLazyPics() {
  if (!('IntersectionObserver' in window)) {
    document.querySelectorAll('#pic-grid img[data-src]').forEach(img => {
      img.src = img.dataset.src; img.removeAttribute('data-src');
    });
    return;
  }
  if (!picObserver) {
    picObserver = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        img.src = img.dataset.src;
        img.removeAttribute('data-src');
        picObserver.unobserve(img);
      }
    }, { rootMargin: '200px' });
  }
  document.querySelectorAll('#pic-grid img[data-src]').forEach(img => picObserver.observe(img));
}

async function picLoadMore() {
  if (!picOffset || picOffset >= picTotal) return;
  const page = await API.picItems(picFilterParams());
  renderPicGrid(page.items || [], true);
  picOffset += (page.items || []).length;
  picTotal = page.total || picTotal;
}

function picFilterChanged() { loadPics(); }

// ── semantic search over photo captions ─────────────────────────────

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
    el.textContent = r.note
      ? `语义搜索：${r.note}`
      : `「${q}」命中 ${(r.results || []).length} 张（模型 ${(r.model || '仅关键词')}）`;
  } catch (e) {
    document.getElementById('pic-counts').textContent = '语义搜索失败：' + e.message;
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
  updatePicActions();
}

function picClearSelection() {
  picSelected.clear();
  updatePicActions();
  const grid = document.getElementById('pic-grid');
  grid.querySelectorAll('.pic-check input').forEach(cb => { cb.checked = false; });
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
      const cb = tile.querySelector('.pic-check input');
      if (cb) cb.checked = picSelected.has(Number(tile.dataset.id));
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

async function picOpenDetail(id) {
  try {
    const it = await API.picItem(id);
    picCurrentId = id;
    document.getElementById('pic-detail-title').textContent = it.name;
    const img = document.getElementById('pic-detail-img');
    img.src = it.thumb_url || '';
    img.style.display = it.thumb_available ? '' : 'none';
    const when = it.taken_at ? `${it.taken_at}（${{exif: '相机时间', exif_naive: '相机时间·无时区', mtime: '文件时间'}[it.taken_at_src] || ''}）` : '时间未知';
    document.getElementById('pic-detail-meta').innerHTML =
      `状态：${it.state} · 来源：${escHtml(it.source_name)} · ${escHtml(it.rel_path)}<br>` +
      `拍摄：${when} · 尺寸：${it.width}×${it.height} · ${Math.round((it.size_bytes || 0) / 1024)}KB<br>` +
      (it.caption ? `描述：${escHtml(it.caption)}<br>` : '') +
      (it.tags ? `标签：${escHtml(it.tags)}<br>` : '') +
      `<code>${escHtml(it.origin_uri || '')}</code>`;
    document.getElementById('pic-detail-overlay').classList.add('active');
  } catch (e) { toast(e.message, 'error'); }
}

function closePicDetail() {
  document.getElementById('pic-detail-overlay').classList.remove('active');
}

function picShowOriginal() {
  if (!picCurrentId) return;
  const img = document.getElementById('pic-detail-img');
  img.src = API.picOriginalUrl(picCurrentId);
  img.style.display = '';
  img.onerror = () => toast('原图读取失败（源不可达或超过大小上限）', 'error');
}

function startPicPoll() {
  if (picPollTimer) return;
  picPollTimer = setInterval(async () => {
    const page = document.getElementById('page-pics');
    if (!page || !page.classList.contains('active')) return;
    try {
      const status = await API.picStatus();
      renderPicStatusLine(status);
      const pending = (status.counts.discovered || 0) + (status.counts.thumb_ready || 0);
      if (pending && !picSearchMode && !picView) {
        const params = picFilterParams();
        params.offset = 0;  // poll refreshes the on-screen window, not the paged tail
        const first = await API.picItems(params);
        if (first.items && !first.items.length && first.total) return;  // paranoid: never blank a full grid
        picSyncGrid(first.items || []);
        picTotal = first.total || picTotal;
      }
    } catch (e) { /* page idle, next tick */ }
  }, 5000);
}

// ── Settings: photo source cards ────────────────────────────────────

async function loadPicSources() {
  const box = document.getElementById('pic-sources-list');
  if (!box) return;
  try {
    const sources = await API.picListSources();
    if (!sources.length) { box.innerHTML = '<div class="setting-hint">还没有照片源。</div>'; return; }
    box.innerHTML = sources.map(s => {
      const c = s.counts || {};
      const badge = { idle: '', scanning: '扫描中', paused: '已暂停', error: '出错' }[s.state] || s.state;
      const addr = s.kind === 'local' ? s.root_path
        : s.kind === 'smb' ? `smb://${s.host}/${s.share}/${s.subpath || ''}` : s.url;
      const buttons =
        `<button class="btn" onclick="picProbeSrc(${s.id})">探测</button>` +
        `<button class="btn" onclick="picScanSrc(${s.id})">扫描</button>` +
        (s.state === 'paused'
          ? `<button class="btn" onclick="picResumeSrc(${s.id})">继续</button>`
          : `<button class="btn" onclick="picPauseSrc(${s.id})">暂停</button>`) +
        `<button class="btn btn-danger" onclick="picDeleteSrc(${s.id})">删除</button>`;
      return `<div class="log-item"><div><b>${escHtml(s.name)}</b> · ${s.kind} · ${escHtml(addr)}` +
        `${s.resuming ? ' · <i>断点待续</i>' : ''}<br>` +
        `<span class="setting-hint">发现 ${c.discovered || 0} · 缩略图 ${c.thumb_ready || 0} · ` +
        `描述 ${c.captioned || 0} · 索引 ${c.embedded || 0} · 失败 ${c.failed || 0}` +
        (s.last_error ? ` · ${escHtml(s.last_error)}` : '') + `</span></div><div>${buttons}</div></div>`;
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
  } catch (e) { /* kill switch off — leave the fields empty */ }
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
