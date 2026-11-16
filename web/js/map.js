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

// 无效响应不能当成“还没有保存过”，否则下一次拖动可能覆盖另一份地图。
function checkedLayout(value) {
  const fail = () => { throw new Error('地图数据不完整，原数据未被覆盖。请重新读取。'); };
  if (!value || !Number.isSafeInteger(value.version) || value.version < 0
    || !Array.isArray(value.nodes) || value.nodes.length > 400
    || !Array.isArray(value.notes) || value.notes.length > MAX_NOTES) fail();
  const names = new Set(), notes = new Set();
  const coordinates = (item) => item && Number.isInteger(item.x) && item.x >= 0 && item.x <= WIDTH
    && Number.isInteger(item.y) && item.y >= 0 && item.y <= HEIGHT;
  for (const node of value.nodes) {
    if (!coordinates(node) || typeof node.name !== 'string' || !node.name.trim()
      || Array.from(node.name.trim()).length > 80 || names.has(key(node.name))) fail();
    names.add(key(node.name));
  }
  for (const note of value.notes) {
    if (!coordinates(note) || typeof note.id !== 'string' || !/^[A-Za-z0-9_-]{1,40}$/.test(note.id)
      || notes.has(note.id) || typeof note.text !== 'string' || !note.text.trim()
      || Array.from(note.text).length > MAX_NOTE) fail();
    notes.add(note.id);
  }
  return structuredClone(value);
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
    if (!nodes.has(id)) nodes.set(id, { name: name.trim(), status: 'untouched', where: null, note: null, latest: false, step: null, ...extra });
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
    // 这个概念最早是在主线的哪一步讲的：用来给地图分区。
    const step = x.progress && Number.isInteger(x.progress.step) ? x.progress.step : null;
    for (const c of expanded) if (step !== null && nodes.get(c).step === null) nodes.get(c).step = step;
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
export function createMap({ api, paper, title, record, explanations, steps, jump, openRecord }) {
  const button = document.getElementById('map-btn');
  const dialog = document.getElementById('map-dialog');
  const stage = document.getElementById('map-stage');
  const frame = document.getElementById('map-frame');
  const status = document.getElementById('map-status');
  const addNoteButton = document.getElementById('map-add-note');
  const retryButton = document.getElementById('map-save-retry');
  const reloadButton = document.getElementById('map-reload');
  let layout = { version: 0, nodes: [], notes: [] };
  let positions = new Map(); // 小写名字 → {x, y}
  let names = new Map();     // 小写名字 → 保存时用的名字
  let remote = false;        // 位置存在后端，还是存在这台电脑的浏览器里
  let scale = 1;
  let saveTimer = 0;
  let loaded = null;         // 已经读过哪篇论文的地图
  let loading = false, loadError = false, saving = false;
  let dirty = false, saveFailed = false, conflict = false, revision = 0;

  const localKey = () => `paperread-map:${paper()}`;
  const editable = () => loaded === paper() && !loading && !loadError && !conflict;

  function controls() {
    stage.inert = loaded !== paper() || loading;
    // 发生冲突或读取失败时仍可选中复制批注，但先停止编辑和拖动。
    stage.querySelectorAll('.map-note textarea').forEach((text) => { text.readOnly = !editable(); });
    stage.querySelectorAll('.map-note-remove').forEach((button) => { button.disabled = !editable(); });
    addNoteButton.disabled = !editable();
    retryButton.hidden = !dirty || !saveFailed || conflict || loadError;
    retryButton.disabled = saving || loading;
    reloadButton.disabled = saving || loading;
    reloadButton.textContent = dirty ? '放弃改动并重新读取' : '重新读取地图';
  }

  async function load(force = false) {
    if (loading || saving) return false;
    if (!force && loaded === paper() && !loadError) return true;
    const target = paper();
    loading = true; controls();
    status.textContent = '正在读取地图…';
    try {
      const source = api();
      // 真实接口仅在 404 时返回 null；网络失败或损坏必须向读者报错。
      const fetched = source?.getMap ? await source.getMap(target) : null;
      let candidate;
      const useRemote = fetched !== null;
      if (useRemote) candidate = checkedLayout(fetched);
      else {
        const saved = localStorage.getItem(`paperread-map:${target}`);
        const legacy = saved === null ? { version: 0, nodes: [], notes: [] } : { version: 0, ...JSON.parse(saved) };
        // 旧页面曾把尚未填写的纸条一起存在浏览器里；它们没有正文，按原行为忽略。
        // 只兼容此历史字段，不修复远程响应，也不重写浏览器原始数据。
        if (Array.isArray(legacy.notes)) legacy.notes = legacy.notes.filter((note) => typeof note?.text !== 'string' || note.text.trim());
        candidate = checkedLayout(legacy);
      }
      if (target !== paper()) return false;
      // 验证成功后才换图；读取失败保留当前可见的草稿和原存储。
      layout = candidate; remote = useRemote;
      positions = new Map(layout.nodes.map((node) => [key(node.name), { x: node.x, y: node.y }]));
      names = new Map(layout.nodes.map((node) => [key(node.name), node.name]));
      loaded = target;
      dirty = false; saveFailed = false; conflict = false; loadError = false;
      status.textContent = remote ? '地图已读取' : '保存在此浏览器';
      return true;
    } catch (error) {
      loadError = true;
      status.textContent = `${error.message || '地图读取失败。'} 当前内容已保留，请重新读取。`;
      return false;
    } finally {
      loading = false; controls();
    }
  }

  async function persist() {
    if (!editable() || !dirty || saving || saveFailed) return;
    clearTimeout(saveTimer); saveTimer = 0;
    const target = paper(), sentRevision = revision;
    const body = {
      nodes: [...positions].map(([id, p]) => ({ name: names.get(id) || id, x: Math.round(p.x), y: Math.round(p.y) })),
      notes: layout.notes.filter((n) => n.text.trim()).map((n) => ({ id: n.id, x: Math.round(n.x), y: Math.round(n.y), text: n.text })),
    };
    saving = true; controls(); status.textContent = '正在保存地图…';
    try {
      // 同一时间只发一份。响应返回后，用它的新版本保存期间发生的后续编辑。
      if (remote) {
        const saved = checkedLayout(await api().saveMap(target, layout.version, body.nodes, body.notes));
        layout.version = saved.version;
      } else {
        checkedLayout({ version: 0, ...body });
        localStorage.setItem(localKey(), JSON.stringify(body));
      }
      dirty = revision !== sentRevision;
      status.textContent = dirty ? '还有改动待保存…' : remote ? '地图已保存' : '已保存在此浏览器';
    } catch (error) {
      saveFailed = true;
      clearTimeout(saveTimer); saveTimer = 0;
      if (error.status === 409) {
        conflict = true;
        status.textContent = '地图在别处改过了，本页改动尚未保存。请先保留需要的批注，再重新读取最新地图。';
      } else {
        status.textContent = `${error.message || '保存失败。'} 尚未确认保存，本页改动已保留，请重试。`;
      }
    } finally {
      saving = false; controls();
      if (dirty && !saveFailed && !conflict) await persist();
    }
  }

  // 拖动时会连着触发很多次，等手停下来再存。
  function saveSoon() {
    dirty = true; revision += 1;
    clearTimeout(saveTimer);
    controls();
    if (saveFailed || conflict || loadError) return;
    status.textContent = saving ? '正在保存，后续改动等待保存…' : '有改动待保存…';
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
      if (!editable() || event.button !== 0 || event.target.closest('textarea, button, .map-info')) return;
      const origin = get();
      const startX = event.clientX;
      const startY = event.clientY;
      let dragging = false;
      try { node.setPointerCapture(event.pointerId); } catch { /* 抓不住时仍然能拖 */ }
      const move = (e) => {
        if (!editable()) return;
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
    const regions = el('div', 'map-regions');
    const here = graph.route[graph.route.length - 1];
    const drawn = []; // 每个三角：{id, node, item, label, size}

    // 分区标题：同一步里讲的概念（两个以上）上方写这一步的名字。跟着三角的位置算，不保存。
    function drawRegions() {
      const names = new Map((steps() || []).map((s) => [s.step, s.name]));
      const groups = new Map();
      for (const entry of drawn) {
        if (entry.node.step === null || entry.node.status === 'untouched' || !names.has(entry.node.step)) continue;
        if (!groups.has(entry.node.step)) groups.set(entry.node.step, []);
        groups.get(entry.node.step).push(positions.get(entry.id));
      }
      const titles = [];
      for (const [step, spots] of groups) {
        if (spots.length < 2) continue;
        const text = names.get(step);
        const label = el('div', 'map-region', Array.from(text).length > 14 ? `${Array.from(text).slice(0, 14).join('')}…` : text);
        const x = spots.reduce((sum, p) => sum + p.x, 0) / spots.length;
        const y = Math.min(...spots.map((p) => p.y)) - 92;
        label.style.left = `${Math.min(WIDTH - 220, Math.max(180, x))}px`;
        label.style.top = `${Math.max(120, y)}px`;
        titles.push(label);
      }
      regions.replaceChildren(...titles);
    }

    // 给每个标签挑位置：在三角四周的八个位置里选压着别人最少的一个。实在放不下的小标签先收起，鼠标移上去再显示。
    function arrangeLabels() {
      const boxes = [
        { x: 0, y: 0, w: 470, h: 122 },                 // 左上角的标题
        { x: WIDTH - 290, y: 0, w: 290, h: 270 },       // 右上角的图例
      ];
      for (const entry of drawn) {
        const spot = positions.get(entry.id);
        const tall = entry.size * 30 / 28;
        boxes.push({ x: spot.x - entry.size / 2, y: spot.y - tall * 0.87, w: entry.size, h: tall });
        if (entry.id === here) boxes.push({ x: spot.x - 40, y: spot.y + 6, w: 84, h: 28 });
      }
      const overlap = (a, b) => Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x))
        * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
      const rank = (entry) => (entry.node.latest ? 0 : entry.node.status === 'untouched' ? 2 : 1);
      for (const entry of [...drawn].sort((a, b) => rank(a) - rank(b))) {
        const spot = positions.get(entry.id);
        const w = entry.label.offsetWidth;
        const h = entry.label.offsetHeight;
        const r = entry.size / 2 + 4;
        const tall = entry.size * 30 / 28 * 0.87;
        const options = [
          [r, -h - 2], [-r - w, -h - 2],                 // 右、左
          [r, -tall - h * 0.6], [r, 2],                  // 右上、右下
          [-r - w, -tall - h * 0.6], [-r - w, 2],        // 左上、左下
          [-w / 2, -tall - h - 4], [-w / 2, 8],          // 正上、正下
        ];
        let best = options[0];
        let bestCost = Infinity;
        let bestHit = 0;
        options.forEach(([dx, dy], index) => {
          const box = { x: spot.x + dx, y: spot.y + dy, w, h };
          let hit = 0;
          for (const other of boxes) hit += overlap(box, other);
          const outside = w * h - overlap(box, { x: 4, y: 4, w: WIDTH - 8, h: HEIGHT - 8 });
          const cost = hit + outside * 4 + index * 12;
          if (cost < bestCost) { bestCost = cost; best = [dx, dy]; bestHit = hit + outside; }
        });
        entry.label.style.left = `${Math.round(best[0])}px`;
        entry.label.style.top = `${Math.round(best[1])}px`;
        entry.label.style.right = 'auto';
        const crowded = entry.node.status === 'untouched' && bestHit > w * h * 0.3;
        entry.item.classList.toggle('map-quiet', crowded);
        if (!crowded) boxes.push({ x: spot.x + best[0], y: spot.y + best[1], w, h });
      }
    }
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
      }, (wasDrag) => {
        if (!wasDrag) { select(); return; }
        // 松手后别的标签让一让，分区标题跟着挪。
        arrangeLabels();
        drawRegions();
        saveSoon();
      });
      item.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } });
      layer.append(item);
      drawn.push({ id, node, item, label: item.querySelector('.map-label'), size });
    }

    for (const note of layout.notes) layer.append(noteCard(note));

    const empty = graph.nodes.size === 0
      ? el('p', 'map-empty', '还没有概念。每讲完一轮，讲过的概念会出现在这张图上。')
      : null;
    stage.replaceChildren(...[el('div', 'map-wash'), regions, svg, layer, cartouche(), legend(), empty].filter(Boolean));
    // 要等标签进了页面才量得出宽度。
    arrangeLabels();
    drawRegions();
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
    const region = el('div', 'map-legend-row');
    region.append(el('span', 'map-legend-region', '大字'), el('span', '', '这些概念是在主线的哪一步讲的'));
    box.append(thin, thick, region);
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
    // 输入时登记改动，关图会立即冲掉等待计时；空纸条不发给后端。
    text.addEventListener('input', () => { if (editable()) { note.text = text.value; saveSoon(); } });
    const remove = el('button', 'icon-btn map-note-remove', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', '撕掉这张批注');
    remove.addEventListener('click', () => {
      if (!editable()) return;
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
    if (!editable()) return;
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
    dialog.showModal();
    if (!await load()) return;
    // 没写字的批注不带到下一次。
    layout.notes = layout.notes.filter((n) => n.text.trim());
    draw();
    controls();
  }

  button.addEventListener('click', open);
  addNoteButton.addEventListener('click', addNote);
  retryButton.addEventListener('click', () => {
    if (!editable() || saving) return;
    saveFailed = false;
    persist();
  });
  reloadButton.addEventListener('click', async () => {
    if (loading || saving) return;
    clearTimeout(saveTimer); saveTimer = 0;
    if (await load(true)) { draw(); controls(); }
  });
  dialog.addEventListener('click', (event) => {
    if (event.target.closest('[data-close]')) dialog.close();
  });
  dialog.addEventListener('close', () => {
    // 关上时把还没存的改动存掉。
    clearTimeout(saveTimer); saveTimer = 0;
    if (dirty && !saveFailed) persist();
  });
  window.addEventListener('beforeunload', (event) => {
    if (dirty || saving) { event.preventDefault(); event.returnValue = ''; }
  });
  new ResizeObserver(fit).observe(frame);

  return {
    // 概念记录有没有这个功能，决定“地图”按钮出不出现。
    sync() { button.hidden = !record(); },
  };
}
