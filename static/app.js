(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const icon = (name, extra = '') => `<svg class="icon ${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const array = value => Array.isArray(value) ? value : [];
  const numeric = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const number = value => numeric(value) ? Number(value) : 0;
  const fmt = value => numeric(value) ? new Intl.NumberFormat('en-US', Number.isInteger(Number(value)) ? { maximumFractionDigits: 0 } : { maximumSignificantDigits: 6 }).format(Number(value)) : '—';
  const compact = value => new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
  const percent = value => numeric(value) ? `${fmt(value)}%` : '—';
  const clamp = value => Math.min(100, Math.max(0, number(value)));
  const initials = name => String(name || '?').trim().split(/\s+/).map(part => Array.from(part)[0]).slice(0, 2).join('').toUpperCase();
  const freshFilters = () => ({ author: '', path: '', kind: 'directory', since: '', until: '', commits: [] });
  const makeContext = () => ({ filters: freshFilters(), selected: new Set() });
  const contexts = new Map();
  const requests = new Map();
  const markupCache = new Map();
  const state = {
    repos: [], id: null, meta: null, metrics: null, context: makeContext(), tab: 'overview',
    metaLoading: false, metricsLoading: false, commitsLoading: false, dataFailed: false,
    commits: null, page: 1, perPage: 50, query: '', fileKind: 'files', chart: 'changes',
    fileSort: { key: 'churn', direction: -1 }, authorSort: { key: 'ownership', direction: -1 }, filePage: 1, authorPage: 1,
    epoch: 0, metaVersion: 0, metricVersion: 0, commitVersion: 0, listRevision: 0, listBusy: false,
    pollTimer: null, online: false, importMode: 'clone', file: null, detailVersion: 0,
    exporting: false, mergeId: null, mergeAuthors: [], deleteId: null
  };
  const currentRepo = () => state.repos.find(repo => String(repo.id) === state.id);
  const ready = () => currentRepo()?.status === 'ready';
  const repoURL = (suffix = '', id = state.id) => `/api/repos/${encodeURIComponent(id)}${suffix}`;
  const text = (id, value) => { if ($(id).textContent !== String(value)) $(id).textContent = value; };
  const announce = message => text('live-status', message);
  function setMarkup(id, markup) {
    if (markupCache.get(id) === markup) return;
    const focusedRepo = $(id).contains(document.activeElement) ? document.activeElement.dataset.repo : undefined;
    markupCache.set(id, markup);
    $(id).innerHTML = markup;
    if (focusedRepo !== undefined) all('[data-repo]', $(id)).find(button => button.dataset.repo === focusedRepo)?.focus();
  }
  const storage = {
    get() { try { return localStorage.getItem('rat.repository'); } catch { return null; } },
    set(id) { try { if (id === null) localStorage.removeItem('rat.repository'); else localStorage.setItem('rat.repository', id); } catch { /* Storage may be disabled. */ } }
  };

  function cancelRequest(key) { requests.get(key)?.abort(); requests.delete(key); }
  async function request(url, options = {}, key, timeout = 30000) {
    if (key) cancelRequest(key);
    const controller = new AbortController();
    if (key) requests.set(key, controller);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    try {
      const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options, signal: controller.signal });
      const type = response.headers.get('content-type') || '';
      if (options.asBlob && response.ok) return await response.blob();
      let payload;
      if (type.includes('json')) payload = await response.json();
      else if (response.ok) throw new Error('The server returned a non-JSON response. Check the API route.');
      if (!response.ok) throw new Error(typeof payload?.error === 'string' ? payload.error : `Request failed (HTTP ${response.status}). Check that the analysis API is running.`);
      if (!payload || typeof payload !== 'object') throw new Error('The API returned an invalid response.');
      return payload;
    } catch (error) {
      if (timedOut) throw new Error(options.method && options.method !== 'GET' ? 'The request timed out. Its outcome is unknown; refresh the repository list before trying again.' : 'The server took too long to respond. Please try again.');
      if (error.name === 'TypeError') throw new Error('Cannot reach the analysis API. Check your connection and backend, then try again.');
      throw error;
    } finally {
      clearTimeout(timer);
      if (key && requests.get(key) === controller) requests.delete(key);
    }
  }
  function showError(id, message) {
    const target = $(`${id}-text`) || $(id);
    if (target.textContent !== message) target.textContent = message;
    $(id).hidden = false;
  }
  function toast(message, error = false) {
    const node = document.createElement('div');
    node.className = `toast${error ? ' error' : ''}`;
    node.innerHTML = `${icon(error ? 'info' : 'check')}<span>${esc(message)}</span><button class="icon-button" aria-label="Dismiss notification">${icon('close')}</button>`;
    node.querySelector('button').addEventListener('click', () => node.remove());
    $('toast-region').append(node);
    setTimeout(() => node.remove(), error ? 12000 : 6500);
  }
  function queryParams(includeCommits = true) {
    const filters = state.context.filters;
    const params = new URLSearchParams();
    if (filters.author !== '') params.set('author', filters.author);
    if (filters.path !== '' || filters.kind === 'file') { params.set('path', filters.path); params.set('kind', filters.kind); }
    if (filters.since !== '') params.set('since', filters.since);
    if (filters.until !== '') params.set('until', filters.until);
    if (includeCommits && filters.commits.length) params.set('commits', filters.commits.join(','));
    return params;
  }
  function dateText(value, includeTime = false) {
    if (!numeric(value)) return '—';
    const date = new Date(Number(value) * 1000);
    if (!Number.isFinite(date.getTime())) return '—';
    return date.toLocaleString('en-US', { year: 'numeric', month: 'short', day: 'numeric', ...(includeTime ? { hour: '2-digit', minute: '2-digit' } : {}) });
  }
  function localDate(value) {
    if (!numeric(value)) return '';
    const date = new Date(Number(value) * 1000);
    const pad = val => String(val).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(refreshRepos, document.hidden ? 30000 : state.repos.some(repo => repo.status === 'indexing') ? 2500 : 15000);
  }
  async function refreshRepos() {
    if (state.listBusy) return;
    state.listBusy = true;
    const revision = state.listRevision;
    $('retry-api').disabled = true;
    try {
      const result = await request('/api/repos', {}, 'repos');
      if (!Array.isArray(result.repos)) throw new Error('The repository list response is missing its repos array.');
      if (revision !== state.listRevision) return;
      const previous = currentRepo();
      state.repos = result.repos.filter(repo => repo && repo.id !== undefined && repo.id !== null);
      state.online = true;
      $('api-error').hidden = true;
      $('connection-status').classList.remove('offline');
      $('connection-status').innerHTML = '<span class="status-dot"></span>Workspace connected';
      const current = currentRepo();
      if (!current) {
        const remembered = storage.get();
        const next = state.repos.find(repo => String(repo.id) === remembered) || state.repos[0];
        if (next || state.id !== null) selectRepo(next ? String(next.id) : null);
      } else if (previous?.status !== current.status || previous?.head !== current.head) {
        selectRepo(state.id, true);
      }
      renderRepositories();
      renderRepositoryState();
    } catch (error) {
      if (error.name === 'AbortError' || revision !== state.listRevision) return;
      state.online = false;
      $('connection-status').classList.add('offline');
      $('connection-status').innerHTML = '<span class="status-dot"></span>API unavailable';
      showError('api-error', error.message);
    } finally { state.listBusy = false; $('retry-api').disabled = false; schedulePoll(); }
  }
  function renderRepositories() {
    text('repo-count', state.repos.length);
    $('repo-search-wrap').hidden = state.repos.length < 5;
    const search = $('repo-search').value.toLocaleLowerCase();
    const repos = state.repos.filter(repo => String(repo.name || repo.id).toLocaleLowerCase().includes(search));
    setMarkup('repo-list', repos.length ? repos.map(repo => {
      const status = ['ready', 'indexing', 'error'].includes(repo.status) ? repo.status : 'indexing';
      return `<button class="repo-item${String(repo.id) === state.id ? ' selected' : ''}" data-repo="${esc(repo.id)}" aria-current="${String(repo.id) === state.id ? 'true' : 'false'}" title="${esc(repo.name || repo.id)}">${icon('repo')}<span><strong>${esc(repo.name || 'Untitled repository')}</strong><small>${status === 'ready' ? `${fmt(repo.commit_count)} commits` : status === 'error' ? 'Indexing failed' : 'Indexing history…'}</small></span><span class="repo-status ${status}" aria-label="${status}"></span></button>`;
    }).join('') : `<div class="sidebar-empty">${state.repos.length ? 'No matching repositories.' : 'Your repositories will live here.'}</div>`);
  }
  function renderRepositoryState() {
    const repo = currentRepo();
    $('welcome-card').hidden = !!repo;
    $('repo-delete').hidden = !repo;
    text('page-title', repo ? repo.name || 'Repository overview' : 'Repository overview');
    text('page-description', repo ? `${repo.reference || 'HEAD'} · ${repo.status === 'ready' ? 'Your history, seen from a different angle.' : repo.status === 'error' ? 'This repository needs your attention.' : 'Making sense of your repository’s history.'}` : 'Understand the people, changes, and patterns behind your code.');
    document.title = `${repo?.name ? `${repo.name} — ` : ''}RAT · Repository intelligence`;
    $('indexing-card').hidden = !repo || repo.status === 'ready';
    if (repo && repo.status !== 'ready') {
      const failed = repo.status === 'error';
      text('indexing-title', failed ? 'Indexing could not be completed' : 'Connecting the dots in your code.');
      text('indexing-description', failed ? repo.error || 'The analyzer could not index this repository. Remove it and re-import to try again.' : 'Reading Git history and calculating metrics. This page updates automatically.');
      setMarkup('indexing-symbol', failed ? icon('info') : '<span class="spinner" aria-hidden="true"></span>');
      const progress = $('indexing-progress');
      progress.hidden = failed;
      if (numeric(repo.progress)) progress.value = clamp(repo.progress); else progress.removeAttribute('value');
      text('indexing-percent', !failed && numeric(repo.progress) ? `${fmt(clamp(repo.progress))}%` : '');
    }
    $('filter-fields').disabled = !ready() || !state.meta || state.metaLoading;
    $('export-button').disabled = !ready() || !state.metrics || state.metricsLoading || state.exporting;
    $('merge-open').disabled = !ready() || array(state.meta?.authors).length < 2 || state.metaLoading;
    renderScope();
  }
  function selectRepo(id, force = false) {
    if (id === state.id && !force) { closeSidebar(); return; }
    state.epoch++;
    ['meta', 'metrics', 'commits', 'detail'].forEach(cancelRequest);
    for (const dialog of ['commit-dialog', 'timeline-dialog']) if ($(dialog).open) $(dialog).close();
    state.id = id;
    if (id !== null && !contexts.has(id)) contexts.set(id, makeContext());
    state.context = id === null ? makeContext() : contexts.get(id);
    state.meta = null; state.metrics = null; state.commits = null;
    state.metaLoading = false; state.metricsLoading = false; state.commitsLoading = false; state.dataFailed = false;
    state.page = 1; state.query = '';
    text('live-status', '');
    $('commit-search').value = ''; $('file-search').value = '';
    $('data-error').hidden = true; $('commits-error').hidden = true;
    storage.set(id);
    syncFilterInputs(); renderRepositories(); renderRepositoryState(); renderMetrics(); renderCommits();
    closeSidebar();
    if (ready()) loadMeta();
  }
  async function loadMeta() {
    if (!ready()) return;
    const epoch = state.epoch, version = ++state.metaVersion;
    state.metaLoading = true; state.dataFailed = false;
    state.meta = null; state.metrics = null; state.commits = null;
    state.metricVersion++; state.commitVersion++;
    cancelRequest('metrics'); cancelRequest('commits');
    state.metricsLoading = false; state.commitsLoading = false;
    $('data-error').hidden = true;
    renderRepositoryState(); renderMetrics(); renderCommits();
    try {
      const meta = await request(repoURL('/meta'), {}, 'meta');
      if (epoch !== state.epoch || version !== state.metaVersion) return;
      if (!Array.isArray(meta.authors) || !Array.isArray(meta.paths)) throw new Error('Repository metadata is missing authors or paths.');
      state.meta = meta;
      if (state.context.filters.author && !meta.authors.some(author => String(author.id) === state.context.filters.author)) state.context.filters.author = '';
      if (meta.repo) Object.assign(currentRepo(), meta.repo);
      populateFilters();
    } catch (error) {
      if (epoch !== state.epoch || version !== state.metaVersion || error.name === 'AbortError') return;
      state.dataFailed = true;
      showError('data-error', error.message);
    } finally {
      if (epoch === state.epoch && version === state.metaVersion) {
        state.metaLoading = false; renderRepositoryState(); renderMetrics(); renderCommits();
        if (state.meta) { loadMetrics(); if (state.tab === 'commits') loadCommits(); }
      }
    }
  }
  async function loadMetrics() {
    if (!ready() || !state.meta) return;
    const epoch = state.epoch, version = ++state.metricVersion;
    state.metricsLoading = true; state.metrics = null; state.dataFailed = false;
    $('data-error').hidden = true;
    renderRepositoryState(); renderMetrics();
    try {
      const metrics = await request(`${repoURL('/metrics')}?${queryParams()}`, {}, 'metrics', 60000);
      if (epoch !== state.epoch || version !== state.metricVersion) return;
      if (!metrics.summary || !Array.isArray(metrics.files) || !Array.isArray(metrics.authors) || !Array.isArray(metrics.directories) || !Array.isArray(metrics.timeline)) throw new Error('The metrics response is incomplete.');
      state.metrics = metrics;
      announce(`Analysis updated. ${fmt(metrics.summary.commit_count)} commits in the current scope.`);
    } catch (error) {
      if (epoch !== state.epoch || version !== state.metricVersion || error.name === 'AbortError') return;
      state.dataFailed = true;
      showError('data-error', error.message);
    } finally {
      if (epoch === state.epoch && version === state.metricVersion) { state.metricsLoading = false; renderMetrics(); renderRepositoryState(); }
    }
  }
  function populateFilters() {
    $('filter-author').innerHTML = '<option value="">All authors</option>' + array(state.meta?.authors).map(author => `<option value="${esc(author.id)}">${esc(author.name || author.email || 'Unnamed author')}${author.email ? ` · ${esc(author.email)}` : ''}</option>`).join('');
    syncFilterInputs();
  }
  function populatePaths() {
    const query = $('filter-path').value.toLocaleLowerCase();
    $('path-options').innerHTML = array(state.meta?.paths).filter(item => item.kind === $('filter-kind').value && item.path.toLocaleLowerCase().includes(query)).slice(0, 500).map(item => `<option value="${esc(item.path)}"></option>`).join('');
  }
  function syncFilterInputs() {
    const filters = state.context.filters;
    $('filter-author').value = filters.author;
    $('filter-path').value = filters.path;
    $('filter-kind').value = filters.kind;
    $('filter-since').value = localDate(filters.since);
    $('filter-until').value = localDate(filters.until);
    $('filter-until').setCustomValidity(''); $('filter-path').setCustomValidity('');
    text('date-label', filters.since !== '' || filters.until !== '' ? 'Custom dates' : 'All time');
    populatePaths();
  }
  function readFilters() {
    const filters = { ...state.context.filters, author: $('filter-author').value, path: $('filter-path').value, kind: $('filter-kind').value };
    $('filter-path').setCustomValidity(filters.kind === 'file' && !filters.path ? 'Choose or enter a file path.' : '');
    for (const key of ['since', 'until']) {
      const input = $(`filter-${key}`);
      filters[key] = input.value ? Math.floor(new Date(input.value).getTime() / 1000) : '';
    }
    const invalidDates = [filters.since, filters.until].some(value => value !== '' && !Number.isFinite(value));
    const reversed = filters.since !== '' && filters.until !== '' && filters.since >= filters.until;
    $('filter-until').setCustomValidity(invalidDates ? 'Enter valid dates.' : reversed ? 'End must be after start. The end instant is excluded.' : '');
    if (invalidDates || reversed) { $('date-filters').hidden = false; $('date-toggle').setAttribute('aria-expanded', 'true'); }
    if (!$('filter-form').reportValidity()) return null;
    return filters;
  }
  function applyFilters(filters) {
    state.context.filters = filters;
    state.filePage = 1; state.authorPage = 1;
    state.commits = null; state.page = 1;
    cancelRequest('commits'); state.commitVersion++; state.commitsLoading = false;
    syncFilterInputs(); renderScope(); renderCommits(); loadMetrics();
    if (state.tab === 'commits') loadCommits();
  }
  function renderScope() {
    const filters = state.context.filters;
    const chips = [];
    const add = (key, value) => chips.push(`<span class="filter-chip">${esc(value)}<button type="button" data-remove-filter="${key}" aria-label="Remove ${esc(key)} filter">${icon('close')}</button></span>`);
    if (filters.author) add('author', array(state.meta?.authors).find(author => String(author.id) === filters.author)?.name || filters.author);
    if (filters.path || filters.kind === 'file') add('path', `${filters.kind === 'file' ? 'File' : 'Directory'}: ${filters.path || '/'}`);
    if (filters.since !== '') add('since', `From ${dateText(filters.since, true)}`);
    if (filters.until !== '') add('until', `Before ${dateText(filters.until, true)} (exclusive)`);
    if (filters.commits.length) add('commits', `${fmt(filters.commits.length)} selected commits`);
    setMarkup('active-filters', chips.length ? chips.join('') : '<span class="scope-dot"></span><span>All history. The whole picture.</span>');
    text('scope-status', state.metaLoading || state.metricsLoading ? 'Updating analysis…' : state.dataFailed ? 'Analysis unavailable' : state.metrics ? `${fmt(state.metrics.summary.commit_count)} of ${fmt(state.metrics.total_commits ?? state.meta?.commit_count)} commits in scope` : ready() ? 'Ready to analyze' : currentRepo() ? 'Waiting for analysis' : 'No repository selected');
  }
  function emptyState(type, title, description) {
    return `<div class="empty-state"><span class="empty-icon">${icon(type)}</span><strong>${esc(title)}</strong><p>${esc(description)}</p></div>`;
  }
  function emptyMessage(type = 'activity') {
    if (state.metaLoading || state.metricsLoading) return `<div class="empty-state"><span class="spinner" aria-hidden="true"></span><strong>Finding the patterns…</strong><p>Loading your repository’s analysis.</p></div>`;
    if (state.dataFailed) return emptyState('info', 'Analysis is unavailable', 'Use Retry above to reconnect with your data.');
    if (ready()) return emptyState(type, 'No matching activity', 'Try a wider date range or reset your filters.');
    if (currentRepo()) return emptyState(type, currentRepo().status === 'error' ? 'Waiting for a successful import' : 'Your insights are on the way', 'Metrics appear once indexing is complete.');
    return emptyState(type, type === 'users' ? 'Great work starts with people' : type === 'file' ? 'Find the heart of your codebase' : 'Your history will take shape here', type === 'users' ? 'Discover the contributors behind every change.' : type === 'file' ? 'See which files see the most change.' : 'Import a repository to see the patterns in your code.');
  }
  function renderMetrics() {
    const summary = state.metrics?.summary;
    const busy = state.metricsLoading || state.metaLoading;
    $('analysis-content').classList.toggle('loading', busy);
    $('analysis-content').setAttribute('aria-busy', String(busy));
    const cards = [
      ['added', 'Lines added', 'plus', 'New lines introduced'], ['removed', 'Lines removed', 'down', 'Lines taken out'],
      ['growth', 'Net growth', 'up', 'Added minus removed'], ['churn', 'Total churn', 'activity', 'Every line of change']
    ];
    $('primary-metrics').innerHTML = cards.map(([key, label, symbol, description]) => `<article class="metric-card ${key}"><div class="metric-top">${label}<span class="metric-icon">${icon(symbol)}</span></div><div class="metric-value${!summary ? ' no-data' : ''}" title="${esc(fmt(summary?.[key]))}">${Math.abs(number(summary?.[key])) >= 10000000 ? compact(number(summary[key])) : fmt(summary?.[key])}</div><div class="metric-description">${icon('info')}${description}</div></article>`).join('');
    const secondary = [['modifications', 'Modifications'], ['frequency', 'Frequency'], ['churn_rate', 'Churn rate'], ['commit_count', 'Commits'], ['file_count', 'Files'], ['author_count', 'Authors']];
    $('secondary-metrics').innerHTML = secondary.map(([key, label]) => `<div class="secondary-metric"><span>${label}</span><strong>${fmt(summary?.[key])}</strong></div>`).join('');
    text('files-tab-count', fmt(summary?.file_count)); text('authors-tab-count', fmt(summary?.author_count)); text('commits-tab-count', fmt(summary?.commit_count));
    const repo = currentRepo();
    const snapshot = [['repo', 'Repository', repo?.name], ['branch', 'Reference', repo?.reference || (repo ? 'HEAD' : null)], ['commit', 'Indexed commits', state.meta ? fmt(state.meta.commit_count) : null], ['clock', 'First commit', state.meta ? dateText(state.meta.min_timestamp) : null], ['clock', 'Latest commit', state.meta ? dateText(state.meta.max_timestamp) : null], ['branch', 'HEAD', repo?.head ? String(repo.head).slice(0, 12) : null]];
    $('repo-snapshot').innerHTML = snapshot.map(([symbol, label, value]) => `<div class="snapshot-row">${icon(symbol)}<span>${label}</span><strong title="${esc(value || '')}">${esc(value || '—')}</strong></div>`).join('');
    renderTimeline(); renderOwnership(); renderHotspots(); renderFiles(); renderAuthors(); renderScope();
  }
  function renderTimeline() {
    const data = array(state.metrics?.timeline).filter(row => /^\d{4}-\d{2}-\d{2}$/.test(row.date) && Number.isFinite(Date.parse(row.date))).slice().sort((a, b) => a.date.localeCompare(b.date));
    $('timeline-data').disabled = !data.length;
    const churn = state.chart === 'churn';
    const legendSpans = $('timeline-legend').querySelectorAll(':scope > span');
    legendSpans[0].innerHTML = `<i class="legend-dot ${churn ? 'purple' : 'teal'}"></i>${churn ? 'Lines changed' : 'Lines added'}`;
    legendSpans[1].hidden = churn;
    if (!data.length) { $('timeline-chart').innerHTML = `<div class="timeline-empty-grid" aria-hidden="true"><span></span><span></span><span></span><span></span></div>${emptyMessage('activity')}`; return; }
    const width = Math.max(280, $('timeline-chart').clientWidth || 600), height = 218, left = 43, right = width - 12, top = 13, bottom = height - 30;
    const minDate = Date.parse(data[0].date), maxDate = Date.parse(data[data.length - 1].date);
    const series = churn ? [['churn', '#8c85af']] : [['added', '#37987d'], ['removed', '#cd8d7a']];
    const maximum = data.reduce((max, row) => Math.max(max, ...series.map(([key]) => Math.max(0, number(row[key])))), 1);
    const ceiling = Math.ceil(maximum / (10 ** Math.floor(Math.log10(maximum)))) * (10 ** Math.floor(Math.log10(maximum)));
    const x = row => minDate === maxDate ? (left + right) / 2 : left + ((Date.parse(row.date) - minDate) / (maxDate - minDate)) * (right - left);
    const y = value => bottom - Math.max(0, number(value)) / ceiling * (bottom - top);
    let svg = `<svg class="timeline-svg" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="activity-chart-title activity-chart-description"><title id="activity-chart-title">${churn ? 'Code churn' : 'Lines added and removed'} over time</title><desc id="activity-chart-description">${data.length} daily observations from ${esc(data[0].date)} to ${esc(data[data.length - 1].date)}. Use View data for exact values.</desc>`;
    for (let i = 0; i < 5; i++) { const value = ceiling * i / 4; svg += `<line class="chart-grid" x1="${left}" y1="${y(value)}" x2="${right}" y2="${y(value)}"/><text class="chart-label" x="${left - 10}" y="${y(value) + 3}" text-anchor="end">${esc(compact(value))}</text>`; }
    for (const [key, color] of series) {
      const points = data.map(row => `${x(row).toFixed(2)},${y(row[key]).toFixed(2)}`).join(' ');
      svg += `<polygon class="chart-area" fill="${color}" points="${x(data[0])},${bottom} ${points} ${x(data[data.length - 1])},${bottom}"/><polyline class="chart-line" stroke="${color}" points="${points}"/>`;
      if (data.length <= 180) svg += data.map(row => `<circle cx="${x(row)}" cy="${y(row[key])}" r="${data.length > 60 ? 2 : 3}" fill="${color}" stroke="white" stroke-width="1"><title>${esc(row.date)} · ${key}: ${esc(fmt(row[key]))} · ${esc(fmt(row.commits))} commits</title></circle>`).join('');
    }
    const labels = new Set([0, Math.floor((data.length - 1) / 3), Math.floor((data.length - 1) * 2 / 3), data.length - 1]);
    for (const index of labels) { const row = data[index]; const label = new Date(`${row.date}T12:00:00Z`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }); svg += `<text class="chart-label" x="${x(row)}" y="${height - 7}" text-anchor="${index === 0 && data.length > 1 ? 'start' : index === data.length - 1 && data.length > 1 ? 'end' : 'middle'}">${esc(label)}</text>`; }
    $('timeline-chart').innerHTML = `${svg}</svg>`;
  }
  function renderOwnership() {
    const authors = array(state.metrics?.authors).slice().sort((a, b) => number(b.ownership) - number(a.ownership)).slice(0, 5);
    $('ownership-chart').innerHTML = authors.length ? authors.map((author, index) => `<div class="bar-row"><span class="avatar tone-${index % 5}" aria-hidden="true">${esc(initials(author.name))}</span><div><span class="bar-label" title="${esc(author.name)}">${esc(author.name || author.email || 'Unnamed author')}</span><div class="bar-track" aria-hidden="true"><div class="bar-fill" style="width:${clamp(number(author.ownership) * 100)}%"></div></div></div><span class="bar-value">${percent(number(author.ownership) * 100)}</span></div>`).join('') : emptyMessage('users');
  }
  function renderHotspots() {
    const files = array(state.metrics?.files).slice().sort((a, b) => number(b.churn) - number(a.churn)).slice(0, 5);
    const max = Math.max(1, ...files.map(file => number(file.churn)));
    $('hotspot-chart').innerHTML = files.length ? files.map(file => `<button class="hotspot-row" data-scope-path="${esc(file.path)}" data-scope-kind="file" title="Analyze ${esc(file.path)}"><span class="hotspot-top">${icon('file')}<span class="bar-label">${esc(file.path)}</span><span class="bar-value">${fmt(file.churn)} lines</span></span><span class="bar-track" aria-hidden="true" style="display:block"><span class="bar-fill" style="display:block;width:${clamp(number(file.churn) / max * 100)}%"></span></span></button>`).join('') : emptyMessage('file');
  }
  const fileColumns = [['path', 'Path'], ['added', 'Added'], ['removed', 'Removed'], ['growth', 'Growth'], ['churn', 'Churn'], ['modifications', 'Modifications'], ['frequency', 'Frequency'], ['churn_rate', 'Churn rate']];
  const authorColumns = [['name', 'Author'], ['added', 'Added'], ['removed', 'Removed'], ['growth', 'Growth'], ['churn', 'Churn'], ['modifications', 'Modifications'], ['ownership', 'Ownership'], ['commit_count', 'Commits']];
  function tableHead(columns, sort, table) {
    return `<tr>${columns.map(([key, label], index) => `<th scope="col" class="${index ? 'number' : ''}" aria-sort="${sort.key === key ? sort.direction === 1 ? 'ascending' : 'descending' : 'none'}"><button data-sort-table="${table}" data-sort-key="${key}">${label}<span class="sort-indicator" aria-hidden="true">${sort.key === key ? sort.direction === 1 ? '↑' : '↓' : '↕'}</span></button></th>`).join('')}</tr>`;
  }
  function sorted(rows, sort) {
    return rows.slice().sort((a, b) => sort.direction * (['path', 'name'].includes(sort.key) ? String(a[sort.key] ?? '').localeCompare(String(b[sort.key] ?? ''), 'en', { numeric: true }) : number(a[sort.key]) - number(b[sort.key])));
  }
  function metricCell(key, value) { return `<td class="number ${key === 'added' ? 'positive' : key === 'removed' || (key === 'growth' && number(value) < 0) ? 'negative' : ''}">${fmt(value)}</td>`; }
  function pagedRows(rows, kind) {
    const key = kind === 'file' ? 'filePage' : 'authorPage';
    const last = Math.max(1, Math.ceil(rows.length / 100));
    state[key] = Math.min(Math.max(1, state[key]), last);
    const offset = (state[key] - 1) * 100;
    text(`${kind}-page-label`, rows.length ? `${fmt(offset + 1)}–${fmt(Math.min(offset + 100, rows.length))} of ${fmt(rows.length)} · Page ${state[key]} of ${last}` : 'No results');
    $(`${kind}-prev`).disabled = state[key] <= 1;
    $(`${kind}-next`).disabled = state[key] >= last;
    return rows.slice(offset, offset + 100);
  }
  function renderFiles() {
    $('file-table-head').innerHTML = tableHead(fileColumns, state.fileSort, 'files');
    const search = $('file-search').value.toLocaleLowerCase();
    const rows = sorted(array(state.metrics?.[state.fileKind]).filter(row => String(row.path).toLocaleLowerCase().includes(search)), state.fileSort);
    const kind = state.fileKind === 'files' ? 'file' : 'directory';
    text('file-result-count', `${fmt(rows.length)} ${state.fileKind}`);
    const visible = pagedRows(rows, 'file');
    $('file-table-body').innerHTML = rows.length ? visible.map(row => `<tr><td><button class="path-button mono" data-scope-path="${esc(row.path)}" data-scope-kind="${kind}">${icon(kind === 'file' ? 'file' : 'folder')}${esc(row.path || '/ (repository root)')}</button></td>${fileColumns.slice(1).map(([key]) => metricCell(key, row[key])).join('')}</tr>`).join('') : `<tr class="table-empty"><td colspan="8">${search && state.metrics ? emptyState('search', 'No matching paths', 'Try another search. This only searches the current results.') : emptyMessage('file')}</td></tr>`;
  }
  function renderAuthors() {
    $('author-table-head').innerHTML = tableHead(authorColumns, state.authorSort, 'authors');
    const rows = pagedRows(sorted(array(state.metrics?.authors), state.authorSort), 'author');
    const identities = new Map(array(state.meta?.authors).map(author => [String(author.id), author]));
    $('author-table-body').innerHTML = rows.length ? rows.map((author, index) => {
      const aliases = array(identities.get(String(author.id))?.aliases);
      const aliasHTML = aliases.length ? `<details class="aliases"><summary>${aliases.length} ${aliases.length === 1 ? 'alias' : 'aliases'}</summary>${aliases.map(alias => `<div class="alias-item">${esc(alias.name)}<br>${esc(alias.email)}</div>`).join('')}</details>` : '';
      return `<tr><td><div class="author-identity"><span class="avatar tone-${index % 5}" aria-hidden="true">${esc(initials(author.name))}</span><div><div class="author-name">${esc(author.name || 'Unnamed author')}</div><div class="author-email">${esc(author.email)}</div></div></div>${aliasHTML}</td>${authorColumns.slice(1).map(([key]) => key === 'ownership' ? `<td class="number ownership-cell">${percent(number(author.ownership) * 100)}<div class="bar-track" aria-hidden="true"><div class="bar-fill" style="width:${clamp(number(author.ownership) * 100)}%"></div></div></td>` : metricCell(key, author[key])).join('')}</tr>`;
    }).join('') : `<tr class="table-empty"><td colspan="8">${emptyMessage('users')}</td></tr>`;
  }
  async function loadCommits() {
    if (!ready() || !state.meta) return;
    const epoch = state.epoch, version = ++state.commitVersion;
    state.commitsLoading = true; state.commits = null;
    $('commits-error').hidden = true;
    renderCommits();
    const params = queryParams(false);
    params.set('q', state.query); params.set('page', state.page); params.set('per_page', state.perPage);
    try {
      const result = await request(`${repoURL('/commits')}?${params}`, {}, 'commits');
      if (epoch !== state.epoch || version !== state.commitVersion) return;
      if (!Array.isArray(result.commits)) throw new Error('The commits response is missing its commits array.');
      state.commits = result;
      const lastPage = Math.max(1, Math.ceil(number(result.total) / state.perPage));
      if (state.page > lastPage) { state.page = lastPage; loadCommits(); return; }
      announce(`Commit page ${state.page} loaded. ${fmt(result.total)} matching commits.`);
    } catch (error) {
      if (epoch !== state.epoch || version !== state.commitVersion || error.name === 'AbortError') return;
      showError('commits-error', error.message);
    } finally { if (epoch === state.epoch && version === state.commitVersion) { state.commitsLoading = false; renderCommits(); } }
  }
  function renderCommits() {
    const rows = array(state.commits?.commits), total = number(state.commits?.total), selected = state.context.selected;
    text('commit-total', state.commits ? `${fmt(total)} commits` : '— commits');
    $('commit-table-body').innerHTML = state.commitsLoading ? '<tr><td colspan="7"><div class="loading-text"><span class="spinner" aria-hidden="true"></span>Loading commit history…</div></td></tr>' : rows.length ? rows.map(commit => `<tr><td><input type="checkbox" data-select-commit="${esc(commit.hash)}" aria-label="Select commit ${esc(commit.short_hash || commit.hash)}" ${selected.has(String(commit.hash)) ? 'checked' : ''}></td><td><button class="commit-button" data-commit-detail="${esc(commit.hash)}"><span class="commit-subject">${esc(commit.subject || '(no subject)')}</span><span class="commit-hash">${esc(commit.short_hash || String(commit.hash).slice(0, 8))}</span></button></td><td><span class="author-name">${esc(commit.author_name || 'Unnamed author')}</span><div class="author-email">${esc(commit.author_email)}</div></td><td class="commit-date" title="${esc(dateText(commit.timestamp, true))}">${esc(dateText(commit.timestamp))}</td>${metricCell('added', commit.added)}${metricCell('removed', commit.removed)}${metricCell('churn', commit.churn)}</tr>`).join('') : `<tr class="table-empty"><td colspan="7">${!$('commits-error').hidden ? emptyState('info', 'Could not load commits', 'Retry to reconnect with the repository history.') : ready() && state.commits ? emptyState('search', 'No commits found', 'Try another search or reset the analysis filters.') : emptyMessage('commit')}</td></tr>`;
    text('page-description-text', total ? `${fmt((state.page - 1) * state.perPage + 1)}–${fmt(Math.min(state.page * state.perPage, total))} of ${fmt(total)} commits` : state.commitsLoading ? 'Loading…' : 'No commits to show');
    text('page-number', `Page ${state.page}${total ? ` of ${Math.ceil(total / state.perPage)}` : ''}`);
    $('previous-page').disabled = state.commitsLoading || state.page <= 1;
    $('next-page').disabled = state.commitsLoading || !total || state.page * state.perPage >= total;
    $('commit-search').disabled = !ready(); $('per-page').disabled = !ready();
    updateSelection();
  }
  function updateSelection() {
    const selected = state.context.selected, rows = array(state.commits?.commits);
    const pageSelected = rows.filter(commit => selected.has(String(commit.hash))).length;
    $('select-page').disabled = !rows.length || state.commitsLoading;
    $('select-page').checked = rows.length > 0 && pageSelected === rows.length;
    $('select-page').indeterminate = pageSelected > 0 && pageSelected < rows.length;
    text('selection-count', selected.size ? `${fmt(selected.size)} selected${state.context.filters.commits.length ? ` · ${fmt(state.context.filters.commits.length)} applied` : ''}` : 'No commits selected');
    $('apply-selection').disabled = !selected.size || !ready() || !state.meta;
    $('clear-selection').disabled = !selected.size && !state.context.filters.commits.length;
    all('[data-select-commit]').forEach(input => { input.checked = selected.has(input.dataset.selectCommit); });
  }
  function setTab(tab, focus = false) {
    if (!['overview', 'files', 'authors', 'commits'].includes(tab)) return;
    state.tab = tab;
    all('[data-tab]').forEach(button => { const selected = button.dataset.tab === tab; button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1; $(`panel-${button.dataset.tab}`).hidden = !selected; });
    text('breadcrumb-current', ({ overview: 'Overview', files: 'Files & directories', authors: 'Authors', commits: 'Commits' })[tab]);
    if (focus) $(`tab-${tab}`).focus();
    if (tab === 'commits' && !state.commits && !state.commitsLoading) loadCommits();
    if (tab === 'overview') renderTimeline();
  }
  function showDialog(id) {
    const dialog = $(id);
    dialog.showModal();
    document.body.classList.add('modal-open');
  }
  function busyDialog(id, busy) {
    const dialog = $(id);
    dialog.dataset.busy = String(busy);
    dialog.setAttribute('aria-busy', String(busy));
    all('button,input,select', dialog).forEach(control => { control.disabled = busy; });
  }
  function setImportMode(mode, clearError = true) {
    state.importMode = mode;
    const zip = mode === 'zip';
    $('clone-fields').hidden = zip; $('zip-fields').hidden = !zip;
    $('clone-url').disabled = zip; $('clone-url').required = !zip;
    $('zip-file').disabled = !zip;
    for (const option of ['clone', 'zip']) { $(`import-${option}-tab`).classList.toggle('selected', option === mode); $(`import-${option}-tab`).setAttribute('aria-pressed', String(option === mode)); }
    if (clearError) $('import-error').hidden = true;
  }
  function chooseFile(file) {
    state.file = file || null;
    text('upload-label', file ? `${file.name} · ${fmt(file.size / 1024 / 1024)} MB` : 'Choose a ZIP or drop it here');
    $('import-error').hidden = true;
  }
  async function importRepository(event) {
    event.preventDefault();
    if ($('import-dialog').dataset.busy === 'true') return;
    const reference = $('import-reference').value.trim() || 'HEAD';
    const zip = state.importMode === 'zip';
    let body;
    if (zip) {
      if (!state.file || !/\.zip$/i.test(state.file.name)) { showError('import-error', 'Choose a ZIP archive to import.'); $('zip-file').focus(); return; }
      body = new FormData(); body.append('file', state.file); body.append('reference', reference);
    } else {
      const url = $('clone-url').value.trim();
      if (!url) { showError('import-error', 'Enter a repository URL.'); $('clone-url').focus(); return; }
      try { const parsed = new URL(url); if (parsed.protocol !== 'https:' || parsed.password || parsed.username) { showError('import-error', 'Use a public HTTPS URL without embedded credentials.'); return; } } catch { showError('import-error', 'Enter a valid public HTTPS repository URL.'); return; }
      body = JSON.stringify({ url, reference });
    }
    $('import-error').hidden = true;
    busyDialog('import-dialog', true);
    $('import-submit').innerHTML = '<span class="spinner small" aria-hidden="true"></span>Starting import…';
    try {
      const repo = await request(`/api/repos/${zip ? 'upload' : 'clone'}`, { method: 'POST', body, ...(zip ? {} : { headers: { 'Content-Type': 'application/json' } }) }, 'import', zip ? 180000 : 60000);
      if (repo.id === undefined || repo.id === null) throw new Error('The import response did not include a repository ID. Refresh the list before retrying.');
      state.listRevision++;
      const normalized = { name: zip ? state.file.name.replace(/\.zip$/i, '') : 'New repository', reference, status: 'indexing', ...repo };
      state.repos = [...state.repos.filter(item => String(item.id) !== String(repo.id)), normalized];
      $('import-dialog').close();
      $('import-form').reset(); chooseFile(null);
      selectRepo(String(repo.id)); setTab('overview');
      toast('Repository added. We’ll keep an eye on indexing for you.');
      refreshRepos();
    } catch (error) { showError('import-error', error.message); }
    finally { busyDialog('import-dialog', false); setImportMode(state.importMode, false); $('import-submit').innerHTML = `${icon('plus')}Import repository`; }
  }
  function renderMergeSources() {
    const target = $('merge-target').value;
    $('merge-sources').innerHTML = state.mergeAuthors.filter(author => String(author.id) !== target).map(author => `<label class="merge-option"><input type="checkbox" name="merge-source" value="${esc(author.id)}"><span><strong>${esc(author.name || 'Unnamed author')}</strong><small>${esc(author.email)}${array(author.aliases).length ? ` · ${author.aliases.length} aliases` : ''}</small></span></label>`).join('');
    $('merge-submit').disabled = true;
  }
  async function mergeAuthors(event) {
    event.preventDefault();
    if ($('merge-dialog').dataset.busy === 'true') return;
    const targetValue = $('merge-target').value;
    const selected = all('input[name="merge-source"]:checked').map(input => input.value);
    if (!selected.length) { showError('merge-error', 'Choose at least one author to merge.'); return; }
    const id = state.mergeId, authors = state.mergeAuthors, target = authors.find(author => String(author.id) === targetValue)?.id;
    const sources = authors.filter(author => selected.includes(String(author.id)) && String(author.id) !== targetValue).map(author => author.id);
    busyDialog('merge-dialog', true); $('merge-error').hidden = true; text('merge-submit', 'Merging…');
    try {
      await request(repoURL('/authors/merge', id), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target, sources }) }, 'merge', 60000);
      const context = contexts.get(id);
      if (context && selected.includes(context.filters.author)) context.filters.author = targetValue;
      $('merge-dialog').close();
      toast('Author identities merged. Refreshing your analysis.');
      if (state.id === id) loadMeta();
    } catch (error) { showError('merge-error', error.message); }
    finally { busyDialog('merge-dialog', false); text('merge-submit', 'Merge authors'); }
  }
  async function openCommit(hash) {
    const version = ++state.detailVersion, epoch = state.epoch;
    if (!$('commit-dialog').open) showDialog('commit-dialog');
    text('commit-detail-title', 'Commit details');
    $('commit-detail-body').innerHTML = '<div class="loading-text"><span class="spinner" aria-hidden="true"></span>Reading this change…</div>';
    try {
      const result = await request(repoURL(`/commits/${encodeURIComponent(hash)}`), {}, 'detail');
      if (version !== state.detailVersion || epoch !== state.epoch || !$('commit-dialog').open) return;
      if (!result.commit || !Array.isArray(result.files)) throw new Error('The commit detail response is incomplete.');
      const commit = result.commit;
      $('commit-detail-body').innerHTML = `<h3 class="detail-subject">${esc(commit.subject || '(no subject)')}</h3><p class="detail-hash mono">${esc(commit.hash || hash)}</p><div class="detail-meta"><span>${esc(commit.author_name || commit.author_email || 'Unknown author')}</span><span>${esc(dateText(commit.timestamp, true))} · committer date</span><span>${fmt(result.files.length)} changed files</span></div><div class="table-scroll" tabindex="0" role="region" aria-label="Changed files"><table><thead><tr><th>Path</th><th class="number">Added</th><th class="number">Removed</th><th class="number">Growth</th><th class="number">Churn</th></tr></thead><tbody>${result.files.length ? result.files.map(file => `<tr><td class="mono">${esc(file.path)}${file.old_path && file.old_path !== file.path ? `<span class="detail-renamed">Renamed from ${esc(file.old_path)}</span>` : ''}</td>${['added', 'removed', 'growth', 'churn'].map(key => metricCell(key, file[key])).join('')}</tr>`).join('') : '<tr><td colspan="5">No file changes were reported for this commit.</td></tr>'}</tbody></table></div>`;
    } catch (error) {
      if (version !== state.detailVersion || error.name === 'AbortError') return;
      $('commit-detail-body').innerHTML = `<div class="notice notice-error" role="alert">${esc(error.message)}</div><button class="button button-secondary" id="retry-detail">Retry</button>`;
      $('retry-detail').addEventListener('click', () => openCommit(hash));
    }
  }
  async function exportCSV() {
    if (!ready() || !state.metrics || state.exporting) return;
    state.exporting = true;
    const name = currentRepo().name || 'repository';
    $('export-button').disabled = true; $('export-button').innerHTML = '<span class="spinner small" aria-hidden="true"></span>Exporting…';
    try {
      const blob = await request(`${repoURL('/export.csv')}?${queryParams()}`, { asBlob: true }, 'export', 60000);
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = `${String(name).replace(/[^a-z0-9._-]/gi, '_')}-files.csv`;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      toast('Your filtered file metrics have been exported.');
    } catch (error) { if (error.name !== 'AbortError') toast(error.message, true); }
    finally { state.exporting = false; $('export-button').innerHTML = `${icon('download')}Export CSV`; renderRepositoryState(); }
  }
  async function deleteRepository() {
    if ($('delete-dialog').dataset.busy === 'true') return;
    const id = state.deleteId;
    busyDialog('delete-dialog', true); $('delete-error').hidden = true; text('confirm-delete', 'Removing…');
    try {
      await request(repoURL('', id), { method: 'DELETE' }, 'delete');
      state.listRevision++;
      contexts.delete(id); state.repos = state.repos.filter(repo => String(repo.id) !== id);
      $('delete-dialog').close();
      if (state.id === id) selectRepo(state.repos.length ? String(state.repos[0].id) : null);
      else { renderRepositories(); renderRepositoryState(); }
      toast('Repository removed from this workspace.');
      refreshRepos();
    } catch (error) { showError('delete-error', error.message); }
    finally { busyDialog('delete-dialog', false); text('confirm-delete', 'Remove repository'); }
  }
  function openSidebar() {
    $('sidebar').classList.add('open'); $('sidebar-scrim').hidden = false;
    $('mobile-menu').setAttribute('aria-expanded', 'true');
    document.querySelector('.app-shell').inert = true;
    document.body.classList.add('modal-open');
    $('nav-overview').focus();
  }
  function closeSidebar() {
    const wasOpen = $('sidebar').classList.contains('open');
    $('sidebar').classList.remove('open'); $('sidebar-scrim').hidden = true;
    $('mobile-menu').setAttribute('aria-expanded', 'false');
    document.querySelector('.app-shell').inert = false;
    if (!document.querySelector('dialog[open]')) document.body.classList.remove('modal-open');
    if (wasOpen) $('mobile-menu').focus();
  }

  document.addEventListener('click', event => {
    const target = event.target.closest('button');
    if (!target) return;
    if (target.hasAttribute('data-open-import')) { closeSidebar(); $('import-error').hidden = true; showDialog('import-dialog'); $('clone-url').disabled ? $('zip-file').focus() : $('clone-url').focus(); }
    if (target.hasAttribute('data-close-dialog')) target.closest('dialog').close();
    if (target.dataset.repo !== undefined) selectRepo(target.dataset.repo);
    if (target.dataset.tab) setTab(target.dataset.tab);
    if (target.dataset.goTab) setTab(target.dataset.goTab, true);
    if (target.dataset.sample) { $('clone-url').value = target.dataset.sample; $('clone-url').focus(); }
    if (target.dataset.scopePath !== undefined) { applyFilters({ ...state.context.filters, path: target.dataset.scopePath, kind: target.dataset.scopeKind }); setTab('files', true); }
    if (target.dataset.removeFilter) {
      const key = target.dataset.removeFilter, filters = { ...state.context.filters };
      if (key === 'commits') { filters.commits = []; state.context.selected.clear(); }
      else if (key === 'path') { filters.path = ''; filters.kind = 'directory'; }
      else filters[key] = '';
      applyFilters(filters);
    }
    if (target.dataset.sortTable) {
      const sort = state[target.dataset.sortTable === 'files' ? 'fileSort' : 'authorSort'];
      sort.direction = sort.key === target.dataset.sortKey ? sort.direction * -1 : ['path', 'name'].includes(target.dataset.sortKey) ? 1 : -1;
      sort.key = target.dataset.sortKey;
      state[target.dataset.sortTable === 'files' ? 'filePage' : 'authorPage'] = 1;
      target.dataset.sortTable === 'files' ? renderFiles() : renderAuthors();
      document.querySelector(`[data-sort-table="${target.dataset.sortTable}"][data-sort-key="${target.dataset.sortKey}"]`)?.focus();
    }
    if (target.dataset.commitDetail) openCommit(target.dataset.commitDetail);
  });
  all('dialog').forEach(dialog => {
    dialog.addEventListener('cancel', event => { if (dialog.dataset.busy === 'true') event.preventDefault(); });
    dialog.addEventListener('close', () => { if (!document.querySelector('dialog[open]')) document.body.classList.remove('modal-open'); if (dialog.id === 'commit-dialog') cancelRequest('detail'); });
    dialog.addEventListener('click', event => { const rect = dialog.getBoundingClientRect(); if (event.target === dialog && dialog.dataset.busy !== 'true' && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close(); });
  });
  $('mobile-menu').addEventListener('click', openSidebar);
  $('sidebar-scrim').addEventListener('click', closeSidebar);
  document.addEventListener('keydown', event => {
    if (!$('sidebar').classList.contains('open')) return;
    if (event.key === 'Escape') closeSidebar();
    if (event.key === 'Tab') {
      const controls = all('a,button,input', $('sidebar')).filter(node => !node.disabled && node.getClientRects().length);
      if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0].focus(); }
    }
  });
  document.querySelector('.analysis-nav').addEventListener('keydown', event => {
    const tabs = all('[data-tab]'), index = tabs.indexOf(document.activeElement);
    if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    setTab(tabs[next].dataset.tab, true);
  });
  $('nav-overview').addEventListener('click', () => { setTab('overview'); closeSidebar(); });
  $('repo-search').addEventListener('input', renderRepositories);
  $('retry-api').addEventListener('click', refreshRepos);
  $('retry-data').addEventListener('click', () => state.meta ? loadMetrics() : loadMeta());
  $('retry-commits').addEventListener('click', loadCommits);
  $('filter-form').addEventListener('submit', event => { event.preventDefault(); const filters = readFilters(); if (filters) applyFilters(filters); });
  $('filter-kind').addEventListener('change', () => { $('filter-path').setCustomValidity(''); populatePaths(); });
  $('filter-path').addEventListener('input', () => { $('filter-path').setCustomValidity(''); populatePaths(); });
  for (const key of ['since', 'until']) $(`filter-${key}`).addEventListener('input', () => $('filter-until').setCustomValidity(''));
  $('date-toggle').addEventListener('click', () => { $('date-filters').hidden = !$('date-filters').hidden; $('date-toggle').setAttribute('aria-expanded', String(!$('date-filters').hidden)); if (!$('date-filters').hidden) $('filter-since').focus(); });
  $('reset-filters').addEventListener('click', () => { state.context.selected.clear(); state.query = ''; $('commit-search').value = ''; $('file-search').value = ''; applyFilters(freshFilters()); announce('All analysis filters and commit selections cleared.'); });
  $('file-search').addEventListener('input', () => { state.filePage = 1; renderFiles(); });
  for (const kind of ['file', 'author']) for (const direction of ['prev', 'next']) $(`${kind}-${direction}`).addEventListener('click', () => { state[kind === 'file' ? 'filePage' : 'authorPage'] += direction === 'next' ? 1 : -1; kind === 'file' ? renderFiles() : renderAuthors(); });
  for (const kind of ['files', 'directories']) $(`show-${kind}`).addEventListener('click', () => { state.fileKind = kind; state.filePage = 1; for (const option of ['files', 'directories']) { $(`show-${option}`).classList.toggle('selected', option === kind); $(`show-${option}`).setAttribute('aria-pressed', String(option === kind)); } renderFiles(); });
  for (const mode of ['changes', 'churn']) $(`chart-${mode}`).addEventListener('click', () => { state.chart = mode; for (const option of ['changes', 'churn']) { $(`chart-${option}`).classList.toggle('selected', option === mode); $(`chart-${option}`).setAttribute('aria-pressed', String(option === mode)); } renderTimeline(); });
  $('timeline-data').addEventListener('click', () => {
    $('timeline-table').innerHTML = `<table><thead><tr><th>Date</th>${['Added', 'Removed', 'Growth', 'Churn', 'Commits'].map(label => `<th class="number">${label}</th>`).join('')}</tr></thead><tbody>${array(state.metrics?.timeline).map(row => `<tr><td>${esc(row.date)}</td>${['added', 'removed', 'growth', 'churn', 'commits'].map(key => metricCell(key, row[key])).join('')}</tr>`).join('')}</tbody></table>`;
    showDialog('timeline-dialog');
  });
  $('commit-search-form').addEventListener('submit', event => { event.preventDefault(); state.query = $('commit-search').value.trim(); state.page = 1; loadCommits(); });
  $('commit-search').addEventListener('search', () => { if (!$('commit-search').value) { state.query = ''; state.page = 1; loadCommits(); } });
  $('per-page').addEventListener('change', () => { state.perPage = Number($('per-page').value); state.page = 1; loadCommits(); });
  $('previous-page').addEventListener('click', () => { if (state.page > 1) { state.page--; loadCommits(); } });
  $('next-page').addEventListener('click', () => { state.page++; loadCommits(); });
  $('commit-table-body').addEventListener('change', event => { const hash = event.target.dataset.selectCommit; if (hash !== undefined) { event.target.checked ? state.context.selected.add(hash) : state.context.selected.delete(hash); updateSelection(); } });
  $('select-page').addEventListener('change', event => { for (const commit of array(state.commits?.commits)) event.target.checked ? state.context.selected.add(String(commit.hash)) : state.context.selected.delete(String(commit.hash)); updateSelection(); });
  $('apply-selection').addEventListener('click', () => { const filters = readFilters(); if (filters) { filters.commits = Array.from(state.context.selected); applyFilters(filters); toast(`${fmt(filters.commits.length)} selected commits applied to analysis.`); } });
  $('clear-selection').addEventListener('click', () => { state.context.selected.clear(); if (state.context.filters.commits.length) applyFilters({ ...state.context.filters, commits: [] }); else updateSelection(); });
  $('import-clone-tab').addEventListener('click', () => setImportMode('clone'));
  $('import-zip-tab').addEventListener('click', () => setImportMode('zip'));
  $('zip-file').addEventListener('change', event => chooseFile(event.target.files[0]));
  for (const type of ['dragenter', 'dragover']) $('upload-zone').addEventListener(type, event => { event.preventDefault(); if ($('import-dialog').dataset.busy !== 'true') $('upload-zone').classList.add('drag-over'); });
  for (const type of ['dragleave', 'drop']) $('upload-zone').addEventListener(type, event => { event.preventDefault(); $('upload-zone').classList.remove('drag-over'); if (type === 'drop' && $('import-dialog').dataset.busy !== 'true') chooseFile(event.dataTransfer.files[0]); });
  $('import-form').addEventListener('submit', importRepository);
  $('merge-open').addEventListener('click', () => { state.mergeId = state.id; state.mergeAuthors = array(state.meta?.authors).slice(); $('merge-target').innerHTML = state.mergeAuthors.map(author => `<option value="${esc(author.id)}">${esc(author.name || 'Unnamed author')} · ${esc(author.email)}</option>`).join(''); $('merge-error').hidden = true; renderMergeSources(); showDialog('merge-dialog'); });
  $('merge-target').addEventListener('change', renderMergeSources);
  $('merge-sources').addEventListener('change', () => { $('merge-submit').disabled = !all('input[name="merge-source"]:checked').length; });
  $('merge-form').addEventListener('submit', mergeAuthors);
  $('export-button').addEventListener('click', exportCSV);
  $('repo-delete').addEventListener('click', () => { state.deleteId = state.id; text('delete-description', `Remove “${currentRepo()?.name || 'this repository'}” and its indexed analysis?`); $('delete-error').hidden = true; showDialog('delete-dialog'); });
  $('confirm-delete').addEventListener('click', deleteRepository);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshRepos(); });
  window.addEventListener('online', refreshRepos);
  let resizeTimer;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (state.tab === 'overview') renderTimeline(); if (innerWidth > 800) closeSidebar(); }, 120); });
  renderMetrics(); renderCommits(); refreshRepos();
})();
