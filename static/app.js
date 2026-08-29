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
const conversationPicker = document.getElementById('conversation-picker');
const newChatBtn = document.getElementById('new-chat');
const stopBtn = document.getElementById('stop');
const navPane = document.getElementById('nav');
const mainPane = document.querySelector('main');
const chatPane = document.getElementById('chat');
const toggleTreeBtn = document.getElementById('toggle-tree');
const toggleChatBtn = document.getElementById('toggle-chat');
const paneTabs = document.querySelectorAll('.pane-tabs button');

let pendingImages = []; // [{dataUrl, mediaType, base64}]
let pendingFiles = [];  // [{name, size, base64}]

// Mirrors MAX_UPLOAD_BYTES in app/config.py. The server is the authority and
// answers 413; this only exists so the user learns before a 10 MB upload.
const MAX_FILE_BYTES = 10 * 1024 * 1024;

// The conversation is the unit now, not the turn - see docs/decisions/0017.
// One EventSource per conversation, opened once and left open: a reload
// replays the whole thing from seq 0 (Postgres/the in-process tail), not just
// whatever turn happened to be running. `myEmail` labels which bubbles are
// "you" in a household-shared conversation where more than one person's
// messages can appear in the same log.
let myEmail = null;
let activeConversationId = null;
let es = null;
let turnUI = null; // the in-progress (or most recently finished) turn's UI state

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

// --- collapsing a sidebar from the header (desktop only - the mobile
// carousel below replaces these with the tab strip, and CSS keeps a pane
// that was left collapsed on desktop visible once the breakpoint hides the
// tree/chat toggles) --------------------------------------------------------

function loadFlag(key) { return localStorage.getItem(key) === '1'; }

let hideTree = loadFlag('memory-agent:hide-tree');
let hideChat = loadFlag('memory-agent:hide-chat');

function applyPaneToggles() {
  layout.classList.toggle('no-tree', hideTree);
  layout.classList.toggle('no-chat', hideChat);
  toggleTreeBtn.setAttribute('aria-pressed', String(!hideTree));
  toggleChatBtn.setAttribute('aria-pressed', String(!hideChat));
}
applyPaneToggles();

toggleTreeBtn.addEventListener('click', () => {
  hideTree = !hideTree;
  localStorage.setItem('memory-agent:hide-tree', hideTree ? '1' : '0');
  applyPaneToggles();
});
toggleChatBtn.addEventListener('click', () => {
  hideChat = !hideChat;
  localStorage.setItem('memory-agent:hide-chat', hideChat ? '1' : '0');
  applyPaneToggles();
});

// --- mobile: swiping between the three panes ------------------------------
//
// Native CSS scroll-snap (app.css's 820px breakpoint) rather than a touch
// gesture handler - momentum, rubber-banding and accessibility come for
// free, and there is nothing to conflict with the tree's own scrolling.

const MOBILE_QUERY = matchMedia('(max-width: 820px)');
const PANE_ELS = { nav: navPane, main: mainPane, chat: chatPane };
const PANE_ORDER = ['nav', 'main', 'chat'];

function goToPane(name) {
  const target = PANE_ELS[name];
  if (target) target.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' });
}

function activePaneName() {
  const idx = Math.round(layout.scrollLeft / Math.max(1, layout.clientWidth));
  return PANE_ORDER[Math.min(PANE_ORDER.length - 1, Math.max(0, idx))];
}

function updateActiveTab() {
  const active = activePaneName();
  paneTabs.forEach((btn) => btn.classList.toggle('active', btn.dataset.pane === active));
  if (active === 'main') clearArticleDot();
}

paneTabs.forEach((btn) => btn.addEventListener('click', () => goToPane(btn.dataset.pane)));
layout.addEventListener('scroll', updateActiveTab);
updateActiveTab();

// A dot on the Article tab when the centre pane changed while some other
// pane was on screen - without it, the auto-navigate-on-write behavior
// (docs/decisions/0016) silently does nothing once that pane is off-screen.
function markArticleDot() {
  if (!MOBILE_QUERY.matches || activePaneName() === 'main') return;
  const btn = document.querySelector('.pane-tabs button[data-pane="main"]');
  if (btn && !btn.querySelector('.pane-dot')) btn.appendChild(el('span', 'pane-dot'));
}
function clearArticleDot() {
  document.querySelector('.pane-tabs .pane-dot')?.remove();
}

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
let dirNodes = new Map(); // dirPath -> {children, toggle}, populated by loadFiles, read by expandAncestors

// Renders the tree, and *only* the tree. It used to also decide what the
// centre pane showed, which is why opening one file reloaded the whole
// listing, why the ↻ button navigated you away from what you were reading,
// and why a directory deep link fell through to whatever files[0] happened to
// be. Choosing the pane is openKbPath()'s job now.
async function loadFiles() {
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
  dirNodes = new Map();
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

  // One guide span per ancestor level, before the toggle/label of every row -
  // a folder that has more siblings below it (guides entry `true`) draws a
  // connecting line through all of its descendants' rows; a last child
  // (`false`) leaves that column blank once its own subtree starts, the same
  // way a file explorer's tree stops a branch's line where the branch ends.
  function renderRowGuides(el, guides) {
    guides.forEach(hasLine => {
      const g = document.createElement('span');
      g.className = hasLine ? 'tree-guide' : 'tree-guide tree-guide-empty';
      el.appendChild(g);
    });
  }

  function renderNode(node, container, depth, guides) {
    const dirEntries = Object.keys(node.dirs).sort().map(name => ({ name, dir: node.dirs[name] }));
    const fileEntries = node.files.sort().filter(f => {
      const base = f.split('/').pop();
      // Files that describe the directory rather than sit in it. Each is
      // reachable through its directory's header; none is a sibling of the
      // pages it governs.
      if (base === 'GUIDE.md' || base === 'SKILL.md' || base === 'VIEW.md') return false;
      if (depth === 0 && base === 'AGENT_GUIDE.md') return false;
      return true;
    }).map(f => ({ file: f }));
    // One combined, ordered list: "is this the last row in this container"
    // has to account for files coming after dirs, not just siblings within
    // one or the other, since that's what decides whether this row's guide
    // column keeps a line running past its own descendants.
    const entries = dirEntries.concat(fileEntries);

    entries.forEach((entry, i) => {
      const isLast = i === entries.length - 1;

      if (entry.dir) {
        const child = entry.dir;
        const dirPath = child.path;

        const skillPath = dirPath + '/SKILL.md';
        // A skill directory's index genuinely *is* its SKILL.md - the folder is
        // one document plus its references, not a collection - so it keeps the
        // behaviour it has always had. Everything else opens a directory view.
        const isSkillDir = fileSet.has(skillPath);

        // Every directory, top-level sections included, is a collapsible
        // header that starts collapsed - expandAncestors() opens the path to
        // whichever file ends up active instead.
        const header = document.createElement('div');
        header.className = depth === 0 ? 'section-header' : 'dir-header';
        renderRowGuides(header, guides);
        const toggle = document.createElement('span');
        toggle.className = 'dir-toggle collapsed';
        const label = document.createElement('span');
        label.className = 'label';
        label.textContent = entry.name;
        header.appendChild(toggle);
        header.appendChild(label);
        // Always set, including for a directory with no guide of its own. It
        // used to be set only when there was a file to open, which left exactly
        // the directories that now gain a view unable to ever show as active.
        header.dataset.path = isSkillDir ? skillPath : dirPath;

        const children = document.createElement('div');
        children.className = 'dir-children collapsed';

        header.onclick = () => {
          const collapsed = children.classList.toggle('collapsed');
          toggle.classList.toggle('collapsed', collapsed);
          // Only render into the centre pane when the click just EXPANDED this
          // header, not when it just collapsed it - openKbFile() re-expands the
          // same header via expandAncestors(), which otherwise undid the
          // collapse on the very click that made it.
          if (collapsed) return;
          if (isSkillDir) openKbFile(skillPath);
          else openKbDir(dirPath);
        };

        dirNodes.set(dirPath, { children, toggle });

        container.appendChild(header);
        renderNode(child, children, depth + 1, guides.concat(!isLast));
        container.appendChild(children);
      } else {
        const f = entry.file;
        const base = f.split('/').pop();
        const name = base.replace(/\.md$/, '');
        const a = document.createElement('a');
        renderRowGuides(a, guides);
        // No chevron-width spacer here, deliberately - a file has nothing to
        // toggle, so its label starts right where a same-depth directory's
        // chevron does, not where that directory's label does. Giving it the
        // same spacer used to push files a whole column deeper than their
        // sibling directories, reading as though they nested one level in.
        const label = document.createElement('span');
        label.className = 'label';
        label.textContent = name;
        a.appendChild(label);
        a.href = '#';
        a.dataset.path = f;
        a.onclick = (e) => { e.preventDefault(); openKbFile(f); };
        container.appendChild(a);
      }
    });
  }

  navFileList.innerHTML = '';
  renderNode(tree, navFileList, 0, []);

  // The tree was just rebuilt, so whatever is on screen lost its highlight.
  // Specs are cached per directory and an agent write may have changed one,
  // so the cache goes with it.
  specCache = new Map();
  if (currentPane && (currentPane.kind === 'kb' || currentPane.kind === 'kbdir')) {
    setActiveTreeLink(currentPane.path);
  }
}

function expandAncestors(path) {
  if (!path) return;
  const parts = path.split('/');
  let prefix = '';
  for (let i = 0; i < parts.length - 1; i++) {
    prefix = prefix ? prefix + '/' + parts[i] : parts[i];
    const node = dirNodes.get(prefix);
    if (node) {
      node.children.classList.remove('collapsed');
      node.toggle.classList.remove('collapsed');
    }
  }
}

function setActiveTreeLink(path) {
  expandAncestors(path);
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
      a.onclick = (e) => { e.preventDefault(); openKbPath(href); };
    }
  });
}

// Full re-parse of the whole raw markdown source into `container`, through
// the same sanitizer as the KB pane. Used for chat text too, where `raw` is
// the accumulated buffer so far rather than a complete message - re-running
// this on every streamed chunk is what lets `**`/`` ` ``/lists render instead
// of showing up literally, at the cost of a moment's flicker on an
// unterminated construct (an open code fence, a half-typed `**`) that
// self-corrects on the next chunk.
function renderMarkdownInto(container, raw) {
  container.innerHTML = marked.parse(raw);
  sanitizeRenderedLinks(container);
  wireRelativeKbLinks(container);
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

// Two navigations can be in flight at once - a click while a directory fetch
// is still running - and the slower one must not paint over the newer one.
// Every pane render takes a token before its first await and gives up if the
// token has moved on. The directory fetch is heavier than a file read, so it
// is the one that loses these races.
let paneSeq = 0;

// A path with an extension is a file; anything else is a directory. Every
// entry in the listing ends in `.md` and directories carry no dots, so this
// decides without consulting the tree - which matters because a deep link is
// rendered before `loadFiles()` has finished populating `knownKbFiles`.
function isFilePath(path) {
  return /\.[a-z0-9]+$/i.test(path);
}

function parentDirOf(path) {
  const cut = path.lastIndexOf('/');
  return cut === -1 ? '' : path.slice(0, cut);
}

// One entry point for every navigation. `GUIDE.md` and `VIEW.md` describe a
// directory rather than living in it, so opening either opens the directory
// itself - which also means an agent write to a spec lands the reader on the
// thing it changed instead of on the config that changed it.
function openKbPath(path, opts) {
  if (path === null || path === undefined) return;
  const base = path.split('/').pop();
  if (base === 'GUIDE.md' || base === 'VIEW.md') {
    return openKbDir(parentDirOf(path), opts);
  }
  return isFilePath(path) ? openKbFile(path, opts) : openKbDir(path, opts);
}

function setPaneUrl(path, opts) {
  if (opts && opts.push === false) return;
  history.pushState({}, '', pathToKbUrl(path));
}

// The directory's own spec, cached per directory. A file at that level needs
// its parent's `page:` block to draw its header chips, and without a cache
// every tree click would pay for a second request. Cleared by loadFiles(),
// which is what runs after an agent write.
let specCache = new Map();

async function specForDir(dir) {
  if (specCache.has(dir)) return specCache.get(dir);
  let spec = null;
  try {
    const res = await fetch('/api/kb/spec?path=' + encodeURIComponent(dir), { cache: 'no-store' });
    if (res.ok) spec = await res.json();
  } catch { /* a missing spec is the common case, not an error */ }
  specCache.set(dir, spec);
  return spec;
}

async function openKbFile(path, opts) {
  if (!path) return;
  const seq = ++paneSeq;
  currentPane = { kind: 'kb', path };
  setActiveTreeLink(path);
  setPaneUrl(path, opts);
  content.innerHTML = '<div class="prose"><div class="empty">Loading…</div></div>';
  try {
    const res = await fetch('/api/kb/file?path=' + encodeURIComponent(path), { cache: 'no-store' });
    if (!res.ok) throw new Error(res.status);
    const { content: body, fields } = await res.json();
    if (seq !== paneSeq) return;
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

    // The field header goes above the prose, and is fetched after it so a
    // directory with no spec costs the reader nothing in time-to-first-paint.
    const spec = await specForDir(parentDirOf(path));
    if (seq !== paneSeq) return;
    if (spec) renderPageHeader(fields || {}, spec.page, spec.view, content);
    content.appendChild(prose);
  } catch (e) {
    if (seq !== paneSeq) return;
    content.innerHTML = '<div class="prose"><div class="empty">Failed to load.</div></div>';
  }
}

async function openKbDir(path, opts) {
  const dir = (path || '').replace(/^\/+|\/+$/g, '');
  const seq = ++paneSeq;
  currentPane = { kind: 'kbdir', path: dir };
  setActiveTreeLink(dir);
  setPaneUrl(dir, opts);
  content.innerHTML = '<div class="prose"><div class="empty">Loading…</div></div>';

  let data;
  try {
    const res = await fetch('/api/kb/dir?path=' + encodeURIComponent(dir), { cache: 'no-store' });
    if (res.status === 404) throw new Error('404');
    if (!res.ok) throw new Error(res.status);
    data = await res.json();
  } catch (err) {
    if (seq !== paneSeq) return;
    // `/static` is cached independently of the image, so a new app.js can meet
    // an old server that has no directory route at all. Falling back to the
    // guide keeps the pane useful instead of blaming the reader for it.
    const guide = dir ? dir + '/GUIDE.md' : 'AGENT_GUIDE.md';
    if (knownKbFiles.has(guide)) return openKbFile(guide, { push: false });
    content.innerHTML = '<div class="prose"><div class="empty">Failed to load.</div></div>';
    return;
  }
  if (seq !== paneSeq) return;

  specCache.set(dir, { view: data.view, page: data.page });

  content.innerHTML = '';
  if (data.guide && data.guide.content) {
    const prose = el('div', 'prose');
    prose.innerHTML = marked.parse(data.guide.content);
    sanitizeRenderedLinks(prose);
    wireRelativeKbLinks(prose);
    content.appendChild(prose);
  }
  renderDirView(data, content, (p) => openKbPath(p));
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
  // On mobile the centre pane is a swipe away rather than always visible, so
  // a write that navigates it needs a signal that survives being off-screen.
  markArticleDot();
  if (target.kind === 'kb') {
    // Open first, refresh the tree second, and only when the tree has never
    // heard of this path - an agent write that created a file needs a new
    // listing, but an edit to an existing one does not, and rebuilding the
    // tree is what used to make following a write feel like a page load.
    openKbPath(target.path);
    if (!knownKbFiles.has(target.path)) loadFiles();
    return;
  }
  if (target.kind === 'upload') { openUpload(target); return; }
}

window.addEventListener('popstate', () => {
  // push:false, or Back would push the entry it just popped and the button
  // would stop making progress.
  openKbPath(initialKbPathFromUrl() || '', { push: false });
});

// --- linking a KB path mentioned in a reply -----------------------------
//
// Heuristic and deliberately simple: a reply is linkified against whatever
// the tree already knows about, once the turn finishes. Walks text nodes
// rather than rewriting a paragraph's full content, because a `.body` now
// holds real markdown-rendered HTML (bold, code, links, lists) that a
// textContent rebuild would silently discard - only the matched text node
// itself is replaced, everything around it is untouched. Built with DOM
// nodes rather than string-and-innerHTML, so this cannot itself become an
// injection vector no matter what text the agent wrote.
function linkifyKbPaths(container) {
  if (!knownKbFiles.size) return;
  container.querySelectorAll('.body').forEach((body) => {
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        // Never relink an existing link, and never touch code - a path
        // mentioned inside a code span/block is source text, not prose.
        for (let a = node.parentNode; a && a !== body; a = a.parentNode) {
          if (a.nodeName === 'A' || a.nodeName === 'CODE' || a.nodeName === 'PRE') {
            return NodeFilter.FILTER_REJECT;
          }
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    let node;
    while ((node = walker.nextNode())) nodes.push(node);

    for (const textNode of nodes) {
      const text = textNode.nodeValue;
      const matches = [];
      for (const path of knownKbFiles) {
        let idx = text.indexOf(path);
        while (idx !== -1) {
          matches.push({ idx, len: path.length, path });
          idx = text.indexOf(path, idx + path.length);
        }
      }
      if (!matches.length) continue;
      matches.sort((a, b) => a.idx - b.idx || b.len - a.len);
      const kept = [];
      let cursor = -1;
      for (const m of matches) {
        if (m.idx < cursor) continue;
        kept.push(m);
        cursor = m.idx + m.len;
      }
      const frag = document.createDocumentFragment();
      let pos = 0;
      for (const m of kept) {
        if (m.idx > pos) frag.appendChild(document.createTextNode(text.slice(pos, m.idx)));
        const a = document.createElement('a');
        a.href = '#';
        a.textContent = text.slice(m.idx, m.idx + m.len);
        a.onclick = (e) => { e.preventDefault(); openInPane({ kind: 'kb', path: m.path }); };
        frag.appendChild(a);
        pos = m.idx + m.len;
      }
      if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
      textNode.replaceWith(frag);
    }
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

function addMessage(role, cls, who) {
  const wrap = el('div', 'msg ' + (cls || ''));
  const roleEl = el('div', 'role', who ? '' : role);
  if (who) {
    // A speaker name in a household-shared conversation: shown for a
    // message from someone other than the viewer, or an answer/decision made
    // by someone other than who was asked. See docs/decisions/0017.
    roleEl.appendChild(el('span', 'who', who));
  }
  wrap.appendChild(roleEl);
  const body = el('div', 'body', '');
  wrap.appendChild(body);
  chatLog.appendChild(wrap);
  scroll();
  return { wrap, body };
}

// --- the conversation: one persistent stream, many turns over its life ----
//
// A conversation is household-shared and long-lived (docs/decisions/0017),
// so unlike the old per-turn stream(), there is exactly ONE EventSource per
// open conversation, and it is never closed except by switching to another
// conversation. Turn boundaries within that one stream are detected from the
// events themselves: a `user_message` whose turn_id differs from the current
// one starts a fresh agent bubble (a NEW turn); one with the SAME turn_id is
// an injected follow-up into the turn already running - see
// app/agent.py's _input_stream. `turn_done`/`turn_failed` end it.

function freshTurnUI(turnId) {
  return {
    turnId, finished: false,
    msg: null, working: null, tick: null, startedAt: null,
    toolLines: new Map(),   // tool_use id -> its .tool div - turn-scoped: a
                             // tool_result can land long after its run closed
    toolTargets: new Map(), // tool_use id -> its {kind, path}, if any
    forms: new Map(),       // request_id -> the ask/permission element
    agents: new Map(),      // agent key -> its <details> container
    lastAgentType: '',
    // currentBody/currentActivity: which of the turn's several blocks is
    // receiving new content right now. Exactly one is non-null at a time;
    // opening one closes the other, which is what keeps prose and activity
    // in true chronological order instead of two segregated columns.
    currentBody: null,      // the <div class="body"> currently receiving prose
    currentActivity: null,  // the <details class="activity"> currently open
    thought: null, todos: null, // reset on every new activity run - see below
    currentRaw: '',          // raw markdown source accumulated for currentBody
    newParaNext: true,      // insert a paragraph break before the next chunk
    hasTextDelta: false,    // true once the CLI streams at least one token
  };
}

// The most recent "you" (or someone else's) message bubble, so a following
// `attachment` event has somewhere to put its chip. Attachments only ever
// follow the ONE user_message that started a fresh turn - an injected
// message cannot carry them (app/main.py refuses that combination) - so
// "most recent" is unambiguous.
let lastYouBubble = null; // {wrap, body, filesBox}

function ensureTurnUI(turnId) {
  if (turnUI && turnUI.turnId === turnId) return turnUI;
  turnUI = freshTurnUI(turnId);
  return turnUI;
}

function updateStopButton() {
  stopBtn.hidden = !(turnUI && !turnUI.finished);
}

function startAgentBubble() {
  const msg = addMessage('agent', '');
  turnUI.msg = msg;
  // Pulsing indicator, with the elapsed time beside it. It stays for the
  // WHOLE turn and is removed only when it reaches a terminal state.
  //
  // It used to be removed on the first token instead, which read as
  // "finished" for the rest of the turn: after one sentence like "Reading
  // the CSV now." there could be minutes of real work - a long tool call, a
  // subagent, a long stretch of thinking - with nothing on screen moving.
  // The elapsed clock is the other half: it distinguishes "slow" from "hung"
  // without anyone having to guess.
  const working = el('div', 'working');
  const dots = el('div', 'thinking');
  for (let i = 0; i < 3; i++) dots.appendChild(el('span'));
  working.appendChild(dots);
  const elapsed = el('span', 'elapsed', '');
  working.appendChild(elapsed);
  msg.wrap.appendChild(working);
  turnUI.working = working;
  // The bubble addMessage() just built already has one <div class="body"> at
  // index 1 - reuse it for the turn's first stretch of prose rather than
  // creating a second one. A turn that never produces text before its first
  // tool call leaves it empty, which app.css hides (`.msg .body:empty`).
  turnUI.currentBody = msg.body;
  turnUI.currentRaw = '';
  turnUI.startedAt = Date.now();
  turnUI.tick = setInterval(() => {
    const s = Math.round((Date.now() - turnUI.startedAt) / 1000);
    elapsed.textContent = s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  }, 1000);
  updateStopButton();
}

function appendText(raw) {
  if (!turnUI || !turnUI.msg) return;
  if (!turnUI.currentBody) {
    // Prose resuming after a run of tool activity: open a fresh bubble BELOW
    // it rather than back-filling the one the run interrupted. This is the
    // whole point - the answer ends up at the bottom, where the reader
    // already is, instead of above everything the turn did to produce it.
    turnUI.currentBody = el('div', 'body', '');
    turnUI.msg.wrap.insertBefore(turnUI.currentBody, turnUI.working);
    turnUI.currentActivity = null;
    turnUI.currentRaw = '';
    turnUI.newParaNext = false;
  }
  const text = raw.replace(/\\n/g, '\n');
  if (turnUI.newParaNext) {
    if (turnUI.currentRaw) turnUI.currentRaw += '\n\n';
    turnUI.newParaNext = false;
  }
  turnUI.currentRaw += text;
  // Full re-parse on every chunk, not an append: marked needs the whole
  // markdown source to get block structure (lists, code fences, bold) right,
  // not just the newest fragment. sanitizeRenderedLinks/wireRelativeKbLinks
  // must re-run here too - each innerHTML replace wipes their prior work.
  renderMarkdownInto(turnUI.currentBody, turnUI.currentRaw);
  scroll();
}

// Structured events carry a JSON payload; text ones stay raw strings.
const parse = (e) => { try { return JSON.parse(e.data); } catch { return null; } };

// The run of steps currently receiving tool/thinking/subagent/todo activity.
// Opening one ends the current prose bubble (see appendText) and resets
// thought/todos, which are per-run rather than per-turn: thinking or a plan
// update that resumes after a stretch of prose belongs to a NEW collapsed
// run, not a backfill of one already closed.
function activitySteps() {
  if (!turnUI.currentActivity) {
    const d = el('details', 'activity');
    d.appendChild(el('summary', '', ''));
    d.appendChild(el('div', 'steps'));
    turnUI.msg.wrap.insertBefore(d, turnUI.working);
    turnUI.currentActivity = d;
    turnUI.currentBody = null;
    turnUI.thought = null;
    turnUI.todos = null;
  }
  return turnUI.currentActivity.querySelector('.steps');
}

function summariseActivity(label) {
  const d = turnUI.currentActivity;
  if (!d) return;
  const n = d.querySelector('.steps').children.length;
  d.querySelector('summary').textContent =
    n + (n === 1 ? ' step' : ' steps') + (label ? ' — ' + label : '');
}

// Insert into the current turn's message, keeping the working indicator last
// so it always sits below the newest thing that happened rather than above.
// `into` bypasses the activity run entirely - used only to nest a tool call
// or thought INSIDE a subagent's own box, which stays flat internally rather
// than growing its own collapsed sub-runs.
function insert(node, into, label) {
  if (!turnUI || !turnUI.msg) return;
  if (into) { into.appendChild(node); scroll(); return; }
  activitySteps().appendChild(node);
  summariseActivity(label);
  scroll();
}

// Some things must stay visible without anyone expanding a run: a question
// or permission request needs a click, and burying it behind a disclosure
// could strand the turn waiting on an answer nobody sees. These are appended
// as top-level siblings and close whatever activity run was open, so the
// next tool call (or piece of prose) starts fresh below them.
function insertTop(node) {
  if (!turnUI || !turnUI.msg) return;
  turnUI.currentActivity = null;
  turnUI.msg.wrap.insertBefore(node, turnUI.working);
  scroll();
}

// Subagent output must never land in the reply, which is why the server tags
// it. Containers are keyed by whatever tag the event carries: agent_start
// reports the SDK's agent id while message events report the Task call's
// tool_use id, and those are different identifiers, so with several subagents
// running at once attribution between blocks can be wrong. What cannot go
// wrong is subagent text reaching the main paragraph. Turn-scoped like
// toolLines, not per-run: a subagent's box must stay findable by key across
// however many activity runs the turn ends up split into.
function agentBox(key) {
  if (!turnUI || !key) return null;
  if (!turnUI.agents.has(key)) {
    const label = 'subagent' + (turnUI.lastAgentType ? ': ' + turnUI.lastAgentType : '');
    const d = el('details', 'subagent');
    d.appendChild(el('summary', '', label + ' — working'));
    insert(d, null, label);
    turnUI.agents.set(key, d);
  }
  return turnUI.agents.get(key);
}

function displayNameFor(email) {
  if (!email) return 'Someone';
  const local = email.split('@')[0];
  return local.charAt(0).toUpperCase() + local.slice(1);
}

function finishTurn(d, failed) {
  // Idempotent, and stale-guarded: a duplicate `turn_done`/`turn_failed`
  // (a replay overlapping the live tail) or one for a turn that is no longer
  // the current one must not touch a bubble that already finished, or worse,
  // the WRONG bubble.
  if (!d || !turnUI || turnUI.turnId !== d.turn_id || turnUI.finished) return;
  turnUI.finished = true;
  clearInterval(turnUI.tick);
  if (turnUI.working) turnUI.working.remove();
  if (failed) {
    turnUI.msg.wrap.appendChild(el('div', 'body err', d.error || 'the turn failed'));
  }
  linkifyKbPaths(turnUI.msg.wrap);
  // Every turn is wrapped in a TigerFS savepoint, so reverting is atomic —
  // and the undo is itself reversible.
  const btn = el('button', 'revert', 'Revert this turn');
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = 'Reverting…';
    const r = await fetch('/api/turns/' + d.turn_id + '/revert', { method: 'POST' });
    btn.textContent = r.ok ? 'Reverted' : 'Revert failed';
  };
  turnUI.msg.wrap.appendChild(btn);
  updateStopButton();
  scroll();
}

// --- the two round-trips back into the running turn -------------------
//
// Both post to the turn that is still executing, and both are disabled by
// the matching resolution event — which also arrives when someone else
// answered first, or when the request timed out on the server.

async function resolveForm(path, body, box, verdict) {
  if (!turnUI) return;
  box.classList.add('resolved');
  const r = await fetch('/api/turns/' + turnUI.turnId + '/' + path, {
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

function closeForm(e, timedOutNote, resolvedNote) {
  const d = parse(e); if (!d || !turnUI) return;
  const box = turnUI.forms.get(d.request_id);
  if (!box || box.classList.contains('resolved')) return;
  box.classList.add('resolved');
  let note = d.timeout ? timedOutNote : resolvedNote(d);
  // Named when it was not the person who was asked - either person may
  // answer a form either person can see. See docs/decisions/0017.
  if (!d.timeout && d.actor && d.actor !== myEmail) note += ` (by ${displayNameFor(d.actor)})`;
  box.appendChild(el('div', 'verdict', note));
  scroll();
}

const LABELS = { Bash: 'bash', Read: 'read', Write: 'write', Edit: 'edit',
                 Glob: 'glob', Grep: 'grep', WebSearch: 'web search',
                 WebFetch: 'web fetch', Task: 'delegate', TodoWrite: 'plan' };

function wireStreamHandlers(stream) {
  // Token-by-token streaming (requires --include-partial-messages support).
  stream.addEventListener('text_delta', (e) => {
    if (turnUI) turnUI.hasTextDelta = true;
    appendText(e.data);
  });
  // Full-turn text from AssistantMessage — used when streaming isn't
  // available, ignored if text_delta already delivered the content.
  stream.addEventListener('text', (e) => {
    if (turnUI && !turnUI.hasTextDelta) { turnUI.newParaNext = true; appendText(e.data); }
  });

  stream.addEventListener('agent_text', (e) => {
    const d = parse(e); if (!d) return;
    const box = agentBox(d.agent); if (!box) return;
    let p = box.querySelector('.agent-text');
    if (!p) { p = el('div', 'agent-text', ''); box.appendChild(p); box._raw = ''; }
    box._raw += (d.text || '').replace(/\\n/g, '\n');
    renderMarkdownInto(p, box._raw);
  });

  function thinkingInto() {
    if (!turnUI) return null;
    if (!turnUI.thought) {
      turnUI.thought = el('details', 'thought');
      turnUI.thought.appendChild(el('summary', '', 'thinking'));
      turnUI.thought.appendChild(el('div', 'text', ''));
      insert(turnUI.thought, null, 'thinking');
    }
    return turnUI.thought.querySelector('.text');
  }
  stream.addEventListener('thinking_delta', (e) => {
    const t = thinkingInto(); if (t) t.textContent += e.data.replace(/\\n/g, '\n');
  });
  stream.addEventListener('thinking', (e) => {
    if (turnUI && !turnUI.thought) {
      const t = thinkingInto(); if (t) t.textContent = e.data.replace(/\\n/g, '\n');
    }
  });

  // Two events arrive per tool call, both carrying the same id: one the
  // instant the call starts (name only, from content_block_start) and one
  // when the assistant message completes (with the arguments). The first is
  // what makes a slow tool visible at all; the second fills it in. Keyed by
  // id so the line is updated rather than drawn twice.
  stream.addEventListener('tool_use', (e) => {
    const d = parse(e); if (!d || !turnUI) return;
    turnUI.newParaNext = true;   // next text starts a new paragraph
    let t = d.id ? turnUI.toolLines.get(d.id) : null;
    if (!t) {
      const label = LABELS[d.name] || String(d.name || '').toLowerCase();
      t = el('div', 'tool', '→ ' + label + ' ');
      if (d.id) turnUI.toolLines.set(d.id, t);
      insert(t, agentBox(d.agent), label);
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
    if (d.id && d.target && d.target.kind) turnUI.toolTargets.set(d.id, d.target);
  });

  stream.addEventListener('tool_result', (e) => {
    const d = parse(e); if (!d || !turnUI) return;
    const line = turnUI.toolLines.get(d.id);
    if (line && !d.ok) {   // successes stay quiet; only failures speak up
      line.classList.add('failed');
      line.appendChild(el('span', 'detail', ' — failed' + (d.detail ? ': ' + d.detail : '')));
      // A failure must stay visible without anyone expanding the collapsed
      // run it happened inside - same reason .tool.failed exists on the line.
      const run = line.closest('details.activity');
      if (run) { run.classList.add('failed'); run.open = true; }
      scroll();
    }
    if (d.ok) {
      const target = turnUI.toolTargets.get(d.id);
      if (target) openInPane(target);
    }
  });

  stream.addEventListener('agent_start', (e) => {
    const d = parse(e); if (!d || !turnUI) return;
    turnUI.lastAgentType = d.agent_type || '';
    agentBox(d.agent_id);
  });
  stream.addEventListener('agent_stop', (e) => {
    const d = parse(e); if (!d || !turnUI) return;
    const box = turnUI.agents.get(d.agent_id);
    if (box) box.querySelector('summary').textContent =
      'subagent' + (d.agent_type ? ': ' + d.agent_type : '') + ' — done';
  });

  stream.addEventListener('todo', (e) => {
    const d = parse(e); if (!d || !turnUI || !Array.isArray(d.todos)) return;
    if (!turnUI.todos) { turnUI.todos = el('div', 'todos'); insert(turnUI.todos, null, 'plan'); }
    turnUI.todos.innerHTML = '';
    for (const t of d.todos) {
      const mark = t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '▸' : '·';
      const text = t.status === 'in_progress' ? (t.activeForm || t.content) : t.content;
      turnUI.todos.appendChild(el('div', 'item ' + (t.status || ''), mark + ' ' + text));
    }
    scroll();
  });

  // The message that starts (or steers) a turn - see the module comment
  // above. Rendered purely from the stream, for every person and every
  // reload alike: nothing is drawn client-side before this event confirms it
  // actually landed.
  stream.addEventListener('user_message', (e) => {
    const d = parse(e); if (!d) return;
    const isMe = d.actor === myEmail;
    const isNewTurn = !turnUI || turnUI.turnId !== d.turn_id;
    const { wrap, body } = addMessage(isMe ? 'you' : 'them', isMe ? 'me' : 'other',
                                       isMe ? null : displayNameFor(d.actor));
    if (d.text) {
      const p = document.createElement('p');
      p.textContent = d.text;
      body.appendChild(p);
    }
    // Images are never persisted server-side (only sent as base64 content
    // blocks), so unlike a file attachment there is nothing to re-render
    // after a reload — see docs/decisions/0017.
    if (d.images) body.appendChild(el('div', 'msg-images-note', '📎 image attached'));
    lastYouBubble = { wrap, body, filesBox: null };

    ensureTurnUI(d.turn_id);
    if (isNewTurn) startAgentBubble();
    else turnUI.newParaNext = true; // an injected message is a paragraph break too
  });

  stream.addEventListener('attachment', (e) => {
    const d = parse(e); if (!d || !lastYouBubble) return;
    if (!lastYouBubble.filesBox) {
      lastYouBubble.filesBox = el('div', 'msg-files');
      lastYouBubble.body.appendChild(lastYouBubble.filesBox);
    }
    const chip = el('div', 'file-chip clickable');
    chip.appendChild(el('span', 'name', d.name));
    chip.onclick = () => openUpload({ url: d.url, name: d.name });
    lastYouBubble.filesBox.appendChild(chip);
    scroll();
  });

  stream.addEventListener('ask', (e) => {
    const d = parse(e); if (!d || !turnUI) return;
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
      resolveForm('answer', { request_id: d.request_id, answers: picked, notes: other.value.trim() },
                  box, 'answered');
    };
    actions.appendChild(submit);
    box.appendChild(actions);
    turnUI.forms.set(d.request_id, box);
    // Never collapsed - this needs a click, and burying it behind a
    // disclosure could strand the turn waiting on an answer nobody sees.
    insertTop(box);
  });

  stream.addEventListener('permission', (e) => {
    const d = parse(e); if (!d || !turnUI) return;
    const box = el('div', 'perm');
    box.appendChild(el('div', 'q', d.title || ('Allow ' + d.tool + '?')));
    const sub = [d.description, d.detail, d.blocked_path, d.reason].filter(Boolean).join(' — ');
    if (sub) box.appendChild(el('div', 'sub', sub));

    const actions = el('div', 'actions');
    const allow = el('button', 'allow', 'Allow');
    const deny = el('button', 'deny', 'Deny');
    allow.onclick = () => resolveForm('permission',
      { request_id: d.request_id, decision: 'allow' }, box, 'allowed');
    deny.onclick = () => resolveForm('permission',
      { request_id: d.request_id, decision: 'deny' }, box, 'denied');
    actions.appendChild(allow);
    actions.appendChild(deny);
    box.appendChild(actions);
    turnUI.forms.set(d.request_id, box);
    insertTop(box);
  });

  stream.addEventListener('answered', (e) =>
    closeForm(e, 'nobody answered in time; the agent carried on', () => 'answered'));
  stream.addEventListener('permission_resolved', (e) =>
    closeForm(e, 'not approved in time; denied', (d) => d.decision === 'allow' ? 'allowed' : 'denied'));

  stream.addEventListener('turn_done', (e) => finishTurn(parse(e), false));
  stream.addEventListener('turn_failed', (e) => finishTurn(parse(e), true));

  // Auto-titling lands seconds after `turn_done`, from a background task -
  // see app/agent.py's _maybe_title_conversation. Only the picker option
  // needs updating; a full loadConversations() would reorder/flicker the
  // whole list for a change that touched exactly one row.
  stream.addEventListener('title', (e) => {
    const d = parse(e); if (!d || !d.title) return;
    const opt = conversationPicker.querySelector(`option[value="${CSS.escape(activeConversationId)}"]`);
    if (opt) opt.textContent = d.title;
  });

  stream.onerror = () => {
    // EventSource reconnects on its own and the server replays from
    // Last-Event-ID, so a transient drop needs no handling here. CLOSED
    // means the browser gave up for good (e.g. a fatal auth failure).
    if (stream.readyState === EventSource.CLOSED) {
      hint.textContent = 'Lost the connection to this conversation. Reload to reconnect.';
    }
  };
}

stopBtn.addEventListener('click', async () => {
  if (!turnUI || turnUI.finished) return;
  stopBtn.disabled = true;
  try {
    await fetch('/api/turns/' + turnUI.turnId + '/stop', { method: 'POST' });
  } finally {
    stopBtn.disabled = false;
  }
});

// --- the conversation list, and switching between conversations -----------

function initialConversationIdFromUrl() {
  const fromQuery = new URLSearchParams(location.search).get('c');
  if (fromQuery) return fromQuery;
  const m = location.pathname.match(/^\/c\/([^/]+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

function setConversationUrlParam(id) {
  const url = new URL(location.href);
  url.searchParams.set('c', id);
  history.replaceState({}, '', url.pathname + url.search + url.hash);
}

async function loadConversations() {
  try {
    const { conversations } = await (await fetch('/api/conversations')).json();
    conversationPicker.innerHTML = '';
    for (const c of conversations) {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.title || 'Untitled — ' + new Date(c.updated_at).toLocaleString();
      conversationPicker.appendChild(opt);
    }
    return conversations;
  } catch {
    return [];
  }
}

function openConversation(id) {
  if (es) { es.close(); es = null; }
  activeConversationId = id;
  turnUI = null;
  lastYouBubble = null;
  chatLog.innerHTML = '';
  stopBtn.hidden = true;
  setConversationUrlParam(id);
  if (!conversationPicker.querySelector(`option[value="${CSS.escape(id)}"]`)) {
    // A direct link to a conversation the list happened not to include yet
    // (e.g. one just created, or a bookmark) - <select>.value silently
    // ignores an id with no matching <option>, so give it one.
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = 'This conversation';
    conversationPicker.appendChild(opt);
  }
  conversationPicker.value = id;

  es = new EventSource('/api/conversations/' + id + '/events');
  wireStreamHandlers(es);
}

conversationPicker.addEventListener('change', () => {
  if (conversationPicker.value) openConversation(conversationPicker.value);
});

newChatBtn.addEventListener('click', async () => {
  const res = await fetch('/api/conversations', { method: 'POST' });
  if (!res.ok) return;
  const { conversation_id } = await res.json();
  await loadConversations();
  openConversation(conversation_id);
});

async function boot() {
  try {
    const me = await (await fetch('/api/me')).json();
    myEmail = me.email;
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

  // In parallel, not in sequence: the pane no longer needs the file list to
  // know what to show, so the reader gets their page without waiting on the
  // tree. An empty path is the workspace root, which is a directory view.
  loadFiles();
  openKbPath(initialKbPath || '', { push: false });

  let conversationList = await loadConversations();
  let target = initialConversationIdFromUrl() || (conversationList[0] && conversationList[0].id);
  if (!target) {
    const res = await fetch('/api/conversations', { method: 'POST' });
    if (res.ok) {
      target = (await res.json()).conversation_id;
      // The picker's <option>s come from this list - setting .value to an id
      // with no matching option is silently ignored by the browser, so a
      // freshly created conversation has to be reloaded into it before
      // openConversation() below can select it.
      conversationList = await loadConversations();
    }
  }
  if (target) openConversation(target);

  // Chat is the pane you came for; land there rather than on the tree, which
  // is what an unscrolled carousel would otherwise show. No smooth-scroll -
  // this is the initial position, not a navigation.
  if (MOBILE_QUERY.matches) layout.scrollLeft = layout.scrollWidth;
  updateActiveTab();
}

// --- composer: auto-grow, Enter-to-send, keyboard-safe viewport -----------

function resizeTextarea() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
}
input.addEventListener('input', resizeTextarea);

// Enter sends on a device with a real keyboard; on touch it stays a newline
// and Send is the only way to send, since an on-screen keyboard has no easy
// Shift+Enter. Read live rather than cached, so a tablet that gains a
// keyboard mid-session starts working immediately.
const ENTER_SENDS = matchMedia('(pointer: fine)');
input.addEventListener('keydown', (e) => {
  // isComposing / keyCode 229: an IME commits its candidate with Enter, and
  // swallowing that would send half a sentence.
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
  if (!ENTER_SENDS.matches) return;
  e.preventDefault();
  form.requestSubmit(); // not .submit() - that bypasses the submit handler
});
send.title = ENTER_SENDS.matches ? 'Enter to send · Shift+Enter for a new line' : 'Send';

// iOS does not shrink 100dvh for an on-screen keyboard, so the composer ends
// up hidden behind it without this. visualViewport is the one API that
// actually reports the keyboard.
if (window.visualViewport) {
  const fitToKeyboard = () => {
    document.documentElement.style.setProperty('--app-h', window.visualViewport.height + 'px');
    // iOS scrolls the outer (layout) viewport to reveal the focused field,
    // but the app is already sized to the visual viewport - that scroll is
    // pure damage on top of a fixed-height flex column.
    window.scrollTo(0, 0);
  };
  window.visualViewport.addEventListener('resize', fitToKeyboard);
  window.visualViewport.addEventListener('scroll', fitToKeyboard);
  fitToKeyboard();
}
// The keyboard opening can leave the log stranded mid-scroll; nudge it back
// to the bottom on focus, same as any other reason to autoscroll.
input.addEventListener('focus', scroll);

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  const images = pendingImages.slice();
  const files = pendingFiles.slice();
  if ((!text && !images.length && !files.length) || !activeConversationId) return;
  input.value = '';
  resizeTextarea();
  pendingImages = [];
  pendingFiles = [];
  previews.innerHTML = '';
  send.disabled = true;

  try {
    const res = await fetch('/api/conversations/' + activeConversationId + '/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        images: images.map(i => ({ media_type: i.mediaType, data: i.base64 })),
        files: files.map(f => ({ name: f.name, data: f.base64 })),
      }),
    });
    if (!res.ok) {
      // Surface the server's own reason. A rejected attachment answers 400
      // or 413 with a detail that names the file, and a 409 names who is
      // busy - "submit failed: 409" alone sends the user hunting for a
      // problem the server already described.
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch {}
      throw new Error(detail || 'submit failed: ' + res.status);
    }
    // Nothing to render here: the `user_message` event on the stream is what
    // draws the bubble, for this sender exactly the same as for anyone else.
  } catch (err) {
    // Put the composer back so nothing pasted or typed is lost. Re-added
    // through the same helpers rather than by reassigning the arrays, so
    // each restored chip gets its remove button wired up again. The text is
    // only restored if the box is still empty - recovering the old message
    // must not destroy a newer one typed while the request was in flight.
    if (!input.value) input.value = text;
    resizeTextarea();
    for (const i of images) addImagePreview(i.dataUrl, i.mediaType, i.base64);
    for (const f of files) addFilePreview(f.name, f.size, f.base64);
    addMessage('error', '').body.classList.add('err');
    chatLog.lastChild.querySelector('.body').textContent = String(err);
  } finally {
    send.disabled = false;
  }
});

boot();
