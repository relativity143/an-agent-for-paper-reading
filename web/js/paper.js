// 左侧：显示论文原文，并在上面标出讲解对应的位置。

import { renderInline, displayMath, markQuote, typeset, escapeHtml } from './rich.js';
import { createPdfView } from './pdf-view.js';

const PLACEHOLDER = /⟨([^⟩]+)⟩/g;

function equationNumber(id) {
  const match = /E(\d+)$/.exec(id);
  return match ? `(${match[1]})` : '';
}

function equationHtml(id, equations) {
  const tex = equations[id];
  if (!tex) return '';
  return `<div class="eq" data-id="${escapeHtml(id)}" id="p-${escapeHtml(id)}">${displayMath(tex)}<span class="eq-no">${equationNumber(id)}</span></div>`;
}

// 段落文字里的 ⟨S2.E1⟩ 是行间公式的占位符，在原位置展开。
function paragraphHtml(text, equations) {
  const pieces = text.split(PLACEHOLDER);
  let html = '';
  for (let i = 0; i < pieces.length; i += 1) {
    if (i % 2 === 1) html += equationHtml(pieces[i], equations);
    else if (pieces[i].trim()) html += `<p>${renderInline(pieces[i].trim())}</p>`;
  }
  return html;
}

export function createPaperView(root, scroller) {
  let content = null;
  const raw = new Map();
  // PDF 论文嵌在页面里显示（pdf-view.js）。载入要一点时间；没载入成时 pdf 是 null，退回浏览器自带的阅读器。
  let pdf = null;
  let pdfReady = Promise.resolve();

  function render(data) {
    content = data;
    raw.clear();
    if (pdf) { pdf.destroy(); pdf = null; }
    root.classList.toggle('paper-pdf', Boolean(data.pdf));
    root.classList.remove('paper-pdf-frame');
    if (data.pdf) {
      root.innerHTML = '<p class="pdf-loading" role="status">正在载入 PDF…</p>';
      pdfReady = createPdfView(root, scroller, data.pdf).then((view) => {
        if (content !== data) { view.destroy(); return; }
        pdf = view;
      }).catch(() => {
        if (content !== data) return;
        // 嵌入显示没成（库没载入，或文件读不了）：用浏览器自带的阅读器，只能按页跳转。
        root.classList.add('paper-pdf-frame');
        root.innerHTML = `<iframe class="pdf" title="论文 PDF" src="${escapeHtml(data.pdf)}"></iframe>`;
      });
      return;
    }
    const parts = [];
    parts.push(`<header class="paper-head"><h2>${escapeHtml(data.title)}</h2><p>${escapeHtml((data.authors || []).join('、'))}</p></header>`);
    for (const block of data.blocks) {
      const id = escapeHtml(block.id);
      if (block.type === 'section') {
        const level = Math.min(4, 2 + (block.level || 1));
        parts.push(`<h${level} class="sec" data-id="${id}" id="p-${id}">${escapeHtml(block.title)}</h${level}>`);
      } else if (block.type === 'paragraph') {
        raw.set(block.id, block.text);
        parts.push(`<div class="para" data-id="${id}" id="p-${id}"><span class="para-id">${id}</span>${paragraphHtml(block.text, data.equations)}</div>`);
      } else if (block.type === 'equation') {
        parts.push(equationHtml(block.id, data.equations));
      } else if (block.type === 'figure') {
        const link = data.source_url ? ` <a href="${escapeHtml(data.source_url)}#${id}" target="_blank" rel="noopener noreferrer">看原图</a>` : '';
        parts.push(`<figure class="fig" data-id="${id}" id="p-${id}"><figcaption>${renderInline(block.caption)}${link}</figcaption></figure>`);
      }
    }
    if (data.attribution) parts.push(`<p class="attribution">${escapeHtml(data.attribution)}</p>`);
    root.innerHTML = parts.join('');
    typeset(root);
  }

  function find(id) {
    return root.querySelector(`[data-id="${CSS.escape(id)}"]`);
  }

  function clearHighlights() {
    root.querySelectorAll('.hl').forEach((el) => {
      el.classList.remove('hl');
      el.removeAttribute('data-hl-label');
    });
    // 之前标过片段的段落恢复成原样。
    root.querySelectorAll('.para[data-marked]').forEach((el) => {
      const id = el.dataset.id;
      el.removeAttribute('data-marked');
      el.innerHTML = `<span class="para-id">${escapeHtml(id)}</span>${paragraphHtml(raw.get(id) || '', content.equations)}`;
      typeset(el);
    });
  }

  // highlights: [{ids, quote, label}]；返回第一个标出来的元素。
  function highlight(highlights, { scroll = true } = {}) {
    if (content && content.pdf) {
      // PDF 的位置是“第几页 + 那句原文”：跳到那一页，把原文在文字层上标出来。
      const marks = (highlights || []).filter((h) => Number.isInteger(h.page) && h.page >= 1);
      const page = marks.length ? marks[0].page : null;
      pdfReady.then(() => {
        if (pdf) {
          pdf.setMarks(marks);
          if (page && scroll) pdf.gotoPage(page, { toMark: true });
        } else if (page && scroll) {
          framePage(page);
        }
      });
      return null;
    }
    clearHighlights();
    let first = null;
    for (const item of highlights || []) {
      for (const id of item.ids || []) {
        const el = find(id);
        if (!el) continue;
        el.classList.add('hl');
        if (item.label) el.dataset.hlLabel = item.label;
        if (item.quote && el.classList.contains('para') && raw.has(id)) {
          const marked = markQuote(raw.get(id), item.quote);
          if (marked !== raw.get(id)) {
            el.dataset.marked = '1';
            el.innerHTML = `<span class="para-id">${escapeHtml(id)}</span>${paragraphHtml(marked, content.equations)}`;
            typeset(el);
          }
        }
        if (!first) first = el;
      }
    }
    if (first && scroll) scrollTo(first);
    return first;
  }

  // 浏览器自带的 PDF 阅读器按 #page=N 跳页。换一个地址才会真的跳，所以带上序号。
  let pageJumps = 0;
  function framePage(n) {
    const frame = root.querySelector('iframe.pdf');
    if (!frame) return;
    pageJumps += 1;
    frame.src = `${content.pdf}?jump=${pageJumps}#page=${n}`;
  }

  function gotoPage(page) {
    const n = Number(page);
    if (!content || !content.pdf || !Number.isInteger(n) || n < 1) return false;
    pdfReady.then(() => { if (pdf) pdf.gotoPage(n); else framePage(n); });
    return true;
  }

  // 从一段位置文字里认出能跳过去的地方（路线图里的位置是模型写的，格式不固定）：
  // PDF 认“第 N 页”，HTML 认第一个在原文里存在的位置编号。认不出来返回 null。
  function resolve(text) {
    if (!content || typeof text !== 'string') return null;
    if (content.pdf) {
      const match = /第\s*(\d+)/.exec(text);
      return match && Number(match[1]) >= 1 ? { page: Number(match[1]) } : null;
    }
    for (const match of text.matchAll(/[A-Za-z]+\d+(?:\.[A-Za-z]+\d+)*/g)) {
      if (find(match[0])) return { id: match[0] };
    }
    return null;
  }

  function isPdf() {
    return Boolean(content && content.pdf);
  }

  function scrollTo(el) {
    const top = el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    scroller.scrollTo({ top: Math.max(0, top - 96), behavior: 'smooth' });
  }

  // 点位置标签时：滚过去并闪一下，不改变当前的标注。
  function flash(id) {
    const el = find(id);
    if (!el) return false;
    scrollTo(el);
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
    return true;
  }

  // 读者当前选中的原文：返回片段和所在段落的编号；PDF 返回片段和页码。
  function selection() {
    if (content && content.pdf) return pdf ? pdf.selection() : null;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    if (!root.contains(range.commonAncestorContainer)) return null;
    const text = sel.toString().replace(/\s+/g, ' ').trim();
    if (text.length < 3) return null;
    let node = range.startContainer;
    if (node.nodeType !== Node.ELEMENT_NODE) node = node.parentElement;
    // 只认段落、公式和图：后端只接受这些位置编号。
    const holder = node && node.closest('.para, .eq, .fig');
    if (!holder) return null;
    return { quote: text.slice(0, 600), where: holder.dataset.id, rect: range.getBoundingClientRect() };
  }

  return { render, highlight, flash, selection, gotoPage, isPdf, resolve };
}
