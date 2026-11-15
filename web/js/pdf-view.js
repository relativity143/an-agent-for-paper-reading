// 只有 PDF 的论文：用 PDF.js 把每一页画在页面里，上面铺一层看不见、但可以选中的文字。
// 这样 PDF 也能像 HTML 论文那样选中原文提问，讲解引用的原文也能标出来。
// 库在 web/vendor/pdfjs/，不依赖外网。扫描件没有文字，只能看，不能选。

const LIB = new URL('../vendor/pdfjs/pdf.min.mjs', import.meta.url).href;
const WORKER = new URL('../vendor/pdfjs/pdf.worker.min.mjs', import.meta.url).href;
// 视口上下各多画这么远；更远的页把画布放掉，几百页的书也不会把内存占满。
const KEEP = 1500;
const MAX_RATIO = 2;

let library = null;
function loadLibrary() {
  if (!library) {
    library = import(LIB).then((pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = WORKER;
      return pdfjs;
    });
  }
  return library;
}

// 比较文字时去掉空白和连字符、合并连字（ﬁ → fi）、不分大小写：
// PDF 里一个词常在行尾被连字符断开（sup-pressed），引文里却是完整的词。
function squash(text) {
  return (text || '').normalize('NFKC').replace(/[\s\-\u00AD\u2010\u2011]+/g, '').toLowerCase();
}

// 在一页的文字块里找出盖住这段引文的那些块。整段找不到时，退一步只找开头的一小段。
export function findSpans(spans, quote) {
  const wanted = squash(quote);
  if (wanted.length < 4) return [];
  const parts = spans.map((span) => squash(span.textContent));
  const starts = [];
  let text = '';
  for (const part of parts) { starts.push(text.length); text += part; }
  let at = text.indexOf(wanted);
  let length = wanted.length;
  if (at < 0) {
    const lead = wanted.slice(0, 24);
    at = lead.length >= 12 ? text.indexOf(lead) : -1;
    length = lead.length;
  }
  if (at < 0) return [];
  return spans.filter((span, i) => parts[i] && starts[i] < at + length && starts[i] + parts[i].length > at);
}

export async function createPdfView(root, scroller, url) {
  const pdfjs = await loadLibrary();
  const doc = await pdfjs.getDocument({ url }).promise;
  const first = (await doc.getPage(1)).getViewport({ scale: 1 });

  const holder = document.createElement('div');
  holder.className = 'pdf-doc';
  const pages = [];
  for (let n = 1; n <= doc.numPages; n += 1) {
    const el = document.createElement('section');
    el.className = 'pdf-sheet';
    el.dataset.page = String(n);
    el.setAttribute('aria-label', `第 ${n} 页`);
    // 先按第一页的比例占位，画到这一页时再按它自己的尺寸改。
    el.style.aspectRatio = `${first.width} / ${first.height}`;
    holder.append(el);
    pages.push({ n, el, state: 'empty', token: 0, task: null, spans: null });
  }
  root.replaceChildren(holder);

  let marks = new Map(); // 页码 → [引文]

  function applyMarks(page) {
    if (!page.spans) return;
    for (const span of page.spans) span.classList.remove('pdf-hit');
    for (const quote of marks.get(page.n) || []) {
      for (const span of findSpans(page.spans, quote)) span.classList.add('pdf-hit');
    }
  }

  async function draw(page) {
    if (page.state !== 'empty') return;
    const width = page.el.clientWidth;
    if (!width) return;
    page.state = 'busy';
    page.token += 1;
    const token = page.token;
    try {
      const source = await doc.getPage(page.n);
      const unit = source.getViewport({ scale: 1 });
      page.el.style.aspectRatio = `${unit.width} / ${unit.height}`;
      const scale = width / unit.width;
      const viewport = source.getViewport({ scale });
      const ratio = Math.min(window.devicePixelRatio || 1, MAX_RATIO);
      const canvas = document.createElement('canvas');
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      const text = document.createElement('div');
      text.className = 'textLayer';
      // 文字层的字号和位置按这几个变量算（PDF.js 的约定）。
      page.el.style.setProperty('--scale-factor', String(scale));
      page.el.style.setProperty('--user-unit', '1');
      page.el.style.setProperty('--total-scale-factor', String(scale));
      page.task = source.render({ canvas, viewport, transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined });
      await page.task.promise;
      const layer = new pdfjs.TextLayer({ textContentSource: await source.getTextContent(), container: text, viewport });
      await layer.render();
      if (token !== page.token) return; // 画的时候这一页已经滚远、被放掉了
      page.el.replaceChildren(canvas, text);
      page.spans = layer.textDivs;
      page.width = width;
      page.state = 'done';
      applyMarks(page);
    } catch (error) {
      if (token !== page.token) return;
      page.state = 'empty';
      if (!error || error.name !== 'RenderingCancelledException') {
        const note = document.createElement('p');
        note.className = 'pdf-sheet-error';
        note.textContent = '这一页没能画出来。';
        page.el.replaceChildren(note);
      }
    }
  }

  function release(page) {
    if (page.state === 'empty') return;
    page.token += 1;
    if (page.task) page.task.cancel();
    page.task = null;
    page.spans = null;
    page.state = 'empty';
    page.el.replaceChildren();
  }

  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const page = pages[Number(entry.target.dataset.page) - 1];
      if (entry.isIntersecting) draw(page); else release(page);
    }
  }, { root: scroller, rootMargin: `${KEEP}px 0px` });
  for (const page of pages) observer.observe(page.el);

  function near(page) {
    const box = page.el.getBoundingClientRect();
    const view = scroller.getBoundingClientRect();
    return box.bottom > view.top - KEEP && box.top < view.bottom + KEEP;
  }

  // 窗口宽度变了：已经画好的页按新宽度重画。
  let resizeTimer = 0;
  const resizer = new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      for (const page of pages) {
        if (page.state === 'done' && Math.abs(page.el.clientWidth - page.width) > 8) release(page);
      }
      for (const page of pages) if (page.state === 'empty' && near(page)) draw(page);
    }, 200);
  });
  resizer.observe(holder);

  function scrollToElement(el, offset) {
    const top = el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    // 直接跳过去：平滑滚动会把路上的每一页都画一遍。
    scroller.scrollTo({ top: Math.max(0, top - offset), behavior: 'auto' });
  }

  // 跳到第 n 页。toMark 为真时，这一页画好后再对准标出来的那句原文。
  async function gotoPage(n, { toMark = false } = {}) {
    const page = pages[n - 1];
    if (!page) return false;
    scrollToElement(page.el, 12);
    page.el.classList.remove('flash');
    void page.el.offsetWidth;
    page.el.classList.add('flash');
    if (toMark) {
      await draw(page);
      const hit = page.el.querySelector('.pdf-hit');
      if (hit) scrollToElement(hit, 120);
    }
    return true;
  }

  // list: [{page, quote}]。换一批标注时，原来的全部去掉。
  function setMarks(list) {
    marks = new Map();
    for (const item of list || []) {
      if (!Number.isInteger(item.page) || !item.quote) continue;
      if (!marks.has(item.page)) marks.set(item.page, []);
      marks.get(item.page).push(item.quote);
    }
    for (const page of pages) applyMarks(page);
  }

  // 读者当前选中的原文：返回片段和它开始的那一页。
  function selection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    if (!holder.contains(range.commonAncestorContainer)) return null;
    const text = sel.toString().replace(/\s+/g, ' ').trim();
    if (text.length < 3) return null;
    let node = range.startContainer;
    if (node.nodeType !== Node.ELEMENT_NODE) node = node.parentElement;
    const pageEl = node && node.closest('.pdf-sheet');
    if (!pageEl) return null;
    return { quote: text.slice(0, 600), page: Number(pageEl.dataset.page), rect: range.getBoundingClientRect() };
  }

  function destroy() {
    observer.disconnect();
    resizer.disconnect();
    clearTimeout(resizeTimer);
    doc.destroy();
  }

  return { pageCount: doc.numPages, gotoPage, setMarks, selection, destroy };
}
