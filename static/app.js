// Three panes, one page: the KB tree (left), a renderer (centre), chat
// (right). State lives on the server: a turn is submitted, then streamed. A
// dropped connection replays from Last-Event-ID rather than losing the turn,
// so closing a laptop mid-answer costs nothing - and a full reload resumes by
// asking for the same turn id again, which replays its whole history because
// a fresh EventSource carries no Last-Event-ID of its own.

// --- elements ----------------------------------------------------------

const layout = document.getElementById('layout');
const navFileList = document.getElementById('file-list');
const content = document.getElementById('content');
const chatLog = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('input');
const send = document.getElementById('send');
const hint = document.getElementById('hint');
const previews = document.getElementById('previews');
const filepicker = document.getElementById('filepicker');

let sessionId = null;
let pendingImages = []; // [{dataUrl, mediaType, base64}]
let pendingFiles = [];  // [{name, size, base64}]

// Mirrors MAX_UPLOAD_BYTES in app/config.py. The server is the authority and
// answers 413; this only exists so the user learns before a 10 MB upload.
const MAX_FILE_BYTES = 10 * 1024 * 1024;

// The turn currently streaming, persisted so a full page reload can resume
// it - localStorage, not a JS variable, because a JS variable dies with the
// tab. Cleared the instant the turn reaches a terminal state.
const ACTIVE_TURN_KEY = 'memory-agent:active-turn';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function humanSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// --- resizable panes ----------------------------------------------------

const TREE_MIN = 160, TREE_MAX = 480, TREE_DEFAULT = 240;
const CHAT_MIN = 320, CHAT_MAX = 720, CHAT_DEFAULT = 420;

function loadWidth(key, fallback, min, max) {
  const raw = Number(localStorage.getItem(key));
  return Number.isFinite(raw) && raw >= min && raw <= max ? raw : fallback;
}

let treeWidth = loadWidth('memory-agent:tree-width', TREE_DEFAULT, TREE_MIN, TREE_MAX);
let chatWidth = loadWidth('memory-agent:chat-width', CHAT_DEFAULT, CHAT_MIN, CHAT_MAX);

function applyPaneWidths() {
  layout.style.setProperty('--tree-w', treeWidth + 'px');
  layout.style.setProperty('--chat-w', chatWidth + 'px');
}
applyPaneWidths();

// `side: 'right'` is the chat gutter: chat sits to the RIGHT of it, so
// dragging right must shrink it rather than grow it, which is the opposite
// sign from the tree gutter's own drag.
function makeResizable(gutter, get, set, min, max, storageKey, side) {
  gutter.addEventListener('pointerdown', (e) => {
    gutter.setPointerCapture(e.pointerId);
    gutter.classList.add('dragging');
    const startX = e.clientX;
    const startWidth = get();

    function onMove(ev) {
      const raw = ev.clientX - startX;
      const delta = side === 'right' ? -raw : raw;
      set(Math.min(max, Math.max(min, startWidth + delta)));
      applyPaneWidths();
    }
    function onUp() {
      gutter.classList.remove('dragging');
      gutter.removeEventListener('pointermove', onMove);
      gutter.removeEventListener('pointerup', onUp);
      localStorage.setItem(storageKey, String(get()));
    }
    gutter.addEventListener('pointermove', onMove);
    gutter.addEventListener('pointerup', onUp);
  });
}

makeResizable(
  document.getElementById('gutter-tree'),
  () => treeWidth, (v) => { treeWidth = v; },
  TREE_MIN, TREE_MAX, 'memory-agent:tree-width',
);
makeResizable(
  document.getElementById('gutter-chat'),
  () => chatWidth, (v) => { chatWidth = v; },
  CHAT_MIN, CHAT_MAX, 'memory-agent:chat-width', 'right',
);

// Auto-scroll only while already at the bottom, so dragging a gutter or
// scrolling up to reread something does not get yanked back down by the next
// streamed token.
let stickToBottom = true;
chatLog.addEventListener('scroll', () => {
  stickToBottom = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 40;
});
function scroll() {
  if (stickToBottom) chatLog.scrollTop = chatLog.scrollHeight;
}

// --- the KB tree (left pane) --------------------------------------------
//
// Carried over from the former standalone /kb page essentially unchanged;
// the one addition is that opening a file now also updates the URL via
// history.pushState instead of only the hash, and a caller can ask for a
// specific path to be selected once the tree finishes loading.

let knownKbFiles = new Set(); // populated by loadFiles, read by linkifyKbPaths

async function loadFiles(openPath) {
  navFileList.innerHTML = '<div class="empty">Loading…</div>';
  let files;
  try {
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 8000);
    const res = await fetch('/api/kb/files', { cache: 'no-store', signal: ctrl.signal });
    clearTimeout(timeout);
    if (!res.ok) { navFileList.innerHTML = '<div class="empty">Not mounted.</div>'; return; }
    ({ files } = await res.json());
  } catch (e) {
    navFileList.innerHTML = '<div class="empty">Failed to load — <a href="#" onclick="loadFiles();return false">retry</a></div>';
    return;
  }
  knownKbFiles = new Set(files);
  if (!files.length) { navFileList.innerHTML = '<div class="empty">Empty.</div>'; return; }

  // Build a recursive tree: { dirs: {name: node}, files: [path] }
  function addToTree(tree, parts, fullPath) {
    if (parts.length === 1) {
      tree.files.push(fullPath);
    } else {
      const name = parts[0];
      if (!tree.dirs[name]) tree.dirs[name] = { dirs: {}, files: [], path: '' };
      tree.dirs[name].path = tree.dirs[name].path || name;
      addToTree(tree.dirs[name], parts.slice(1), fullPath);
    }
  }
  const tree = { dirs: {}, files: [] };
  const fileSet = knownKbFiles;
  files.forEach(f => addToTree(tree, f.split('/'), f));

  // Fix dir paths after tree is built
  function fixPaths(node, prefix) {
    Object.keys(node.dirs).forEach(name => {
      const child = node.dirs[name];
      child.path = prefix ? prefix + '/' + name : name;
      fixPaths(child, child.path);
    });
  }
  fixPaths(tree, '');

  // Un-nest a skill's references/ child into the skill folder itself: the
  // tree still reflects the real filesystem shape underneath, this just
  // changes how a skill directory's node is rendered.
  function hoistReferences(node) {
    Object.values(node.dirs).forEach(hoistReferences);
    if (fileSet.has(node.path + '/SKILL.md') && node.dirs.references) {
      const ref = node.dirs.references;
      node.files.push(...ref.files);
      Object.keys(ref.dirs).forEach(name => { node.dirs[name] = ref.dirs[name]; });
      delete node.dirs.references;
    }
  }
  hoistReferences(tree);

  function renderNode(node, container, depth) {
    Object.keys(node.dirs).sort().forEach(name => {
      const child = node.dirs[name];
      const dirPath = child.path;

      const skillPath = dirPath + '/SKILL.md';
      const guidePath = dirPath + '/GUIDE.md';
      const openPath = fileSet.has(skillPath) ? skillPath : fileSet.has(guidePath) ? guidePath : null;

      if (depth === 0) {
        // Top-level dirs are section headers, not collapsible
        const section = document.createElement('div');
        section.className = 'section-header';
        section.textContent = name;
        if (openPath) section.dataset.path = openPath;
        section.onclick = () => { if (openPath) openKbFile(openPath); };
        container.appendChild(section);
        renderNode(child, container, depth + 1);
      } else {
        const header = document.createElement('div');
        header.className = 'dir-header';
        header.style.paddingLeft = (12 + (depth - 1) * 12) + 'px';
        if (openPath) header.dataset.path = openPath;
        const toggle = document.createElement('span');
        toggle.className = 'dir-toggle';
        toggle.textContent = '▾';
        const label = document.createElement('span');
        label.textContent = name;
        header.appendChild(toggle);
        header.appendChild(label);

        const children = document.createElement('div');
        children.className = 'dir-children';

        header.onclick = () => {
          const collapsed = children.classList.toggle('collapsed');
          toggle.classList.toggle('collapsed', collapsed);
          if (openPath) openKbFile(openPath);
        };

        container.appendChild(header);
        renderNode(child, children, depth + 1);
        container.appendChild(children);
      }
    });

    node.files.sort().forEach(f => {
      const base = f.split('/').pop();
      if (base === 'GUIDE.md' || base === 'SKILL.md') return;
      if (depth === 0 && base === 'AGENT_GUIDE.md') return;
      const name = base.replace(/\.md$/, '');
      const a = document.createElement('a');
      a.textContent = name;
      a.href = '#';
      a.dataset.path = f;
      a.style.paddingLeft = (16 + Math.max(0, depth - 1) * 12) + 'px';
      a.onclick = (e) => { e.preventDefault(); openKbFile(f); };
      container.appendChild(a);
    });
  }

  navFileList.innerHTML = '';
  renderNode(tree, navFileList, 0);

  // An explicit request (a tool_use target, a reply link, a tree click that
  // forced a refresh) always wins, even for a path the .md-only listing does
  // not carry - the agent may have written a .txt or .csv, and refusing to
  // open it because the tree cannot show it defeats the point of following
  // the write at all.
  const target = openPath
    ? openPath
    : files.includes(initialKbPath) ? initialKbPath : files[0];
  openKbFile(target);
}

function setActiveTreeLink(path) {
  document.querySelectorAll('nav a, nav .dir-header, nav .section-header, .nav-title').forEach(el =>
    el.classList.toggle('active', el.dataset.path === path));
}

function openAgentGuide() {
  if (knownKbFiles.has('AGENT_GUIDE.md')) openKbFile('AGENT_GUIDE.md');
}

// --- sanitising rendered markdown ---------------------------------------
//
// Untrusted content reaches this renderer from more than one place: KB pages
// the agent writes after reading a fetched web page, and uploaded documents
// "someone sent". Script executing on this origin is not a stolen cookie, it
// is a confused deputy holding an agent that can write the wiki and answer
// its own permission prompts - so raw HTML and non-http(s) URLs are refused
// rather than trusted. See docs/decisions/0016.

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
marked.use({ renderer: { html: ({ text }) => escapeHtml(text) } });

const SAFE_URL_SCHEMES = new Set(['http:', 'https:', 'mailto:']);
function sanitizeRenderedLinks(container) {
  container.querySelectorAll('a[href], img[src]').forEach((node) => {
    const attr = node.hasAttribute('href') ? 'href' : 'src';
    const raw = node.getAttribute(attr);
    if (!raw) return;
    let url;
    try { url = new URL(raw, location.href); } catch { node.removeAttribute(attr); return; }
    if (!SAFE_URL_SCHEMES.has(url.protocol)) node.removeAttribute(attr);
  });
}

function wireRelativeKbLinks(container) {
  container.querySelectorAll('a').forEach(a => {
    const href = a.getAttribute('href');
    if (href && !href.startsWith('http') && href.endsWith('.md')) {
      a.href = '#';
      a.onclick = (e) => { e.preventDefault(); openKbFile(href); };
    }
  });
}

// --- the centre pane: KB articles, agent writes, uploads ----------------

let currentPane = null; // {kind: 'kb', path} | {kind: 'upload', url, name}

function pathToKbUrl(path) {
  return '/kb/' + path.split('/').map(encodeURIComponent).join('/');
}

function initialKbPathFromUrl() {
  const m = location.pathname.match(/^\/kb\/(.+)$/);
  if (m) return decodeURIComponent(m[1]);
  const hash = decodeURIComponent(location.hash.slice(1));
  return hash || null;
}
const initialKbPath = initialKbPathFromUrl();

async function openKbFile(path) {
  if (!path) return;
  currentPane = { kind: 'kb', path };
  setActiveTreeLink(path);
  history.pushState({}, '', pathToKbUrl(path));
  content.innerHTML = '<div class="prose"><div class="empty">Loading…</div></div>';
  try {
    const res = await fetch('/api/kb/file?path=' + encodeURIComponent(path), { cache: 'no-store' });
    if (!res.ok) throw new Error(res.status);
    const { content: body } = await res.json();
    const prose = el('div', 'prose');
    if (path.endsWith('.md')) {
      prose.innerHTML = marked.parse(body);
      sanitizeRenderedLinks(prose);
      wireRelativeKbLinks(prose);
    } else {
      // Never markdown, never innerHTML: a non-.md KB file (the agent wrote
      // a .txt or .csv) is shown as literal text.
      const pre = el('pre');
      pre.textContent = body;
      prose.appendChild(pre);
    }
    content.innerHTML = '';
    content.appendChild(prose);
  } catch (e) {
    content.innerHTML = '<div class="prose"><div class="empty">Failed to load.</div></div>';
  }
}

const UPLOAD_IMAGE_EXT = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp']);
const UPLOAD_TEXT_EXT = new Set(['.md', '.txt', '.csv', '.json']);

function extOf(name) {
  const m = /\.[^.]+$/.exec(name || '');
  return m ? m[0].toLowerCase() : '';
}

// `url` may be a local blob: URL (the composer already has the bytes, no
// round-trip needed) or the /api/uploads/... route (a reload has only the
// event, not the bytes).
async function openUpload({ url, name }) {
  currentPane = { kind: 'upload', url, name };
  setActiveTreeLink(null);
  const ext = extOf(name);
  const prose = el('div', 'prose');
  if (UPLOAD_IMAGE_EXT.has(ext)) {
    const img = document.createElement('img');
    img.src = url;
    img.alt = name;
    prose.appendChild(img);
  } else if (UPLOAD_TEXT_EXT.has(ext)) {
    prose.innerHTML = '<div class="empty">Loading…</div>';
    content.innerHTML = '';
    content.appendChild(prose);
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) throw new Error(res.status);
      const pre = el('pre');
      pre.textContent = await res.text();
      prose.innerHTML = '';
      prose.appendChild(pre);
    } catch {
      prose.innerHTML = '<div class="empty">Failed to load.</div>';
    }
    return;
  } else {
    // An opaque type is a download, never a render - see _upload_media_type
    // in app/main.py, which is what actually enforces this on the wire.
    const notice = el('div', 'empty');
    const link = document.createElement('a');
    link.href = url;
    link.textContent = 'Download ' + name;
    notice.appendChild(link);
    prose.appendChild(notice);
  }
  content.innerHTML = '';
  content.appendChild(prose);
}

function openInPane(target) {
  if (!target) return;
  if (target.kind === 'kb') { loadFiles(target.path); return; }
  if (target.kind === 'upload') { openUpload(target); return; }
}

window.addEventListener('popstate', () => {
  const path = initialKbPathFromUrl();
  if (path) openKbFile(path);
});

// --- linking a KB path mentioned in a reply -----------------------------
//
// Heuristic and deliberately simple: a reply is linkified against whatever
// the tree already knows about, once the turn finishes. Built with DOM
// nodes rather than string-and-innerHTML, so this cannot itself become an
// injection vector no matter what text the agent wrote.
function linkifyKbPaths(container) {
  if (!knownKbFiles.size) return;
  container.querySelectorAll('.body > p').forEach((p) => {
    const text = p.textContent;
    const matches = [];
    for (const path of knownKbFiles) {
      let idx = text.indexOf(path);
      while (idx !== -1) {
        matches.push({ idx, len: path.length, path });
        idx = text.indexOf(path, idx + path.length);
      }
    }
    if (!matches.length) return;
    matches.sort((a, b) => a.idx - b.idx || b.len - a.len);
    const kept = [];
    let cursor = -1;
    for (const m of matches) {
      if (m.idx < cursor) continue;
      kept.push(m);
      cursor = m.idx + m.len;
    }
    p.textContent = '';
    let pos = 0;
    for (const m of kept) {
      if (m.idx > pos) p.appendChild(document.createTextNode(text.slice(pos, m.idx)));
      const a = document.createElement('a');
      a.href = '#';
      a.textContent = text.slice(m.idx, m.idx + m.len);
      a.onclick = (e) => { e.preventDefault(); openInPane({ kind: 'kb', path: m.path }); };
      p.appendChild(a);
      pos = m.idx + m.len;
    }
    if (pos < text.length) p.appendChild(document.createTextNode(text.slice(pos)));
  });
}

// --- attachments: composer preview, and the sent chip/thumbnail --------

// Images keep the vision path (inlined as content blocks); everything else is
// written to the agent's scratch directory and referenced by path. Splitting
// on the browser's own type is enough - the server does not care which list a
// thing arrived in beyond that.
function isImage(file) { return (file.type || '').startsWith('image/'); }

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('could not read ' + file.name));
    reader.onload = (ev) => {
      const dataUrl = ev.target.result;
      resolve({ dataUrl, base64: dataUrl.slice(dataUrl.indexOf(',') + 1) });
    };
    reader.readAsDataURL(file);
  });
}

async function acceptFile(file) {
  if (file.size > MAX_FILE_BYTES) {
    hint.textContent = file.name + ' is ' + humanSize(file.size) + ' — the limit is ' + humanSize(MAX_FILE_BYTES) + '.';
    return;
  }
  const { dataUrl, base64 } = await readBase64(file);
  if (isImage(file)) addImagePreview(dataUrl, file.type, base64);
  else addFilePreview(file.name, file.size, base64);
}

function addFilePreview(name, size, base64) {
  const entry = { name, size, base64 };
  pendingFiles.push(entry);

  const chip = el('div', 'file-chip');
  chip.appendChild(el('span', 'name', name));
  chip.appendChild(el('span', 'size', humanSize(size)));
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = '×';
  btn.title = 'Remove attachment';
  btn.onclick = () => {
    const i = pendingFiles.indexOf(entry);
    if (i !== -1) pendingFiles.splice(i, 1);
    chip.remove();
  };
  chip.appendChild(btn);
  previews.appendChild(chip);
}

function addImagePreview(dataUrl, mediaType, base64) {
  const idx = pendingImages.length;
  pendingImages.push({ dataUrl, mediaType, base64 });

  const entry = pendingImages[pendingImages.length - 1];
  const thumb = document.createElement('div');
  thumb.className = 'img-thumb';
  const img = document.createElement('img');
  img.src = dataUrl;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = '×';
  btn.title = 'Remove image';
  btn.onclick = () => {
    const i = pendingImages.indexOf(entry);
    if (i !== -1) pendingImages.splice(i, 1);
    thumb.remove();
  };
  thumb.appendChild(img);
  thumb.appendChild(btn);
  previews.appendChild(thumb);
}

// A base64 payload the browser already holds, as a data: URL - so viewing
// what you just attached costs no round-trip, and works even before the
// turn has a server-known upload URL.
function dataUrlFor(name, base64) {
  const ext = extOf(name);
  const mediaType = UPLOAD_IMAGE_EXT.has(ext) ? 'image/' + ext.slice(1) : 'application/octet-stream';
  return 'data:' + mediaType + ';base64,' + base64;
}

input.addEventListener('paste', (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    // 'string' items are the text being pasted; only files are attachments,
    // otherwise pasting ordinary text would be swallowed by preventDefault.
    if (item.kind !== 'file') continue;
    const file = item.getAsFile();
    if (!file) continue;
    e.preventDefault();
    acceptFile(file);
  }
});

document.getElementById('attach').addEventListener('click', () => filepicker.click());
filepicker.addEventListener('change', () => {
  for (const file of filepicker.files) acceptFile(file);
  // Reset, or picking the same file twice in a row fires no change event.
  filepicker.value = '';
});

// Drag-and-drop onto the composer. dragover must be cancelled or the browser
// navigates away to the dropped file, losing the conversation.
for (const type of ['dragenter', 'dragover']) {
  form.addEventListener(type, (e) => { e.preventDefault(); form.classList.add('dragover'); });
}
form.addEventListener('dragleave', (e) => {
  if (e.target === form) form.classList.remove('dragover');
});
form.addEventListener('drop', (e) => {
  e.preventDefault();
  form.classList.remove('dragover');
  for (const file of e.dataTransfer.files) acceptFile(file);
});

function addMessage(role, cls) {
  const wrap = el('div', 'msg ' + (cls || ''));
  wrap.appendChild(el('div', 'role', role));
  const body = el('div', 'body', '');
  wrap.appendChild(body);
  chatLog.appendChild(wrap);
  scroll();
  return { wrap, body };
}

async function boot() {
  try {
    const me = await (await fetch('/api/me')).json();
    document.getElementById('who').textContent = me.email;
  } catch { document.getElementById('who').textContent = 'not signed in'; }
  try {
    const h = await (await fetch('/healthz')).json();
    const dot = document.getElementById('health');
    dot.classList.add(h.kb_mounted ? 'ok' : 'bad');
    dot.title = h.kb_mounted ? 'knowledge base mounted' : 'KNOWLEDGE BASE NOT MOUNTED';
    const warnings = [];
    if (!h.kb_mounted) warnings.push('Warning: the knowledge base is not mounted — answers will not use it.');
    // A connected service whose credential is dead or nearly dead. Said here
    // rather than only in /healthz because re-authorising is a chore somebody has
    // to remember, and nothing in this app schedules a reminder — so the next
    // best thing is telling whoever opens the page.
    const stale = [];
    for (const [name, s] of Object.entries(h.mcp_catalog || {})) {
      // days_left goes negative when Google still honours a grant we reckon
      // should already be gone — worth saying, but not as "-3d left".
      if (s.state === 'expired') stale.push(`${name} (expired)`);
      else if (s.state === 'expiring') stale.push(`${name} (${s.days_left < 0 ? 'overdue' : s.days_left + 'd left'})`);
    }
    if (stale.length) warnings.push(`Connected service access needs renewing: ${stale.join(', ')} — re-run scripts/google-auth.sh and set the secrets it prints.`);
    if (warnings.length) hint.textContent = warnings.join(' ');
  } catch {}

  loadFiles();

  const resumeId = localStorage.getItem(ACTIVE_TURN_KEY);
  if (resumeId) {
    send.disabled = true;
    send.textContent = 'Working…';
    stream(resumeId, { resuming: true });
  }
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  const images = pendingImages.slice();
  const files = pendingFiles.slice();
  if (!text && !images.length && !files.length) return;
  input.value = '';
  pendingImages = [];
  pendingFiles = [];
  previews.innerHTML = '';
  send.disabled = true;
  send.textContent = 'Working…';

  const { wrap: youWrap, body: youBody } = addMessage('you', 'me');
  const fileChips = []; // [{chip, name, base64}], wired to a click handler once turnId is known
  if (images.length) {
    const imgRow = document.createElement('div');
    imgRow.className = 'msg-images';
    for (const img of images) {
      const el = document.createElement('img');
      el.src = img.dataUrl;
      el.onclick = () => openUpload({ url: img.dataUrl, name: 'image' });
      imgRow.appendChild(el);
    }
    youBody.appendChild(imgRow);
  }
  if (files.length) {
    const fileRow = el('div', 'msg-files');
    for (const f of files) {
      const chip = el('div', 'file-chip clickable');
      chip.appendChild(el('span', 'name', f.name));
      chip.appendChild(el('span', 'size', humanSize(f.size)));
      chip.onclick = () => openUpload({ url: dataUrlFor(f.name, f.base64), name: f.name });
      fileRow.appendChild(chip);
      fileChips.push(chip);
    }
    youBody.appendChild(fileRow);
  }
  if (text) {
    const p = document.createElement('p');
    p.textContent = text;
    youBody.appendChild(p);
  }

  let turnId;
  try {
    const res = await fetch('/api/turns', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        session_id: sessionId,
        images: images.map(i => ({ media_type: i.mediaType, data: i.base64 })),
        files: files.map(f => ({ name: f.name, data: f.base64 })),
      }),
    });
    if (!res.ok) {
      // Surface the server's own reason. A rejected attachment answers 400 or
      // 413 with a detail that names the file, and "submit failed: 413" alone
      // sends the user hunting for a problem the server already described.
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch {}
      throw new Error(detail || 'submit failed: ' + res.status);
    }
    turnId = (await res.json()).turn_id;
  } catch (err) {
    // Put the composer back. A 409 ("a turn is already running") is an ordinary
    // outcome now that one turn runs at a time, not an exceptional one, and
    // losing a paragraph and its attachments to it would teach people to copy
    // their message before every send. The submitted bubble goes too, so the
    // log does not show a message that was never sent.
    // Re-added through the same helpers rather than by reassigning the arrays,
    // so each restored chip gets its remove button wired up again. Nothing
    // pasted while the request was in flight is cleared, and the text is only
    // restored if the box is still empty - recovering the old message must not
    // destroy a newer one.
    youWrap.remove();
    if (!input.value) input.value = text;
    for (const i of images) addImagePreview(i.dataUrl, i.mediaType, i.base64);
    for (const f of files) addFilePreview(f.name, f.size, f.base64);
    addMessage('error', '').body.classList.add('err');
    chatLog.lastChild.querySelector('.body').textContent = String(err);
    send.disabled = false;
    send.textContent = 'Send';
    return;
  }
  localStorage.setItem(ACTIVE_TURN_KEY, turnId);
  stream(turnId, { resuming: false });
});

function stream(turnId, { resuming }) {
  const msg = addMessage('agent', '');
  let attachmentsBox = null; // built lazily, only when resuming

  // Pulsing indicator, with the elapsed time beside it. It stays for the WHOLE
  // turn and is removed only by finish().
  //
  // It used to be removed on the first token instead, which read as "finished"
  // for the rest of the turn: after one sentence like "Reading the CSV now."
  // there could be minutes of real work — a long tool call, a subagent, a long
  // stretch of thinking — with nothing on screen moving. The only remaining hint
  // that the turn was alive was the Revert button not being there yet, which is
  // not a signal anyone reads. The elapsed clock is the other half: it
  // distinguishes "slow" from "hung" without anyone having to guess.
  const working = el('div', 'working');
  const dots = el('div', 'thinking');
  for (let i = 0; i < 3; i++) dots.appendChild(el('span'));
  working.appendChild(dots);
  const elapsed = el('span', 'elapsed', '');
  working.appendChild(elapsed);
  msg.wrap.appendChild(working);

  const startedAt = Date.now();
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - startedAt) / 1000);
    elapsed.textContent = s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  }, 1000);

  const es = new EventSource('/api/turns/' + turnId + '/events');
  let hasTextDelta = false; // true once the CLI streams at least one token
  let currentPara = null;   // the <p> currently receiving streamed tokens
  let newParaNext = true;   // start a fresh <p> on the next text chunk

  function appendText(raw) {
    if (newParaNext) {
      currentPara = el('p', '');
      msg.body.appendChild(currentPara);
      newParaNext = false;
    }
    currentPara.textContent += raw.replace(/\\n/g, '\n');
    scroll();
  }

  // Structured events carry a JSON payload; text ones stay raw strings.
  const parse = (e) => { try { return JSON.parse(e.data); } catch { return null; } };

  // Insert into the main message, keeping the working indicator last so it
  // always sits below the newest thing that happened rather than above it.
  function insert(node, into) {
    if (into) { into.appendChild(node); }
    else { msg.wrap.insertBefore(node, working); }
    scroll();
  }

  const toolLines = new Map();   // tool_use id -> its .tool div
  const toolTargets = new Map(); // tool_use id -> its {kind, path}, if any
  const forms = new Map();       // request_id -> the ask/permission element
  const agents = new Map();      // agent key -> its <details> container
  let lastAgentType = '';
  let thought = null;
  let todos = null;

  // Subagent output must never land in the reply, which is why the server tags
  // it. Containers are keyed by whatever tag the event carries: agent_start
  // reports the SDK's agent id while message events report the Task call's
  // tool_use id, and those are different identifiers, so with several subagents
  // running at once attribution between blocks can be wrong. What cannot go
  // wrong is subagent text reaching the main paragraph.
  function agentBox(key) {
    if (!key) return null;
    if (!agents.has(key)) {
      const d = el('details', 'subagent');
      d.appendChild(el('summary', '', 'subagent' + (lastAgentType ? ': ' + lastAgentType : '') + ' — working'));
      insert(d);
      agents.set(key, d);
    }
    return agents.get(key);
  }

  // Token-by-token streaming (requires --include-partial-messages support).
  es.addEventListener('text_delta', (e) => { hasTextDelta = true; appendText(e.data); });
  // Full-turn text from AssistantMessage — used when streaming isn't available,
  // ignored if text_delta already delivered the content.
  es.addEventListener('text', (e) => {
    if (!hasTextDelta) { newParaNext = true; appendText(e.data); }
  });

  es.addEventListener('agent_text', (e) => {
    const d = parse(e); if (!d) return;
    const box = agentBox(d.agent); if (!box) return;
    let p = box.querySelector('.agent-text');
    if (!p) { p = el('div', 'agent-text', ''); box.appendChild(p); }
    p.textContent += (d.text || '').replace(/\\n/g, '\n');
  });

  const thinkingInto = () => {
    if (!thought) {
      thought = el('details', 'thought');
      thought.appendChild(el('summary', '', 'thinking'));
      thought.appendChild(el('div', 'text', ''));
      insert(thought);
    }
    return thought.querySelector('.text');
  };
  es.addEventListener('thinking_delta', (e) => {
    thinkingInto().textContent += e.data.replace(/\\n/g, '\n');
  });
  es.addEventListener('thinking', (e) => {
    if (!thought) thinkingInto().textContent = e.data.replace(/\\n/g, '\n');
  });

  const LABELS = { Bash: 'bash', Read: 'read', Write: 'write', Edit: 'edit',
                   Glob: 'glob', Grep: 'grep', WebSearch: 'web search',
                   WebFetch: 'web fetch', Task: 'delegate', TodoWrite: 'plan' };

  // Two events arrive per tool call, both carrying the same id: one the instant
  // the call starts (name only, from content_block_start) and one when the
  // assistant message completes (with the arguments). The first is what makes a
  // slow tool visible at all; the second fills it in. Keyed by id so the line is
  // updated rather than drawn twice.
  es.addEventListener('tool_use', (e) => {
    const d = parse(e); if (!d) return;
    newParaNext = true;   // next text starts a new paragraph
    let t = d.id ? toolLines.get(d.id) : null;
    if (!t) {
      const label = LABELS[d.name] || String(d.name || '').toLowerCase();
      t = el('div', 'tool', '→ ' + label + ' ');
      if (d.id) toolLines.set(d.id, t);
      insert(t, agentBox(d.agent));
    }
    if (d.detail) {
      // .args, not .detail: tool_result appends its own .detail span for a
      // failure, and this must never overwrite that.
      let args = t.querySelector('.args');
      if (!args) { args = el('span', 'detail args', ''); t.appendChild(args); }
      args.textContent = d.detail;
      scroll();
    }
    // Stashed, not acted on: the tool has not run yet, and a Write that then
    // fails must not move the centre pane. tool_result is what decides.
    if (d.id && d.target && d.target.kind) toolTargets.set(d.id, d.target);
  });

  es.addEventListener('tool_result', (e) => {
    const d = parse(e); if (!d) return;
    const line = toolLines.get(d.id);
    if (line && !d.ok) {   // successes stay quiet; only failures speak up
      line.classList.add('failed');
      line.appendChild(el('span', 'detail', ' — failed' + (d.detail ? ': ' + d.detail : '')));
      scroll();
    }
    if (d.ok) {
      const target = toolTargets.get(d.id);
      if (target) openInPane(target);
    }
  });

  es.addEventListener('agent_start', (e) => {
    const d = parse(e); if (!d) return;
    lastAgentType = d.agent_type || '';
    agentBox(d.agent_id);
  });
  es.addEventListener('agent_stop', (e) => {
    const d = parse(e); if (!d) return;
    const box = agents.get(d.agent_id);
    if (box) box.querySelector('summary').textContent =
      'subagent' + (d.agent_type ? ': ' + d.agent_type : '') + ' — done';
  });

  es.addEventListener('todo', (e) => {
    const d = parse(e); if (!d || !Array.isArray(d.todos)) return;
    if (!todos) { todos = el('div', 'todos'); insert(todos); }
    todos.innerHTML = '';
    for (const t of d.todos) {
      const mark = t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '▸' : '·';
      const text = t.status === 'in_progress' ? (t.activeForm || t.content) : t.content;
      todos.appendChild(el('div', 'item ' + (t.status || ''), mark + ' ' + text));
    }
    scroll();
  });

  // The user's own message only exists client-side - nothing replays its
  // text. On a normal send it is already on screen with its own chips, so
  // this event is ignored there; it exists for the resume path, where a
  // reload has nothing BUT this event to show what was attached.
  es.addEventListener('attachment', (e) => {
    if (!resuming) return;
    const d = parse(e); if (!d) return;
    if (!attachmentsBox) {
      const box = addMessage('you', 'me');
      attachmentsBox = el('div', 'msg-files');
      box.body.appendChild(attachmentsBox);
      chatLog.insertBefore(box.wrap, msg.wrap);
    }
    const chip = el('div', 'file-chip clickable');
    chip.appendChild(el('span', 'name', d.name));
    chip.onclick = () => openUpload({ url: d.url, name: d.name });
    attachmentsBox.appendChild(chip);
  });

  // --- the two round-trips back into the running turn --------------------
  //
  // Both post to the turn that is still executing, and both are disabled by the
  // matching resolution event — which also arrives when another tab answered
  // first, or when the request timed out on the server.

  async function resolve(path, body, box, verdict) {
    box.classList.add('resolved');
    const r = await fetch('/api/turns/' + turnId + '/' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let note = verdict;
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch {}
      note = detail || 'could not send that';
    }
    box.appendChild(el('div', 'verdict', note));
    scroll();
  }

  es.addEventListener('ask', (e) => {
    const d = parse(e); if (!d) return;
    const box = el('div', 'ask');
    if (d.header) box.appendChild(el('div', 'sub', d.header));
    box.appendChild(el('div', 'q', d.question || ''));
    const type = d.multi_select ? 'checkbox' : 'radio';
    (d.options || []).forEach((opt, i) => {
      const label = el('label');
      const cb = document.createElement('input');
      cb.type = type; cb.name = 'opt-' + d.request_id; cb.value = opt;
      if (!d.multi_select && i === 0) cb.checked = true;
      label.appendChild(cb);
      label.appendChild(document.createTextNode(' ' + opt));
      box.appendChild(label);
    });
    const other = document.createElement('input');
    other.type = 'text';
    other.placeholder = (d.options || []).length ? 'or write your own…' : 'your answer…';
    box.appendChild(other);

    const actions = el('div', 'actions');
    const submit = el('button', 'primary', 'Answer');
    submit.onclick = () => {
      const picked = [...box.querySelectorAll('input[type=' + type + ']:checked')].map(i => i.value);
      resolve('answer', { request_id: d.request_id, answers: picked, notes: other.value.trim() },
              box, 'answered');
    };
    actions.appendChild(submit);
    box.appendChild(actions);
    forms.set(d.request_id, box);
    insert(box);
  });

  es.addEventListener('permission', (e) => {
    const d = parse(e); if (!d) return;
    const box = el('div', 'perm');
    box.appendChild(el('div', 'q', d.title || ('Allow ' + d.tool + '?')));
    const sub = [d.description, d.detail, d.blocked_path, d.reason].filter(Boolean).join(' — ');
    if (sub) box.appendChild(el('div', 'sub', sub));

    const actions = el('div', 'actions');
    const allow = el('button', 'allow', 'Allow');
    const deny = el('button', 'deny', 'Deny');
    allow.onclick = () => resolve('permission',
      { request_id: d.request_id, decision: 'allow' }, box, 'allowed');
    deny.onclick = () => resolve('permission',
      { request_id: d.request_id, decision: 'deny' }, box, 'denied');
    actions.appendChild(allow);
    actions.appendChild(deny);
    box.appendChild(actions);
    forms.set(d.request_id, box);
    insert(box);
  });

  function closeForm(e, timedOutNote, resolvedNote) {
    const d = parse(e); if (!d) return;
    const box = forms.get(d.request_id);
    if (!box || box.classList.contains('resolved')) return;
    box.classList.add('resolved');
    box.appendChild(el('div', 'verdict', d.timeout ? timedOutNote : resolvedNote(d)));
    scroll();
  }
  es.addEventListener('answered', (e) =>
    closeForm(e, 'nobody answered in time; the agent carried on', () => 'answered'));
  es.addEventListener('permission_resolved', (e) =>
    closeForm(e, 'not approved in time; denied', (d) => d.decision === 'allow' ? 'allowed' : 'denied'));

  es.addEventListener('session', (e) => { sessionId = e.data; });
  es.addEventListener('status', () => {});

  let finished = false;
  const finish = (failed, detail) => {
    // Idempotent: `done` and a subsequent onerror can both arrive, and a second
    // pass would append a second Revert button to the same turn.
    if (finished) return;
    finished = true;
    clearInterval(tick);
    working.remove();
    es.close();
    send.disabled = false;
    send.textContent = 'Send';
    linkifyKbPaths(msg.wrap);
    if (failed) {
      const n = el('div', 'body err', detail || 'the turn failed');
      msg.wrap.appendChild(n);
    }
    // Every turn is wrapped in a TigerFS savepoint, so reverting is atomic
    // — and the undo is itself reversible.
    const btn = el('button', 'revert', 'Revert this turn');
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = 'Reverting…';
      const r = await fetch('/api/turns/' + turnId + '/revert', { method: 'POST' });
      btn.textContent = r.ok ? 'Reverted' : 'Revert failed';
    };
    msg.wrap.appendChild(btn);
    scroll();
  };

  // The marker is cleared ONLY on these two server-authoritative events, not
  // from onerror below. A page reload closes this EventSource exactly the
  // same way a truly dead connection does, and that closure fires onerror
  // synchronously in the dying document, before the reloaded page's boot()
  // ever runs - clearing the marker there defeated resume for the one case
  // it exists to handle.
  es.addEventListener('done', () => { localStorage.removeItem(ACTIVE_TURN_KEY); finish(false); });
  es.addEventListener('failed', (e) => { localStorage.removeItem(ACTIVE_TURN_KEY); finish(true, e.data); });
  es.onerror = () => {
    // EventSource reconnects on its own and the server replays from
    // Last-Event-ID, so a transient drop needs no handling here.
    if (es.readyState !== EventSource.CLOSED) return;
    if (!resuming) { finish(true, 'connection closed'); return; }
    // A resumed id can be gone for good - the Registry evicts oldest-finished
    // turns past 200, or the marker is simply stale. Confirmed before
    // clearing it, so an ordinary network hiccup during resume does not
    // discard state that a retry would have recovered.
    fetch('/api/turns/' + turnId)
      .then((r) => { if (r.status === 404) localStorage.removeItem(ACTIVE_TURN_KEY); })
      .catch(() => {})
      .finally(() => finish(true, 'connection closed'));
  };
}

boot();
