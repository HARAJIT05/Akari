/* =========================================================
   Akari Dashboard — Frontend Logic
   ========================================================= */

// ── State ──────────────────────────────────────────────────
let cfg = {};
let appState = {};
let coverCache = {};
let editingAnime = null; // null = add, string = anime name being edited

// Unicode-safe base64 (handles Japanese/Chinese anime names)
function safeB64(str) {
  return btoa(encodeURIComponent(str).replace(/%([0-9A-F]{2})/g,
    (_, p1) => String.fromCharCode(parseInt(p1, 16))));
}

// ── Init ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await refreshAll();
  setTab('overview');
  setInterval(refreshAll, 30_000);
});

async function refreshAll() {
  try {
    const [configRes, stateRes] = await Promise.all([
      fetch('/api/config').then(r => r.json()),
      fetch('/api/state').then(r => r.json()),
    ]);
    cfg = configRes;
    appState = stateRes;
    renderCurrentTab();
  } catch (e) {
    console.error('Refresh failed:', e);
  }
}

// ── Tab Navigation ──────────────────────────────────────────
let currentTab = 'overview';

function setTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  document.querySelectorAll('.tab-pane').forEach(el => {
    el.classList.toggle('active', el.id === `tab-${tab}`);
  });
  const titles = {
    overview:  '🏠 Overview',
    anime:     '📺 Anime Watchlist',
    downloads: '⬇️ Active Downloads',
    history:   '📁 Download History',
    settings:  '⚙️ Settings',
    telegram:  '📱 Telegram',
    logs:      '📋 Live Logs',
  };
  document.getElementById('page-title').textContent = titles[tab] || tab;
  renderCurrentTab();
}

function renderCurrentTab() {
  if (currentTab === 'overview')  renderOverview();
  if (currentTab === 'anime')     renderAnimeTab();
  if (currentTab === 'downloads') loadDownloads();
  if (currentTab === 'history')   loadHistory();
  if (currentTab === 'settings')  renderSettings();
  if (currentTab === 'telegram')  renderTelegram();
  if (currentTab === 'logs')      loadLogs();
}

// ── AniList Cover Art ───────────────────────────────────────
async function fetchCover(name) {
  if (coverCache[name]) return coverCache[name];
  try {
    const q = `query{Media(search:"${name}",type:ANIME){coverImage{large}}}`;
    const r = await fetch('https://graphql.anilist.co', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q }),
    });
    const data = await r.json();
    const url = data?.data?.Media?.coverImage?.large || null;
    coverCache[name] = url;
    return url;
  } catch {
    return null;
  }
}

function coverImg(url, cls = 'anime-cover') {
  if (url) return `<img src="${url}" class="${cls}" alt="" loading="lazy">`;
  return `<div class="${cls}-placeholder">🎌</div>`;
}

// ── Helpers ─────────────────────────────────────────────────
function statusBadge(status) {
  const map = {
    downloading: `<span class="badge badge-downloading">⬇ Downloading</span>`,
    seeding:     `<span class="badge badge-seeding">✓ Seeding</span>`,
    idle:        `<span class="badge badge-idle">– Idle</span>`,
  };
  return map[status] || `<span class="badge badge-idle">– ${status || 'Not started'}</span>`;
}

function fmtSpeed(bytes) {
  if (!bytes) return '0 B/s';
  if (bytes > 1_048_576) return (bytes / 1_048_576).toFixed(1) + ' MB/s';
  if (bytes > 1024) return (bytes / 1024).toFixed(0) + ' KB/s';
  return bytes + ' B/s';
}

function fmtSize(bytes) {
  if (!bytes) return '?';
  if (bytes > 1_073_741_824) return (bytes / 1_073_741_824).toFixed(2) + ' GiB';
  if (bytes > 1_048_576)     return (bytes / 1_048_576).toFixed(1) + ' MiB';
  return (bytes / 1024).toFixed(0) + ' KiB';
}

function fmtEta(secs) {
  if (!secs || secs < 0 || secs > 8640000) return '∞';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

// ── Overview Tab ────────────────────────────────────────────
async function renderOverview() {
  const animeList = cfg.anime || [];
  const downloading = Object.values(appState).filter(s => s.status === 'downloading').length;
  const seeding = Object.values(appState).filter(s => s.status === 'seeding').length;

  document.getElementById('stat-anime').textContent   = animeList.length;
  document.getElementById('stat-dl').textContent      = downloading;
  document.getElementById('stat-seed').textContent    = seeding;
  document.getElementById('stat-poll').textContent    = (cfg.poll_interval_minutes || 15) + 'm';

  const grid = document.getElementById('overview-grid');
  if (!animeList.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1">
      <div class="empty-icon">📭</div>
      <div class="empty-text">No anime in your watchlist yet.<br>Go to <b>Anime</b> to add some!</div>
    </div>`;
    return;
  }

  grid.innerHTML = animeList.map(a => {
    const s = appState[a.name] || {};
    const ep = s.last_episode ? `EP${s.last_episode}` : 'No episodes yet';
    return `
      <div class="anime-card" id="card-${safeB64(a.name).replace(/=/g,'')}">
        <div class="anime-cover-placeholder">🎌</div>
        <div class="anime-body">
          <div class="anime-name" title="${a.name}">${a.name}</div>
          <div class="anime-ep">${ep} · ${a.preferred_resolution}</div>
          ${statusBadge(s.status)}
          ${s.status === 'downloading' && s.progress !== undefined ? `
            <div class="progress-wrap" style="margin-top:8px">
              <div class="progress-bar"><div class="progress-fill" style="width:${s.progress||0}%"></div></div>
            </div>` : ''}
        </div>
      </div>`;
  }).join('');

  // Lazy-load covers
  for (const a of animeList) {
    const card = document.getElementById('card-' + safeB64(a.name).replace(/=/g,''));
    if (!card) continue;
    const url = await fetchCover(a.name);
    if (url) {
      const ph = card.querySelector('.anime-cover-placeholder');
      if (ph) ph.outerHTML = `<img src="${url}" class="anime-cover" alt="${a.name}" loading="lazy">`;
    }
  }
}

// ── Anime Tab ───────────────────────────────────────────────
async function renderAnimeTab() {
  const animeList = cfg.anime || [];
  const container = document.getElementById('anime-list');

  if (!animeList.length) {
    container.innerHTML = `<div class="empty-state">
      <div class="empty-icon">📭</div>
      <div class="empty-text">No anime tracked yet. Click <b>+ Add Anime</b> to get started!</div>
    </div>`;
    return;
  }

  container.innerHTML = animeList.map(a => {
    const s = appState[a.name] || {};
    const ep = s.last_episode ? `EP${s.last_episode}` : 'Not started';
    return `
      <div class="anime-list-item" id="listitem-${safeB64(a.name).replace(/=/g,'')}">
        <div class="anime-list-thumb-ph">🎌</div>
        <div class="anime-list-info">
          <div class="anime-list-name">${a.name}</div>
          <div class="anime-list-meta">${ep} · ${a.season ? 'S' + a.season.replace(/^S/i, '') + ' · ' : ''}${a.preferred_resolution} · ${a.nyaa_query}</div>
          ${statusBadge(s.status)}
        </div>
        <div class="anime-list-actions">
          <button class="btn btn-ghost btn-sm" data-dl-name="${escAttr(a.name)}" onclick="openDownloadEpisode(this.dataset.dlName)">⬇️ Episode</button>
          <button class="btn btn-ghost btn-sm" data-edit-name="${escAttr(a.name)}" onclick="openEditAnime(this.dataset.editName)">✏️ Edit</button>
          <button class="btn btn-danger btn-sm" data-del-name="${escAttr(a.name)}" onclick="confirmDeleteAnime(this.dataset.delName)">🗑️</button>
        </div>
      </div>`;
  }).join('');

  // Lazy-load cover thumbs
  for (const a of animeList) {
    const item = document.getElementById('listitem-' + safeB64(a.name).replace(/=/g,''));
    if (!item) continue;
    const url = await fetchCover(a.name);
    if (url) {
      const ph = item.querySelector('.anime-list-thumb-ph');
      if (ph) ph.outerHTML = `<img src="${url}" class="anime-list-thumb" alt="${a.name}">`;
    }
  }
}

// ── Anime Modal ─────────────────────────────────────────────
function openAddAnime() {
  editingAnime = null;
  document.getElementById('modal-title').textContent = '➕ Add Anime';
  document.getElementById('anime-form').reset();
  document.getElementById('anime-season').value = '';
  document.getElementById('anime-res').value = '1080p';
  document.getElementById('anime-cat').value = '1_2';
  document.getElementById('anime-uncensored').checked = false;
  openModal('anime-modal');
}

function openEditAnime(name) {
  const a = (cfg.anime || []).find(x => x.name === name);
  if (!a) return;
  editingAnime = name;
  document.getElementById('modal-title').textContent = '✏️ Edit Anime';
  document.getElementById('anime-name').value = a.name;
  document.getElementById('anime-season').value = a.season || '';
  document.getElementById('anime-query').value = a.nyaa_query;
  document.getElementById('anime-res').value = a.preferred_resolution || '1080p';
  document.getElementById('anime-cat').value = a.category || '1_2';
  document.getElementById('anime-uncensored').checked = !!a.prefer_uncensored;
  openModal('anime-modal');
}

async function saveAnime() {
  const name   = document.getElementById('anime-name').value.trim();
  const season = document.getElementById('anime-season').value.trim();
  const query  = document.getElementById('anime-query').value.trim();
  const res    = document.getElementById('anime-res').value;
  const cat    = document.getElementById('anime-cat').value;
  const uncen  = document.getElementById('anime-uncensored').checked;

  if (!name || !query) { toast('Name and query are required', 'error'); return; }

  const payload = { name, nyaa_query: query, season, preferred_resolution: res, category: cat, prefer_uncensored: uncen };
  const isEdit  = !!editingAnime;
  const url     = isEdit ? `/api/anime/${encodeURIComponent(editingAnime)}` : '/api/anime';
  const method  = isEdit ? 'PUT' : 'POST';

  const res2 = await apiCall(method, url, payload);
  if (res2.ok) {
    toast(isEdit ? `'${name}' updated!` : `'${name}' added to watchlist!`, 'success');
    closeModal('anime-modal');
    await refreshAll();
  }
}

async function confirmDeleteAnime(name) {
  if (!confirm(`Remove "${name}" from your watchlist?\n\nThis will NOT delete any downloaded files.`)) return;
  const res = await apiCall('DELETE', `/api/anime/${encodeURIComponent(name)}`);
  if (res.ok) {
    toast(`'${name}' removed`, 'info');
    await refreshAll();
  }
}

// ── Manual Episode Download ──────────────────────────────────
let _dlAnime = null;

function openDownloadEpisode(name) {
  _dlAnime = name;
  document.getElementById('dl-ep-anime-name').textContent = name;
  document.getElementById('dl-ep-number').value = '';
  document.getElementById('dl-ep-results').innerHTML = '';
  document.getElementById('dl-ep-confirm').style.display = 'none';
  document.getElementById('dl-ep-search-btn').disabled = false;
  openModal('dl-ep-modal');
}

async function searchEpisode() {
  const ep = parseInt(document.getElementById('dl-ep-number').value);
  if (!ep || ep < 1) { toast('Enter a valid episode number', 'error'); return; }

  const btn = document.getElementById('dl-ep-search-btn');
  btn.textContent = '⏳ Searching…';
  btn.disabled = true;

  const resultsEl = document.getElementById('dl-ep-results');
  resultsEl.innerHTML = '<div class="dl-ep-loading">Searching Nyaa.si…</div>';
  document.getElementById('dl-ep-confirm').style.display = 'none';

  try {
    const data = await fetch(
      `/api/anime/${encodeURIComponent(_dlAnime)}/search-episode?episode=${ep}`
    ).then(r => r.json());

    if (!data.found || !data.results.length) {
      resultsEl.innerHTML = `<div class="dl-ep-none">😕 No releases found for episode ${ep} on Nyaa.si.<br><small>Try a different episode number or check back later.</small></div>`;
    } else {
      const best = data.results[0];
      resultsEl.innerHTML = `
        <div class="dl-ep-found">
          <div class="dl-ep-count">Found ${data.results.length} release${data.results.length > 1 ? 's' : ''} — best pick:</div>
          <div class="dl-ep-best">
            <div class="dl-ep-title">${escHtml(best.title)}</div>
            <div class="dl-ep-meta">
              <span class="dl-ep-badge ${best.trusted ? 'trusted' : 'untrusted'}">${best.trusted ? '✓ Trusted' : '⚠ Untrusted'}</span>
              <span>💾 ${best.size}</span>
              <span>🌱 ${best.seeders} seeders</span>
            </div>
          </div>
          ${data.results.length > 1 ? `
          <details class="dl-ep-more">
            <summary>Show all ${data.results.length} releases</summary>
            ${data.results.slice(1).map(r => `
              <div class="dl-ep-alt">
                <span class="dl-ep-alt-title">${escHtml(r.title)}</span>
                <span class="dl-ep-alt-meta">${r.trusted ? '✓' : '⚠'} ${r.size} · ${r.seeders}🌱</span>
              </div>`).join('')}
          </details>` : ''}
        </div>`;
      document.getElementById('dl-ep-confirm').style.display = 'flex';
    }
  } catch (e) {
    resultsEl.innerHTML = `<div class="dl-ep-none">❌ Search failed: ${e.message}</div>`;
  }

  btn.textContent = '🔍 Search';
  btn.disabled = false;
}

async function confirmDownload() {
  const ep = parseInt(document.getElementById('dl-ep-number').value);
  const btn = document.getElementById('dl-ep-confirm-btn');
  btn.textContent = '⏳ Queuing…';
  btn.disabled = true;

  const res = await apiCall('POST', `/api/anime/${encodeURIComponent(_dlAnime)}/download-episode`, {
    episode: ep
  });

  btn.textContent = '⬇️ Download';
  btn.disabled = false;

  if (res.ok) {
    toast(`EP${ep} queued! ${res.release ? '· ' + res.release.slice(0,50) + '…' : ''}`, 'success');
    closeModal('dl-ep-modal');
    setTab('downloads');
  }
}

// ── Downloads Tab ───────────────────────────────────────────
async function loadDownloads() {
  const container = document.getElementById('downloads-list');
  container.innerHTML = '<div style="color:var(--text-muted);font-size:.85rem">Loading…</div>';
  try {
    const data = await fetch('/api/downloads').then(r => r.json());
    if (data.error) {
      container.innerHTML = `<div class="empty-state">
        <div class="empty-icon">🔌</div>
        <div class="empty-text">${data.error}<br><small>Configure qBittorrent in <b>Settings</b></small></div>
      </div>`;
      return;
    }
    if (!data.torrents.length) {
      container.innerHTML = `<div class="empty-state">
        <div class="empty-icon">✅</div>
        <div class="empty-text">No active downloads. The bot is idle.</div>
      </div>`;
      return;
    }
    container.innerHTML = data.torrents.map(t => `
      <div class="download-card">
        <div class="download-top">
          <div class="download-name">${escHtml(t.name)}</div>
          <div class="download-actions">
            ${t.status === 'paused' ? 
              `<button class="btn btn-ghost btn-sm" onclick="actionDownload('${t.gid}', 'resume')">▶️</button>` : 
              `<button class="btn btn-ghost btn-sm" onclick="actionDownload('${t.gid}', 'pause')">⏸️</button>`
            }
            <button class="btn btn-danger btn-sm" onclick="actionDownload('${t.gid}', 'cancel')">❌</button>
          </div>
        </div>
        <div class="progress-wrap">
          <div class="progress-bar"><div class="progress-fill" style="width:${t.progress}%"></div></div>
          <div class="progress-label"><span>${t.progress}%</span><span>${t.status}</span></div>
        </div>
        <div class="download-meta">
          <span>⬇ ${fmtSpeed(t.dlspeed)}</span>
          <span>⬆ ${fmtSpeed(t.upspeed)}</span>
          <span>💾 ${fmtSize(t.size)}</span>
          <span>🕐 ETA ${fmtEta(t.eta)}</span>
          <span>🌱 ${t.num_seeds} seeds</span>
        </div>
      </div>`).join('');
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">❌</div><div class="empty-text">Failed to load downloads</div></div>`;
  }
}

async function actionDownload(gid, action) {
  if (action === 'cancel' && !confirm('Are you sure you want to cancel this download and delete files?')) return;
  const res = await apiCall('POST', `/api/downloads/${gid}/${action}`);
  if (res.ok) {
    toast(`Download ${action}d!`, 'success');
    loadDownloads();
  }
}

// ── Settings Tab ────────────────────────────────────────────
function renderSettings() {
  const a2 = cfg.aria2 || {};
  const dl = cfg.downloads || {};

  document.getElementById('a2-host').value   = a2.host   || 'http://aria2';
  document.getElementById('a2-port').value   = a2.port   || 6800;
  document.getElementById('a2-secret').value = a2.secret || 'akarisecret';
  document.getElementById('dl-path').value   = dl.save_path || '/downloads';
  document.getElementById('poll-mins').value = cfg.poll_interval_minutes || 15;
  document.getElementById('trusted-only').checked = cfg.trusted_only !== false;
}

async function saveSettings() {
  const updated = {
    ...cfg,
    poll_interval_minutes: parseInt(document.getElementById('poll-mins').value) || 15,
    trusted_only: document.getElementById('trusted-only').checked,
    aria2: {
      host:   document.getElementById('a2-host').value.trim(),
      port:   parseInt(document.getElementById('a2-port').value) || 6800,
      secret: document.getElementById('a2-secret').value.trim(),
    },
    downloads: {
      save_path: document.getElementById('dl-path').value.trim(),
    },
  };
  const res = await apiCall('POST', '/api/config', updated);
  if (res.ok) { toast('Settings saved!', 'success'); cfg = updated; }
}

async function testAria2() {
  const btn = document.getElementById('btn-test-a2');
  btn.textContent = '⏳ Testing…';
  btn.disabled = true;
  const res = await apiCall('POST', '/api/aria2/test', {
    host:   document.getElementById('a2-host').value.trim(),
    port:   parseInt(document.getElementById('a2-port').value) || 6800,
    secret: document.getElementById('a2-secret').value.trim(),
  });
  btn.textContent = '🔌 Test Connection';
  btn.disabled = false;
  toast(res.message, res.ok ? 'success' : 'error');
}

// ── Telegram Tab ────────────────────────────────────────────
function renderTelegram() {
  const tg = cfg.telegram || {};
  document.getElementById('tg-token').value   = tg.bot_token || '';
  document.getElementById('tg-chat').value    = tg.chat_id   || '';
  document.getElementById('tg-start').checked = tg.send_on_start !== false;
  document.getElementById('tg-ep').checked    = tg.send_on_new_episode !== false;
  document.getElementById('tg-del').checked   = !!tg.send_on_delete;
}

async function saveTelegram() {
  const updated = {
    ...cfg,
    telegram: {
      bot_token:          document.getElementById('tg-token').value.trim(),
      chat_id:            document.getElementById('tg-chat').value.trim(),
      send_on_start:      document.getElementById('tg-start').checked,
      send_on_new_episode:document.getElementById('tg-ep').checked,
      send_on_delete:     document.getElementById('tg-del').checked,
    },
  };
  const res = await apiCall('POST', '/api/config', updated);
  if (res.ok) { toast('Telegram settings saved!', 'success'); cfg = updated; }
}

async function testTelegram() {
  const btn = document.getElementById('btn-test-tg');
  btn.textContent = '⏳ Sending…';
  btn.disabled = true;
  const res = await apiCall('POST', '/api/telegram/test', {
    bot_token: document.getElementById('tg-token').value.trim(),
    chat_id:   document.getElementById('tg-chat').value.trim(),
  });
  btn.textContent = '📤 Send Test';
  btn.disabled = false;
  toast(res.message, res.ok ? 'success' : 'error');
}

// ── Logs Tab ────────────────────────────────────────────────
async function loadLogs() {
  const viewer = document.getElementById('log-viewer');
  const wasAtBottom = viewer.scrollTop + viewer.clientHeight >= viewer.scrollHeight - 20;

  try {
    const data = await fetch('/api/logs?lines=400').then(r => r.json());
    viewer.innerHTML = (data.logs || []).map(line => {
      const level = line.includes('[ERROR') ? 'ERROR' :
                    line.includes('[WARNING') ? 'WARNING' :
                    line.includes('[DEBUG') ? 'DEBUG' : 'INFO';
      return `<span class="log-line ${level}">${escHtml(line)}</span>`;
    }).join('\n');
    if (wasAtBottom) viewer.scrollTop = viewer.scrollHeight;
  } catch {
    viewer.innerHTML = '<span class="log-line ERROR">Failed to load logs</span>';
  }
}

function clearLogs() {
  document.getElementById('log-viewer').innerHTML = '';
}

// ── Check Now ───────────────────────────────────────────────
async function checkNow() {
  const res = await apiCall('POST', '/api/check-now');
  toast(res.message || 'Check triggered!', 'info');
}

// ── Modal Helpers ───────────────────────────────────────────
function openModal(id) {
  document.getElementById(id).classList.add('open');
}
function closeModal(id) {
  document.getElementById(id).classList.remove('open');
}

// ── API Helper ──────────────────────────────────────────────
async function apiCall(method, url, body = null) {
  try {
    const opts = { method, headers: {} };
    if (body) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = data.detail || data.message || `Error ${res.status}`;
      toast(msg, 'error');
      return { ok: false, message: msg };
    }
    return { ok: true, ...data };
  } catch (e) {
    toast('Network error: ' + e.message, 'error');
    return { ok: false, message: e.message };
  }
}

// ── Toast ────────────────────────────────────────────────────
function toast(msg, type = 'info') {
  const icons = { success: '✅', error: '❌', info: 'ℹ️' };
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = `<span>${icons[type]||'ℹ️'}</span><span>${msg}</span>`;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => {
    el.style.animation = 'slide-out 0.25s ease forwards';
    setTimeout(() => el.remove(), 250);
  }, 3500);
}

// ── Util ─────────────────────────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function escAttr(s) {
  return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Download History Tab ─────────────────────────────────────
async function loadHistory() {
  const container = document.getElementById('history-list');
  container.innerHTML = '<div style="color:var(--text-muted);font-size:.85rem">Loading…</div>';

  try {
    const data = await fetch('/api/history').then(r => r.json());
    const items = data.history || [];

    if (!items.length) {
      container.innerHTML = `<div class="empty-state">
        <div class="empty-icon">📭</div>
        <div class="empty-text">No download history yet.<br>The bot will populate this as episodes are downloaded.</div>
      </div>`;
      return;
    }

    const statusIcon = { complete: '✅', downloading: '⬇️', error: '❌', idle: '–' };
    const statusCls  = { complete: 'badge-seeding', downloading: 'badge-downloading', error: 'badge-error', idle: 'badge-idle' };

    container.innerHTML = items.map(h => {
      const icon    = statusIcon[h.status] || '–';
      const cls     = statusCls[h.status]  || 'badge-idle';
      const dateStr = h.updated_at ? new Date(h.updated_at).toLocaleString() : '—';
      const hasPath = !!h.host_path;
      return `
        <div class="history-card">
          <div class="history-top">
            <div class="history-left">
              <div class="history-name">${escHtml(h.name)}</div>
              <div class="history-meta">
                ${h.episode ? `<span>EP${h.episode}</span>` : ''}
                ${h.resolution ? `<span>${h.resolution}</span>` : ''}
                ${h.size ? `<span>💾 ${h.size}</span>` : ''}
                <span>🕐 ${dateStr}</span>
              </div>
              ${h.release_title ? `<div class="history-release">${escHtml(h.release_title)}</div>` : ''}
            </div>
            <div class="history-right">
              <span class="badge ${cls}">${icon} ${h.status}</span>
            </div>
          </div>
          ${hasPath ? `
          <div class="history-path">
            <span class="history-path-icon">📂</span>
            <span class="history-path-text" title="${escAttr(h.host_path)}">${escHtml(h.host_path)}</span>
            <div class="history-path-actions">
              <button class="btn btn-ghost btn-sm" title="Copy path"
                data-copy="${escAttr(h.host_path)}" onclick="copyPath(this.dataset.copy)">📋 Copy</button>
              <button class="btn btn-ghost btn-sm" title="Open in file manager"
                data-path="${escAttr(h.host_path)}" onclick="openFolder(this.dataset.path)">📂 Open</button>
            </div>
          </div>` : `<div class="history-path history-no-path">📂 File path not yet recorded</div>`}
        </div>`;
    }).join('');
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">❌</div><div class="empty-text">Failed to load history</div></div>`;
  }
}

async function copyPath(path) {
  try {
    await navigator.clipboard.writeText(path);
    toast('Path copied to clipboard!', 'success');
  } catch {
    // Fallback
    const el = document.createElement('textarea');
    el.value = path;
    document.body.appendChild(el);
    el.select();
    document.execCommand('copy');
    el.remove();
    toast('Path copied!', 'success');
  }
}

async function openFolder(hostPath) {
  const res = await apiCall('POST', '/api/open-folder', { path: hostPath });
  if (res.opened) {
    toast('Opening in file manager…', 'success');
  } else {
    // File manager couldn't be opened from Docker — copy path instead
    await copyPath(res.folder || hostPath);
    toast('Path copied! Paste it in your file manager address bar (Ctrl+L).', 'info');
  }
}
