/* PodcastAutomation dashboard.
 *
 * Plain ES2020, no build step, no CDN, no framework: the dashboard has to work
 * on a machine with the internet unplugged.
 *
 * XSS: every value that comes from the API is inserted with textContent.
 * There is no innerHTML / outerHTML / insertAdjacentHTML / document.write
 * anywhere in this file, so stored text - a file name, an error from Spotify -
 * can never become markup. tests/test_api.py fails the build if that changes.
 */
'use strict';

const $ = (sel) => document.querySelector(sel);
const TOKEN = (() => {
  const m = location.hash.match(/token=([A-Za-z0-9_-]+)/);
  return m ? m[1] : '';
})();

let state = { jobs: [], shows: [], status: null, token: TOKEN };

async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options);
  opts.headers['X-API-Token'] = state.token || TOKEN;
  if (opts.body) opts.headers['Content-Type'] = 'application/json';
  const res = await fetch('/api' + path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* non-JSON */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function fmtBytes(n) {
  if (!n) return '-';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
}

function fmtTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return d.toLocaleString();
}

const STATE_CLASS = {
  PUBLISHED: 'ok', SCHEDULED: 'ok', ARCHIVED: 'ok',
  FAILED: 'err', NEEDS_USER_ACTION: 'err',
  RETRY_WAIT: 'warn', NEEDS_RECONCILIATION: 'warn',
  UPLOADING: 'info', PROCESSING: 'info', PUBLISHING: 'info',
  QUEUED: 'info', AUTHENTICATING: 'info', READY_TO_PUBLISH: 'info',
  ARCHIVING: 'info', VALIDATING: 'info', DISCOVERED: 'info', VALIDATED: 'info',
  CANCELLED: '',
};

function badgeClass(stateName) { return STATE_CLASS[stateName] || ''; }

/* ------------------------------------------------------------------ cards */

function renderCards(status, jobs) {
  const box = $('#summary-cards');
  box.textContent = '';
  const active = jobs.filter(j => ['QUEUED', 'RETRY_WAIT', 'UPLOADING', 'PROCESSING',
    'PUBLISHING', 'AUTHENTICATING', 'READY_TO_PUBLISH', 'VALIDATING', 'ARCHIVING',
    'NEEDS_RECONCILIATION'].includes(j.state)).length;
  const published = jobs.filter(j => ['PUBLISHED', 'SCHEDULED', 'ARCHIVED'].includes(j.state)).length;
  const failed = jobs.filter(j => ['FAILED', 'NEEDS_USER_ACTION'].includes(j.state)).length;
  const waiting = (status?.watcher?.pending_files) || 0;

  const cards = [
    { n: active, l: 'In progress', c: active ? 'info' : '' },
    { n: published, l: 'Published', c: published ? 'good' : '' },
    { n: failed, l: 'Needs attention', c: failed ? 'err' : '' },
    { n: waiting, l: 'Files being copied', c: waiting ? 'warn' : '' },
  ];
  for (const c of cards) {
    const card = el('div', 'card ' + c.c);
    card.appendChild(el('div', 'n', c.n));
    card.appendChild(el('div', 'l', c.l));
    box.appendChild(card);
  }
}

/* ---------------------------------------------------------------- workers */

/** Render the live state of every worker thread the service is really running. */
function renderWorkers(status) {
  const box = $('#worker-list');
  if (!box) return;
  box.textContent = '';
  const workers = status?.workers || [];
  if (!workers.length) {
    box.appendChild(el('p', 'empty', 'No workers are configured.'));
    return;
  }
  for (const w of workers) {
    const row = el('div', 'worker-row');
    const left = el('div', 'worker-main');

    const name = el('span', 'worker-name', w.id);
    left.appendChild(name);

    const live = w.alive && !w.paused;
    let detail;
    if (!w.alive) {
      detail = 'not running';
    } else if (w.paused) {
      detail = 'paused';
    } else if (w.current_job) {
      detail = (w.current_state || 'working') + ' \u00b7 ' + w.current_job;
    } else {
      detail = 'waiting for work';
    }
    left.appendChild(el('span', 'worker-state', detail));

    const badge = el('span', 'worker-badge ' + (live ? 'good' : 'warn'),
      live ? (w.current_job ? 'working' : 'ready') : 'idle');
    row.appendChild(left);
    row.appendChild(badge);
    box.appendChild(row);
  }
}

/* ------------------------------------------------------------------ shows */

function renderShows(shows) {
  const grid = $('#show-grid');
  grid.textContent = '';
  if (!shows.length) {
    grid.appendChild(el('p', 'empty', 'No shows are configured yet. See config.yaml.'));
    return;
  }
  for (const s of shows) {
    const card = el('div', 'show');
    card.appendChild(el('h3', null, s.name));
    const path = el('div', 'path', s.incoming);
    card.appendChild(path);
    const meta = el('div', 'meta',
      `${s.incoming_files} file(s) waiting · ${s.counts.ARCHIVED || 0} archived · ${fmtBytes(s.archived_bytes)}`);
    card.appendChild(meta);
    if (!s.spotify_show_id) {
      card.appendChild(el('div', 'meta',
        'Tip: set spotify_show_id in config.yaml so the right show is always chosen.'));
    }
    const actions = el('div', 'job-actions');
    const open = el('button', 'btn btn-ghost btn-sm', 'Open folder');
    open.onclick = () => openFolder(s.incoming);
    actions.appendChild(open);
    card.appendChild(actions);
    grid.appendChild(card);
  }
}

/* ------------------------------------------------------------------- jobs */

function renderJobs(jobs) {
  const list = $('#job-list');
  list.textContent = '';
  if (!jobs.length) {
    list.appendChild(el('p', 'empty',
      'Nothing here yet. Drop MyEpisode.mp4 and MyEpisode.txt into a show\u2019s incoming folder.'));
    return;
  }
  for (const j of jobs) list.appendChild(jobCard(j));
}

function jobCard(j) {
  const card = el('div', 'job s-' + j.state);

  const head = el('div', 'job-head');
  const left = el('div');
  left.appendChild(el('div', 'job-title', j.title || '(not validated yet)'));
  const bits = [j.filename];
  if (j.duration) bits.push(j.duration);
  if (j.resolution) bits.push(j.resolution);
  if (j.size) bits.push(fmtBytes(j.size));
  if (j.media_kind && j.media_kind !== 'unknown') bits.push(j.media_kind);
  left.appendChild(el('div', 'job-file', bits.filter(Boolean).join(' · ')));
  head.appendChild(left);

  const right = el('div');
  const badge = el('span', 'badge ' + badgeClass(j.state), j.state_label || j.state);
  right.appendChild(badge);
  head.appendChild(right);
  card.appendChild(head);

  if (j.status_message) card.appendChild(el('div', 'job-msg', j.status_message));
  if (j.state_hint) card.appendChild(el('div', 'job-msg', j.state_hint));
  if (j.last_error && j.state !== 'PUBLISHED' && j.state !== 'ARCHIVED') {
    card.appendChild(el('div', 'job-err', j.last_error));
  }

  const live = ['UPLOADING', 'PROCESSING', 'PUBLISHING', 'AUTHENTICATING', 'READY_TO_PUBLISH'];
  if (live.includes(j.state)) {
    const bar = el('div', 'bar' + ((j.progress || 0) <= 0 ? ' indeterminate' : ''));
    const fill = el('i');
    if ((j.progress || 0) > 0) fill.style.width = Math.min(100, j.progress) + '%';
    bar.appendChild(fill);
    card.appendChild(bar);
  }

  const actions = el('div', 'job-actions');
  if (j.can_retry) actions.appendChild(btn('Retry', 'btn btn-sm', () => act(`/jobs/${j.id}/retry`)));
  if (j.can_cancel) actions.appendChild(btn('Cancel', 'btn btn-ghost btn-sm', () => act(`/jobs/${j.id}/cancel`)));
  if (j.can_archive) actions.appendChild(btn('Archive now', 'btn btn-ghost btn-sm', () => act(`/jobs/${j.id}/archive`)));
  if (j.state === 'FAILED' || j.state === 'NEEDS_USER_ACTION') {
    actions.appendChild(btn('Publish anyway', 'btn btn-ghost btn-sm', () => {
      if (confirm('Publish this episode even though validation reported problems?')) {
        act(`/jobs/${j.id}/force`);
      }
    }));
  }
  if (j.media_path) actions.appendChild(btn('Open folder', 'btn btn-ghost btn-sm', () => openFolder(dirOf(j.media_path))));
  if (j.archived_path) actions.appendChild(btn('Open archive', 'btn btn-ghost btn-sm', () => openFolder(dirOf(j.archived_path))));
  if (j.spotify_url) {
    const link = el('button', 'btn btn-ghost btn-sm', 'Open in Spotify');
    link.onclick = () => api('/open-url?url=' + encodeURIComponent(j.spotify_url)).catch(showError);
    actions.appendChild(link);
  }
  actions.appendChild(btn('Details', 'btn btn-ghost btn-sm', () => showDetails(j.id)));
  card.appendChild(actions);
  return card;
}

function btn(label, cls, onClick) {
  const b = el('button', cls, label);
  b.onclick = () => { b.disabled = true; Promise.resolve(onClick()).finally(() => { b.disabled = false; }); };
  return b;
}

function dirOf(p) {
  const parts = String(p).split(/[\\/]/);
  parts.pop();
  return parts.join('/') || p;
}

async function openFolder(path) {
  try {
    const r = await api('/open-folder?path=' + encodeURIComponent(path));
    if (!r.opened) showError(new Error(r.hint || r.error || 'Could not open the folder'));
  } catch (e) { showError(e); }
}

async function act(path) {
  try {
    await api(path, { method: 'POST' });
    await refresh();
  } catch (e) { showError(e); }
}

/* ---------------------------------------------------------------- details */

async function showDetails(id) {
  const j = await api('/jobs/' + id);
  $('#modal-title').textContent = j.title || j.filename;
  const body = $('#modal-body');
  body.textContent = '';
  const dl = el('dl', 'kv');
  const rows = [
    ['Show', j.show_name], ['File', j.filename], ['State', j.state_label || j.state],
    ['Message', j.status_message], ['Last error', j.last_error],
    ['Season / Episode', (j.season ?? '-') + ' / ' + (j.episode_number ?? '-')],
    ['Explicit', j.explicit ? 'yes' : 'no'],
    ['Chapters', j.chapters],
    ['Publish', j.publish_mode === 'SCHEDULE'
      ? `scheduled ${fmtTime(j.publish_at)} (${j.timezone})` : 'immediately'],
    ['Media', `${j.media_kind} · ${j.duration || '?'} · ${j.resolution || ''}`],
    ['Fingerprint', `${j.content_hash} (media ${j.media_hash})`],
    ['Spotify episode', j.spotify_episode_id],
    ['Spotify URL', j.spotify_url],
    ['Retries', j.retry_count],
    ['Next attempt', j.next_attempt_at ? fmtTime(j.next_attempt_at) : '-'],
    ['Created', fmtTime(j.created_at)], ['Updated', fmtTime(j.updated_at)],
    ['Published', j.published_at ? fmtTime(j.published_at) : '-'],
    ['Archived', j.archived_at ? fmtTime(j.archived_at) : '-'],
  ];
  for (const [k, v] of rows) {
    if (v === null || v === undefined || v === '' || v === '-') continue;
    dl.appendChild(el('dt', null, k));
    dl.appendChild(el('dd', null, v));
  }
  body.appendChild(dl);

  if (j.diagnostics) {
    body.appendChild(el('h3', null, 'Diagnostics'));
    body.appendChild(el('pre', 'log', JSON.stringify(j.diagnostics, null, 2)));
  }
  if (j.events && j.events.length) {
    body.appendChild(el('h3', null, 'History'));
    const pre = el('pre', 'log');
    for (const e of j.events) pre.textContent += `[${e.at}] ${e.state || '-'} ${e.message}\n`;
    body.appendChild(pre);
  }
  $('#modal').hidden = false;
}

/* --------------------------------------------------------------- activity */

function renderActivity(events, logLines) {
  const box = $('#activity');
  box.textContent = '';
  if (!events.length) box.appendChild(el('p', 'empty', 'No activity yet.'));
  for (const e of events.slice(0, 60)) {
    const row = el('div');
    row.appendChild(el('span', 't', e.title || e.show_id || ''));
    row.appendChild(document.createTextNode(' ' + (e.message || '')));
    box.appendChild(row);
  }
  $('#log').textContent = (logLines || []).join('\n');
  const logBox = $('#log');
  logBox.scrollTop = logBox.scrollHeight;
}

/* ------------------------------------------------------------------ top */

function renderTop(status) {
  $('#root-line').textContent = 'Root: ' + (status?.app?.root || '-');
  const pill = $('#spotify-pill');
  const label = $('#spotify-label');
  pill.classList.remove('ok', 'warn', 'err');
  const s = status?.spotify;
  const login = s ? null : null;
  if (s && s.ok) {
    pill.classList.add('ok'); label.textContent = 'Spotify connected';
  } else if (s) {
    pill.classList.add('warn');
    label.textContent = (s.summary || 'Spotify not connected').slice(0, 60);
  }
  const paused = status?.app?.queue_paused;
  $('#btn-pause').textContent = paused ? 'Resume queue' : 'Pause queue';
}

function showError(err) {
  console.error(err);
  $('#modal-title').textContent = 'Something went wrong';
  const body = $('#modal-body');
  body.textContent = '';
  body.appendChild(el('p', 'job-err', err.message || String(err)));
  $('#modal').hidden = false;
}

/* ----------------------------------------------------------------- loop */

let timer = null;

async function refresh() {
  try {
    const [status, jobs, events, logs] = await Promise.all([
      api('/status'),
      api('/jobs?limit=200' + ($('#filter-show').value ? '&show_id=' + encodeURIComponent($('#filter-show').value) : '')
        + ($('#filter-state').value ? '&state=' + encodeURIComponent($('#filter-state').value) : '')),
      api('/events?limit=60'),
      api('/logs?lines=200'),
    ]);
    state.status = status; state.jobs = jobs; state.shows = status.shows || [];
    renderTop(status);
    renderCards(status, state.jobs);
    renderWorkers(status);
    renderShows(state.shows);
    renderJobs(state.jobs);
    renderActivity(events, logs.lines);
    populateShowFilter();
  } catch (e) {
    if (!document.querySelector('#job-list .empty')) showError(e);
  }
}

let showFilterReady = false;
function populateShowFilter() {
  const sel = $('#filter-show');
  if (showFilterReady) return;
  showFilterReady = true;
  for (const s of state.shows) {
    const opt = el('option', null, s.name);
    opt.value = s.id;
    sel.appendChild(opt);
  }
}

/* -------------------------------------------------------------- wiring */

$('#btn-refresh').onclick = refresh;
$('#filter-show').onchange = refresh;
$('#filter-state').onchange = refresh;
$('#modal-close').onclick = () => { $('#modal').hidden = true; };
$('#modal').onclick = (e) => { if (e.target.id === 'modal') $('#modal').hidden = true; };

$('#btn-pause').onclick = async () => {
  const paused = state.status?.app?.queue_paused;
  await act(paused ? '/queue/resume' : '/queue/pause');
};

$('#btn-connect').onclick = async () => {
  try {
    const r = await api('/spotify/connect', { method: 'POST' });
    $('#modal-title').textContent = 'Sign in to Spotify';
    const body = $('#modal-body');
    body.textContent = '';
    body.appendChild(el('p', null, r.message ||
      'A Spotify window will open. Sign in there - this app never sees your password.'));
    const poll = el('p', 'job-msg', 'Waiting for you to sign in…');
    body.appendChild(poll);
    $('#modal').hidden = false;
    const tick = setInterval(async () => {
      try {
        const st = await api('/spotify/status');
        poll.textContent = st.login?.message || 'Waiting…';
        if (st.login?.state && st.login.state !== 'running' && st.login.state !== 'idle') {
          clearInterval(tick);
          await refresh();
        }
      } catch (e) { clearInterval(tick); }
    }, 2000);
  } catch (e) { showError(e); }
};

$('#btn-disconnect').onclick = async () => {
  if (!confirm('Delete the saved Spotify session? You will have to sign in again.')) return;
  try {
    const r = await api('/spotify/disconnect', { method: 'POST' });
    await refresh();
    if (!r.disconnected) showError(new Error(r.message));
  } catch (e) { showError(e); }
};

refresh();
timer = setInterval(refresh, 3000);
