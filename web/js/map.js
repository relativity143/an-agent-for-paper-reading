// 概念地图（docs/agent-architecture.md 4.8 节）。
// 把这篇论文的概念记录画成一张图：装饰性的地图作底，每个概念是一个红色立体三角。
// 三角、连线和路线都从概念记录和讲解历史算出来；要保存的只有位置和批注。
// 底图只是装饰，位置没有含义。概念名和批注来自模型或读者，只当作文字显示。

import { renderInline, typeset } from './rich.js';

const WIDTH = 1440;
const HEIGHT = 900;
const MARGIN = { left: 70, right: 200, top: 120, bottom: 60 };
const GAP = 150;          // 两个三角之间至少隔这么远，标签才不容易叠在一起
const MAX_NOTE = 300;
const MAX_NOTES = 60;
const STATUS_LABEL = { explained: '讲过', unsure: '还不懂', known: '已经会', untouched: '还没碰过' };

const key = (name) => name.trim().toLowerCase();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function rich(tag, className, text) {
  const node = el(tag, className);
  node.innerHTML = renderInline(text);
  typeset(node);
  return node;
}

// 立体三角：左面亮、右面暗，下面一小片影子。四种画法对应四种状态。
function triangle(status, size) {
  const solid = status === 'known' || status === 'unsure' || status === 'untouched';
  const left = solid ? 'fill="#D9362C"' : 'fill="#FBF1E4" stroke="#A3212B" stroke-width="1.6" stroke-linejoin="round"';
  const right = solid ? 'fill="#8F1C17"' : 'fill="#EBCFC0" stroke="#A3212B" stroke-width="1.6" stroke-linejoin="round"';
  const shadow = status === 'untouched' ? '' : '<ellipse cx="15" cy="27" rx="12" ry="3" fill="#1B2A33" fill-opacity="0.3"></ellipse>';
  const badge = status === 'unsure'
    ? '<circle cx="23" cy="6" r="6" fill="#FFD766" stroke="#3D2B00" stroke-width="1"></circle>'
      + '<path d="M 21,4.6 C 21,2.4 25,2.4 25,4.8 C 25,6.4 23,6.2 23,7.8" fill="none" stroke="#3D2B00" stroke-width="1.3" stroke-linecap="round"></path>'
      + '<circle cx="23" cy="9.6" r="0.8" fill="#3D2B00"></circle>'
    : '';
  const width = size;
  const height = Math.round(size * 30 / 28);
  return `<svg class="map-tri" width="${width}" height="${height}" viewBox="0 0 28 30" aria-hidden="true">${shadow}`
    + `<path d="M 14,2 L 2,23 L 15,28 Z" ${left}></path><path d="M 14,2 L 15,28 L 26,23 Z" ${right}></path>${badge}</svg>`;
}

// 从概念记录和讲解历史算出图上的点、细线和路线。
function buildGraph(record, explanations) {
  const nodes = new Map(); // 小写名字 → {name, status, where, note, latest}
  const edges = new Map();
  const route = [];
  const add = (name, extra) => {
    if (typeof name !== 'string' || !name.trim()) return null;
    const id = key(name);
    if (!nodes.has(id)) nodes.set(id, { name: name.trim(), status: 'untouched', where: null, note: null, latest: false, ...extra });
    return id;
  };
  const link = (a, b) => {
    if (!a || !b || a === b) return;
    const id = a < b ? `${a}\n${b}` : `${b}\n${a}`;
    edges.set(id, [a, b]);
  };
  for (const concept of record.concepts) {
    add(concept.name, { status: concept.status, where: concept.where || null, note: concept.note || null });
  }
  let latest = [];
  for (const x of explanations) {
    if (x.type === 'overview') {
      for (const item of x.foundations || []) add(item && item.name);
      continue;
    }
    const expanded = (x.concepts || []).map((c) => add(c && c.name)).filter(Boolean);
    const assumed = (x.assumes || []).map((a) => add(a && a.concept)).filter(Boolean);
    const mentioned = (x.unexpanded || []).map((name) => add(name)).filter(Boolean);
    for (const a of assumed) for (const c of expanded) link(a, c);
    for (let i = 1; i < expanded.length; i += 1) link(expanded[i - 1], expanded[i]);
    const anchor = expanded[expanded.length - 1] || assumed[assumed.length - 1];
    for (const m of mentioned) link(anchor, m);
    // 路线只走真的讲过的概念（在记录里的）。
    for (const c of expanded) {
      if (nodes.get(c).status !== 'untouched' && route[route.length - 1] !== c) route.push(c);
    }
    if (expanded.length) latest = expanded;
  }
  for (const id of latest) nodes.get(id).latest = true;
  return { nodes, edges: [...edges.values()], route };
}

// 名字决定的伪随机数：同一个概念每次算出来的初始位置一样。
function seeded(name) {
  let h = 2166136261;
  for (const ch of name) { h ^= ch.codePointAt(0); h = Math.imul(h, 16777619); }
  return () => {
    h ^= h << 13; h ^= h >>> 17; h ^= h << 5;
    return ((h >>> 0) % 10000) / 10000;
  };
}

// 给还没有位置的概念找地方：靠近已经放好的邻居，和别的三角保持距离。已有的位置不动。
function placeNew(graph, positions) {
  const clampX = (x) => Math.min(WIDTH - MARGIN.right, Math.max(MARGIN.left, x));
  const clampY = (y) => Math.min(HEIGHT - MARGIN.bottom, Math.max(MARGIN.top, y));
  const neighbours = new Map();
  for (const [a, b] of graph.edges) {
    if (!neighbours.has(a)) neighbours.set(a, []);
    if (!neighbours.has(b)) neighbours.set(b, []);
    neighbours.get(a).push(b);
    neighbours.get(b).push(a);
  }
  let last = null;
  for (const p of positions.values()) last = p;
  let added = false;
  for (const [id, node] of graph.nodes) {
    if (positions.has(id)) continue;
    const random = seeded(node.name);
    const near = (neighbours.get(id) || []).map((n) => positions.get(n)).filter(Boolean);
    const anchor = near.length
      ? { x: near.reduce((s, p) => s + p.x, 0) / near.length, y: near.reduce((s, p) => s + p.y, 0) / near.length }
      : last || { x: 420, y: 560 };
    const start = random() * Math.PI * 2;
    let best = null;
    let bestScore = -Infinity;
    for (let step = 0; step < 220 && bestScore < GAP; step += 1) {
      const radius = GAP + step * 8;
      const angle = start + step * 2.399963; // 黄金角：一圈一圈往外找
      const candidate = { x: clampX(anchor.x + Math.cos(angle) * radius), y: clampY(anchor.y + Math.sin(angle) * radius * 0.72) };
      let nearest = Infinity;
      for (const p of positions.values()) nearest = Math.min(nearest, Math.hypot(p.x - candidate.x, p.y - candidate.y));
      if (nearest > bestScore) { bestScore = nearest; best = candidate; }
    }
    const spot = { x: Math.round(best.x), y: Math.round(best.y) };
    positions.set(id, spot);
    last = spot;
    added = true;
  }
  return added;
}

// api() 返回接口对象；paper() 返回论文编号；title() 返回论文标题；
// record() 返回当前的概念记录（没有这个功能时是 null）；explanations() 返回按顺序排好的讲解；
// jump(where) 让左边的原文跳到一个位置，跳成了返回 true；openRecord() 打开概念记录。
export function createMap({ api, paper, title, record, explanations, jump, openRecord }) {
  const button = document.getElementById('map-btn');
  const dialog = document.getElementById('map-dialog');
  const stage = document.getElementById('map-stage');
  const frame = document.getElementById('map-frame');
  const status = document.getElementById('map-status');
  let layout = { version: 0, nodes: [], notes: [] };
  let positions = new Map(); // 小写名字 → {x, y}
  let names = new Map();     // 小写名字 → 保存时用的名字
  let remote = false;        // 位置存在后端，还是存在这台电脑的浏览器里
  let scale = 1;
  let saveTimer = 0;
  let loaded = null;         // 已经读过哪篇论文的地图

  const localKey = () => `paperread-map:${paper()}`;

  async function load() {
    if (loaded === paper()) return;
    layout = { version: 0, nodes: [], notes: [] };
    remote = false;
    const source = api();
    let fetched = null;
    if (source && source.getMap) {
      try { fetched = await source.getMap(paper()); } catch { fetched = null; }
    }
    if (fetched && Array.isArray(fetched.nodes) && Array.isArray(fetched.notes)) {
      layout = fetched;
      remote = true;
    } else {
      // 后端还没有这个接口：位置和批注存在这台电脑的浏览器里。
      try {
        const saved = JSON.parse(localStorage.getItem(localKey()));
        if (saved && Array.isArray(saved.nodes) && Array.isArray(saved.notes)) layout = { version: 0, ...saved };
      } catch { /* 读不到就从空的开始 */ }
    }
    positions = new Map();
    names = new Map();
    for (const node of layout.nodes) {
      if (typeof node.name !== 'string' || !Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
      positions.set(key(node.name), { x: node.x, y: node.y });
      names.set(key(node.name), node.name);
    }
    layout.notes = layout.notes.filter((n) => n && typeof n.id === 'string' && typeof n.text === 'string');
    loaded = paper();
  }

  async function persist() {
    const body = {
      nodes: [...positions].map(([id, p]) => ({ name: names.get(id) || id, x: Math.round(p.x), y: Math.round(p.y) })),
      notes: layout.notes.map((n) => ({ id: n.id, x: Math.round(n.x), y: Math.round(n.y), text: n.text })),
    };
    if (!remote) {
      try { localStorage.setItem(localKey(), JSON.stringify(body)); status.textContent = ''; } catch { status.textContent = '位置没能存下来（浏览器不让保存）。'; }
      return;
    }
    try {
      const saved = await api().saveMap(paper(), layout.version, body.nodes, body.notes);
      layout.version = saved.version;
      status.textContent = '';
    } catch (error) {
      if (error.status === 409) {
        // 别处改过：读到最新的再画一遍，这一次的改动不强行盖上去。
        loaded = null;
        await load();
        draw();
        status.textContent = '地图在别处改过了，已经读到最新的；刚才那一下没有保存，请再拖一次。';
      } else {
        status.textContent = error.message || '位置没能保存。';
      }
    }
  }

  // 拖动时会连着触发很多次，等手停下来再存。
  function saveSoon() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { saveTimer = 0; persist(); }, 500);
  }

  function fit() {
    const box = frame.getBoundingClientRect();
    if (!box.width || !box.height) return;
    scale = Math.min(box.width / WIDTH, box.height / HEIGHT);
    stage.style.transform = `translate(${(box.width - WIDTH * scale) / 2}px, ${(box.height - HEIGHT * scale) / 2}px) scale(${scale})`;
  }

  // 让一个元素可以拖。moved(x, y) 在位置变化时调用，done(wasDrag) 在松手时调用。
  function draggable(node, get, moved, done) {
    node.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || event.target.closest('textarea, button, .map-info')) return;
      const origin = get();
      const startX = event.clientX;
      const startY = event.clientY;
      let dragging = false;
      try { node.setPointerCapture(event.pointerId); } catch { /* 抓不住时仍然能拖 */ }
      const move = (e) => {
        const dx = (e.clientX - startX) / scale;
        const dy = (e.clientY - startY) / scale;
        if (!dragging && Math.hypot(dx, dy) < 4) return;
        dragging = true;
        node.classList.add('dragging');
        moved(Math.min(WIDTH - 10, Math.max(10, origin.x + dx)), Math.min(HEIGHT - 10, Math.max(20, origin.y + dy)));
      };
      const stop = () => {
        node.removeEventListener('pointermove', move);
        node.removeEventListener('pointerup', stop);
        node.removeEventListener('pointercancel', stop);
        node.classList.remove('dragging');
        done(dragging);
      };
      node.addEventListener('pointermove', move);
      node.addEventListener('pointerup', stop);
      node.addEventListener('pointercancel', stop);
    });
  }

  function draw() {
    const current = record();
    if (!current) return;
    const graph = buildGraph(current, explanations());
    for (const [id, node] of graph.nodes) names.set(id, node.name);
    const added = placeNew(graph, positions);

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${WIDTH} ${HEIGHT}`);
    svg.setAttribute('class', 'map-lines');
    svg.setAttribute('aria-hidden', 'true');
    function drawLines() {
      const seg = (a, b) => `M ${positions.get(a).x},${positions.get(a).y} L ${positions.get(b).x},${positions.get(b).y}`;
      const thin = graph.edges.map(([a, b]) => seg(a, b)).join(' ');
      const path = graph.route.length > 1
        ? `M ${graph.route.map((id) => `${positions.get(id).x},${positions.get(id).y}`).join(' L ')}`
        : '';
      svg.innerHTML = `<path d="${thin}" fill="none" stroke="#FBF8EE" stroke-opacity="0.75" stroke-width="4.5" stroke-linecap="round"></path>`
        + `<path d="${thin}" fill="none" stroke="#A3212B" stroke-width="1.7" stroke-linecap="round"></path>`
        + (path ? `<path d="${path}" fill="none" stroke="#FBF8EE" stroke-width="9" stroke-linecap="round" stroke-linejoin="round"></path>`
          + `<path d="${path}" fill="none" stroke="#C2261F" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"></path>` : '');
    }
    drawLines();

    const layer = el('div', 'map-layer');
    const here = graph.route[graph.route.length - 1];
    for (const [id, node] of graph.nodes) {
      const spot = positions.get(id);
      const size = node.latest ? 38 : node.status === 'untouched' ? 22 : 28;
      const item = el('div', `map-node map-${node.status}${node.latest ? ' map-latest' : ''}${spot.x > WIDTH - 330 ? ' map-flip' : ''}`);
      item.style.left = `${spot.x}px`;
      item.style.top = `${spot.y}px`;
      item.tabIndex = 0;
      item.setAttribute('role', 'button');
      item.setAttribute('aria-label', `${node.name}，${STATUS_LABEL[node.status]}`);
      item.innerHTML = triangle(node.status, size);
      item.append(rich('span', 'map-label', node.name));
      if (id === here) item.append(el('span', 'map-here', '你在这里'));
      const select = () => showInfo(node, item);
      draggable(item, () => positions.get(id), (x, y) => {
        positions.set(id, { x, y });
        item.style.left = `${x}px`;
        item.style.top = `${y}px`;
        item.classList.toggle('map-flip', x > WIDTH - 330);
        drawLines();
      }, (wasDrag) => { if (wasDrag) saveSoon(); else select(); });
      item.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
      layer.append(item);
    }

    for (const note of layout.notes) layer.append(noteCard(note));

    const empty = graph.nodes.size === 0
      ? el('p', 'map-empty', '还没有概念。每讲完一轮，讲过的概念会出现在这张图上。')
      : null;
    stage.replaceChildren(...[el('div', 'map-wash'), svg, layer, cartouche(), legend(), empty].filter(Boolean));
    if (added) saveSoon();
    fit();
  }

  function cartouche() {
    const box = el('div', 'map-title');
    box.append(el('div', 'map-title-name', title() || '概念地图'));
    box.append(el('div', 'map-title-note', '底图只是装饰，位置没有含义。三角可以拖动。'));
    return box;
  }

  function legend() {
    const box = el('div', 'map-legend');
    box.append(el('h3', '', '图例'));
    for (const [state, text] of [['known', '已经会：实心'], ['explained', '讲过：只有轮廓'], ['unsure', '还不懂：带黄色问号'], ['untouched', '还没碰过：小而半透明']]) {
      const row = el('div', `map-legend-row map-${state}`);
      row.innerHTML = triangle(state, 20);
      row.append(el('span', '', text));
      box.append(row);
    }
    const thin = el('div', 'map-legend-row');
    thin.append(el('span', 'map-legend-thin'), el('span', '', '细线：在同一轮讲解里一起出现'));
    const thick = el('div', 'map-legend-row');
    thick.append(el('span', 'map-legend-thick'), el('span', '', '粗线：这篇论文走过的路'));
    box.append(thin, thick);
    return box;
  }

  // 点一个三角：看它的状态、备注，跳到讲它的地方。
  function showInfo(node, anchor) {
    stage.querySelectorAll('.map-info').forEach((n) => n.remove());
    const box = el('div', `map-info${anchor.classList.contains('map-flip') ? ' map-info-flip' : ''}`);
    box.append(rich('strong', '', node.name), el('span', 'map-info-status', STATUS_LABEL[node.status]));
    if (node.note) box.append(el('p', '', node.note));
    if (node.status === 'untouched') box.append(el('p', '', '讲解里提到过，还没有展开讲。想知道的话可以直接提问。'));
    const actions = el('div', 'map-info-actions');
    if (node.where) {
      const go = el('button', 'btn btn-small', `看原文 ${node.where}`);
      go.type = 'button';
      go.addEventListener('click', () => { if (jump(node.where)) dialog.close(); });
      actions.append(go);
    }
    const edit = el('button', 'btn btn-small', '在记录里改');
    edit.type = 'button';
    edit.addEventListener('click', () => { dialog.close(); openRecord(); });
    const close = el('button', 'icon-btn', '×');
    close.type = 'button';
    close.setAttribute('aria-label', '关上');
    close.addEventListener('click', () => box.remove());
    actions.append(edit, close);
    box.append(actions);
    anchor.append(box);
  }

  // 批注：贴在图上的小纸条，可以拖、可以改、可以撕掉。
  function noteCard(note) {
    const card = el('div', 'map-note');
    card.style.left = `${note.x}px`;
    card.style.top = `${note.y}px`;
    const text = el('textarea');
    text.maxLength = MAX_NOTE;
    text.value = note.text;
    text.rows = 3;
    text.setAttribute('aria-label', '批注');
    text.placeholder = '写点什么…';
    text.addEventListener('input', () => { note.text = text.value; });
    text.addEventListener('change', () => {
      // 空的批注不保存：留着它在图上，等写了字再存。
      if (note.text.trim()) saveSoon();
    });
    const remove = el('button', 'icon-btn map-note-remove', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', '撕掉这张批注');
    remove.addEventListener('click', () => {
      layout.notes = layout.notes.filter((n) => n !== note);
      card.remove();
      saveSoon();
    });
    card.append(remove, text);
    draggable(card, () => ({ x: note.x, y: note.y }), (x, y) => {
      note.x = x; note.y = y;
      card.style.left = `${x}px`;
      card.style.top = `${y}px`;
    }, (wasDrag) => { if (wasDrag && note.text.trim()) saveSoon(); });
    return card;
  }

  function addNote() {
    if (layout.notes.length >= MAX_NOTES) { status.textContent = `批注最多 ${MAX_NOTES} 张。`; return; }
    const count = layout.notes.length;
    const note = { id: `n-${Date.now().toString(36)}-${count}`, x: 520 + (count % 5) * 36, y: 300 + (count % 5) * 30, text: '' };
    layout.notes.push(note);
    const card = noteCard(note);
    stage.querySelector('.map-layer').append(card);
    card.querySelector('textarea').focus();
  }

  async function open() {
    if (!record()) return;
    status.textContent = '';
    dialog.showModal();
    await load();
    // 没写字的批注不带到下一次。
    layout.notes = layout.notes.filter((n) => n.text.trim());
    draw();
  }

  button.addEventListener('click', open);
  document.getElementById('map-add-note').addEventListener('click', addNote);
  dialog.addEventListener('click', (event) => {
    if (event.target.closest('[data-close]')) dialog.close();
  });
  dialog.addEventListener('close', () => {
    // 关上时把还没存的改动存掉。
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = 0; persist(); }
  });
  new ResizeObserver(fit).observe(frame);

  return {
    // 概念记录有没有这个功能，决定“地图”按钮出不出现。
    sync() { button.hidden = !record(); },
  };
}
