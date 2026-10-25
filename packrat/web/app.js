'use strict';

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const view = $('#view');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? '' : /(s|sh|ch|x)$/.test(word) ? 'es' : 's'}`;
let canReveal = false;
let routeSeq = 0;

async function api(path, opts = {}) {
  if (opts.method && opts.method !== 'GET') forgetLibraryViews();  // an edit may change what the library shows
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return res.json();
}

function fmtSize(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i > 1 ? 1 : 0)} ${units[i]}`;
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 1800);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard API needs https or localhost; fall back for LAN access over http.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  toast('Path copied');
}

async function reveal(params) {
  try {
    await api('/api/reveal?' + new URLSearchParams(params), { method: 'POST' });
  } catch (e) {
    toast(e.message);
  }
}

// ---------------------------------------------------------------- icons

const ICONS = {
  folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2h9A1.5 1.5 0 0 1 21 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"/>',
  audio: '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
  font: '<path d="M4 20 10 4h1l6 16M6.5 14h8"/><path d="M17 20h4"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 17-5-5-9 8"/>',
  source: '<path d="M8 8 4 12l4 4M16 8l4 4-4 4M13 5l-2 14"/>',
  doc: '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5M8 13h8M8 17h6"/>',
  archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4"/>',
  other: '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/>',
};
/** Text with a popup hint on hover or tap; `info` is a small "?" that opens one. */
const tip = (label, text, cls = '') => `<span class="tip ${cls}" tabindex="0" data-tip="${esc(text)}">${label}</span>`;
const info = (text, cls = '') => tip('?', text, `info ${cls}`);
const icon = (name, cls = '') => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ICONS.other}</svg>`;

// ---------------------------------------------------------------- routing

function parseHash() {
  const h = location.hash.slice(1) || '/';
  const [path, qs = ''] = h.split('?');
  return { path, params: new URLSearchParams(qs) };
}

function link(path, params = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null && v !== false)).toString();
  return '#' + path + (qs ? '?' + qs : '');
}

// Going back to the library shouldn't refetch ~800 packs, rebuild every card and lose your place, so the last
// few library views are kept as they were (elements, handlers, loaded covers, scroll position) and put back.
// Any edit or finished background job clears them, so a kept view is never out of date.
const libraryViews = new Map();  // hash -> { nodes, scrollY }
let shownLibrary = null;          // hash of the library view on screen, if any
let lastHash = null;

function forgetLibraryViews() { libraryViews.clear(); }

function keepLibraryView() {
  if (!shownLibrary) return;
  libraryViews.delete(shownLibrary);  // most recent last
  libraryViews.set(shownLibrary, { nodes: [...view.childNodes], scrollY: window.scrollY });
  while (libraryViews.size > 6) libraryViews.delete(libraryViews.keys().next().value);
  shownLibrary = null;
}

async function route() {
  const seq = ++routeSeq;
  const { path, params } = parseHash();
  const hash = location.hash || '#/';
  const navigated = hash !== lastHash;  // false when the same page re-renders, e.g. after a job finishes
  lastHash = hash;
  if (navigated) keepLibraryView();
  else shownLibrary = null;
  const m = path.match(/^\/pack\/(\d+)/);
  if (!path.startsWith('/search')) $('#q').value = '';
  $('#q').placeholder = path.startsWith('/search') && params.get('src') === 'itch'
    ? 'Search itch.io or paste a link' : 'Search packs and files…  ( / )';
  if (!path.startsWith('/guide')) guideObserver?.disconnect();
  const section = ['purchases', 'guide', 'settings'].find((n) => path.startsWith(`/${n}`)) || 'library';
  $$('[data-nav]').forEach((a) => a.classList.toggle('on', a.dataset.nav === section));
  let restored = false;
  try {
    if (path.startsWith('/guide')) await viewGuide(seq, params);
    else if (path.startsWith('/settings')) await viewSettings(seq);
    else if (m) await viewPack(seq, +m[1], params);
    else if (path.startsWith('/search')) await viewSearch(seq, params);
    else if (path.startsWith('/purchases/add')) location.replace(link('/search', { q: params.get('q') || '', src: 'itch' }));
    else if (path.startsWith('/purchases')) await viewPurchases(seq, params);
    else restored = await viewHome(seq, params, hash);
  } catch (e) {
    if (seq === routeSeq) view.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
  // A new page starts at the top (the guide scrolls itself); a restored library view keeps its place.
  if (navigated && !restored && !path.startsWith('/guide') && seq === routeSeq) window.scrollTo(0, 0);
}

// ---------------------------------------------------------------- home

function packCard(p) {
  const counts = [
    p.image_count && plural(p.image_count, 'image'),
    p.audio_count && plural(p.audio_count, 'sound'),
    p.font_count && plural(p.font_count, 'font'),
  ].filter(Boolean).join(' · ') || plural(p.file_count, 'file');
  const fallback = p.audio_count > p.image_count ? 'audio' : p.font_count && !p.image_count ? 'font' : p.kind === 'archive' ? 'archive' : 'folder';
  const cover = p.cover
    ? `<img src="${p.cover}" loading="lazy" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',innerHTML:this.dataset.ph}))" data-ph="${esc(icon(fallback))}">`
    : `<div class="ph">${icon(fallback)}</div>`;
  const ext = p.kind === 'archive' ? p.path.split('.').pop() : '';
  const badges = [
    ext && `<span class="badge">${esc(ext)}</span>`,
    p.dupes ? `<span class="badge" title="Same pack found elsewhere">×${p.dupes + 1}</span>` : '',
    p.hits ? `<span class="badge">${plural(p.hits, 'match')}</span>` : '',
    p.new ? '<span class="badge new">New</span>' : '',
    p.favorite ? '<span class="badge fav">★</span>' : '',
  ].join('');
  return `<a class="card" href="#/pack/${p.id}">
    <div class="thumb checker">${cover}<div class="badges">${badges}</div></div>
    <div class="body">
      <div class="name" title="${esc(p.name)}">${esc(p.name)}</div>
      <div class="sub">${esc(counts)}</div>
      <div class="sub">${esc(p.grp || p.root)} · ${fmtSize(p.size)}</div>
    </div></a>`;
}

/** Renders the library grid, or puts back a kept copy of it. Returns true when it was put back. */
async function viewHome(seq, params, hash) {
  const kept = libraryViews.get(hash);
  if (kept) {
    view.replaceChildren(...kept.nodes);
    window.scrollTo(0, kept.scrollY);
    shownLibrary = hash;
    return true;
  }
  const cur = Object.fromEntries(params);
  const [facets, packs] = await Promise.all([api('/api/facets'), api('/api/packs?' + params)]);
  if (seq !== routeSeq) return false;
  canReveal = facets.can_reveal;
  const t = facets.totals;
  const with_ = (k, v) => link('/', { ...cur, [k]: cur[k] === String(v) ? '' : v });
  const item = (k, v, label, n) => `<a href="${with_(k, v)}" class="${cur[k] === String(v) ? 'on' : ''}"><span>${esc(label)}</span><span class="n">${n ?? ''}</span></a>`;

  if (!t.packs) {
    view.innerHTML = `<div class="empty"><h2>No packs yet</h2><p>Add your asset folders in <a href="#/settings">Settings</a>.</p></div>`;
    return false;
  }

  const title = [cur.new && 'Recently added', cur.root, cur.type, cur.tag && `#${cur.tag}`, cur.fav && 'favorites'].filter(Boolean).join(' · ') || 'All packs';
  view.innerHTML = `<div class="layout">
    <aside class="side">
      <div>
        <h4>Library</h4>
        <a href="#/" class="${!cur.root && !cur.type && !cur.tag && !cur.fav && !cur.new ? 'on' : ''}"><span>All packs</span><span class="n">${t.packs}</span></a>
        ${facets.roots.map((r) => item('root', r.name, r.name, r.count)).join('')}
        ${t.new ? item('new', 1, 'Recently added', t.new) : ''}
        ${item('fav', 1, '★ Favorites', t.favorites || 0)}
        <h4>Contains</h4>
        ${item('type', 'images', 'Images', t.with_images)}
        ${item('type', 'audio', 'Audio', t.with_audio)}
        ${item('type', 'fonts', 'Fonts', t.with_fonts)}
      </div>
      <div>
        <h4>Tags</h4>
        <div class="tags">${facets.tags.map((tg) => item('tag', tg.name, tg.name, tg.count)).join('')}</div>
        <h4>Totals</h4>
        <div class="stats">${plural(t.files, 'file')}<br>${t.images.toLocaleString()} images · ${t.audio.toLocaleString()} sounds<br>${fmtSize(t.size)} on disk
        ${facets.missing ? `<br><a href="${link('/', { missing: 1 })}" style="padding:0">${facets.missing} missing packs</a>` : ''}</div>
      </div>
    </aside>
    <section>
      <div class="toolbar">
        <h2>${esc(title)}</h2><span class="dim">${plural(packs.length, 'pack')}</span>
        <span class="grow"></span>
        <select id="sort" aria-label="Sort">
          ${[['name', 'Name'], ['recent', 'Recently added'], ['size', 'Size'], ['rating', 'Rating']]
            .map(([v, l]) => `<option value="${v}" ${(cur.sort || 'name') === v ? 'selected' : ''}>${l}</option>`).join('')}
        </select>
      </div>
      ${packs.length ? `<div class="grid">${packs.map(packCard).join('')}</div>` : '<div class="empty">No packs match.</div>'}
    </section>
  </div>`;
  $('#sort').onchange = (e) => { location.hash = link('/', { ...cur, sort: e.target.value }); };
  shownLibrary = hash;
  return false;
}

// ---------------------------------------------------------------- file rendering

function fileActions(f) {
  return `<div class="acts">
    <button class="btn small" data-copy="${esc(f.display_path)}" title="${esc(f.display_path)}">Copy path</button>
    ${canReveal ? `<button class="btn small" data-reveal="${f.id}" title="Show in folder">Show</button>` : ''}
    <a class="btn small" href="${f.raw}?download=1" title="Download">⤓</a>
  </div>`;
}

const fontsLoaded = new Set();
function loadFont(f, el) {
  if (!['.ttf', '.otf', '.woff', '.woff2'].includes(f.ext) || !window.FontFace) return;
  const family = `asset-font-${f.id}`;
  const apply = () => { el.style.fontFamily = `"${family}", sans-serif`; };
  if (fontsLoaded.has(family)) return apply();
  new FontFace(family, `url(${f.raw})`).load().then((ff) => {
    document.fonts.add(ff);
    fontsLoaded.add(family);
    apply();
  }).catch(() => { el.textContent = 'No preview'; });
}

function fileRow(f, showPack) {
  const where = showPack ? `${f.pack_name} / ${f.rel}` : f.dir;
  const opener = ['doc', 'image', 'other'].includes(f.kind) || f.ext === '.pdf';
  const name = opener ? `<a href="${f.raw}" target="_blank" rel="noopener">${esc(f.name)}</a>` : esc(f.name);
  let middle = '';
  if (f.kind === 'audio') middle = `<audio controls preload="none" src="${f.raw}"></audio>`;
  if (f.kind === 'font') middle = `<div class="sample" data-font="${f.id}">The quick brown fox 0123</div>`;
  return `<div class="row" data-id="${f.id}">
    ${icon(f.kind, 'ico')}
    <div class="nm"><div title="${esc(f.rel)}">${name}</div>${where ? `<small>${showPack ? `<a href="#/pack/${f.pack_id}">${esc(f.pack_name)}</a> / ${esc(f.rel)}` : esc(where)}</small>` : ''}</div>
    ${middle}
    <span class="sz">${fmtSize(f.size)}</span>
    ${fileActions(f)}
  </div>`;
}

function tile(f, showPack) {
  const sub = showPack ? f.pack_name : '';
  return `<button class="tile" data-tile="${f.id}" title="${esc(f.rel)}">
    <div class="thumb checker">${f.thumb ? `<img src="${f.thumb}" loading="lazy" alt="">` : `<div class="ph">${icon(f.kind)}</div>`}</div>
    <div class="cap">${esc(f.name)}${sub ? `<small>${esc(sub)}</small>` : ''}</div>
  </button>`;
}

/** Renders files into a container with a tile grid (images) and a row list (everything else). */
class FileList {
  constructor(el, { showPack = false } = {}) {
    this.el = el;
    this.showPack = showPack;
    this.images = [];
    el.innerHTML = '<div class="tiles"></div><div class="rows"></div>';
    this.tiles = $('.tiles', el);
    this.rows = $('.rows', el);
    el.addEventListener('click', (e) => {
      const t = e.target.closest('[data-tile]');
      if (t) openLightbox(this.images, this.images.findIndex((f) => f.id === +t.dataset.tile));
    });
    this.sync();
  }

  add(files) {
    const imgs = files.filter((f) => f.thumb);
    const rest = files.filter((f) => !f.thumb);
    this.images.push(...imgs);
    this.tiles.insertAdjacentHTML('beforeend', imgs.map((f) => tile(f, this.showPack)).join(''));
    this.rows.insertAdjacentHTML('beforeend', rest.map((f) => fileRow(f, this.showPack)).join(''));
    for (const f of rest.filter((x) => x.kind === 'font')) {
      const el = $(`[data-font="${f.id}"]`, this.rows);
      if (el) loadFont(f, el);
    }
    this.sync();
  }

  sync() {
    this.tiles.hidden = !this.tiles.children.length;
    this.rows.hidden = !this.rows.children.length;
  }
}

// Shared click handling for copy / reveal buttons anywhere.
document.addEventListener('click', (e) => {
  const c = e.target.closest('[data-copy]');
  if (c) { e.preventDefault(); copyText(c.dataset.copy); return; }
  const r = e.target.closest('[data-reveal]');
  if (r) { e.preventDefault(); reveal({ file_id: r.dataset.reveal }); return; }
  const rp = e.target.closest('[data-reveal-pack]');
  if (rp) { e.preventDefault(); reveal({ pack_id: rp.dataset.revealPack }); }
});

// ---------------------------------------------------------------- pack page

const KIND_TABS = [
  ['', 'All', 'file_count'], ['image', 'Images', 'image_count'], ['audio', 'Audio', 'audio_count'],
  ['font', 'Fonts', 'font_count'], ['source', 'Source', 'source_count'], ['doc', 'Docs', 'doc_count'],
  ['archive', 'Archives', 'archive_count'], ['other', 'Other', 'other_count'],
];

async function viewPack(seq, id, params) {
  const cur = Object.fromEntries(params);
  const dir = cur.dir || '';
  const kind = cur.kind || '';
  const flat = cur.flat === '1';
  if (!canReveal) canReveal = (await api('/api/facets')).can_reveal;
  const [p, listing] = await Promise.all([
    api(`/api/packs/${id}`),
    api(`/api/packs/${id}/files?` + new URLSearchParams({ dir, kind, flat, limit: 300 })),
  ]);
  if (seq !== routeSeq) return;
  // Many zips wrap everything in one top folder; skip straight into it.
  if (!dir && !flat && listing.dirs.length === 1 && !listing.files.length) {
    location.replace(link(`/pack/${id}`, { dir: listing.dirs[0].path, kind }));
    return;
  }
  const here = (extra) => link(`/pack/${id}`, { dir, kind, flat: flat ? 1 : '', ...extra });

  const segs = dir ? dir.split('/') : [];
  const dirCrumbs = [`<a href="${here({ dir: '' })}">${esc(p.name)}</a>`]
    .concat(segs.map((s, i) => `<a href="${here({ dir: segs.slice(0, i + 1).join('/') })}">${esc(s)}</a>`))
    .join(' / ');
  const dirNames = new Set(listing.dirs.map((d) => d.path));
  const files = listing.files.filter((f) => !(f.kind === 'archive' && dirNames.has(f.rel)));

  view.innerHTML = `
    <div class="pack-head">
      <div class="crumbs"><a href="#/">Library</a> / <a href="${link('/', { root: p.root })}">${esc(p.root)}</a>${p.grp ? ` / ${esc(p.grp)}` : ''}</div>
      <h1>${esc(p.name)}${p.missing ? ' <span class="badge">missing</span>' : ''}</h1>
      <div class="pathline">
        <code class="mono">${esc(p.display_path)}</code>
        <button class="btn small" data-copy="${esc(p.display_path)}">Copy path</button>
        ${canReveal ? `<button class="btn small" data-reveal-pack="${p.id}">Show in folder</button>` : ''}
      </div>
    </div>
    <div class="pack-layout">
      <section>
        <nav class="tabs">${KIND_TABS.filter(([, , k]) => p[k]).map(([v, l, k]) =>
          `<a href="${link(`/pack/${id}`, { dir, kind: v, flat: flat ? 1 : '' })}" class="${kind === v ? 'on' : ''}">${l} <span class="n">${p[k].toLocaleString()}</span></a>`).join('')}</nav>
        <div class="dirbar">
          <div class="dirpath grow">${dirCrumbs}</div>
          <a class="btn small" href="${here({ flat: flat ? '' : 1 })}">${flat ? 'Show folders' : 'Show all files'}</a>
        </div>
        ${listing.dirs.length ? `<div class="dirs">${listing.dirs.map((d) =>
          `<a class="dir" href="${here({ dir: d.path })}">${icon(/\.(zip|rar|7z)$/i.test(d.name) ? 'archive' : 'folder')}<span>${esc(d.name)}</span><span class="n">${d.count.toLocaleString()}</span></a>`).join('')}</div>` : ''}
        <div id="files"></div>
        ${!files.length && !listing.dirs.length ? '<div class="empty">No files here.</div>' : ''}
        <div class="more" id="more"></div>
      </section>
      <aside>${packPanel(p)}</aside>
    </div>`;

  const list = new FileList($('#files'));
  list.add(files);
  let offset = listing.files.length;
  const more = $('#more');
  const renderMore = () => {
    more.innerHTML = offset < listing.total ? `<button class="btn">Load more (${(listing.total - offset).toLocaleString()} left)</button>` : '';
  };
  more.onclick = async () => {
    more.innerHTML = '<span class="dim">Loading…</span>';
    const next = await api(`/api/packs/${id}/files?` + new URLSearchParams({ dir, kind, flat, offset, limit: 300 }));
    offset += next.files.length;
    list.add(next.files.filter((f) => !(f.kind === 'archive' && dirNames.has(f.rel))));
    renderMore();
  };
  renderMore();
  bindPackPanel(p);
}

function storePanel(it) {
  return `<div class="panel store-panel">
    <h3>From ${it.store === 'itch' ? 'itch.io' : esc(it.store)}</h3>
    ${it.cover_url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener"><img class="cover" src="${esc(it.cover_url)}" alt="" loading="lazy"></a>` : ''}
    <div class="t"><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a></div>
    <div class="dim" style="font-size:12.5px">by ${it.author_url ? `<a href="${esc(it.author_url)}" target="_blank" rel="noopener">${esc(it.author)}</a>` : esc(it.author)}${it.purchased_at ? ` · bought ${esc(it.purchased_at.slice(0, 10))}` : ''}</div>
    <dl class="kv" style="margin-top:8px">
      <dt>License</dt><dd>${esc(it.license || (it.terms ? 'see terms' : 'not stated'))}</dd>
      ${it.ai ? `<dt>AI</dt><dd>${esc(it.ai)}</dd>` : ''}
    </dl>
    ${it.terms ? `<details><summary>Usage terms from the page</summary><pre>${esc(it.terms)}</pre></details>` : ''}
    ${it.tags.length ? `<div style="margin-top:8px">${it.tags.map((t) => `<a class="chip" href="${link('/search', { q: t })}">${esc(t)}</a>`).join('')}</div>` : ''}
  </div>`;
}

function packPanel(p) {
  const stars = [1, 2, 3, 4, 5].map((n) => `<button type="button" data-star="${n}" class="${n <= p.rating ? 'on' : ''}" aria-label="${n} stars">★</button>`).join('');
  return `
    ${(p.store || []).map(storePanel).join('')}
    <form class="panel" id="meta">
      <h3>Your notes</h3>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div class="stars">${stars}</div>
        <label style="margin:0;display:flex;gap:6px;align-items:center;color:var(--text)"><input type="checkbox" name="favorite" style="width:auto" ${p.favorite ? 'checked' : ''}> Favorite</label>
      </div>
      <label>Store or creator</label><input name="vendor" value="${esc(p.vendor)}" placeholder="e.g. itch.io, LimeZu">
      <label>License</label><input name="license" value="${esc(p.license)}" placeholder="e.g. CC0, commercial OK, no resale">
      <label>Tags (comma-separated)</label><input name="tags" value="${esc(p.tags.join(', '))}" placeholder="topdown, cozy, farm">
      <div style="margin-top:6px">${p.auto_tags.map((t) => `<a class="chip auto" href="${link('/', { tag: t })}" title="Auto-detected">${esc(t)}</a>`).join('')}</div>
      <label>Notes</label><textarea name="notes" placeholder="What it's good for, where you used it">${esc(p.notes)}</textarea>
      <div class="save"><button class="btn primary" type="submit">Save</button><span class="dim" id="meta-status"></span></div>
    </form>
    <div class="panel">
      <h3>Pack</h3>
      <dl class="kv">
        <dt>Type</dt><dd>${p.kind === 'archive' ? 'Archive (not extracted)' : p.kind === 'loose' ? 'Loose files' : 'Folder'}</dd>
        <dt>Size</dt><dd>${fmtSize(p.size)}</dd>
        <dt>Files</dt><dd>${p.file_count.toLocaleString()}</dd>
        <dt>Modified</dt><dd>${p.mtime ? new Date(p.mtime * 1000).toLocaleDateString() : '—'}</dd>
      </dl>
    </div>
    ${p.duplicates.length ? `<div class="panel"><h3>Also found at</h3>${p.duplicates.map((d) =>
      `<a class="duprow" href="#/pack/${d.id}"><div class="t">${esc(d.name)}${d.kind === 'archive' ? ' <span class="dim">(archive)</span>' : ''}</div><small class="dim mono">${esc(d.display_path)}</small></a>`).join('')}</div>` : ''}`;
}

function bindPackPanel(p) {
  const form = $('#meta');
  let rating = p.rating;
  $$('[data-star]', form).forEach((b) => {
    b.onclick = () => {
      const n = +b.dataset.star;
      rating = rating === n ? 0 : n;
      $$('[data-star]', form).forEach((s) => s.classList.toggle('on', +s.dataset.star <= rating));
    };
  });
  form.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    try {
      await api(`/api/packs/${p.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          vendor: fd.get('vendor'), license: fd.get('license'), tags: fd.get('tags'),
          notes: fd.get('notes'), rating, favorite: fd.get('favorite') === 'on',
        }),
      });
      $('#meta-status').textContent = 'Saved';
      setTimeout(() => { const s = $('#meta-status'); if (s) s.textContent = ''; }, 1500);
    } catch (err) {
      $('#meta-status').textContent = err.message;
    }
  };
}

// ---------------------------------------------------------------- purchases

const STATUS = {
  missing: 'Not downloaded', partial: 'Partly downloaded', downloaded: 'In library', 'no-files': 'No files', hidden: 'Hidden',
};
const MATCH_NOTE = { exact: '', size: ' (same size, renamed)', name: ' (matched by name)', title: ' (matched by title)' };

const STORE_NAME = { itch: 'itch.io' };
/** Something a Download of this item would fetch (the server marks each file, see is_pending). */
const hasPending = (it) => it.files.some((f) => f.pending);

function purchaseRow(it) {
  const files = it.files.map((f) => {
    let where;
    if (f.pack_id) {
      where = `<span class="where"><span class="ok">✓</span> <a href="#/pack/${f.pack_id}">${esc(f.pack_name)}</a>${MATCH_NOTE[f.match] || ''}</span>`;
    } else {
      where = `<span class="where">${f.extra ? 'extra, not needed' : '<span class="bad">✗</span> missing'}</span>
         <button class="btn small" data-dl-file="${f.id}">Download</button>`;
    }
    return `<div class="pfile"><span class="fn">${esc(f.filename)}</span><span class="dim">${fmtSize(f.size)}</span>${where}</div>`;
  }).join('');
  const bought = it.purchased_at ? it.purchased_at.slice(0, 10) : '';
  const link_ = it.url;
  const by = it.author ? `by ${it.author_url ? `<a href="${esc(it.author_url)}" target="_blank" rel="noopener">${esc(it.author)}</a>` : esc(it.author)}` : '';
  const meta = [by, bought && `${it.free ? 'added' : 'bought'} ${bought}`, it.license && esc(it.license), it.ai && `AI: ${esc(it.ai)}`]
    .filter(Boolean).join(' · ');
  const actions = [];
  if (it.downloading) actions.push(`<button class="btn small primary" disabled data-progress-item="${it.id}">${it.downloading === 'running' ? 'Downloading…' : 'Queued…'}</button>`);
  else if (hasPending(it)) actions.push(`<button class="btn small primary" data-dl-item="${it.id}">Download</button>`);
  actions.push(`<button class="btn small" data-hide="${it.id}" data-hidden="${it.hidden ? 1 : 0}" title="Hide from these lists">${it.hidden ? 'Unhide' : 'Hide'}</button>`);
  return `<div class="purchase" data-item="${it.id}">
    <a class="pcover" href="${esc(link_)}" target="_blank" rel="noopener">${it.cover_url ? `<img src="${esc(it.cover_url)}" alt="" loading="lazy">` : ''}</a>
    <div class="pbody">
      <div class="ptitle">${link_ ? `<a href="${esc(link_)}" target="_blank" rel="noopener">${esc(it.title)}</a>` : esc(it.title)}<span class="pill st-${it.status}">${STATUS[it.status]}</span><span class="chip">${STORE_NAME[it.store] || esc(it.store)}</span>${it.free ? '<span class="chip" title="Free asset added from itch.io">Free</span>' : ''}</div>
      <div class="pmeta">${meta}</div>
      ${it.tags.length ? `<div>${it.tags.slice(0, 10).map((t) => `<span class="chip">${esc(t)}</span>`).join('')}</div>` : ''}
      <div class="pfiles">${files || '<span class="dim">No files to download.</span>'}</div>
    </div>
    <div class="pacts" style="display:flex;flex-direction:column;gap:6px;align-items:stretch">${actions.join('')}</div>
  </div>`;
}

async function viewPurchases(seq, params) {
  const cur = Object.fromEntries(params);
  const status = cur.status ?? 'missing';
  const query = status === 'hidden' ? { hidden: 1 } : { status: status === 'all' ? '' : status };
  const [summary, items] = await Promise.all([
    api('/api/store/summary'),
    api('/api/store/items?' + new URLSearchParams(query)),
  ]);
  if (seq !== routeSeq) return;
  const c = summary.counts;
  const tabs = ['missing', 'partial', 'downloaded', 'no-files', 'all', 'hidden']
    .map((k) => [k, k === 'all' ? 'All' : k === 'partial' ? 'Partly' : STATUS[k]]);
  const count = (k) => (k === 'all' ? summary.total - (c.hidden || 0) : c[k] || 0);
  const here = (extra) => link('/purchases', { status, ...extra });

  const assetPanel = summary.pending_files ? `<div class="panel" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:220px"><b>${plural(summary.pending_items, 'purchase')}</b> with ${plural(summary.pending_files, 'missing file')} (${fmtSize(summary.pending_bytes)}).
          <div class="dim" style="font-size:12.5px">${summary.download_dir ? `Saves to <code class="mono">${esc(summary.download_dir)}</code>.` : 'Choose a download folder in <a href="#/settings">Settings</a>.'}
          ${summary.sync_every_hours ? `Syncs every ${summary.sync_every_hours} h${summary.auto_download ? ' and downloads new purchases' : ''}.` : ''}
          ${summary.last_sync ? `Last synced ${new Date(summary.last_sync * 1000).toLocaleString()}.` : ''}</div></div>
        <button class="btn primary" id="dl-all">Download all missing</button>
      </div>` : '';

  view.innerHTML = `<div class="layout" style="grid-template-columns:1fr">
    <section>
      <div class="toolbar">
        <h2>Purchases</h2>
        <span class="grow"></span>
        <input id="pfilter" type="search" placeholder="Filter by title, author or tag" style="padding:5px 9px;border-radius:7px;border:1px solid var(--border);background:var(--panel);min-width:220px">
        <select id="psort" aria-label="Sort">
          <option value="recent">Recently bought</option><option value="title">Title</option><option value="author">Author</option>
        </select>
        <a class="btn" href="#/search?src=itch" title="Add free assets from itch.io">Add from itch.io</a>
        <button class="btn" id="sync">Sync itch.io</button>
      </div>
      <div class="dim" id="bulk-bar" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:-4px 0 10px;font-size:12.5px" hidden>
        <span id="shown-count"></span>
        <button class="btn small" id="hide-shown">${status === 'hidden' ? 'Unhide all shown' : 'Hide all shown'}</button>
        <button class="btn small primary" id="get-shown">Download all shown</button>
      </div>
      ${assetPanel}
      ${summary.total ? `<nav class="tabs">${tabs.map(([v, l]) => `<a href="${here({ status: v })}" class="${status === v ? 'on' : ''}">${l} <span class="n">${count(v)}</span></a>`).join('')}</nav>` : ''}
      <div class="plist" id="plist"></div>
    </section></div>`;

  let shown = [];
  const render = () => {
    const f = $('#pfilter').value.trim().toLowerCase();
    const sort = $('#psort').value;
    let list = items.filter((it) => !f || [it.title, it.author, ...it.tags].join(' ').toLowerCase().includes(f));
    if (sort === 'title') list = [...list].sort((a, b) => a.title.localeCompare(b.title));
    if (sort === 'author') list = [...list].sort((a, b) => a.author.localeCompare(b.author) || a.title.localeCompare(b.title));
    shown = list;
    const empty = summary.total ? 'Nothing here.'
      : summary.has_itch ? 'No purchases yet. Press <b>Sync itch.io</b>.'
        : 'Connect itch.io in <a href="#/settings">Settings</a> to see your purchases.';
    $('#plist').innerHTML = list.length ? list.map(purchaseRow).join('') : `<div class="empty">${empty}</div>`;
    // Bulk actions appear once the list is filtered.
    $('#bulk-bar').hidden = !(f && list.length);
    $('#shown-count').textContent = `${plural(list.length, 'purchase')} match`;
    $('#get-shown').hidden = !list.some(hasPending);
  };
  $('#pfilter').oninput = render;
  $('#psort').onchange = render;
  $('#hide-shown').onclick = async () => {
    const hide = status !== 'hidden';
    if (!confirm(`${hide ? 'Hide' : 'Unhide'} ${plural(shown.length, 'purchase')}?`)) return;
    await api('/api/store/hide-many', { method: 'POST', body: JSON.stringify({ item_ids: shown.map((i) => i.id), hidden: hide }) });
    refreshNavCount();
    route();
  };

  const startDownload = async (body, btn) => {
    try {
      if (btn) btn.disabled = true;
      const job = await api('/api/store/download', { method: 'POST', body: JSON.stringify(body) });
      toast(job.queued_behind ? 'Queued after the current download' : 'Download started');
      const row = btn && btn.closest('.purchase');
      if (row && btn.dataset.dlItem) btn.dataset.progressItem = row.dataset.item;
      trackActivity();
    } catch (err) {
      toast(err.message);
      if (btn) btn.disabled = false;
    }
  };
  const dlAll = $('#dl-all');
  if (dlAll) {
    dlAll.onclick = () => {
      if (confirm(`Download ${plural(summary.pending_files, 'file')} (${fmtSize(summary.pending_bytes)}) to ${summary.download_dir}?`)) {
        startDownload({ all_missing: true }, dlAll);
      }
    };
  }
  $('#get-shown').onclick = () => {
    const ids = shown.filter(hasPending).map((i) => i.id);
    if (!ids.length) return;
    if (!confirm(`Download what's missing from ${plural(ids.length, 'purchase')}?`)) return;
    startDownload({ item_ids: ids }, $('#get-shown'));
  };
  $('#plist').onclick = async (e) => {
    const di = e.target.closest('[data-dl-item]');
    if (di) return startDownload({ item_ids: [+di.dataset.dlItem] }, di);
    const df = e.target.closest('[data-dl-file]');
    if (df) return startDownload({ file_ids: [+df.dataset.dlFile] }, df);
    const b = e.target.closest('[data-hide]');
    if (!b) return;
    await api(`/api/store/items/${b.dataset.hide}/hide?hidden=${b.dataset.hidden === '1' ? 'false' : 'true'}`, { method: 'POST' });
    b.closest('.purchase').remove();
    refreshNavCount();
  };
  $('#sync').onclick = async () => {
    await api('/api/store/sync', { method: 'POST' });
    trackActivity();
  };
  render();
  trackActivity();
}

// ---------------------------------------------------------------- add free itch.io assets

const ITCH_PAGE = /^(https?:\/\/)?[a-z0-9][a-z0-9-]*\.itch\.io\/[a-z0-9]/i;

/** One search result or looked-up page: what it is, whether it's already in Purchases, and what can be done. */
function itchResult(r) {
  const lib = r.library && !r.library.hidden ? r.library : null;
  const paid = r.free === false || (r.free === undefined && r.price);
  let state = '';
  const actions = [];
  if (lib) {
    state = `<span class="pill st-${lib.status}">${STATUS[lib.status]}</span>`;
    if (lib.downloading) actions.push(`<button class="btn small primary" disabled data-progress-item="${lib.item_id}">${lib.downloading === 'running' ? 'Downloading…' : 'Queued…'}</button>`);
    else if (lib.pending) actions.push(`<button class="btn small primary" data-dl-item="${lib.item_id}">Download</button>`);
    else if (lib.pack_id) actions.push(`<a class="btn small primary" href="#/pack/${lib.pack_id}">Open pack</a>`);
    actions.push(`<a class="btn small" href="${link('/purchases', { status: 'all' })}">${lib.owned ? 'Purchased' : 'In Purchases'}</a>`);
  } else if (paid) {
    state = `<span class="chip">${esc(r.price || 'Paid')}</span>`;
    actions.push(`<a class="btn small" href="${esc(r.url)}" target="_blank" rel="noopener" title="Buy or claim it on itch.io, then sync">Open on itch.io</a>`);
  } else {
    state = '<span class="chip">Free</span>';
    actions.push(`<button class="btn small primary" data-add-url="${esc(r.url)}" title="Add to Purchases and download">Add &amp; download</button>`);
  }
  const files = (r.files || []).map((f) => `<div class="pfile"><span class="fn">${esc(f.filename)}</span><span class="dim">${fmtSize(f.size)}</span></div>`).join('');
  const by = r.author ? `by ${r.author_url ? `<a href="${esc(r.author_url)}" target="_blank" rel="noopener">${esc(r.author)}</a>` : esc(r.author)}` : '';
  return `<div class="purchase">
    <a class="pcover" href="${esc(r.url)}" target="_blank" rel="noopener">${r.cover_url ? `<img src="${esc(r.cover_url)}" alt="" loading="lazy">` : ''}</a>
    <div class="pbody">
      <div class="ptitle"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>${state}</div>
      <div class="pmeta">${[by, r.short_text && esc(r.short_text)].filter(Boolean).join(' · ')}</div>
      ${files ? `<div class="pfiles">${files}</div>` : ''}
    </div>
    <div class="pacts" style="display:flex;flex-direction:column;gap:6px;align-items:stretch">${actions.join('')}</div>
  </div>`;
}

/** The search page's tabs: the same query in your library or on itch.io. */
function searchTabs(q, src) {
  return `<nav class="tabs src-tabs">
    <a href="${link('/search', { q })}" class="${src ? '' : 'on'}">Your library</a>
    <a href="${link('/search', { q, src: 'itch' })}" class="${src === 'itch' ? 'on' : ''}">itch.io</a>
    <span class="grow"></span>${src === 'itch' ? info('Free: downloads into your library. Paid: opens on itch.io; sync after buying.', 'right') : ''}
  </nav>`;
}

function storedFreeOnly() {
  try { return localStorage.getItem('itch-free-only') !== '0'; } catch { return true; }
}

async function viewItchSearch(seq, q) {
  view.innerHTML = `<div class="layout" style="grid-template-columns:1fr"><section>
      ${searchTabs(q, 'itch')}
      <div class="plist" id="itch-results"><div class="empty">${!q ? 'Search itch.io for assets, or paste a page link.'
        : ITCH_PAGE.test(q) ? 'Looking up the page…' : 'Searching itch.io…'}</div></div>
    </section></div>`;
  if (!q) { $('#q').focus(); return; }

  let results;
  try {
    results = ITCH_PAGE.test(q)
      ? [await api('/api/itch/lookup?' + new URLSearchParams({ url: q }))]
      : await api('/api/itch/search?' + new URLSearchParams({ q }));
  } catch (e) {
    if (seq === routeSeq) $('#itch-results').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  if (seq !== routeSeq) return;
  if (!results.length) {
    $('#itch-results').innerHTML = `<div class="empty">No assets found for “${esc(q)}”.</div>`;
    return;
  }
  // itch.io search can't filter by price, so Free only and Refine filter the results here.
  $('#itch-results').insertAdjacentHTML('beforebegin', `<div class="refine">
      <input id="refine" type="search" placeholder="Refine results" autocomplete="off">
      <label class="check"><input type="checkbox" id="free-only" ${storedFreeOnly() ? 'checked' : ''}> Free only</label>
      <span class="dim" id="refine-count"></span>
    </div>`);
  const isFree = (r) => (r.free !== undefined ? r.free : !r.price);  // search lists no price for free and pay-what-you-want
  const render = () => {
    const words = $('#refine').value.toLowerCase().split(/\s+/).filter(Boolean);
    const freeOnly = $('#free-only').checked;
    const shown = results.filter((r) => (!freeOnly || isFree(r) || r.library)
      && words.every((w) => [r.title, r.author, r.short_text].join(' ').toLowerCase().includes(w)));
    $('#refine-count').textContent = shown.length === results.length ? plural(results.length, 'result') : `${shown.length} of ${results.length}`;
    $('#itch-results').innerHTML = shown.length ? shown.map(itchResult).join('')
      : '<div class="empty">Nothing matches. Clear the filters to see all results.</div>';
    trackActivity();
  };
  $('#refine').oninput = render;
  $('#free-only').onchange = () => {
    try { localStorage.setItem('itch-free-only', $('#free-only').checked ? '1' : '0'); } catch { /* storage unavailable */ }
    render();
  };
  render();

  $('#itch-results').onclick = async (e) => {
    const add = e.target.closest('[data-add-url]');
    const dl = e.target.closest('[data-dl-item]');
    if (!add && !dl) return;
    const btn = add || dl;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = add ? 'Adding…' : 'Starting…';
    try {
      let itemId = dl && +dl.dataset.dlItem;
      if (dl) {
        await api('/api/store/download', { method: 'POST', body: JSON.stringify({ item_ids: [itemId] }) });
      } else {
        const r = await api('/api/itch/add', { method: 'POST', body: JSON.stringify({ url: add.dataset.addUrl }) });
        refreshNavCount();
        if (r.download === 'none') { toast(`Added ${r.title}`); route(); return; }
        itemId = r.item_id;
      }
      btn.dataset.progressItem = itemId;  // trackActivity shows progress here, then re-renders with "Open pack"
      trackActivity();
    } catch (err) {
      toast(err.message);
      btn.disabled = false;
      btn.textContent = label;
    }
  };
}

async function refreshNavCount() {
  try {
    const s = await api('/api/store/summary');
    const n = (s.counts.missing || 0);
    const el = $('#nav-missing');
    el.textContent = n;
    el.hidden = !n;
    el.title = `${n} purchases not downloaded`;
  } catch { /* store not set up */ }
}

// ---------------------------------------------------------------- guide

let guideObserver = null;

/** The sticky header wraps on narrow screens; expose its height so the guide nav can sit below it. */
function measureHeader() {
  document.documentElement.style.setProperty('--header-h', `${$('.top').offsetHeight}px`);
}
window.addEventListener('resize', measureHeader);

function storedOS() {
  try {
    const v = localStorage.getItem('guide-os');
    if (v === 'windows' || v === 'linux') return v;
  } catch { /* storage unavailable */ }
  return /Windows/i.test(navigator.userAgent) ? 'windows' : 'linux';
}

async function viewGuide(seq, params) {
  const html = await (await fetch('guide.html', { cache: 'no-cache' })).text();
  if (seq !== routeSeq) return;
  view.innerHTML = html;
  const layout = $('.guide-layout', view);
  measureHeader();

  // Windows / Linux switch for commands run on your own computer.
  const setOS = (os) => {
    layout.dataset.osShow = os;
    $$('[data-set-os]', layout).forEach((b) => b.classList.toggle('on', b.dataset.setOs === os));
    try { localStorage.setItem('guide-os', os); } catch { /* storage unavailable */ }
  };
  $$('[data-set-os]', layout).forEach((b) => { b.onclick = () => setOS(b.dataset.setOs); });
  setOS(storedOS());

  // Section links (#g-…) scroll within the page and update the address without re-rendering.
  const go = (id, smooth = true) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto' });
    history.replaceState(null, '', link('/guide', { s: id }));
  };
  $$('a[href^="#g-"]', view).forEach((a) => {
    const id = a.getAttribute('href').slice(1);
    a.href = link('/guide', { s: id });
    a.onclick = (e) => { e.preventDefault(); go(id); };
  });

  // Highlight the section being read in the side nav.
  const navLinks = new Map($$('.guide-nav nav a', layout).map((a) => [a.href.split('s=')[1], a]));
  guideObserver?.disconnect();
  guideObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      navLinks.forEach((a, id) => a.classList.toggle('on', id === e.target.id));
      navLinks.get(e.target.id)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }, { rootMargin: '-80px 0px -70% 0px' });
  $$('.guide h2[id]', layout).forEach((h) => guideObserver.observe(h));

  $$('pre', view).forEach((pre) => {
    const b = document.createElement('button');
    b.className = 'btn small copy-code';
    b.textContent = 'Copy';
    b.onclick = () => copyText(pre.querySelector('code')?.innerText ?? pre.innerText);
    pre.append(b);
  });
  const target = params.get('s');
  if (target) go(target, false);
  else window.scrollTo(0, 0);
}

// ---------------------------------------------------------------- search

async function viewSearch(seq, params) {
  const q = params.get('q') || '';
  const kind = params.get('kind') || '';
  $('#q').value = q;
  if (params.get('src') === 'itch') return viewItchSearch(seq, q);
  if (!canReveal) canReveal = (await api('/api/facets')).can_reveal;
  const res = await api('/api/search?' + new URLSearchParams({ q, kind, limit: 200 }));
  if (seq !== routeSeq) return;
  const tabs = [['', 'All'], ['image', 'Images'], ['audio', 'Audio'], ['font', 'Fonts'], ['source', 'Source'], ['doc', 'Docs']];
  view.innerHTML = `<div class="layout" style="grid-template-columns:1fr">
    <section>
      ${searchTabs(q, '')}
      ${res.packs.length ? `<h3 class="section-title">Packs <span class="dim">${res.packs.length}</span></h3>
        <div class="grid compact" id="pack-hits">${res.packs.slice(0, 12).map(packCard).join('')}</div>
        ${res.packs.length > 12 ? `<div class="more"><button class="btn" id="all-packs">Show all ${res.packs.length} packs</button></div>` : ''}` : ''}
      <h3 class="section-title">Files <span class="dim">${res.total.toLocaleString()}</span></h3>
      <nav class="tabs">${tabs.map(([v, l]) => `<a href="${link('/search', { q, kind: v })}" class="${kind === v ? 'on' : ''}">${l}</a>`).join('')}</nav>
      <div id="files"></div>
      ${!res.total ? `<div class="empty">No files match “${esc(q)}”.${res.packs.length ? '' : ` <a href="${link('/search', { q, src: 'itch' })}">Search itch.io</a>`}</div>` : ''}
      <div class="more" id="more"></div>
    </section></div>`;
  const allPacks = $('#all-packs');
  if (allPacks) {
    allPacks.onclick = () => {
      $('#pack-hits').innerHTML = res.packs.map(packCard).join('');
      allPacks.parentElement.remove();
    };
  }
  const list = new FileList($('#files'), { showPack: true });
  list.add(res.files);
  let offset = res.files.length;
  const more = $('#more');
  const renderMore = () => {
    more.innerHTML = offset < res.total ? `<button class="btn">Load more (${(res.total - offset).toLocaleString()} left)</button>` : '';
  };
  more.onclick = async () => {
    const next = await api('/api/search?' + new URLSearchParams({ q, kind, offset, limit: 200 }));
    offset += next.files.length;
    list.add(next.files);
    renderMore();
  };
  renderMore();
}

$('#search').onsubmit = (e) => {
  e.preventDefault();
  const q = $('#q').value.trim();
  const here = parseHash();
  const itch = ITCH_PAGE.test(q) || (here.path.startsWith('/search') && here.params.get('src') === 'itch');
  location.hash = q || itch ? link('/search', { q, src: itch ? 'itch' : '' }) : '#/';
};
document.addEventListener('keydown', (e) => {
  if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
    e.preventDefault();
    $('#q').focus();
  }
});

// ---------------------------------------------------------------- lightbox

const lb = { list: [], i: 0 };

function openLightbox(list, i) {
  if (i < 0) return;
  lb.list = list;
  lb.i = i;
  renderLightbox();
  $('#lightbox').hidden = false;
}

function closeLightbox() {
  $('#lightbox').hidden = true;
  $('#lightbox').innerHTML = '';
}

function renderLightbox() {
  const f = lb.list[lb.i];
  const box = $('#lightbox');
  const src = f.ext === '.psd' ? f.thumb : f.raw;
  box.innerHTML = `
    <div class="stage"><img alt="${esc(f.name)}" src="${src}"></div>
    <div class="info">
      <div class="nm"><div>${esc(f.name)} <span id="lb-dim" style="color:#9a9aa0"></span></div><small>${esc(f.display_path)}</small></div>
      <span style="color:#9a9aa0">${lb.i + 1} / ${lb.list.length}${lb.list.length >= 300 ? '+' : ''}</span>
      <button class="btn small" data-copy="${esc(f.display_path)}">Copy path</button>
      ${canReveal ? `<button class="btn small" data-reveal="${f.id}">Show in folder</button>` : ''}
      <a class="btn small" href="${f.raw}?download=1">Download</a>
      ${f.pack_name ? `<a class="btn small" href="#/pack/${f.pack_id}" data-close>Open pack</a>` : ''}
    </div>
    ${lb.list.length > 1 ? '<button class="nav prev" aria-label="Previous">‹</button><button class="nav next" aria-label="Next">›</button>' : ''}
    <button class="btn close" aria-label="Close">✕</button>`;
  const img = $('img', box);
  img.onload = () => {
    // Scale pixel art up by whole multiples so it stays crisp.
    const stage = $('.stage', box);
    const maxW = stage.clientWidth - 48;
    const maxH = stage.clientHeight - 48;
    const scale = Math.max(1, Math.floor(Math.min(maxW / img.naturalWidth, maxH / img.naturalHeight, 8)));
    img.style.width = `${img.naturalWidth * scale}px`;
    $('#lb-dim').textContent = `${img.naturalWidth}×${img.naturalHeight}${scale > 1 ? ` · ${scale}×` : ''}`;
  };
  box.querySelector('.prev')?.addEventListener('click', () => step(-1));
  box.querySelector('.next')?.addEventListener('click', () => step(1));
  $('.close', box).onclick = closeLightbox;
  box.querySelector('[data-close]')?.addEventListener('click', closeLightbox);
  $('.stage', box).onclick = (e) => { if (e.target.classList.contains('stage')) closeLightbox(); };
}

function step(d) {
  lb.i = (lb.i + d + lb.list.length) % lb.list.length;
  renderLightbox();
}

document.addEventListener('keydown', (e) => {
  if ($('#lightbox').hidden) return;
  if (e.key === 'Escape') closeLightbox();
  if (e.key === 'ArrowLeft') step(-1);
  if (e.key === 'ArrowRight') step(1);
});

// ---------------------------------------------------------------- settings

/** Folder picker. Resolves with the chosen folder's path, or null if cancelled. */
function pickFolder(start) {
  const dlg = $('#picker');
  return new Promise((resolve) => {
    let current = null;
    const show = async (path) => {
      let d;
      try {
        d = await api('/api/browse?' + new URLSearchParams({ path: path || '' }));
      } catch (e) {
        toast(e.message);
        if (path) return show(null);
        return;
      }
      current = d.path;
      dlg.innerHTML = `<form method="dialog" class="picker">
        <h3>Choose a folder</h3>
        <div class="picker-path">
          <button type="button" class="btn small" data-go="${esc(d.parent ?? '')}" ${d.path ? '' : 'disabled'} title="Up one folder">↑</button>
          <input id="picker-input" value="${esc(d.path ?? '')}" placeholder="Paste a path, or pick below" spellcheck="false">
          <button type="button" class="btn small" id="picker-go">Go</button>
        </div>
        <div class="picker-list">${d.dirs.length
          ? d.dirs.map((x) => `<button type="button" data-go="${esc(x.path)}">${icon('folder')}<span>${esc(x.name)}</span></button>`).join('')
          : '<div class="dim" style="padding:12px">No subfolders.</div>'}</div>
        <div class="save">
          <button class="btn primary" value="ok" ${d.path ? '' : 'disabled'}>Use this folder</button>
          <button class="btn" value="cancel">Cancel</button>
        </div>
      </form>`;
      $$('[data-go]', dlg).forEach((b) => { b.onclick = () => show(b.dataset.go || null); });
      $('#picker-go', dlg).onclick = () => show($('#picker-input', dlg).value.trim());
      $('#picker-input', dlg).onkeydown = (e) => {
        if (e.key === 'Enter') { e.preventDefault(); show(e.target.value.trim()); }
      };
    };
    dlg.onclose = () => resolve(dlg.returnValue === 'ok' ? current : null);
    dlg.returnValue = '';
    show(start).then(() => dlg.showModal());
  });
}

async function viewSettings(seq) {
  const s = await api('/api/settings');
  if (seq !== routeSeq) return;
  const roots = s.roots.map((r) => ({ ...r }));
  const it = s.itch;
  const keyBlock = it.has_key
    ? `<p>Connected${it.user ? ` as <b>${esc(it.user)}</b>` : ''}. <button type="button" class="btn small" id="key-remove">Remove key</button></p>`
    : `<p class="dim">Create an <a class="tip" data-tip="itch.io → Settings → API keys" href="https://itch.io/user/settings/api-keys" target="_blank" rel="noopener">API key on itch.io</a>.</p>
      <div class="inline"><input id="key" type="password" autocomplete="off" spellcheck="false" placeholder="API key">
        <button type="button" class="btn primary" id="key-save">Connect</button></div>`;

  view.innerHTML = `<div class="settings">
    <h2>${s.first_run ? 'Welcome to Packrat' : 'Settings'}</h2>
    ${s.first_run ? `<p class="lead">Add your asset folders to start.</p>` : ''}
    <form id="settings-form">
      <section class="panel">
        <h3>Library folders ${info('Each folder or zip/rar inside is one pack.')}</h3>
        <div id="roots" class="roots"></div>
        <button type="button" class="btn" id="root-add">Add folder…</button>
      </section>

      <section class="panel">
        <h3>itch.io${s.first_run ? ' (optional)' : ''}</h3>
        <div id="key-block">${keyBlock}</div>
        <label for="dl">Download folder ${info('One subfolder per purchase. Always in your library.')}</label>
        <div class="inline"><input id="dl" value="${esc(it.download_dir)}" spellcheck="false">
          <button type="button" class="btn" id="dl-pick">Browse…</button></div>
        <label for="hours">Sync automatically</label>
        <div class="inline"><select id="hours">
          ${[[0, 'Off'], [1, 'Every hour'], [6, 'Every 6 hours'], [24, 'Once a day']]
            .map(([v, l]) => `<option value="${v}" ${+it.sync_every_hours === v ? 'selected' : ''}>${l}</option>`).join('')}
          ${[0, 1, 6, 24].includes(+it.sync_every_hours) ? '' : `<option value="${it.sync_every_hours}" selected>Every ${it.sync_every_hours} hours</option>`}
        </select></div>
        <label class="check"><input type="checkbox" id="auto" ${it.auto_download ? 'checked' : ''}> Download new purchases after each sync</label>
      </section>

      <div class="save"><button class="btn primary" id="save">${s.first_run ? 'Save and scan' : 'Save'}</button><span id="save-status" class="dim"></span></div>
    </form>

    <section class="panel about">
      <h3>Files</h3>
      <dl class="kv">
        <dt>Settings file</dt><dd><code>${esc(s.config_path)}</code></dd>
        <dt>Database</dt><dd><code>${esc(s.data_dir)}</code></dd>
        <dt>Archive tools</dt><dd>${s.archive_tools.length ? esc(s.archive_tools.join(', ')) : `None found ${info('Install 7-Zip to read .rar and .7z packs.')}`}</dd>
      </dl>
      ${s.on_network ? '<p class="warn">No login: anyone on your network can change these.</p>' : ''}
    </section>
  </div>`;

  const renderRoots = () => {
    $('#roots').innerHTML = roots.length
      ? roots.map((r, i) => `<div class="root">${icon('folder')}<code>${esc(r.path)}</code>
          <button type="button" class="btn small" data-remove="${i}">Remove</button></div>`).join('')
      : '<p class="warn">No folders yet.</p>';
    $$('[data-remove]').forEach((b) => { b.onclick = () => { roots.splice(+b.dataset.remove, 1); renderRoots(); }; });
  };
  renderRoots();

  $('#root-add').onclick = async () => {
    const path = await pickFolder(roots.at(-1)?.path);
    if (path && !roots.some((r) => r.path === path)) { roots.push({ path, containers: [] }); renderRoots(); }
  };
  $('#dl-pick').onclick = async () => {
    const path = await pickFolder($('#dl').value.trim() || null);
    if (path) $('#dl').value = path;
  };

  const bindKey = () => {
    const save = $('#key-save');
    if (save) {
      save.onclick = async () => {
        save.disabled = true;
        try {
          const r = await api('/api/settings/itch-key', { method: 'POST', body: JSON.stringify({ key: $('#key').value }) });
          toast(`Connected${r.itch.user ? ` as ${r.itch.user}` : ''}. Syncing purchases…`);
          route();
          trackActivity();
        } catch (e) {
          toast(e.message);
          save.disabled = false;
        }
      };
      $('#key').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); save.click(); } };
    }
    const remove = $('#key-remove');
    if (remove) {
      remove.onclick = async () => {
        if (!confirm('Remove your itch.io API key?')) return;
        await api('/api/settings/itch-key', { method: 'DELETE' });
        route();
      };
    }
  };
  bindKey();

  $('#settings-form').onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('#save');
    btn.disabled = true;
    try {
      const r = await api('/api/settings', {
        method: 'PUT',
        body: JSON.stringify({
          roots,
          itch: { download_dir: $('#dl').value.trim() || null, sync_every_hours: +$('#hours').value, auto_download: $('#auto').checked },
        }),
      });
      $('#save-status').textContent = r.rescan ? 'Saved. Scanning…' : 'Saved';
      if (r.rescan) pollJob('scan', $('#scan-status'), $('#rescan'), 'Scan finished');
      refreshNavCount();
      if (s.first_run) location.hash = '#/';
    } catch (err) {
      $('#save-status').textContent = err.message;
    }
    btn.disabled = false;
  };
}

// ---------------------------------------------------------------- background activity

let activityTimer = null;
let activityWas = { download: false, itch: false };

function downloadText(job) {
  const cur = job.current || '';
  if (cur.startsWith('Indexing')) return 'Indexing…';
  const pct = /(\d+)%$/.exec(cur);
  const n = job.total > 1 ? ` ${Math.min(job.done + 1, job.total)}/${job.total}` : '';
  return `Downloading${n}${pct ? ` · ${pct[1]}%` : '…'}`;
}

/** Shows downloads and syncs in the header (and on their buttons) while they run, on any page. */
async function trackActivity() {
  clearTimeout(activityTimer);
  const a = await api('/api/activity').catch(() => null);
  if (!a) return;
  const { download: dl, itch: sync } = a;
  const el = $('#activity');
  el.hidden = !dl.running && !sync.running;
  el.textContent = dl.running ? downloadText(dl) : 'Syncing itch.io…';
  $$('[data-progress-item]').forEach((b) => {
    const st = a.items[b.dataset.progressItem];
    if (st) b.textContent = st === 'running' ? downloadText(dl) : 'Queued…';
  });
  if ($('#dl-all')) $('#dl-all').disabled = dl.running;
  if ($('#sync')) $('#sync').disabled = sync.running;

  if (activityWas.download && !dl.running) {
    const r = dl.result?.itch;
    toast(dl.error ? `Download failed: ${dl.error}` : r?.failed ? `${plural(r.failed, 'download')} failed` : 'Downloads finished');
  }
  if (activityWas.itch && !sync.running) toast(sync.error ? `Sync failed: ${sync.error}` : 'Sync finished');
  const finished = (activityWas.download && !dl.running) || (activityWas.itch && !sync.running);
  activityWas = { download: dl.running, itch: sync.running };
  if (finished) {
    forgetLibraryViews();
    refreshNavCount();
    // Re-render pages that show purchases or packs; not ones with a form you might be editing.
    const { path } = parseHash();
    if (path === '/' || path.startsWith('/purchases') || path.startsWith('/search')) route();
  }
  if (dl.running || sync.running) activityTimer = setTimeout(trackActivity, 1000);
}

// ---------------------------------------------------------------- scanning

const polling = new Set();
const jobUI = {};

/** Shows a background job's progress in `status`, disables `btn` while it runs, re-renders when it finishes. */
async function pollJob(name, statusEl, btnEl, doneLabel, wasRunning = false) {
  if (!wasRunning) jobUI[name] = { status: statusEl, btn: btnEl }; // views re-render; always use the latest elements
  if (polling.has(name) && !wasRunning) return;
  polling.add(name);
  const s = await api(`/api/jobs/${name}`).catch(() => null);
  const { status, btn } = jobUI[name];
  if (s && s.running) {
    btn.disabled = true;
    status.textContent = s.total ? `${s.current} ${s.done}/${s.total}` : s.current || 'Working…';
    setTimeout(() => pollJob(name, status, btn, doneLabel, true), 1500);
    return;
  }
  polling.delete(name);
  btn.disabled = false;
  if (s && s.error) status.textContent = `Failed: ${s.error}`;
  else if (wasRunning && s && s.result) {
    status.textContent = doneLabel;
    forgetLibraryViews();  // a scan or download may have added or changed packs
    refreshNavCount();
    route();
  } else status.textContent = '';
}

$('#rescan').onclick = async () => {
  await api('/api/scan', { method: 'POST' });
  pollJob('scan', $('#scan-status'), $('#rescan'), 'Rescan finished');
};

window.addEventListener('hashchange', route);
// A new install opens on the setup page.
api('/api/settings').then((s) => {
  if (s.first_run && (location.hash || '#/') === '#/') location.hash = '#/settings';
  else route();
}).catch(route);
pollJob('scan', $('#scan-status'), $('#rescan'), 'Rescan finished');
refreshNavCount();
trackActivity();
